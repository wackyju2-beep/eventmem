"""eventmem / Hermes — the tool-result reminder (the "write it down" nudge).

Design §4.1: hook callbacks cannot issue tool calls, but a ``tool_execution`` middleware
can rewrite a tool result.  So every state-changing tool call gets one extra line telling
the model to call ``evt_add`` before the turn ends.  Whether a call "changes state" is a
Hermes question — it is about *Hermes'* tool names (``write_file``, ``terminal``,
``execute_code``, …) and the shell grammar inside their arguments — which is why this
whole module lives in the adapter and not in the core.

The classifier deliberately counts *failed* mutating commands: a command retried three
times and abandoned is the most valuable kind of 教训.  It only ever answers "append the
nudge?" — a false positive costs one line, a false negative costs a memory.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from typing import Any, Optional

from eventmem_core.i18n import tr

logger = logging.getLogger(__name__)

# The nudge appended to a mutating tool's result.  Kept byte-identical everywhere so a
# second registered middleware (or a reload) can recognise its own line and not double it.
MEMORY_HINT_ZH = "\n[memory] 该动作改变了系统状态；本回合结束前请调用 evt_add 补一条 event"
MEMORY_HINT_EN = ("\n[memory] this changed system state; call evt_add before the turn ends "
                  "to record the event")


def memory_hint() -> str:
    """The reminder line appended to state-changing tool results, in the active language."""
    return tr(MEMORY_HINT_ZH, MEMORY_HINT_EN)


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
