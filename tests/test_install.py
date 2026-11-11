#!/usr/bin/env python3
"""eventmem P3 tests — the installer: asset choice, resumable download, pack creation.

Run directly:  <python> tests/test_install.py

The network is real but local: one ``ThreadingHTTPServer`` plays GitHub releases, the HF
model API and the file host, including ``Range`` support.  Assertions are about bytes and
headers, not about our own helper functions.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import traceback
import urllib.request
import tarfile
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CORE = REPO / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

os.environ.setdefault("EVENTMEM_LANG", "zh")

import eventmem_core.install as install  # noqa: E402
import eventmem_core.packs as packs  # noqa: E402
import eventmem_core.runtime as runtime  # noqa: E402

CASES: list = []

PAYLOAD = b"gguf" + bytes(range(256)) * 400  # ~100 KB, not compressible to nothing
SHA = hashlib.sha256(PAYLOAD).hexdigest()
MODEL_FILE = "lev-Q4_K_M.gguf"
MODEL_PATH = f"/ggml-org/lev-GGUF/resolve/main/{MODEL_FILE}"


def case(fn):
    CASES.append(fn)
    return fn


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def check_eq(got, want, msg):
    if got != want:
        raise AssertionError(f"{msg}: got {got!r}, want {want!r}")


# --------------------------------------------------------------------------- fake internet

class Fake:
    """Routes + request log for the local stand-in server."""

    def __init__(self):
        self.routes: dict[str, tuple[str, bytes]] = {}
        self.requests: list[dict] = []
        self.ignore_range = False
        self.port = 0

    def url(self, path: str = "") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def put(self, path: str, body: bytes, ctype: str = "application/octet-stream") -> None:
        self.routes[path] = (ctype, body)

    def put_json(self, path: str, data) -> None:
        self.put(path, json.dumps(data).encode(), "application/json")


class Handler(BaseHTTPRequestHandler):
    fake: Fake

    def log_message(self, *args):  # silence
        pass

    def do_GET(self):  # noqa: N802
        path = urlsplit(self.path).path
        self.fake.requests.append({"path": path, "range": self.headers.get("Range")})
        route = self.fake.routes.get(path)
        if route is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ctype, body = route
        start = 0
        raw_range = self.headers.get("Range") or ""
        if raw_range.startswith("bytes=") and not self.fake.ignore_range:
            try:
                start = int(raw_range[6:].split("-")[0])
            except ValueError:
                start = 0
        chunk = body[start:]
        self.send_response(206 if start else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(chunk)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        self.end_headers()
        self.wfile.write(chunk)


class server:
    def __init__(self, fake: Fake):
        self.fake = fake
        self.httpd = None
        self.thread = None

    def __enter__(self):
        handler = type("Bound", (Handler,), {"fake": self.fake})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.fake.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self.fake

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        return False


def asset_list() -> list[dict]:
    """Asset names shaped like the real b-builds (2026-11), including the traps."""
    names = [
        "cudart-llama-b9000-bin-ubuntu-cuda-12.8-x64.tar.gz",
        "cudart-llama-bin-win-cuda-12.4-x64.zip",
        "llama-b9000-bin-ubuntu-cuda-12.8-x64.tar.gz",
        "llama-b9000-bin-ubuntu-cuda-13.5-x64.tar.gz",
        "llama-b9000-bin-ubuntu-openvino-2026.4.1-x64.tar.gz",
        "llama-b9000-bin-ubuntu-x64.tar.gz",
        "llama-b9000-bin-win-cpu-x64.zip",
        "llama-b9000-bin-win-avx2-x64.zip",
        "llama-b9000-bin-win-cuda-12.4-x64.zip",
        "llama-b9000-bin-win-cuda-13.5-x64.zip",
        "llama-b9000-ui.tar.gz",
    ]
    return [{"name": n, "browser_download_url": "http://example.invalid/" + n} for n in names]


def release_list(fake: Fake) -> list[dict]:
    """Newest first: a semver release with no binaries, then the b-build that has them."""
    return [
        {"tag_name": "v0.9.0", "assets": [{"name": "nightly-tag.txt",
                                           "browser_download_url": fake.url("/dl/nightly-tag.txt")}]},
        {"tag_name": "b9000",
         "assets": [{"name": a["name"],
                     "browser_download_url": fake.url("/dl/" + a["name"])}
                    for a in asset_list()]},
    ]


def llama_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("build/bin/llama-server", "#!/bin/sh\necho fake\n")
        bundle.writestr("build/bin/README", "x")
    return buffer.getvalue()


def llama_targz() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        data = b"#!/bin/sh\necho fake\n"
        info = tarfile.TarInfo("build/bin/llama-server")
        info.size = len(data)
        bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# --------------------------------------------------------------------------- picking

@case
def test_pick_asset_prefers_cuda_on_linux_nvidia():
    chosen = install.pick_asset(asset_list(), {"os": "linux", "arch": "x64", "accel": "nvidia"})
    check_eq(chosen["name"], "llama-b9000-bin-ubuntu-cuda-12.8-x64.tar.gz",
             "lowest cuda toolkit wins: it runs on more drivers")


@case
def test_pick_asset_plain_on_linux_cpu():
    chosen = install.pick_asset(asset_list(), {"os": "linux", "arch": "x64", "accel": "none"})
    check_eq(chosen["name"], "llama-b9000-bin-ubuntu-x64.tar.gz",
             "openvino/sycl are not the plain build")


@case
def test_pick_asset_windows_cpu_uses_the_cpu_slot():
    chosen = install.pick_asset(asset_list(), {"os": "windows", "arch": "x64", "accel": "none"})
    check_eq(chosen["name"], "llama-b9000-bin-win-cpu-x64.zip", "windows cpu build")


@case
def test_pick_asset_windows_nvidia_prefers_cuda_and_never_cudart():
    chosen = install.pick_asset(asset_list(), {"os": "windows", "arch": "x64", "accel": "nvidia"})
    check_eq(chosen["name"], "llama-b9000-bin-win-cuda-12.4-x64.zip", "cuda, not cudart")


@case
def test_resolve_release_skips_a_release_without_binaries():
    """真实世界的坑：/releases/latest 是 v0.9.0，只有一个 nightly-tag.txt。"""
    fake = Fake()
    releases = release_list(fake)
    release, asset = install.resolve_release(
        {"os": "linux", "arch": "x64", "accel": "none"},
        api="http://example.invalid/releases", http=lambda _url: releases)
    check_eq(release["tag_name"], "b9000", "walked past the empty stable release")
    check_eq(asset["name"], "llama-b9000-bin-ubuntu-x64.tar.gz", "and found the binary")


@case
def test_pick_asset_says_nothing_when_nothing_fits():
    check_eq(install.pick_asset([{"name": "llama-bin-ubuntu-x64.zip"}],
                                {"os": "windows", "arch": "x64", "accel": "none"}), None,
             "wrong os is not a match")
    check_eq(install.pick_asset([], {"os": "linux", "arch": "x64", "accel": "none"}), None,
             "empty release")


@case
def test_pick_model_file_by_quant():
    files = ["lev-Q8_0.gguf", "lev-Q4_K_M.gguf", "README.md", "lev-Q4_K_M-imatrix.gguf"]
    check_eq(install.pick_model_file(files, "Q4_K_M"), "lev-Q4_K_M.gguf", "exact quant")
    check_eq(install.pick_model_file(files, "Q8_0"), "lev-Q8_0.gguf", "other quant")
    check_eq(install.pick_model_file(files, "Q2_K"), None, "not offered")


@case
def test_probe_hardware_is_sane():
    hw = install.probe_hardware()
    check(hw["os"] in ("linux", "windows"), hw)
    check(hw["arch"] in ("x64", "arm64"), hw)
    check(hw["accel"] in ("nvidia", "amd", "none"), hw)


# --------------------------------------------------------------------------- download

@case
def test_download_resumes_from_a_part_file():
    fake = Fake()
    fake.put("/file.bin", PAYLOAD)
    with server(fake) as net:
        root = Path(tempfile.mkdtemp(prefix="eventmem-dl-"))
        try:
            target = root / "file.bin"
            target.with_name("file.bin.part").write_bytes(PAYLOAD[:1000])
            install.download(net.url("/file.bin"), target, expected_sha=SHA, label="x")
            check_eq(target.read_bytes(), PAYLOAD, "resumed file is whole")
            check_eq(target.with_name("file.bin.part").exists(), False, "part file cleaned up")
            ranges = [r["range"] for r in net.requests if r["path"] == "/file.bin"]
            check_eq(ranges, ["bytes=1000-"], "asked only for what was missing")
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


@case
def test_download_restarts_when_the_server_ignores_range():
    fake = Fake()
    fake.put("/file.bin", PAYLOAD)
    fake.ignore_range = True
    with server(fake) as net:
        root = Path(tempfile.mkdtemp(prefix="eventmem-dl2-"))
        try:
            target = root / "file.bin"
            target.with_name("file.bin.part").write_bytes(b"stale")
            install.download(net.url("/file.bin"), target, expected_sha=SHA)
            check_eq(target.read_bytes(), PAYLOAD, "restarted from zero, no corruption")
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


@case
def test_download_refuses_a_bad_checksum():
    fake = Fake()
    fake.put("/file.bin", PAYLOAD)
    with server(fake) as net:
        root = Path(tempfile.mkdtemp(prefix="eventmem-dl3-"))
        try:
            target = root / "file.bin"
            seen = ""
            try:
                install.download(net.url("/file.bin"), target,
                                 expected_sha="0" * 64, label="x")
            except RuntimeError as exc:
                seen = str(exc)
            check("sha256" in seen, f"refused loudly: {seen!r}")
            check_eq(target.exists(), False, "nothing left behind")
            check_eq(target.with_name("file.bin.part").exists(), False, "partial removed too")
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- model + llama

@case
def test_install_model_reads_the_hf_api_and_verifies():
    fake = Fake()
    fake.put_json("/api/models/ggml-org/lev-GGUF", {
        "id": "ggml-org/lev-GGUF",
        "cardData": {"license": "apache-2.0"},
        "siblings": [
            {"rfilename": "lev-Q8_0.gguf", "lfs": {"sha256": "a" * 64}},
            {"rfilename": MODEL_FILE, "lfs": {"sha256": SHA}},
        ],
    })
    fake.put(MODEL_PATH, PAYLOAD)
    with server(fake) as net:
        root = Path(tempfile.mkdtemp(prefix="eventmem-model-"))
        try:
            paths = install.Paths(str(root))
            got = install.install_model(paths, "ggml-org/lev-GGUF", "Q4_K_M",
                                        endpoint=net.url())
            check_eq(got["file"], MODEL_FILE, "chosen by quant")
            check_eq(got["license"], "apache-2.0", "license read from the card")
            check_eq(Path(got["path"]).read_bytes(), PAYLOAD, "bytes on disk")
            check_eq(Path(got["path"]).parent.name, "lev", "folder named after the repo")
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


@case
def test_install_model_says_which_quants_exist():
    fake = Fake()
    fake.put_json("/api/models/ggml-org/lev-GGUF", {
        "siblings": [{"rfilename": "lev-Q8_0.gguf"}],
    })
    with server(fake) as net:
        root = Path(tempfile.mkdtemp(prefix="eventmem-model2-"))
        try:
            seen = ""
            try:
                install.install_model(install.Paths(str(root)), "ggml-org/lev-GGUF", "Q4_K_M",
                                      endpoint=net.url())
            except RuntimeError as exc:
                seen = str(exc)
            check("Q8_0" in seen, f"lists what is offered: {seen!r}")
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


@case
def test_install_llama_extracts_and_records_the_binary():
    fake = Fake()
    with server(fake) as net:
        fake.put_json("/releases", release_list(net))
        fake.put("/dl/llama-b9000-bin-win-cpu-x64.zip", llama_zip())
        root = Path(tempfile.mkdtemp(prefix="eventmem-llama-"))
        try:
            paths = install.Paths(str(root))
            state = install.install_llama(paths, hardware={"os": "windows", "arch": "x64",
                                                           "accel": "none"},
                                          release_api=net.url("/releases"))
            binary = Path(state["binary"])
            check(binary.exists() and binary.name == "llama-server", f"found {binary}")
            check(os.access(binary, os.X_OK), "made executable")
            marker = json.loads(paths.binary_state.read_text(encoding="utf-8"))
            check_eq(marker["build"], "b9000", "marker records the build")
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


@case
def test_install_llama_handles_the_linux_tarball():
    """The real linux asset is a .tar.gz, not a zip."""
    fake = Fake()
    with server(fake) as net:
        fake.put_json("/releases", release_list(net))
        fake.put("/dl/llama-b9000-bin-ubuntu-x64.tar.gz", llama_targz())
        root = Path(tempfile.mkdtemp(prefix="eventmem-tar-"))
        try:
            paths = install.Paths(str(root))
            state = install.install_llama(paths, hardware={"os": "linux", "arch": "x64",
                                                           "accel": "none"},
                                          release_api=net.url("/releases"))
            check(Path(state["binary"]).exists(), f"extracted {state['binary']}")
            check_eq(Path(state["binary"]).name, "llama-server", "binary name")
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


@case
def test_runtime_finds_the_installed_binary_through_the_marker():
    root = Path(tempfile.mkdtemp(prefix="eventmem-marker-"))
    try:
        paths = install.Paths(str(root))
        folder = paths.backends / "lev"
        folder.mkdir(parents=True)
        (folder / "model.gguf").write_bytes(b"x")
        (folder / "config.yaml").write_text("protocol: systemone\nweights: model.gguf\n",
                                            encoding="utf-8")
        binary = root / "runtimes" / "llama.cpp" / "b9000" / "build" / "bin" / "llama-server"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        paths.runtimes.mkdir(parents=True, exist_ok=True)
        paths.binary_state.write_text(json.dumps({"binary": str(binary)}), encoding="utf-8")

        pack = packs.load_pack(folder)
        old = os.environ.pop("EVENTMEM_LLAMA_SERVER", None)
        try:
            command = runtime.resolve_command(pack, 18090)
        finally:
            if old is not None:
                os.environ["EVENTMEM_LLAMA_SERVER"] = old
        check_eq(command[0], str(binary), "the marker decides which llama.cpp runs")
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- pack + end to end

@case
def test_make_pack_writes_a_hand_editable_file():
    root = Path(tempfile.mkdtemp(prefix="eventmem-pack-"))
    try:
        paths = install.Paths(str(root))
        (paths.models / "lev").mkdir(parents=True)
        (paths.models / "lev" / MODEL_FILE).write_bytes(b"x")
        config = install.make_pack(paths, "lev", weights="../../models/lev/" + MODEL_FILE,
                                   model="lev", license="apache-2.0")
        text = config.read_text(encoding="utf-8")
        check("launch: auto" in text, "self-managed by default")
        check("license: apache-2.0" in text, "license recorded")

        pack = packs.load_pack(config)
        check_eq(pack.kind, "self", "weights -> self")
        check_eq(pack.model, "lev", "model id")
        check(Path(pack.weights).exists(), f"relative weights resolve: {pack.weights}")

        again = ""
        try:
            install.make_pack(paths, "lev", weights="x")
        except RuntimeError as exc:
            again = str(exc)
        check("--force" in again, f"refuses to clobber a hand-edited pack: {again!r}")
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


@case
def test_install_without_llama_still_produces_a_usable_pack():
    fake = Fake()
    fake.put_json("/api/models/ggml-org/lev-GGUF", {
        "cardData": {"license": "apache-2.0"},
        "siblings": [{"rfilename": MODEL_FILE, "lfs": {"sha256": SHA}}],
    })
    fake.put(MODEL_PATH, PAYLOAD)
    with server(fake) as net:
        root = Path(tempfile.mkdtemp(prefix="eventmem-e2e-"))
        try:
            paths = install.Paths(str(root))
            old = os.environ.get("HF_ENDPOINT")
            os.environ["HF_ENDPOINT"] = net.url()
            try:
                result = install.install(paths, "lev", repo="ggml-org/lev-GGUF",
                                         quant="Q4_K_M", skip_llama=True)
            finally:
                if old is None:
                    os.environ.pop("HF_ENDPOINT", None)
                else:
                    os.environ["HF_ENDPOINT"] = old

            pack = packs.load_pack(result["pack"])
            check_eq(pack.name, "lev", "pack name")
            check(Path(pack.weights).exists(), "weights path resolves through the pack")
            check_eq(result["model"]["license"], "apache-2.0", "license carried through")
        finally:
            import shutil

            shutil.rmtree(root, ignore_errors=True)


@case
def test_manifest_lists_what_is_installed(capsys=None):
    root = Path(tempfile.mkdtemp(prefix="eventmem-list-"))
    try:
        paths = install.Paths(str(root))
        (paths.models / "lev").mkdir(parents=True)
        (paths.models / "lev" / MODEL_FILE).write_bytes(b"x" * 2048)
        install.make_pack(paths, "lev", weights="../../models/lev/" + MODEL_FILE)
        install.main(["list", "--root", str(root)])
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    passed = failed = 0
    for fn in CASES:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {fn.__name__}")
    print(f"PASS {passed} / FAIL {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
