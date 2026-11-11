"""eventmem_core.install — put a decision backend on this machine.

Two downloads and one config file:

1. **llama.cpp** — the release whose asset matches this machine (linux/windows, x64/arm64,
   cuda / vulkan / avx512 …).  Asset names are the part of this that rots, so the rule is a
   preference list in :data:`BACKEND_PREFERENCE`, not a wall of ``if``s, and every install
   prints which asset it picked and why.
2. **A decision model** — a GGUF from HuggingFace.  The file is chosen by quant from the
   repo's own file list (never guessed), streamed with ``Range`` resume, and checked against
   the sha256 HF publishes when it publishes one.
3. **A pack** — ``backends/<name>/config.yaml`` pointing at those two, so the daemon can
   launch it and ``decision.Chain`` can find it.

Nothing here needs a third-party package: urllib, zipfile/tarfile, hashlib.

The URL shapes (as of 2026-11)::

    https://api.github.com/repos/ggml-org/llama.cpp/releases/latest
    https://huggingface.co/api/models/<repo>
    https://huggingface.co/<repo>/resolve/main/<file>?download=true

``HF_ENDPOINT`` redirects the last two (``https://hf-mirror.com`` in mainland China).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

__all__ = [
    "BACKEND_PREFERENCE", "DEFAULT_REPO", "DEFAULT_QUANT",
    "Paths", "probe_hardware", "pick_asset", "pick_model_file", "resolve_release",
    "download", "install_llama", "install_model", "make_pack", "install", "state",
    "set_progress", "main",
]

LLAMA_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30"
HF_API = "https://huggingface.co/api/models/{repo}"
HF_FILE = "https://huggingface.co/{repo}/resolve/main/{file}?download=true"

DEFAULT_REPO = "ggml-org/lev-GGUF"
DEFAULT_QUANT = "Q4_K_M"

#: Asset-name substrings to prefer, by (os, accelerator).  First hit wins; an empty tuple
#: means "the plain build".  Windows CPU builds are named by accelerator slot ("cpu").
BACKEND_PREFERENCE: dict[tuple[str, str], tuple[str, ...]] = {
    ("linux", "none"): (),
    ("linux", "nvidia"): ("cuda",),
    ("linux", "amd"): ("vulkan", "rocm", "hip"),
    ("windows", "none"): ("cpu", "avx512", "avx2", "noavx"),
    ("windows", "nvidia"): ("cuda",),
    ("windows", "amd"): ("vulkan", "rocm", "hip"),
}

#: Anything that names a *backend other than plain CPU*.  Used to keep the "plain build"
#: filter honest: `openvino` and `opencl` are easy to miss and are not plain.
_ACCEL_TAGS = ("cuda", "vulkan", "hip", "rocm", "sycl", "musa", "kompute", "openvino",
               "opencl", "zdnn", "snapdragon", "metal")


# --------------------------------------------------------------------------- paths

@dataclass(frozen=True)
class Paths:
    """Everything the installer touches, under one root (default ``~/.hermes/eventmem``)."""

    root: str

    @property
    def base(self) -> Path:
        return Path(self.root).expanduser()

    @property
    def runtimes(self) -> Path:
        return self.base / "runtimes" / "llama.cpp"

    @property
    def models(self) -> Path:
        return self.base / "models"

    @property
    def backends(self) -> Path:
        return self.base / "backends"

    @property
    def binary_state(self) -> Path:
        return self.runtimes / "current.json"


# --------------------------------------------------------------------------- hardware

def _nvidia() -> str:
    """``"nvidia"`` when an NVIDIA driver answers, else ``""``.  Never raises."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=5, check=False)
    except Exception:  # noqa: BLE001 - no driver, no container, no nvidia-smi
        return ""
    return "nvidia" if out.returncode == 0 and out.stdout.strip() else ""


def probe_hardware() -> dict[str, Any]:
    """What this machine can run.  Best effort: a wrong guess only picks a slower build."""
    system = platform.system().lower()
    os_name = "windows" if system.startswith("win") else "linux"
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "x64"
    accel = _nvidia() or ("amd" if shutil.which("rocm-smi") else "none")
    return {"os": os_name, "arch": arch, "accel": accel, "system": system,
            "machine": machine, "cpu": platform.processor() or platform.machine()}


# --------------------------------------------------------------------------- picking

def _asset_name(asset: Any) -> str:
    if isinstance(asset, dict):
        return str(asset.get("name") or "")
    return str(asset)


def _url_name(url: str) -> str:
    """The file name a URL ends with (query string and redirects ignored)."""
    return Path(urlsplit(url).path).name or "llama-download"


def _cuda_version(name: str) -> tuple[int, int]:
    match = re.search(r"cuda-(\d+)\.(\d+)", name)
    return (int(match.group(1)), int(match.group(2))) if match else (99, 99)


def pick_asset(assets: Iterable[Any], hardware: dict[str, Any]) -> Optional[Any]:
    """The llama.cpp release asset for *hardware*; ``None`` when nothing fits.

    Rules, in order: it must be a binary package (``bin``) for this OS and arch; then the
    accelerator preference list decides; then the plain build.  ``cudart`` packs are the
    CUDA *libraries*, not the server, so they are never picked.  Among several CUDA builds
    the **lowest** toolkit version wins — it runs on more drivers.
    """
    os_name = hardware.get("os", "linux")
    arch = hardware.get("arch", "x64")
    accel = hardware.get("accel", "none")
    os_tag = "ubuntu" if os_name == "linux" else "win"

    pool = []
    for asset in assets:
        name = _asset_name(asset).lower()
        if "bin" not in name or "cudart" in name:
            continue
        if os_tag not in name or arch not in name:
            continue
        pool.append(asset)
    if not pool:
        return None

    preference = BACKEND_PREFERENCE.get((os_name, accel), ())
    for tag in preference:
        matches = [a for a in pool if tag in _asset_name(a).lower()]
        if matches:
            if tag == "cuda":
                matches.sort(key=lambda a: _cuda_version(_asset_name(a).lower()))
            return matches[0]
    # plain build: nothing that smells like an accelerator or an instruction-set variant
    for asset in pool:
        name = _asset_name(asset).lower()
        if not any(tag in name for tag in _ACCEL_TAGS + ("avx",)):
            return asset
    # Windows CPU builds used to exist only as avx variants; take the slowest safe one last.
    for fallback in ("avx2", "avx512", "noavx"):
        for asset in pool:
            if fallback in _asset_name(asset).lower():
                return asset
    return None


def resolve_release(hardware: dict[str, Any], *, api: str = LLAMA_RELEASES_API,
                    http=None) -> tuple[Optional[dict], Optional[Any]]:
    """Newest release that actually ships a binary for this machine.

    ``/releases/latest`` is **not** usable: as of 2026-11 it points at a semver release
    (``v0.9.0``) whose only asset is ``nightly-tag.txt``, while the binaries live on the
    ``b####`` prereleases.  So we walk the release list and take the first one that has a
    matching asset — a stable release wins automatically if it ever ships one.
    """
    fetch = http or _http_json
    releases = fetch(api)
    if isinstance(releases, dict):       # a single-release response, as in tests
        releases = [releases]
    for release in releases:
        asset = pick_asset(release.get("assets") or [], hardware)
        if asset is not None:
            return release, asset
    return None, None


def pick_model_file(files: Iterable[str], quant: str = DEFAULT_QUANT) -> Optional[str]:
    """The GGUF whose name carries *quant*; exact-ish match first, then a looser one."""
    names = [name for name in files if str(name).lower().endswith(".gguf")]
    upper = quant.upper()
    exact = [n for n in names if Path(n).stem.upper().endswith("-" + upper)]
    if exact:
        return sorted(exact, key=len)[0]
    loose = [n for n in names if upper in Path(n).stem.upper()]
    if loose:
        return sorted(loose, key=len)[0]
    return None


# --------------------------------------------------------------------------- http

def _endpoint() -> str:
    return (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")


def _http_json(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "eventmem-install/1",
    })
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _sha256(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, dest: str | Path, *, expected_sha: str = "", label: str = "",
             timeout: float = 60.0) -> Path:
    """Stream *url* to *dest*, resuming a ``.part`` file, then verify the sha when given.

    Resume is a ``Range`` request for the bytes already on disk; a server that ignores it
    restarts from zero rather than corrupting the file.
    """
    target = Path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    have = part.stat().st_size if part.exists() else 0

    headers = {"User-Agent": "eventmem-install/1"}
    if have:
        headers["Range"] = f"bytes={have}-"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        status = int(getattr(resp, "status", 200) or 200)
        if have and status != 206:      # server ignored Range: start over
            have = 0
            mode = "wb"
        else:
            mode = "ab" if have else "wb"
        total = 0
        length = resp.headers.get("Content-Length")
        try:
            total = int(length) + have if length else 0
        except ValueError:
            total = 0
        with open(part, mode) as handle:
            copied = 0
            for block in iter(lambda: resp.read(1 << 20), b""):
                handle.write(block)
                copied += len(block)
                if copied >= (32 << 20):  # progress every 32 MB, not every chunk
                    _progress(label or target.name, have + copied, total)
                    copied = 0
    final = part.stat().st_size if part.exists() else 0
    _progress(label or target.name, final, total, done_flag=True)

    if expected_sha:
        got = _sha256(part)
        if got.lower() != expected_sha.lower():
            part.unlink(missing_ok=True)
            raise RuntimeError(f"{target.name}: sha256 不匹配（{got[:12]} ≠ {expected_sha[:12]}）")
    os.replace(part, target)
    return target


#: One progress listener, replaced by the daemon while an install job runs so the GUI can
#: draw a bar.  A list, not a bare global, so the setter cannot shadow the reader.
_PROGRESS: list[Any] = [None]


def set_progress(hook: Optional[Any]) -> None:
    """Install ``fn(label, done_bytes, total_bytes, done_flag)``; ``None`` restores stderr."""
    _PROGRESS[0] = hook


def _progress(label: str, done: int, total: int, *, done_flag: bool = False) -> None:
    hook = _PROGRESS[0]
    if hook is not None:
        try:
            hook(label, done, total, done_flag)
        except Exception:  # noqa: BLE001 - a broken listener must not abort a download
            _PROGRESS[0] = None
        return
    if not sys.stderr.isatty():
        if done_flag:
            print(f"  {label}: {done / 1e6:.1f} MB", file=sys.stderr)
        return
    pct = f" {done * 100 // total:3d}%" if total else ""
    end = "\n" if done_flag else "\r"
    print(f"  {label}: {done / 1e6:.1f} MB{pct}", end=end, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- llama.cpp

def _extract(archive: Path, into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    if archive.name.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(into)
        return
    with tarfile.open(archive) as bundle:  # noqa: S202 - our own download
        try:
            bundle.extractall(into, filter="data")   # 3.12+, backported to 3.11.4
        except TypeError:                             # pragma: no cover - old 3.11
            bundle.extractall(into)


def _find_binary(root: Path) -> Optional[Path]:
    names = ("llama-server", "llama-server.exe")
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name in names:
            return path
    return None


def install_llama(paths: Paths, *, hardware: Optional[dict[str, Any]] = None,
                  release_api: str = LLAMA_RELEASES_API,
                  asset_url: str = "") -> dict[str, Any]:
    """Download the matching llama.cpp release and record where ``llama-server`` lives."""
    hw = hardware or probe_hardware()

    if asset_url:
        chosen = {"name": _url_name(asset_url), "browser_download_url": asset_url}
        tag = "custom"
    else:
        release, chosen = resolve_release(hw, api=release_api)
        if chosen is None:
            raise RuntimeError(
                "没有匹配的 llama.cpp 发行包；用 --llama-url 指定一个（"
                f"{hw['os']}/{hw['arch']}/{hw['accel']}）")
        tag = str(release.get("tag_name") or "unknown")
    name = str(chosen.get("name") or "")
    url = str(chosen.get("browser_download_url") or "")
    if not url:
        raise RuntimeError(f"发行包 {name} 没有下载地址")

    target_dir = paths.runtimes / tag
    archive = target_dir / name
    if archive.exists() and not _find_binary(target_dir):
        archive.unlink()                      # half-extracted from an earlier run
    if not archive.exists():
        print(f"llama.cpp {tag}: {name}", file=sys.stderr)
        download(url, archive, label=name)
    binary = _find_binary(target_dir)
    if binary is None:                     # fresh download, or a half-extracted earlier run
        _extract(archive, target_dir)
        binary = _find_binary(target_dir)
    if binary is None:
        raise RuntimeError(f"{name} 里没有 llama-server")
    binary.chmod(binary.stat().st_mode | 0o755)
    state = {"binary": str(binary), "build": tag, "asset": name, "url": url,
             "hardware": hw, "ts": time.time()}
    paths.runtimes.mkdir(parents=True, exist_ok=True)
    paths.binary_state.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"llama-server -> {binary}", file=sys.stderr)
    return state


def _extract_and_return(archive: Path, into: Path) -> Path:
    _extract(archive, into)
    return into


# --------------------------------------------------------------------------- models

def install_model(paths: Paths, repo: str = DEFAULT_REPO, quant: str = DEFAULT_QUANT,
                  *, endpoint: str = "") -> dict[str, Any]:
    """Fetch one GGUF from a HuggingFace repo, verified, into ``models/<name>/``."""
    base = (endpoint or _endpoint()).rstrip("/")
    meta = _http_json(HF_API.format(repo=repo).replace("https://huggingface.co", base, 1))
    siblings = meta.get("siblings") or []
    files = [s.get("rfilename") if isinstance(s, dict) else str(s) for s in siblings]
    filename = pick_model_file(files, quant)
    if filename is None:
        have = ", ".join(sorted({Path(f).stem.split("-")[-1] for f in files if str(f).endswith(".gguf")}))
        raise RuntimeError(f"{repo} 里没有 {quant} 量化（有：{have or '空'}）")
    shas = {s.get("rfilename"): (s.get("lfs") or {}).get("sha256")
            for s in siblings if isinstance(s, dict)}
    expected = str(shas.get(filename) or "")

    folder = paths.models / repo.split("/")[-1].removesuffix("-GGUF").lower()
    target = folder / filename
    if target.exists() and (not expected or _sha256(target) == expected):
        print(f"已存在 {target}", file=sys.stderr)
    else:
        print(f"{repo}: {filename}", file=sys.stderr)
        download(HF_FILE.format(repo=repo, file=filename).replace("https://huggingface.co", base, 1),
                 target, expected_sha=expected, label=filename)
    return {"repo": repo, "file": filename, "path": str(target), "sha256": expected,
            "license": str((meta.get("cardData") or {}).get("license") or ""),
            "folder": str(folder)}


# --------------------------------------------------------------------------- pack

def make_pack(paths: Paths, name: str, *, weights: str, protocol: str = "systemone",
              model: str = "", license: str = "", force: bool = False) -> Path:
    """Write ``backends/<name>/config.yaml`` — hand-editable from then on."""
    folder = paths.backends / name
    folder.mkdir(parents=True, exist_ok=True)
    config = folder / "config.yaml"
    if config.exists() and not force:
        raise RuntimeError(f"{config} 已存在（加 --force 覆盖）")
    lines = [
        "# 由 `eventmem models install` 生成；之后可以随便改，GUI 与手写读写同一份文件。",
        f"protocol: {protocol}",
        f"model: {model or name}",
        f"weights: {weights}",
        "launch: auto",
        "args: []",
    ]
    if license:
        lines.append(f"license: {license}")
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config


# --------------------------------------------------------------------------- orchestration

def install(paths: Paths, name: str = "lev", *, repo: str = DEFAULT_REPO,
            quant: str = DEFAULT_QUANT, skip_llama: bool = False,
            llama_url: str = "", force: bool = False) -> dict[str, Any]:
    """The whole thing: runtime (unless skipped), model, pack."""
    hw = probe_hardware()
    result: dict[str, Any] = {"hardware": hw}
    if not skip_llama:
        result["llama"] = install_llama(paths, hardware=hw, asset_url=llama_url)
    model = install_model(paths, repo, quant)
    result["model"] = model
    weights = os.path.relpath(model["path"], paths.backends / name)
    result["pack"] = str(make_pack(paths, name, weights=weights, model=name,
                                   license=model.get("license", ""), force=force))
    return result


def state(paths: Paths) -> dict[str, Any]:
    """What is installed under this root — the shape ``/api/models`` and ``models list`` use."""
    marker: dict[str, Any] = {}
    if paths.binary_state.exists():
        try:
            marker = json.loads(paths.binary_state.read_text(encoding="utf-8"))
        except ValueError:
            marker = {}
    models = []
    if paths.models.is_dir():
        for path in sorted(paths.models.glob("*/*.gguf")):
            models.append({"repo": path.parent.name, "file": path.name, "path": str(path),
                           "size": path.stat().st_size})
    packs = sorted(paths.backends.glob("*/config.yaml")) if paths.backends.exists() else []
    return {
        "root": str(paths.base),
        "llama": marker,
        "models": models,
        "backends": [path.parent.name for path in packs],
    }


def _print_list(paths: Paths) -> None:
    found = state(paths)
    llama = found["llama"]
    print(f"root: {found['root']}")
    print(f"llama.cpp: {llama.get('build') or '（未安装）'}"
          f"{' -> ' + llama['binary'] if llama.get('binary') else ''}")
    print(f"models: {len(found['models'])}")
    for model in found["models"]:
        print(f"  {model['repo']}/{model['file']}  {model['size'] / 1e9:.2f} GB")
    print(f"backends: {len(found['backends'])}")
    for name in found["backends"]:
        print(f"  {name}")


def main(argv: Optional[list[str]] = None, *, root: str = "") -> int:
    parser = argparse.ArgumentParser(prog="eventmem models",
                                     description="安装/查看决策后端（llama.cpp + GGUF + pack）")
    parser.add_argument("command", nargs="?", default="list",
                        choices=("list", "install", "probe", "llama"))
    parser.add_argument("name", nargs="?", default="", help="后端名（也是 pack 目录名）")
    parser.add_argument("--root", default=root or os.environ.get("EVENTMEM_ROOT", "~/.hermes/eventmem"))
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--quant", default=DEFAULT_QUANT)
    parser.add_argument("--no-llama", action="store_true", help="只装模型和 pack")
    parser.add_argument("--llama-url", default="", help="跳过发行包匹配，直接给一个 URL")
    parser.add_argument("--force", action="store_true", help="覆盖已有的 pack 配置")
    args = parser.parse_args(argv)
    paths = Paths(args.root)

    if args.command == "probe":
        print(json.dumps(probe_hardware(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "list":
        _print_list(paths)
        return 0
    if args.command == "llama":
        state = install_llama(paths, asset_url=args.llama_url)
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    name = args.name or "lev"
    result = install(paths, name, repo=args.repo, quant=args.quant,
                     skip_llama=args.no_llama, llama_url=args.llama_url, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\n下一步：把 {name} 加进 config.yaml 的 decision.backends，然后重启 daemon。",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
