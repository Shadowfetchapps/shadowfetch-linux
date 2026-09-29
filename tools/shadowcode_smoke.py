#!/usr/bin/env python3
"""Host-side smoke check of the pinned ShadowCode .deb. No VM, no install, no root.

    python3 tools/shadowcode_smoke.py            # the verified build/ copy
    python3 tools/shadowcode_smoke.py --deb X.deb --json-out report.json

What it establishes, and what it does not:

  * The .deb is the pinned, upstream-signed one (same check the package gate
    runs). A smoke run of the wrong bytes would be a smoke run of nothing.
  * Extracted with `dpkg-deb -x` into a temporary directory -- never installed.
  * Every ELF under usr/bin and usr/lib/shadowcode resolves all its shared
    libraries on THIS host (`ldd`, "not found" is the failure). The bundled
    llama.cpp libraries resolve through their $ORIGIN runpath.
  * `usr/bin/shadowcode --version` answers "ShadowCode <pinned version>" with no
    DISPLAY and no WAYLAND_DISPLAY, from a throwaway HOME: the launcher's CLI
    path does not need a desktop. (clap handles --version before any window or
    engine exists; ShadowCode's src-tauri/src/main.rs parses args first.)
  * `usr/lib/shadowcode/llama-server --version` and `llama-cli --version` run,
    and report the llama.cpp commit the signed RELEASE-MANIFEST.json pins.

It does NOT prove the app opens a window, that the Debian testing userland in
the image satisfies the dependencies (this host may be a different
distribution -- the result names it), or anything about local-model inference.
The container smoke in the package gate and the `shadowcode` VM acceptance
case are where those are decided.

Exit 0 when every check passed, 1 otherwise. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

# APPENDED, not prepended: tools/release/ also holds a module named `acceptance`,
# which would shadow the tools/acceptance/ package for any later import.
_RELEASE_DIR = str(Path(__file__).resolve().parent / "release")
if _RELEASE_DIR not in sys.path:
    sys.path.append(_RELEASE_DIR)

import shadowcode  # noqa: E402
from shadowcode import ShadowCodeError  # noqa: E402


def run(argv: list[str], *, env: dict[str, str] | None = None, timeout: float = 60) -> dict:
    try:
        result = subprocess.run(argv, env=env, text=True, capture_output=True,
                                timeout=timeout, check=False)
        return {"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    except subprocess.TimeoutExpired as error:
        return {"exit": None, "stdout": str(error.stdout or ""), "stderr": f"timeout after {timeout}s"}
    except OSError as error:
        return {"exit": None, "stdout": "", "stderr": str(error)}


def is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) == b"\x7fELF"
    except OSError:
        return False


def host_identity() -> str:
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return "unknown"
    match = re.search(r'^PRETTY_NAME="?([^"\n]*)', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def smoke(deb: Path, pin: shadowcode.Pin, *, verify: bool = True) -> dict:
    report: dict = {"deb": str(deb), "pinned_version": pin.version,
                    "host": host_identity(), "checks": []}

    def check(name: str, ok: bool, detail: str = "") -> None:
        report["checks"].append({"name": name, "state": "PASSED" if ok else "FAILED",
                                 "detail": detail})
        print(f"{'PASSED' if ok else 'FAILED'} {name}" + (f" -- {detail}" if detail else ""))

    if verify:
        try:
            line = shadowcode.verify_pinned_artifact(pin, deb, "deb")
            check("the .deb is the pinned, upstream-signed ShadowCode", True, line)
        except ShadowCodeError as error:
            check("the .deb is the pinned, upstream-signed ShadowCode", False, str(error))
            return report

    env = shadowcode.trusted_env()
    with tempfile.TemporaryDirectory(prefix="shadowcode-smoke-") as temporary:
        root = Path(temporary) / "root"
        home = Path(temporary) / "home"
        home.mkdir()
        extracted = run(["/usr/bin/dpkg-deb", "-x", str(deb), str(root)], env=env, timeout=300)
        check("dpkg-deb -x extracts the package", extracted["exit"] == 0, extracted["stderr"].strip())
        if extracted["exit"] != 0:
            return report
        depends = run(["/usr/bin/dpkg-deb", "-f", str(deb), "Depends"], env=env)["stdout"].strip()
        report["depends"] = depends
        missing_packages = []
        for clause in depends.split(","):
            name = clause.strip().split()[0] if clause.strip() else ""
            if not name:
                continue
            status = run(["/usr/bin/dpkg-query", "-W", "-f=${db:Status-Abbrev}", name], env=env)
            if not status["stdout"].startswith("ii"):
                missing_packages.append(name)
        report["host_missing_depends"] = missing_packages
        print(f"OBSERVED Depends: {depends}")
        print(f"OBSERVED Depends not installed under that name on this host "
              f"({report['host']}): {missing_packages or 'none'}")

        elves = sorted(
            path for base in ("usr/bin", shadowcode.RUNTIME_PREFIX.rstrip("/"))
            for path in (root / base).rglob("*")
            if path.is_file() and not path.is_symlink() and is_elf(path)
        )
        unresolved: dict[str, list[str]] = {}
        clean_env = {k: v for k, v in env.items() if k != "LD_LIBRARY_PATH"}
        for elf in elves:
            result = run(["/usr/bin/ldd", str(elf)], env=clean_env)
            lost = [line.split("=>")[0].strip() for line in result["stdout"].splitlines()
                    if "not found" in line]
            if lost:
                unresolved[elf.relative_to(root).as_posix()] = lost
        report["ldd_unresolved"] = unresolved
        check(f"every shared library resolves on this host ({len(elves)} ELF files)",
              not unresolved and bool(elves),
              "; ".join(f"{k}: {', '.join(v)}" for k, v in unresolved.items()))

        headless = {k: v for k, v in clean_env.items()
                    if k not in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS")}
        headless.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                         "XDG_DATA_HOME": str(home / ".local/share"),
                         "XDG_CACHE_HOME": str(home / ".cache")})
        launcher = run([str(root / shadowcode.LAUNCHER), "--version"], env=headless, timeout=60)
        text = (launcher["stdout"] + launcher["stderr"]).strip()
        report["shadowcode_version_output"] = text
        check("usr/bin/shadowcode --version runs headless and names the pinned version",
              launcher["exit"] == 0 and text == f"ShadowCode {pin.version}",
              f"exit={launcher['exit']} output={text[:200]!r}")

        manifest = json.loads((pin.vendor_dir / "RELEASE-MANIFEST.json").read_text(encoding="utf-8"))
        pinned_runtime = re.search(r"commit=([0-9a-f]{40})", manifest.get("runtime_pin", ""))
        runtime_commit = pinned_runtime.group(1) if pinned_runtime else ""
        report["runtime_commit_pinned"] = runtime_commit
        for name in (shadowcode.LLAMA_SERVER, shadowcode.LLAMA_CLI):
            result = run([str(root / name), "--version"], env=headless, timeout=60)
            text = (result["stdout"] + result["stderr"]).strip()
            report[f"{Path(name).name}_version_output"] = text
            match = re.search(r"commit ([0-9a-f]{7,40})", text)
            check(f"{name} --version runs and reports the pinned llama.cpp commit",
                  result["exit"] == 0 and bool(match) and runtime_commit.startswith(match.group(1)),
                  f"exit={result['exit']} "
                  f"reported={match.group(1) if match else None} pinned={runtime_commit[:12]}")

        entry = root / shadowcode.DESKTOP_FILE
        if Path("/usr/bin/desktop-file-validate").is_file():
            result = run(["/usr/bin/desktop-file-validate", str(entry)], env=env)
            check("the desktop entry validates", result["exit"] == 0,
                  (result["stdout"] + result["stderr"]).strip()[:300])
        else:
            print("SKIPPED desktop entry validation (desktop-file-validate not installed)")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--deb", type=Path, help="default: the pinned build/ copy")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    try:
        pin = shadowcode.load_pin()
    except (ShadowCodeError, OSError) as error:
        print(f"SHADOWCODE_SMOKE_FAILED: {error}", file=sys.stderr)
        return 1
    deb = args.deb or pin.build_deb
    if not deb.is_file():
        print(f"SHADOWCODE_SMOKE_FAILED: {deb} is missing; run python3 tools/fetch_shadowcode.py",
              file=sys.stderr)
        return 1
    report = smoke(deb, pin)
    failed = [c for c in report["checks"] if c["state"] != "PASSED"]
    report["verdict"] = "PASS" if report["checks"] and not failed else "FAIL"
    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nSHADOWCODE_SMOKE_{'PASSED' if report['verdict'] == 'PASS' else 'FAILED'} "
          f"({len(report['checks'])} checks, {len(failed)} failed, host: {report['host']})")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
