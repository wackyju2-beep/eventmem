#!/usr/bin/env python3
"""Install/packaging contract: the Windows installer and the DSH bundle stay separate and safe.

Run directly:  python tests/test_install_contract.py

These are static checks on purpose: they run on every OS (including the Windows CI job) and
catch the regressions that matter for publishing — a bundle that claims to install the whole
system, a launcher that binds off loopback, or a token path that is dropped from the daemon
command line.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
PASS = FAIL = 0


def check(cond: bool, message: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {message}")
    else:
        FAIL += 1
        print(f"FAIL {message}")


def main() -> int:
    repo = REPO
    ps1 = (repo / "install.ps1").read_text(encoding="utf-8")
    patch = (repo / "adapters" / "dsh" / "cordis.patch.yml").read_text(encoding="utf-8")
    pkg = json.loads((repo / "adapters" / "dsh" / "package.json").read_text(encoding="utf-8"))
    adapter_readme = (repo / "adapters" / "dsh" / "README.md").read_text(encoding="utf-8")

    check("--host 127.0.0.1" in ps1, "Windows launcher binds the daemon to loopback")
    check("0.0.0.0" not in ps1, "Windows launcher never binds all interfaces")
    check("--token-file" in ps1, "Windows launcher can require an API token")
    check("RequireToken" in ps1, "installer exposes -RequireToken")
    check("daemon.token" in ps1, "installer knows where the token file lives")
    check("$selfcheck --url $Url --db $DbPath --token-file $TokenPath" in ps1,
          "self-check sends the token when one is required")
    check("adapters\\dsh" in ps1 and "Add plugin" in ps1,
          "installer tells the user the DSH plugin is a separate step")

    check("mode: local" in patch, "bundle default is local mode")
    check("mint" not in patch.lower(), "bundle default names no private SSH host")
    check(not re.search(r"tunnel:\s*true", patch), "bundle default does not open a tunnel")

    check(pkg.get("private") is not True, "bundle is publishable (private:true removed)")
    check("dsh" in pkg.get("engines", {}), "bundle declares engines.dsh")
    check("eventmem daemon" in pkg.get("description", "").lower(),
          "package description says the daemon is separate")
    check(pkg.get("dsh", {}).get("bundle", {}).get("patch") == "./cordis.patch.yml",
          "bundle still declares its patch")
    check("tokenFile" in adapter_readme and "tunnelHost" in adapter_readme,
          "adapter guide documents tokenFile and tunnelHost")

    # The adapter must not acquire a dependency on the core or on a DSH package: a linked
    # directory install has neither in its resolution path.
    for name in ("index.js", "lib/config.js", "lib/net.js", "lib/setup.js"):
        text = (repo / "adapters" / "dsh" / name).read_text(encoding="utf-8")
        imports = re.findall(r"from\s+['\"]([^'\"]+)['\"]", text)
        check(all(item.startswith(".") or item.startswith("node:") for item in imports),
              f"{name} imports only relative/node modules")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
