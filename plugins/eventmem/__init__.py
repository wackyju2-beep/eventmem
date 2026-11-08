"""eventmem — event-shaped local memory for Hermes (design v2.1).

The memory unit is an *event*: time + kind + one sentence. Events are append-only and
grouped by human-named projects; relations between them (改变/否定/重复/因为/细化) are
computed at night by a local decision model. Recall is global — vector + FTS5 → RRF —
and then expands along the project skeleton and the change chains (design §6).

Three pieces live in this module:

* ``register(ctx)`` installs the provider and the ``tool_execution`` middleware.
* ``on_tool_result(...)`` appends one reminder line to every state-changing tool result,
  so the model writes its event in the same turn (design §4.1). Hook callbacks cannot
  issue tool calls; middleware can rewrite the result, which is why the nudge lives here.
* ``EventMemProvider`` is the ``MemoryProvider`` surface: ``prefetch`` for recall and
  seven tools (``evt_add`` / ``expand`` / ``mem_neighbors`` / ``mem_recall`` / ``proj_add`` /
  ``proj_list`` /
  ``evt_forget``).

``store.py`` / ``recall.py`` / ``embed.py`` are imported lazily on purpose: this module
must import (and its middleware must keep working) even while those files are absent,
half-written, or broken. A broken write path may lose events; it must never break a turn.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import shlex
import threading
import time
from pathlib import Path
from typing import Any, Optional

from agent.memory_provider import MemoryProvider, RecallStatus, spawn_context_thread

try:  # 宿主里是包，standalone 脚本（worker）是平铺
    from .i18n import tr  # noqa: E402
except ImportError:  # pragma: no cover
    from i18n import tr  # type: ignore[no-redef]  # noqa: E402

logger = logging.getLogger(__name__)

PROVIDER_NAME = "eventmem"
PLUGIN_VERSION = "0.1.0"
DB_SUBDIR = "eventmem"
DB_NAME = "events.db"

# The nudge appended to a mutating tool's result. Kept byte-identical everywhere so a
# second registered middleware (or a reload) can recognise its own line and not double it.
MEMORY_HINT_ZH = "\n[memory] 该动作改变了系统状态；本回合结束前请调用 evt_add 补一条 event"
MEMORY_HINT_EN = ("\n[memory] this changed system state; call evt_add before the turn ends "
                  "to record the event")


def memory_hint() -> str:
    """The reminder line appended to state-changing tool results, in the active language."""
    return tr(MEMORY_HINT_ZH, MEMORY_HINT_EN)

KINDS: tuple[str, ...] = ("决定", "尝试", "结果", "变更", "教训")
RELS: tuple[str, ...] = ("改变", "否定", "重复", "因为", "细化")
UNFILED = "unfiled"
MAX_EVENTS_PER_CALL = 3

# 回合末抽取用的 auxiliary LLM 任务（register() 里注册，可在 auxiliary.eventmem_extract 配置）
AUX_TASK = "eventmem_extract"
def _extract_system() -> str:
    """The extraction prompt, in the language active right now (not at import time)."""
    return tr(
        "你是事件记忆抽取器。读一轮对话，抽出 0–3 条值得长期记住的事件。\n"
        "每条一句话（第一人称，≤120 字），kind 只能是 决定 / 尝试 / 结果 / 变更 / 教训。\n"
        "记：结论、决定及理由、持久改变、踩过的坑；不记寒暄、过程性步骤、复述。\n"
        "实质对话（不是寒暄/确认）至少 1 条；只有纯寒暄才返回空数组。\n"
        '只输出严格 JSON，不要代码块、不要解释：{"events":[{"kind":"结果","text":"…"}]}',
        "You extract event memory. Read one conversation turn and pull out 0–3 events worth\n"
        "remembering long-term.\n"
        "One sentence per event (first person, ≤120 characters); kind is one of\n"
        "决定 / 尝试 / 结果 / 变更 / 教训.\n"
        "Record conclusions, decisions and their reasons, lasting changes, dead ends and the\n"
        "traps hit on the way; skip greetings, step-by-step narration and restatements.\n"
        "A substantive turn needs at least 1 event; an empty array is only for small talk.\n"
        'Output strict JSON only, no code fence, no prose: {"events":[{"kind":"结果","text":"…"}]}',
    )

def _aux_llm(messages: list[dict[str, Any]], *, max_tokens: int = 500,
             timeout: float = 60.0) -> Any:
    """One auxiliary-model call for post-turn extraction (tests monkeypatch this)."""
    from agent.auxiliary_client import call_llm

    return call_llm(task=AUX_TASK, messages=messages, temperature=0,
                    max_tokens=max_tokens, timeout=timeout)


# --------------------------------------------------------------------------- lazy imports

_MODULES: dict[str, Any] = {}


def _module(name: str) -> Any:
    """Import a sibling module (store/recall/embed), or ``None`` while it is unavailable.

    Successes are cached; failures are retried on the next call so a module written during
    the same process lifetime (dev loop, tests) is picked up without a restart.
    """
    cached = _MODULES.get(name)
    if cached is not None:
        return cached
    package = __package__ or __name__
    try:
        mod = importlib.import_module(f".{name}", package)
    except Exception as exc:  # noqa: BLE001 - a missing sibling must not break the plugin
        logger.debug("eventmem: module %r unavailable (%s: %s)", name, type(exc).__name__, exc)
        return None
    _MODULES[name] = mod
    return mod


# --------------------------------------------------------------------------- redaction

def _redact(text: str) -> str:
    """Best-effort secret scrub before anything is persisted (design §11.8).

    The host redactor is optional: without it we store what we were given rather than
    failing the write.
    """
    if not text:
        return text
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True, redact_url_credentials=True)
    except Exception:  # noqa: BLE001
        return text


# --------------------------------------------------------------------------- shell parsing

_EDIT_TOOLS = frozenset({
    # Every file-editing tool is state-changing regardless of its arguments.
    "write_file", "patch", "create_file", "edit_file", "apply_patch", "delete_file",
    "move_file", "rename_file", "append_file", "replace_in_file", "multi_edit",
    "str_replace_editor", "notebook_edit", "skill_manage",
})
_SHELL_TOOLS = frozenset({"terminal", "run_command", "shell", "bash", "exec_command"})
_CODE_TOOLS = frozenset({"execute_code"})


def _split_segments(command: str) -> list[str]:
    """Split a shell line on top-level ``;`` / ``&&`` / ``||`` / ``|`` / newline.

    Quotes and backslash escapes are respected so ``echo "a; b"`` stays one segment; a
    construct we do not model (``$(...)``) may over-split, which only adds candidates.
    """
    segments: list[str] = []
    buf: list[str] = []
    quote = ""
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if ch in ";\n":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        if command.startswith("&&", i) or command.startswith("||", i):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch == "|":
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def _writes_via_redirect(segment: str) -> bool:
    """True when the segment redirects stdout/stderr to a real file (``>`` / ``>>``).

    ``2>&1`` (fd duplication) and ``>/dev/null`` are not durable writes and are ignored.
    """
    quote = ""
    i, n = 0, len(segment)
    while i < n:
        ch = segment[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == ">":
            append = segment.startswith(">>", i)
            j = i + (2 if append else 1)
            while j < n and segment[j] in " \t":
                j += 1
            target = segment[j:].split()[0] if segment[j:].strip() else ""
            if target.startswith("&") or target.startswith("/dev/null"):
                i = j + len(target)
                continue
            return True
        i += 1
    return False


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return segment.split()


def _base(token: str) -> str:
    return os.path.basename(token).rstrip("/").lower()


def _strip_assignments(tokens: list[str]) -> list[str]:
    """Drop leading ``NAME=value`` env assignments (``FOO=1 rm x``)."""
    out = list(tokens)
    while out and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", out[0]):
        out.pop(0)
    return out


_VALUE_FLAGS: dict[str, set[str]] = {
    "sudo": {"-u", "-g", "-p", "-C", "-h", "-r", "-t", "-U", "-D"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S"},
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p", "-t"},
    "stdbuf": {"-i", "-o", "-e"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "xargs": {"-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a", "-R", "--max-args",
              "--max-procs", "--delimiter", "--arg-file"},
    "caffeinate": {"-t", "-w"},
    "ssh": {"-p", "-i", "-l", "-o", "-L", "-R", "-D", "-F", "-E", "-c", "-m", "-b",
            "-e", "-J", "-S", "-W", "-Q"},
    "git": {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
            "--config-env"},
}
_WRAPPERS = frozenset({"sudo", "doas", "env", "nohup", "command", "exec", "builtin",
                       "unbuffer", "time", "nice", "ionice", "stdbuf", "timeout",
                       "caffeinate"})
_READONLY_MODULES = frozenset({"pytest", "unittest", "json.tool", "pydoc", "timeit",
                               "site", "platform", "compileall"})


def _skip_flags(tokens: list[str], base: str) -> list[str]:
    """Drop leading options (and their values) so the wrapped command comes first."""
    values = _VALUE_FLAGS.get(base, set())
    out = list(tokens)
    while out and out[0].startswith("-"):
        flag = out.pop(0)
        if flag in values and out:
            out.pop(0)
    return out


# Read-only sub-commands for tools whose family name alone says nothing.
_SUBCOMMAND_READONLY: dict[str, set[str]] = {
    "git": {"status", "log", "diff", "show", "rev-parse", "describe", "blame", "grep",
            "ls-files", "ls-tree", "shortlog", "whatchanged", "merge-base", "cat-file",
            "check-ignore", "name-rev", "for-each-ref", "show-ref", "reflog",
            "count-objects", "verify-pack"},
    "docker": {"ps", "images", "logs", "inspect", "stats", "version", "info", "top",
               "port", "diff", "history", "search", "events"},
    "podman": {"ps", "images", "logs", "inspect", "stats", "version", "info", "top",
               "port", "history", "search", "events"},
    "systemctl": {"status", "show", "is-active", "is-enabled", "is-failed", "cat",
                  "help", "get-default", "is-system-running", "list-units",
                  "list-unit-files", "list-timers", "list-sockets", "list-dependencies"},
    "apt": {"list", "show", "search", "policy", "depends", "rdepends", "madison", "changelog"},
    "apt-get": {"list", "show", "search", "policy", "depends", "rdepends", "changelog"},
    "pip": {"list", "show", "freeze", "check", "index", "inspect"},
    "pip3": {"list", "show", "freeze", "check", "index", "inspect"},
    "uv": set(),
    "npm": {"ls", "list", "view", "info", "outdated", "search", "explain", "root", "prefix"},
    "pnpm": {"list", "ls", "view", "info", "outdated", "search", "root"},
    "yarn": {"list", "info", "why", "outdated", "search", "cache"},
    "conda": {"list", "info", "search", "env"},
    "mamba": {"list", "info", "search", "env"},
    "gem": {"list", "search", "info", "env", "contents", "which"},
    "brew": {"list", "info", "search", "outdated", "config", "doctor"},
    "cargo": {"search", "metadata", "tree", "version", "locate-project", "pkgid"},
    "go": {"version", "env", "list", "doc", "help"},
    "kubectl": {"get", "describe", "logs", "version", "explain", "top", "api-resources",
                "api-versions", "events", "config"},
    "helm": {"list", "ls", "status", "history", "show", "search", "version", "get"},
    "snap": {"list", "find", "info", "version"},
    "dnf": {"list", "info", "search", "repoquery", "repolist", "check-update"},
    "yum": {"list", "info", "search", "repolist", "check-update"},
}

# Commands that change machine state by definition.
_WRITE_COMMANDS = frozenset({
    "rm", "rmdir", "unlink", "mkdir", "touch", "mv", "cp", "ln", "install", "rsync",
    "chmod", "chown", "chgrp", "truncate", "dd", "tee", "shred", "mkfs", "mkfs.ext4",
    "mkfs.xfs", "mkswap", "mount", "umount", "swapon", "swapoff", "parted", "fdisk",
    "sfdisk", "wipefs", "crontab", "at", "batch", "systemd-run", "useradd", "usermod",
    "userdel", "groupadd", "groupdel", "passwd", "chpasswd", "visudo", "iptables",
    "ip6tables", "nft", "ufw", "firewall-cmd", "ssh-keygen", "gpg", "openssl",
    "update-alternatives", "update-rc.d", "rc-update", "chkconfig", "launchctl",
    "systemctl", "service", "journalctl", "docker", "podman", "docker-compose",
    "kubectl", "helm", "git", "svn", "hg",
    "apt", "apt-get", "aptitude", "dpkg", "snap", "dnf", "yum", "pacman", "apk",
    "zypper", "brew", "port", "pip", "pip3", "pipx", "poetry", "uv", "conda", "mamba",
    "npm", "pnpm", "yarn", "bun", "gem", "composer", "dotnet", "cargo", "rustup",
    "go", "make", "cmake", "ninja", "meson", "gcc", "g++", "clang", "clang++", "rustc",
    "javac", "mvn", "gradle", "ant",
    "ffmpeg", "convert", "magick", "yt-dlp", "zip", "unzip", "7z", "tar", "gzip",
    "gunzip", "bzip2", "xz", "zstd", "chattr",
})

_CODE_WRITE_MARKERS: tuple[str, ...] = (
    "open(", "write", "write_text", "writelines", "unlink", "remove(", "removedirs",
    "rmtree", "makedirs", "mkdir", "rename", "replace(", "subprocess", "os.system",
    "chmod", "chown", "shutil", "copyfile", "copytree", "move(", "savez", "savetxt",
    "to_csv", "to_parquet", "to_excel", "savefig", ".save(", ".dump(", "sqlite3.connect",
    "create_engine", "terraform", "pip install", "npm install", "git commit",
)


def _code_mutates(code: str) -> bool:
    low = (code or "").lower()
    return any(marker in low for marker in _CODE_WRITE_MARKERS)


_SHELLS = frozenset({"bash", "sh", "zsh", "fish", "dash", "ksh"})

# Here-doc / `python -` scripts: the body is not a command line, so `_split_segments` cannot
# classify it. Only the write-marker scan can tell whether such a script mutates.
_HEREDOC_INTERPRETER_RE = re.compile(
    r"(?m)^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:python[0-9.]*|pypy[0-9.]*|node|nodejs|deno|ruby|perl|php|lua|bash|sh|zsh|fish|dash|ksh)\b"
)

# ``hermes`` subcommands: config/plugins/cron writes change state the family table cannot see.
_HERMES_READONLY_SUB = frozenset({"status", "version", "doctor", "logs", "completion", "which",
                                  "help", "--version", "--help"})
_HERMES_READONLY_INNER: dict[str, set[str]] = {
    "config": {"get", "list", "show"},
    "plugins": {"list", "ls", "status", "info", "search", "show"},
    "memory": {"list", "status", "show"},
    "cron": {"list", "ls", "show"},
    "skill": {"list", "ls", "view", "show", "search"},
    "skills": {"list", "ls", "view", "show", "search"},
    "tools": {"list", "ls"},
    "models": {"list", "ls"},
}

# ``gh`` verbs that write to GitHub (issue / PR / release / repo / gist).
_GH_MUTATING = frozenset({"create", "merge", "close", "reopen", "edit", "delete", "comment",
                          "review", "approve", "upload", "clone", "set", "add", "remove",
                          "archive", "sync", "import", "pin", "unpin", "rename", "login",
                          "logout"})


def _first_word(tokens: list[str]) -> str:
    return next((t for t in tokens if not t.startswith("-")), "")


def _hermes_mutates(rest: list[str]) -> bool:
    sub = _first_word(rest)
    if not sub or sub in _HERMES_READONLY_SUB:
        return False
    allowed = _HERMES_READONLY_INNER.get(sub)
    if allowed is None:
        return True  # setup / config set / update / backup ... write something
    tail = rest[rest.index(sub) + 1:] if sub in rest else rest
    return _first_word(tail) not in allowed


def _interpreter_mutates(name: str, rest: list[str], depth: int = 0) -> bool:
    """A script run is an action; ``-c``/``-e`` snippets only when they write.

    ``python -`` / ``bash <<EOF`` (script on stdin) return False here: the caller scans the
    here-doc body with ``_code_mutates`` instead, because the body is not a command line.
    """
    args = list(rest)
    shell = name in _SHELLS
    if any(a == "-" or a.startswith("<<") for a in args):
        return False
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag in ("--version", "--help", "-V", "-h"):
            return False
        if flag == "-c" or (shell and flag.startswith("-") and not flag.startswith("--")
                            and "c" in flag):
            code = " ".join(args)
            return _command_is_mutating(code, depth + 1) if shell else _code_mutates(code)
        if flag in ("-e", "-E", "--eval", "-Command") and not shell:
            return _code_mutates(" ".join(args))
        if flag in ("-m", "-M", "--module"):
            module = args[0] if args else ""
            return module not in _READONLY_MODULES
    return bool(args)  # a script path was given


def _tar_mutates(rest: list[str]) -> bool:
    for tok in rest:
        if not tok.startswith("-"):
            continue
        flags = tok[1:]
        if "t" in flags:
            return False
        if any(c in flags for c in "cxruA"):
            return True
    return False  # `tar` with no mode is a usage error


def _segment_is_mutating(segment: str, depth: int = 0) -> bool:
    if _writes_via_redirect(segment):
        return True
    return _classify_tokens(_tokens(segment), depth)


def _classify_tokens(tokens: list[str], depth: int = 0) -> bool:
    """Classify one tokenized command; wrappers recurse into what they wrap."""
    tokens = _strip_assignments(tokens)
    if not tokens or depth > 4:
        return False

    base = _base(tokens[0])
    rest = tokens[1:]

    # Wrappers: recurse on the command they wrap.
    if base in _WRAPPERS:
        tail = _skip_flags(rest, base)
        if base == "timeout" and tail and not tail[0].startswith("-"):
            tail = tail[1:]  # `timeout 30 <cmd>`: skip the duration
        return _classify_tokens(tail, depth + 1)
    if base == "xargs":
        return _classify_tokens(_skip_flags(rest, base), depth + 1)
    if base == "find":
        # `find -exec ... ;` / `-delete` are the two ways find mutates.
        return any(tok in ("-delete", "--delete", "-exec", "-execdir", "-ok") for tok in rest)
    if base == "ssh":
        target = _skip_flags(rest, "ssh")
        if not target:
            return False
        remote = " ".join(target[1:])  # drop the host; the rest is a remote shell command
        if not remote.strip():
            return False
        return any(_segment_is_mutating(segment, depth + 1) for segment in _split_segments(remote))
    if base == "crontab":
        return not any(t in ("-l", "--list") for t in rest)
    if base == "hermes":
        return _hermes_mutates(rest)
    if base == "gh":
        sub = _first_word(rest)
        verb = _first_word(rest[rest.index(sub) + 1:]) if sub in rest else ""
        return sub in _GH_MUTATING or verb in _GH_MUTATING

    # Editing commands whose mutating form depends on a flag.
    if base == "sed":
        return any(t == "--in-place" or t == "-i" or t.startswith("-i") or t.startswith("--in-place=")
                   for t in rest)
    if base in ("perl", "perl5"):
        return any(t.startswith("-i") or t == "-pi" for t in rest)
    if base in ("awk", "gawk", "mawk"):
        return any(t in ("-i", "--include") for t in rest) and "inplace" in " ".join(rest).lower()
    if base in ("curl", "wget"):
        if base == "wget":
            if any(t == "--spider" for t in rest):
                return False
            for flag in ("-O", "--output-document"):
                if flag in rest:
                    target = rest[rest.index(flag) + 1] if rest.index(flag) + 1 < len(rest) else ""
                    return target != "-"  # `-O -` streams to stdout
            return True  # plain `wget URL` writes the file into the cwd
        for tok in rest:
            if tok in ("-o", "-O", "-P", "-T", "--output", "--output-document",
                       "--directory-prefix", "--upload-file"):
                return True
            # Short flags cluster: `curl -so file url` is `-s -o file url`.
            if re.match(r"^-[A-Za-z]*[oO]", tok) or tok.startswith(
                    ("--output=", "--output-document=", "--upload-file=", "--directory-prefix=")):
                return True
        return False
    if base == "pacman":
        return not any(re.match(r"^-[A-Za-z]*Q", t) for t in rest)
    if base == "tar":
        return _tar_mutates(rest)
    if base == "dpkg":
        return not any(t in ("-l", "--list", "-L", "--listfiles", "-s", "--status",
                             "-S", "--search", "-p", "--print-avail") for t in rest)
    if base in ("journalctl",):
        return any(t.startswith(("--vacuum-time", "--vacuum-size", "--vacuum-files", "--rotate",
                                 "--flush", "--sync", "--relinquish-var")) for t in rest)

    # Interpreters: `python train.py` is an action, `python -c "print(1)"` is not.
    if re.fullmatch(r"(python[0-9.]*|pypy[0-9.]*|node|nodejs|deno|bun|ruby|php|lua|"
                    r"Rscript|julia|bash|sh|zsh|fish|dash|ksh)", base):
        return _interpreter_mutates(base, rest, depth)
    if base == "pytest":
        return False

    # Family tables.
    readonly = _SUBCOMMAND_READONLY.get(base)
    if readonly is not None:
        rest = _skip_flags(rest, base)  # `git -C /path status` → subcommand is `status`
        sub = next((t for t in rest if not t.startswith("-")), "")
        if not sub or sub in ("--version", "--help"):
            return False
        if base in ("docker", "podman") and sub in {"container", "image", "network", "volume"}:
            inner = next((t for t in rest[rest.index(sub) + 1:] if not t.startswith("-")), "")
            return inner not in {"ls", "list", "inspect", "ps", "history"}
        if base in ("docker", "podman") and sub == "compose":
            inner = next((t for t in rest[rest.index(sub) + 1:] if not t.startswith("-")), "")
            return inner not in {"logs", "ps", "config", "top", "images", "version"}
        if base == "systemctl":
            return sub not in readonly
        if base == "kubectl" and sub == "config":
            inner = next((t for t in rest[rest.index(sub) + 1:] if not t.startswith("-")), "")
            return inner not in {"view", "get-contexts", "current-context", "get"}
        if base == "git" and sub in {"branch", "tag", "remote", "config", "stash", "worktree"}:
            flags = set(rest)
            if sub == "branch":
                return bool(flags & {"-d", "-D", "-m", "-M", "-f", "--delete", "--move",
                                     "--force", "-u", "--set-upstream-to", "-c", "-C"})
            if sub == "tag":
                return bool(flags & {"-d", "-D", "-a", "-s", "-f", "--delete", "-m"})
            if sub == "remote":
                inner = next((t for t in rest[rest.index(sub) + 1:] if not t.startswith("-")), "")
                return inner in {"add", "remove", "rm", "rename", "set-url", "set-head",
                                 "prune", "update"}
            if sub == "config":
                return not bool(flags & {"--get", "--get-all", "-l", "--list", "--get-regexp"})
            if sub == "stash":
                inner = next((t for t in rest[rest.index(sub) + 1:] if not t.startswith("-")), "")
                return inner not in ("list", "show")
            return True
        return sub not in readonly

    if base in _WRITE_COMMANDS:
        return True
    return False


def _command_is_mutating(command: Any, depth: int = 0) -> bool:
    if isinstance(command, (list, tuple)):
        command = " && ".join(str(part) for part in command)
    if not isinstance(command, str) or not command.strip():
        return False
    if any(_segment_is_mutating(segment, depth) for segment in _split_segments(command)):
        return True
    # `python - <<'PY' … PY` / `bash <<EOF`: the body is script source, not a command line, so
    # the segment walk sees only `python -`. Scan the whole text for write markers instead.
    if "<<" in command and _HEREDOC_INTERPRETER_RE.search(command):
        return _code_mutates(command)
    return False


def is_state_changing(tool_name: str, args: Optional[dict[str, Any]] = None) -> bool:
    """Does this tool call leave something behind after the session ends?

    Deliberately includes *failed* mutating commands (design §4.1): a command that was
    retried three times and abandoned is the most valuable kind of 教训.
    """
    name = str(tool_name or "").strip().lower()
    args = args if isinstance(args, dict) else {}
    if name in _EDIT_TOOLS:
        return True
    if name in _SHELL_TOOLS:
        return _command_is_mutating(args.get("command"))
    if name in _CODE_TOOLS:
        return _code_mutates(str(args.get("code") or args.get("source") or ""))
    return False


_MEMORY_HINT_BODIES = (MEMORY_HINT_ZH.strip(), MEMORY_HINT_EN.strip())


def on_tool_result(tool_name: str = "", args: Optional[dict[str, Any]] = None,
                   next_call: Optional[Any] = None, **ctx: Any) -> Any:
    """``tool_execution`` middleware: run the tool, then nudge on a state change.

    Signature matches ``hermes_cli/middleware.py::_run_execution_chain``, which invokes
    the callback with keyword arguments (``tool_name``, ``args``, ``next_call``,
    ``original_args``, session/task ids and the two schema-version keys) — hence the
    keyword defaults and ``**ctx``. ``next_call`` is single-use and must be called
    exactly once; a failure is re-raised instead of being swallowed.
    """
    if next_call is None:
        raise TypeError("eventmem.on_tool_result: next_call is required")
    result = next_call()
    try:
        changing = is_state_changing(tool_name, args)
    except Exception as exc:  # noqa: BLE001 - a classifier bug must not fail the tool call
        logger.debug("eventmem: is_state_changing(%r) failed: %s", tool_name, exc)
        return result
    if not changing:
        return result
    if isinstance(result, str):
        if any(body in result for body in _MEMORY_HINT_BODIES):
            return result
        return result + memory_hint()
    if isinstance(result, list):  # OpenAI-style content blocks
        try:
            if any(isinstance(block, dict)
                   and any(body in str(block.get("text", "")) for body in _MEMORY_HINT_BODIES)
                   for block in result):
                return result
            return list(result) + [{"type": "text", "text": memory_hint().strip()}]
        except Exception:  # noqa: BLE001
            return result
    # Structured results (dict/None/...) are left untouched: the reminder is model-facing
    # text and there is no safe place to put it inside an arbitrary payload.
    return result


# --------------------------------------------------------------------------- provider

class EventMemProvider(MemoryProvider):
    """Event memory provider: append-only events, project skeleton, hybrid recall."""

    def __init__(self) -> None:
        self._home = Path.home() / ".hermes"
        self._session_id = ""
        self._primary = True
        self._store: Optional[Any] = None
        self._encoder: Optional[Any] = None
        self._encoder_dead = False
        self._lock = threading.RLock()
        self._session_project = ""
        self._last_recall: Optional[RecallStatus] = None
        # 跨轮召回状态（已给过正文的 id / 已给过骨架的项目）。会话边界、压缩、撤销时丢弃重建。
        self._recall_state: Optional[Any] = None
        self._init_error = ""
        self._warm_thread: Optional[threading.Thread] = None
        # 本回合是否已经写过事件（on_turn_start 置 False，evt_add 成功置 True）。
        # sync_turn 在回合结束后读它，决定要不要做回合末抽取（设计 §4.5）。
        self._turn_wrote = False

    # -- identity / lifecycle ------------------------------------------------

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def is_available(self) -> bool:
        """Store code is the only hard requirement; embeddings degrade to FTS."""
        return _module("store") is not None

    def unavailable_reason(self) -> str:
        if _module("store") is None:
            return tr("eventmem: 无法导入 eventmem.store（store.py 缺失或损坏）",
                  "eventmem: cannot import eventmem.store (store.py missing or broken)")
        return ""

    def initialize(self, session_id: str, **kwargs) -> None:
        self._home = Path(kwargs.get("hermes_home") or self._home)
        self._session_id = session_id or ""
        context = str(kwargs.get("agent_context") or "primary")
        self._primary = context in ("primary", "")
        self._session_project = ""
        self._recall_state = None
        self._ensure_store()
        self._warm_encoder()

    def shutdown(self) -> None:
        with self._lock:
            store, self._store = self._store, None
        close = getattr(store, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("eventmem: store close failed: %s", exc)

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs: Any) -> None:
        switched = bool(new_session_id) and new_session_id != self._session_id
        self._session_id = new_session_id or self._session_id
        if reset:
            self._session_project = ""
        # 新会话 / 压缩（就地压缩时 session_id 不变，靠 reason 识别）/ 撤销 之后，
        # 「上一轮给过什么」都不再成立：计数归零，下一轮按新会话给骨架。
        if reset or rewound or switched or str(kwargs.get("reason") or "") == "compression":
            self._recall_state = None

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        """New turn: the write flag starts clean (design §4.5)."""
        self._turn_wrote = False

    def system_prompt_block(self) -> str:
        # Design §4.2: the write rules live in evt_add's tool description, not in a new
        # system-prompt section. Returning "" also keeps the cached prefix byte-stable.
        return ""

    def recall_status(self) -> Optional[RecallStatus]:
        return self._last_recall

    # -- storage / encoder ---------------------------------------------------

    def _db_path(self) -> Path:
        return self._home / DB_SUBDIR / DB_NAME

    def _ensure_store(self) -> Optional[Any]:
        if self._store is not None:
            return self._store
        with self._lock:
            if self._store is not None:
                return self._store
            mod = _module("store")
            if mod is None:
                self._init_error = tr("eventmem.store 不可用", "eventmem.store unavailable")
                return None
            try:
                self._db_path().parent.mkdir(parents=True, exist_ok=True)
                self._store = mod.Store(str(self._db_path()))
                self._init_error = ""
            except Exception as exc:  # noqa: BLE001 - never crash a turn over storage
                self._init_error = f"{type(exc).__name__}: {exc}"
                logger.warning("eventmem: store init failed (%s)", self._init_error)
                self._store = None
            return self._store

    def _get_encoder(self) -> Optional[Any]:
        if self._encoder is not None or self._encoder_dead:
            return self._encoder
        with self._lock:
            if self._encoder is not None or self._encoder_dead:
                return self._encoder
            mod = _module("embed")
            if mod is None:
                self._encoder_dead = True
                return None
            try:
                self._encoder = mod.Encoder()
                check = getattr(self._encoder, "available", None)
                if callable(check) and not check():
                    reason = getattr(self._encoder, "reason", lambda: "")()
                    logger.warning("eventmem: encoder unavailable (%s); recall degrades to FTS", reason)
                    self._encoder_dead = True
                    self._encoder = None
            except Exception as exc:  # noqa: BLE001 - vectors are optional, FTS still works
                logger.warning("eventmem: encoder unavailable (%s); recall degrades to FTS", exc)
                self._encoder_dead = True
        return self._encoder

    def _warm_encoder(self) -> None:
        """Load the ONNX session off the turn's critical path.

        Cold load measured at ~1.3s on this box; without warming, the first ``prefetch`` of a
        process pays it inside the host's 8s prefetch window and the first ``evt_add`` pays it
        inline. Best-effort: a failure here is retried by the next ``encode()``.
        """
        if self._warm_thread is not None and self._warm_thread.is_alive():
            return
        encoder = self._get_encoder()
        load = getattr(encoder, "load", None) if encoder is not None else None
        if not callable(load):
            return
        try:
            thread = spawn_context_thread(load, name="eventmem-warm")
            thread.start()
            self._warm_thread = thread
        except Exception as exc:  # noqa: BLE001 - warm-up is optional
            logger.debug("eventmem: encoder warm-up skipped (%s)", exc)

    def _encode(self, text: str, *, is_query: bool = False) -> Optional[bytes]:
        encoder = self._get_encoder()
        if encoder is None or not text:
            return None
        try:
            vec = encoder.encode(text, is_query=is_query)
        except TypeError:
            try:
                vec = encoder.encode(text)
            except Exception:  # noqa: BLE001
                return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("eventmem: encode failed: %s", exc)
            return None
        return _vector_bytes(vec)

    def _session_default_project(self) -> str:
        """Sticky project for this session (design §4.3), from memory or the store."""
        if self._session_project:
            return self._session_project
        store = self._ensure_store()
        getter = getattr(store, "get_session_project", None) if store is not None else None
        if callable(getter) and self._session_id:
            try:
                row = getter(self._session_id)
            except Exception:  # noqa: BLE001
                row = None
            # store.Store returns {"session_id","project_id","project"}; tolerate a bare name too.
            name = row.get("project") if isinstance(row, dict) else row
            if isinstance(name, str) and name:
                self._session_project = name
        return self._session_project

    def _remember_session_project(self, name: str) -> None:
        self._session_project = name or ""
        store = self._ensure_store()
        setter = getattr(store, "set_session_project", None) if store is not None else None
        if callable(setter) and self._session_id:
            try:
                setter(self._session_id, name or None)
            except Exception as exc:  # noqa: BLE001
                logger.debug("eventmem: persist session project failed: %s", exc)

    # -- recall --------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> Any:
        """Recall for the upcoming turn; ``None`` when nothing matches."""
        self._last_recall = None
        text = (query or "").strip()
        if not text:
            return None
        store = self._ensure_store()
        if store is None:
            return None
        mod = _module("recall")
        if mod is None:
            return None
        state = self._recall_state
        if state is None:
            factory = getattr(mod, "RecallState", None)
            if callable(factory):
                state = factory()
                self._recall_state = state
        kwargs: dict[str, Any] = {"state": state} if state is not None else {}
        # 一次编码，两处共用：recall 的向量臂与项目候选都要 query 向量，各编一次就是 60 ms。
        encoder = self._get_encoder()
        qvec = None
        encode = getattr(mod, "query_vector", None)
        if callable(encode):
            try:
                qvec = encode(encoder, text)
            except Exception as exc:  # noqa: BLE001 - 编码失败就当没有向量臂
                logger.debug("eventmem: query encode failed (%s)", exc)
                qvec = None
        try:
            block = mod.recall(store, encoder, text, qvec=qvec, **kwargs)
        except Exception as exc:  # noqa: BLE001 - recall must never fail a turn
            logger.warning("eventmem: recall failed (%s)", exc)
            return None
        if not isinstance(block, str) or not block.strip():
            block = ""
        # 项目候选（方案：向量匹配 → 只给标题，模型只能选不能造）。recall 空时也单独给这一行：
        # 「这一回合没有任何命中」正是模型最不知道该往哪个项目里写的时候。
        hint = ""
        suggest = getattr(mod, "project_hint", None)
        if callable(suggest):
            try:
                hint = suggest(store, encoder, text, qvec=qvec) or ""
            except Exception as exc:  # noqa: BLE001 - 候选失败不影响召回
                logger.debug("eventmem: project hint failed (%s)", exc)
        if hint:
            block = (block.rstrip() + "\n\n" + hint) if block else hint
        if not block.strip():
            return None
        self._last_recall = RecallStatus(
            provider_label=PROVIDER_NAME,
            # Count rendered event lines (each ends with `#<id>`), not the block header.
            count=sum(1 for line in block.splitlines() if re.search(r"#\d+", line)),
        )
        return block

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: Optional[list[dict[str, Any]]] = None,
                  turn_author: Optional[dict[str, Any]] = None) -> None:
        """Turn end: distill a turn that wrote no events into 0–3 events via the aux model.

        The host has no channel to call the main model after it stops, so this runs on the
        manager's background worker: one auxiliary completion over the user message and the
        reply, then normal ``add_event`` writes (project / session / vector included).
        """
        if not self._primary or self._turn_wrote:
            return
        question = _squash(user_content, 2000)
        answer = _squash(assistant_content, 3000)
        if not question or not answer:
            return
        try:
            from agent.memory_provider import is_trivial_prompt
        except Exception:  # noqa: BLE001 - older hosts: record rather than skip
            is_trivial_prompt = None
        if is_trivial_prompt is not None and is_trivial_prompt(user_content or ""):
            return
        store = self._ensure_store()
        add = getattr(store, "add_event", None) if store is not None else None
        if not callable(add):
            return

        events = self._extract_events(question, answer)
        if events is None:
            # 抽取调用失败：留一条机械记录，至少不丢这一轮。
            events = [{
                "kind": "结果",
                "text": tr("用户问「%s」，我答：%s", 'User asked "%s"; I answered: %s') % (
                    _squash(user_content, 60), _first_sentence(assistant_content, 90)),
            }]
        if not events:
            logger.info("eventmem: extractor found nothing worth recording")
            return

        project = self._session_default_project() or None
        for item in events[:MAX_EVENTS_PER_CALL]:
            text = str(item.get("text") or "").strip()
            kind = str(item.get("kind") or "").strip()
            if not text or kind not in KINDS:
                continue
            vec = self._encode("[%s] %s %s" % (project or UNFILED, kind, text))
            try:
                add(kind, text, project=project,
                    session=session_id or self._session_id or None, vec=vec)
            except Exception as exc:  # noqa: BLE001 - never fail a turn over housekeeping
                logger.warning("eventmem: auto-record failed (%s)", exc)

    def _extract_events(self, question: str, answer: str) -> Optional[list[dict[str, Any]]]:
        """Ask the auxiliary model to distill the turn; ``None`` when the call/parse failed."""
        try:
            response = _aux_llm([
                {"role": "system", "content": _extract_system()},
                {"role": "user", "content": tr("用户：%s\n\n助手：%s", "User: %s\n\nAssistant: %s")
                 % (question, answer)},
            ])
            raw = response.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 - caller falls back to the mechanical record
            logger.warning("eventmem: extraction call failed (%s)", exc)
            return None
        parsed = _parse_events(raw)
        if parsed is None:
            logger.warning("eventmem: extraction returned unparsable output")
        return parsed

    # -- tools ---------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "evt_add",
                "description": tr(
                    "记录本回合发生的事件（0–3 条）。只在本回合的最后一个工具调用里写。\n"
                    "三条判据：\n"
                    "① 持久副作用（文件/配置/硬件/环境/代码库变了）→ 变更；\n"
                    "② 假设闭环（多次报错重试折算成一条）→ 尝试+结果，或证伪死路 → 教训；\n"
                    "③ 契约拍板（定方向、定约束、排除方案，带理由）→ 决定。\n"
                    "问答也要记：把「用户问了什么 + 结论/依据是什么」写成一条结果或教训，"
                    "不能因为没动文件就不记。\n"
                    "只有寒暄、纯确认、复述已知内容可以传空数组；实质回合没写（或传空数组），"
                    "回合结束后插件会用机械摘要自动补一条。",
                    "Record what happened this turn (0–3 events). Write them in the last tool call "
                    "of the turn.\n"
                    "Three tests:\n"
                    "(1) lasting side effect (files, config, hardware, environment or code changed) "
                    "→ 变更;\n"
                    "(2) hypothesis closed (several failed attempts collapse into one) → 尝试 + 结果, "
                    "or a dead end → 教训;\n"
                    "(3) contract settled (direction, constraints, rejected options, with reasons) "
                    "→ 决定.\n"
                    "Q&A counts too: write what the user asked plus the conclusion or its basis as a "
                    "结果 or a 教训 — don't skip it just because no file changed.\n"
                    "An empty array is only for greetings, bare confirmations and restating what is "
                    "already known; if a substantive turn writes nothing, the plugin appends a "
                    "mechanical summary after the turn.",
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "events": {
                            "type": "array",
                            "minItems": 0,
                            "maxItems": MAX_EVENTS_PER_CALL,
                            "description": tr("按发生顺序排列；0 条表示本回合无需记录。",
                                              "In the order they happened; 0 means nothing to record."),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "kind": {"type": "string", "enum": list(KINDS),
                                             "description": tr(
                                                 "决定 / 尝试 / 结果 / 变更 / 教训。",
                                                 "决定=decision, 尝试=attempt, 结果=result, "
                                                 "变更=change, 教训=lesson. Send the Chinese word.",
                                             )},
                                    "text": {"type": "string",
                                             "description": tr(
                                                 "一句话，≤120 字，第一人称；有\"为什么\"就带上。",
                                                 "One sentence, ≤120 characters, first person; "
                                                 "include the why when there is one.",
                                             )},
                                    "project": {
                                        "type": "string",
                                        "description": tr(
                                            "现有项目名，或 \"unfiled\"。不填则继承本会话"
                                            "默认项目。未知名字会被拒绝（不会自动建项目）。",
                                            "An existing project name, or \"unfiled\". Omitted = "
                                            "inherit the session's default project. Unknown names "
                                            "are rejected (no project is ever created implicitly).",
                                        ),
                                    },
                                    "artifact": {
                                        "type": "string",
                                        "description": tr(
                                            "大块不可压缩的原文：命令、配置、日志、代码。"
                                            "全文单独存，不进事件正文，用时 expand 取回。",
                                            "Bulk text that cannot be compressed: commands, configs, "
                                            "logs, code. Stored on its own, never in the event body; "
                                            "fetch it back with expand.",
                                        ),
                                    },
                                    "artifact_kind": {
                                        "type": "string",
                                        "description": tr("命令 / 配置 / 日志 / 代码 / 其他",
                                                          "command / config / log / code / other"),
                                    },
                                    "path": {
                                        "type": "string",
                                        "description": tr(
                                            "artifact 的源文件路径（可选，写绝对路径）。同一路径的多次改动"
                                            "靠它连成一条链——内容变了 sha 会变，路径不会。",
                                            "Source file of the artifact (optional, absolute). Events "
                                            "touching the same path get linked; the sha changes with "
                                            "the content, the path does not.",
                                        ),
                                    },
                                    "extra_projects": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": tr("同一事件还属于哪些项目（可选）。",
                                                          "Other projects this event also belongs to (optional)."),
                                    },
                                    "links": {
                                        "type": "array",
                                        "description": tr(
                                            "与同批前序事件或已有事件的关系；只在确知因果时写。",
                                            "Relations to earlier events in this batch or to existing "
                                            "ones; only when the connection is certain.",
                                        ),
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "rel": {"type": "string", "enum": list(RELS),
                                                        "description": tr(
                                                            "关系种类：改变 / 否定 / 重复 / 因为 / 细化。",
                                                            "改变=changes, 否定=negates, 重复=repeats, "
                                                            "因为=because, 细化=refines. Send the Chinese word.",
                                                        )},
                                                "index": {
                                                    "type": "integer", "minimum": 1,
                                                    "description": tr(
                                                        "同批事件的序号（1 起），必须早于本条。",
                                                        "1-based position within this batch; must point "
                                                        "to an earlier event.",
                                                    ),
                                                },
                                                "event_id": {
                                                    "type": "integer",
                                                    "description": tr(
                                                        "已有事件的 id（与 index 二选一）。",
                                                        "Id of an existing event (either this or index).",
                                                    ),
                                                },
                                                "conf": {
                                                    "type": "number",
                                                    "minimum": 0,
                                                    "maximum": 1,
                                                    "description": tr("可选，默认 1.0。",
                                                                      "Optional, default 1.0."),
                                                },
                                            },
                                            "required": ["rel"],
                                        },
                                    },
                                },
                                "required": ["kind", "text"],
                            },
                        },
                    },
                    "required": ["events"],
                },
            },
            {
                "name": "expand",
                "description": tr(
                    "取回一条事件的 artifact 全文（命令/配置/日志/代码原样返回）。"
                    "事件没有 artifact 时只返回事件本身。",
                    "Fetch the full artifact text behind one event (commands, configs, logs, code, "
                    "verbatim). Without an artifact it just returns the event.",
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "integer",
                                     "description": tr("召回行末尾的 #id。",
                                                       "The #id at the end of a recall line.")},
                    },
                    "required": ["event_id"],
                },
            },
            {
                "name": "mem_neighbors",
                "description": tr(
                    "按 #id 展开一条记忆在图上的邻居：谁改了/否定了它（后来的事件指过来）、"
                    "它自己取代或解释了谁（它指出去）、同会话前后几条。"
                    "召回行看着不完整、或想知道「这条是怎么来的/后来怎么样了」时用它。",
                    "Expand one memory by #id: who changed or negated it (later events pointing at it), "
                    "what it supersedes or explains, and its same-session neighbours. Use it when a "
                    "recall line looks incomplete or you need the story around one event.",
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "integer",
                                     "description": tr("行首的 #id。", "The #id from a memory line.")},
                        "rels": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(RELS)},
                            "description": tr(
                                "跟哪几种关系，默认 改变/否定/因为。细化/重复数量大，需要时再加。",
                                "Relations to follow; default change/negate/because. Add refine/repeat "
                                "only when you need them — they are numerous.",
                            ),
                        },
                        "depth": {"type": "integer", "minimum": 1, "maximum": 3,
                                  "description": tr("展开几跳，默认 1。", "Hops to expand, default 1.")},
                    },
                    "required": ["event_id"],
                },
            },
            {
                "name": "mem_recall",
                "description": tr(
                    "按全局相似度检索事件记忆（向量 + FTS5 → RRF），返回带日期、"
                    "项目名和 #id 的渲染块。自动注入不够时用它换措辞再查；"
                    "要原文时对命中的 #id 调 expand。",
                    "Search event memory by global similarity (vector + FTS5 → RRF); returns a "
                    "rendered block with dates, project names and #ids. Use it when the automatic "
                    "injection was not enough — try different wording — and call expand on a hit "
                    "when you need the full text.",
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string",
                                  "description": tr("自然语言或关键词。",
                                                    "Natural language or keywords.")},
                        "budget_tokens": {"type": "integer", "minimum": 100, "maximum": 4000,
                                          "description": tr("注入预算，默认 600。",
                                                            "Injection budget, default 600.")},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "proj_add",
                "description": tr(
                    "新建一个人工命名的项目。项目名只能由人/模型显式建立，"
                    "evt_add 不会自动建项目；同名已存在时返回已有 id。",
                    "Create a human-named project. Project names exist only because a person or the "
                    "model created them explicitly — evt_add never creates one; an existing name "
                    "returns its id.",
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string",
                                 "description": tr("项目名，例如 \"小主机\"。",
                                                   "Project name, e.g. \"mini-pc\".")},
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "proj_list",
                "description": tr(
                    "列出所有在用项目名（evt_add 的 project 只能从这里选，或写 \"unfiled\"）。",
                    "List the active project names (evt_add's project must come from here, or be "
                    "\"unfiled\").",
                ),
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "evt_forget",
                "description": tr(
                    "永久删除一条事件：事件行、它的边、项目关联、无人引用的 artifact。"
                    "不可撤销，只在用户明确要求遗忘时使用。",
                    "Permanently delete one event: the row, its edges, its project links and any "
                    "artifact nothing else references. Irreversible — only when the user explicitly "
                    "asks to forget something.",
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "integer",
                                     "description": tr("要删除的事件 id。",
                                                       "Id of the event to delete.")},
                    },
                    "required": ["event_id"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs: Any) -> str:
        try:
            if tool_name == "evt_add":
                return _json(self._tool_add(args or {}))
            if tool_name == "expand":
                return _json(self._tool_expand(args or {}))
            if tool_name == "mem_neighbors":
                return _json(self._tool_neighbors(args or {}))
            if tool_name == "mem_recall":
                return _json(self._tool_recall(args or {}))
            if tool_name == "proj_add":
                return _json(self._tool_project_add(args or {}))
            if tool_name == "proj_list":
                return _json(self._tool_project_list())
            if tool_name == "evt_forget":
                return _json(self._tool_forget(args or {}))
        except Exception as exc:  # noqa: BLE001 - report, never raise into the loop
            logger.warning("eventmem: tool %s failed: %s", tool_name, exc)
            return _json({"error": f"{type(exc).__name__}: {exc}"})
        return _json({"error": f"unknown tool {tool_name}"})

    # -- tool implementations ------------------------------------------------

    def _store_or_error(self) -> tuple[Optional[Any], Optional[dict[str, Any]]]:
        store = self._ensure_store()
        if store is None:
            return None, {"error": tr("eventmem 存储不可用", "eventmem storage unavailable"),
                 "detail": self._init_error}
        return store, None

    def _tool_add(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self._primary:
            return {"error": tr("eventmem: 当前不是主会话（agent_context 非 primary），不写事件",
                        "eventmem: not the primary session (agent_context != primary); "
                        "no events written")}
        store, err = self._store_or_error()
        if err:
            return err
        items = args.get("events")
        if items is None:
            items = []
        if not isinstance(items, list):
            return {"error": tr("events 必须是数组", "events must be an array")}
        if len(items) > MAX_EVENTS_PER_CALL:
            return {"error": tr(f"一次最多 {MAX_EVENTS_PER_CALL} 条事件（收到 {len(items)} 条）",
                            f"at most {MAX_EVENTS_PER_CALL} events per call "
                            f"(got {len(items)})")}
        if not items:
            # 空数组只表示「模型看过、没写」——不当作已记录：实质回合会在回合末被抽取补写。
            return {"ok": True, "count": 0,
                 "note": tr("本回合没有需要记录的事件", "nothing to record this turn")}

        known = [str(p.get("name")) for p in (store.list_projects() or [])]
        inherited = self._session_default_project()
        prepared: list[dict[str, Any]] = []
        for pos, item in enumerate(items, 1):
            if not isinstance(item, dict):
                return {"error": tr(f"第 {pos} 条不是对象", f"entry {pos} is not an object")}
            kind = str(item.get("kind") or "").strip()
            text = str(item.get("text") or "").strip()
            if kind not in KINDS:
                return {"error": tr(f"第 {pos} 条 kind={kind!r} 非法", f"entry {pos}: invalid kind={kind!r}"),
                 "kinds": list(KINDS)}
            if not text:
                return {"error": tr(f"第 {pos} 条 text 为空", f"entry {pos}: empty text")}
            # 不校验长度：120 字是工具描述里的软约束，超长也整条收——截断就等于丢内容。
            project, project_error = self._resolve_project(item, store, known, inherited, pos)
            if project_error:
                return project_error
            extras, extra_error = self._resolve_extras(item, store, known, pos)
            if extra_error:
                return extra_error
            prepared.append({
                "kind": kind,
                "text": _redact(text),
                "project": project,
                "extras": extras,
                "artifact": str(item.get("artifact") or "") or "",
                "artifact_kind": str(item.get("artifact_kind") or "其他").strip() or "其他",
                "art_path": str(item.get("path") or "").strip() or None,
                "links": item.get("links") if isinstance(item.get("links"), list) else [],
            })

        written: list[dict[str, Any]] = []
        for item in prepared:
            art_id = None
            artifact = item["artifact"]
            if artifact:
                try:
                    art_id = store.add_artifact(item["artifact_kind"], _redact(artifact))
                except Exception as exc:  # noqa: BLE001 - event still matters without the blob
                    logger.warning("eventmem: artifact write failed (%s)", exc)
                    art_id = None
            label = item["project"] or UNFILED
            vec = self._encode(f"[{label}] {item['kind']} {item['text']}")
            ts = time.time()
            try:
                event_id = store.add_event(
                    item["kind"], item["text"], ts=ts,
                    project=item["project"], session=self._session_id or None,
                    art_id=art_id, art_path=item.get("art_path"),
                    extra_projects=item["extras"] or None, vec=vec,
                )
            except Exception as exc:  # noqa: BLE001
                return {"error": tr(f"写入失败：{type(exc).__name__}: {exc}",
                              f"write failed: {type(exc).__name__}: {exc}"),
                        "written": written, "known_projects": known}
            written.append({
                "id": event_id, "ts": ts, "kind": item["kind"], "text": item["text"],
                "project": item["project"] or UNFILED, "artifact": bool(art_id),
            })

        sticky = next((w["project"] for w in written if w["project"] != UNFILED), "")
        if sticky:
            self._remember_session_project(sticky)

        edge_errors = self._write_links(store, prepared, written)
        self._turn_wrote = True
        result: dict[str, Any] = {"ok": True, "count": len(written), "events": written}
        if sticky:
            result["session_project"] = sticky
        if edge_errors:
            result["edge_errors"] = edge_errors
        return result

    def _resolve_project(self, item: dict[str, Any], store: Any, known: list[str],
                         inherited: str, pos: int) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        """``None`` means unfiled; an unknown *name* is an error, never a new project."""
        if "project" not in item or item.get("project") is None:
            # 会话粘性也要过归档检查：项目在会话中途被归档后，不能靠「不写 project」绕过去。
            return (inherited or None) if inherited in known else None, None
        raw = str(item.get("project") or "").strip()
        if not raw or raw == UNFILED:
            return None, None
        try:
            row = store.get_project(raw)
        except Exception as exc:  # noqa: BLE001
            return None, {"error": tr(f"第 {pos} 条项目查询失败：{exc}",
                             f"entry {pos}: project lookup failed: {exc}")}
        if not row:
            return None, {
                "error": tr(f"第 {pos} 条：未知项目 {raw!r}；不会自动新建",
                            f"entry {pos}: unknown project {raw!r}; nothing is created "
                            "automatically"),
                "known_projects": known,
                "hint": tr("先用 proj_add 建项目，或写 \"unfiled\"/省略 project",
                            "create it with proj_add first, or use \"unfiled\" / omit project"),
            }
        if str(row.get("status") or "active") == "archived":
            return None, {
                "error": tr(f"第 {pos} 条：项目 {raw!r} 已归档，不再接收新事件",
                            f"entry {pos}: project {raw!r} is archived and takes no new events"),
                "known_projects": known,
                "hint": tr("改写一个在用项目名，或写 \"unfiled\"",
                            "use an active project name, or \"unfiled\""),
            }
        return str(row.get("name") or raw), None

    def _resolve_extras(self, item: dict[str, Any], store: Any, known: list[str],
                        pos: int) -> tuple[list[str], Optional[dict[str, Any]]]:
        raw = item.get("extra_projects")
        if not isinstance(raw, list):
            return [], None
        out: list[str] = []
        for name in raw:
            name = str(name or "").strip()
            if not name or name == UNFILED:
                continue
            if name in out:
                continue
            try:
                row = store.get_project(name)
            except Exception as exc:  # noqa: BLE001
                return [], {"error": tr(f"第 {pos} 条次要项目查询失败：{exc}",
                             f"entry {pos}: secondary project lookup failed: {exc}")}
            if not row:
                return [], {"error": tr(f"第 {pos} 条：未知次要项目 {name!r}",
                            f"entry {pos}: unknown secondary project {name!r}"),
                            "known_projects": known}
            if str(row.get("status") or "active") == "archived":
                return [], {"error": tr(f"第 {pos} 条：次要项目 {name!r} 已归档，不能挂新事件",
                            f"entry {pos}: secondary project {name!r} is archived"),
                            "known_projects": known}
            out.append(str(row.get("name") or name))
        return out, None

    def _write_links(self, store: Any, prepared: list[dict[str, Any]],
                     written: list[dict[str, Any]]) -> list[str]:
        """Batch-internal edges (§5): the model knows the order, Jev only fills across days."""
        errors: list[str] = []
        for pos, (item, new) in enumerate(zip(prepared, written)):
            for link in item["links"]:
                if not isinstance(link, dict):
                    errors.append(tr(f"#{new['id']}: link 不是对象", f"#{new['id']}: link is not an object"))
                    continue
                rel = str(link.get("rel") or "").strip()
                if rel not in RELS:
                    errors.append(tr(f"#{new['id']}: rel={rel!r} 非法", f"#{new['id']}: invalid rel={rel!r}"))
                    continue
                dst: Optional[int] = None
                if link.get("index") is not None:
                    try:
                        index = int(link["index"])
                    except (TypeError, ValueError):
                        index = 0
                    # 1-based position inside this batch; only earlier events may be targets.
                    if not 1 <= index <= len(written) or index - 1 >= pos:
                        errors.append(tr(f"#{new['id']}: index={index} 必须指向本批前序事件",
                      f"#{new['id']}: index={index} must point to an earlier event in this batch"))
                        continue
                    dst = written[index - 1]["id"]
                elif link.get("event_id") is not None:
                    try:
                        dst = int(link["event_id"])
                    except (TypeError, ValueError):
                        errors.append(tr(f"#{new['id']}: event_id 不是整数", f"#{new['id']}: event_id is not an integer"))
                        continue
                    target = store.get_event(dst)
                    if not target:
                        # 指向不存在的事件会让边悬空，模型却以为连上了（返回 ok）。
                        errors.append(tr(f"#{new['id']}: event_id={dst} 不存在", f"#{new['id']}: no event #{dst}"))
                        continue
                    if (_event_key(target) >= _event_key(new)):
                        errors.append(tr(f"#{new['id']}: event_id={dst} 不早于本条，只能连向更早的事件",
                      f"#{new['id']}: event {dst} is not older than this one; links only go back"))
                        continue
                if dst is None:
                    errors.append(tr(f"#{new['id']}: link 缺少 index 或 event_id", f"#{new['id']}: link needs index or event_id"))
                    continue
                if dst == new["id"]:
                    errors.append(tr(f"#{new['id']}: 不能连向自己", f"#{new['id']}: an event cannot link to itself"))
                    continue
                try:
                    conf = link.get("conf")
                    store.add_edge(new["id"], dst, rel,
                                   1.0 if conf is None else float(conf), "llm_batch")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"#{new['id']} → #{dst} {rel}: {exc}")
        return errors

    def _tool_expand(self, args: dict[str, Any]) -> dict[str, Any]:
        store, err = self._store_or_error()
        if err:
            return err
        event_id = _as_int(args.get("event_id", args.get("id")))
        if event_id is None:
            return {"error": tr("event_id 必须是整数", "event_id must be an integer")}
        event = store.get_event(event_id)
        if not event:
            return {"error": tr(f"没有事件 #{event_id}", f"no event #{event_id}")}
        out: dict[str, Any] = {
            "id": event.get("id", event_id),
            "date": _fmt_ts(event.get("ts")),
            "kind": event.get("kind"),
            "text": event.get("text"),
            "project": event.get("project") or UNFILED,
            "artifact": None,
        }
        art_id = event.get("art_id")
        if art_id:
            artifact = store.get_artifact(int(art_id))
            if artifact:
                out["artifact"] = {"kind": artifact.get("kind"), "text": artifact.get("text")}
            else:
                out["note"] = tr(f"artifact #{art_id} 已不存在", f"artifact #{art_id} is gone")
        else:
            out["note"] = tr("该事件没有 artifact", "this event has no artifact")
        if event.get("art_path"):
            out["path"] = event["art_path"]
        return out

    def _tool_neighbors(self, args: dict[str, Any]) -> dict[str, Any]:
        """Expand one event's graph neighbourhood (store.edges_* → rendered block)."""
        store, err = self._store_or_error()
        if err:
            return err
        event_id = _as_int(args.get("event_id", args.get("id")))
        if event_id is None:
            return {"error": tr("event_id 必须是整数", "event_id must be an integer")}
        event = store.get_event(event_id)
        if not event:
            return {"error": tr(f"没有事件 #{event_id}", f"no event #{event_id}")}
        mod = _module("recall")
        if mod is None:
            return {"error": tr("recall 模块不可用", "recall module unavailable")}
        rels = args.get("rels")
        if isinstance(rels, str):
            rels = [part for part in re.split(r"[,/\s]+", rels) if part]
        if not isinstance(rels, list) or not rels:
            rels = None
        depth = _as_int(args.get("depth")) or 1
        block = mod.neighbours(store, event_id, rels=rels, depth=depth) or ""
        return {"event_id": event_id, "context": block, "found": bool(block.strip())}

    def _tool_recall(self, args: dict[str, Any]) -> dict[str, Any]:
        store, err = self._store_or_error()
        if err:
            return err
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": tr("query 不能为空", "query must not be empty")}
        mod = _module("recall")
        if mod is None:
            return {"error": tr("recall 模块不可用", "recall module unavailable")}
        budget = args.get("budget_tokens")
        try:
            budget_tokens = int(budget) if budget is not None else 600
        except (TypeError, ValueError):
            budget_tokens = 600
        block = mod.recall(store, self._get_encoder(), query,
                           budget_tokens=max(100, min(budget_tokens, 4000)))
        block = block or ""
        return {"query": query, "context": block, "found": bool(block.strip())}

    def _tool_project_add(self, args: dict[str, Any]) -> dict[str, Any]:
        store, err = self._store_or_error()
        if err:
            return err
        name = re.sub(r"\s+", " ", str(args.get("name") or "")).strip()
        if not name:
            return {"error": tr("name 不能为空", "name must not be empty")}
        if name == UNFILED:
            return {"error": tr("\"unfiled\" 是未归类的保留名，不能作为项目名",
                            "\"unfiled\" is the reserved name for unclassified events")}
        if len(name) > 60:
            return {"error": tr("项目名过长（上限 60 字）", "project name too long (max 60 characters)")}
        existing = store.get_project(name)
        if existing:
            if str(existing.get("status") or "active") == "archived":
                return {"ok": True, "id": existing.get("id"), "name": name, "existed": True,
                        "archived": True,
                        "note": tr("该项目已归档，不接收新事件，也不作为本会话默认项目",
                            "this project is archived: no new events, and it will not become "
                            "the session default")}
            self._remember_session_project(name)
            return {"ok": True, "id": existing.get("id"), "name": name, "existed": True,
                    "session_project": name}
        project_id = store.add_project(name)
        self._remember_session_project(name)
        return {"ok": True, "id": project_id, "name": name, "existed": False,
                "session_project": name}

    def _tool_project_list(self) -> dict[str, Any]:
        store, err = self._store_or_error()
        if err:
            return err
        projects = []
        for row in store.list_projects() or []:
            projects.append({"id": row.get("id"), "name": row.get("name"),
                             "status": row.get("status") or "active"})
        return {"projects": projects, "count": len(projects),
                "note": tr("evt_add 的 project 只能选这里，或写 \"unfiled\"",
                        "evt_add's project must come from this list, or be \"unfiled\"")}

    def _tool_forget(self, args: dict[str, Any]) -> dict[str, Any]:
        store, err = self._store_or_error()
        if err:
            return err
        event_id = _as_int(args.get("event_id", args.get("id")))
        if event_id is None:
            return {"error": tr("event_id 必须是整数", "event_id must be an integer")}
        event = store.get_event(event_id)
        if not event:
            return {"error": tr(f"没有事件 #{event_id}", f"no event #{event_id}")}
        store.forget(event_id)
        return {"ok": True, "forgotten": event_id}


# --------------------------------------------------------------------------- helpers

def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _squash(text: Any, limit: int = 0) -> str:
    """Collapse whitespace (and optionally truncate) before storing a turn excerpt."""
    out = " ".join(str(text or "").split())
    if limit and len(out) > limit:
        out = out[: max(0, int(limit) - 1)] + "…"
    return out


def _first_sentence(text: Any, limit: int) -> str:
    """First sentence/clause of a reply, whitespace-collapsed and capped at ``limit``."""
    out = " ".join(str(text or "").split())
    if not out:
        return ""
    cut = len(out)
    for mark in ("。", "！", "？", "!", "?", "；"):
        i = out.find(mark)
        if 0 <= i < cut:
            cut = i + 1
    if cut < 20:  # 首句太短（"好。"）时不要只留两个字
        cut = len(out)
    out = out[:cut]
    if len(out) > limit:
        out = out[: max(0, int(limit) - 1)] + "…"
    return out


def _parse_events(raw: str) -> Optional[list[dict[str, Any]]]:
    """Parse the extractor's JSON; ``[]`` for "nothing to record", ``None`` when unusable."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start:end + 1])
    except (TypeError, ValueError):
        return None
    items = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        body = str(item.get("text") or "").strip()
        if kind in KINDS and body:
            out.append({"kind": kind, "text": body})
        if len(out) >= MAX_EVENTS_PER_CALL:
            break
    return out


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _event_key(ev: dict[str, Any]) -> tuple[float, int]:
    """Sort key for an event dict — ``(ts, id)``, the same order ``chain()`` uses."""
    try:
        return (float(ev.get("ts") or 0.0), int(ev.get("id") or 0))
    except (TypeError, ValueError):
        return (0.0, 0)


def _fmt_ts(ts: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _vector_bytes(vec: Any) -> Optional[bytes]:
    """float32 bytes for ``events.vec``; ``None`` when the value is unusable."""
    if vec is None:
        return None
    try:
        import numpy as np

        return np.asarray(vec, dtype=np.float32).tobytes()
    except Exception:  # noqa: BLE001 - numpy may be absent on the launcher interpreter
        pass
    try:
        import array

        return array.array("f", [float(x) for x in vec]).tobytes()
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- registration

def register(ctx: Any) -> None:
    """Install the provider and the tool-execution middleware."""
    ctx.register_memory_provider(EventMemProvider())
    register_middleware = getattr(ctx, "register_middleware", None)
    if callable(register_middleware):
        register_middleware("tool_execution", on_tool_result)
    else:  # pragma: no cover - every real PluginContext has it
        logger.warning("eventmem: plugin context has no register_middleware; "
                       "the state-change reminder is disabled")
    register_aux = getattr(ctx, "register_auxiliary_task", None)
    if callable(register_aux):
        try:
            register_aux(
                AUX_TASK,
                display_name=tr("eventmem 回合记忆提取", "eventmem turn extraction"),
                description=tr("回合结束后把这一轮对话抽成 0–3 条事件（插件内部使用）",
                        "Extract 0–3 events from one conversation turn (plugin-internal)"),
                defaults={},
            )
        except Exception as exc:  # noqa: BLE001 - unregistered task still uses the auto route
            logger.debug("eventmem: auxiliary task registration failed: %s", exc)
