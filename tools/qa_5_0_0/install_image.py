#!/usr/bin/env python3
"""Produce a throwaway installed disk by driving Calamares with the harness's
own install automation -- WITHOUT touching any acceptance ledger or manifest.

Uses tools/acceptance/cases.py:case_install unchanged (same AT-SPI page
assertions, same real key events, same post-install boot checks). What differs,
and why:

  * The Context is private: evidence and the transcript go under
    work/qa-5.0.0/images/<name>/, no receipt, no ledger entry. This is image
    PRODUCTION (a 4.1.0 upgrade base, a 5.0.0 screenshot/stress machine), not
    acceptance, and it must never be mistaken for a recorded case.
  * The account is generic and configurable (default demo / "Demo User" /
    host shadowfetch). The password is generated here and written only to
    <image dir>/account.json -- it is a test value for a throwaway VM.
  * --boot-keys sends keys to the live medium's boot menu (e.g. to pick the
    4.1 "Ice" entry: `--boot-keys down ret`), watched by framebuffer.
  * --progress-shots captures the framebuffer every installer poll while the
    installation runs (the Calamares slideshow), into the image directory.

  install_image.py --name base-41-ice --iso shadowfetch-4.1.0-amd64.iso \
      --version 4.1.0 --boot-keys down ret
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import shutil
import string
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))

from acceptance import cases  # noqa: E402
from acceptance.evidence import EvidenceSet, sha256_file  # noqa: E402
from acceptance.vm import Guest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True)
    parser.add_argument("--iso", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--firmware", choices=("bios", "uefi"), default="bios")
    parser.add_argument("--disk-gib", type=int, default=40)
    parser.add_argument("--username", default="demo")
    parser.add_argument("--fullname", default="Demo User")
    parser.add_argument("--hostname", default="shadowfetch")
    parser.add_argument("--boot-keys", nargs="*", default=[])
    parser.add_argument("--boot-keys-delay", type=float, default=0.0,
                        help="seconds after the first non-blank frame before sending boot keys")
    parser.add_argument("--progress-shots", action="store_true")
    parser.add_argument("--memory-mb", type=int, default=8192)
    args = parser.parse_args()

    out = REPO / "work" / "qa-5.0.0" / "images" / args.name
    if out.exists():
        raise SystemExit(f"refusing to reuse {out}")
    out.mkdir(parents=True)

    alphabet = string.ascii_lowercase + string.digits
    password = "".join(secrets.choice(alphabet) for _ in range(14))
    account = {"fullname": args.fullname, "username": args.username,
               "hostname": args.hostname, "password": password}
    (out / "account.json").write_text(json.dumps(account, indent=2) + "\n")
    (out / "account.json").chmod(0o600)
    cases.INSTALL_ACCOUNT.clear()
    cases.INSTALL_ACCOUNT.update(account)

    # -- boot-menu keys: watch the framebuffer, then press ------------------
    original_boot = Guest.boot

    def boot(self, medium, iso=None, *, note=""):
        original_boot(self, medium, iso, note=note)
        if medium != "live" or not args.boot_keys:
            return

        def press():
            shots = out / "bootmenu"
            shots.mkdir(exist_ok=True)
            deadline = time.monotonic() + 120
            index = 0
            while time.monotonic() < deadline:
                time.sleep(1.0)
                try:
                    target = shots / f"frame-{index:03d}.png"
                    self.screenshot(target)
                    index += 1
                    # a boot menu is drawn when the frame is not (almost) uniform
                    data = target.read_bytes()
                    if len(data) > 12000:
                        time.sleep(args.boot_keys_delay)
                        self.sendkeys(args.boot_keys, gap=0.4)
                        time.sleep(1.0)
                        self.screenshot(shots / "after-keys.png")
                        print(f"boot keys {args.boot_keys} sent after frame {index}", flush=True)
                        return
                except Exception as error:  # noqa: BLE001
                    print(f"boot-key watcher: {error}", flush=True)
            print("boot-key watcher: no menu seen", flush=True)

        threading.Thread(target=press, daemon=True).start()

    Guest.boot = boot

    # -- slideshow captures during the installation --------------------------
    if args.progress_shots:
        original_page = cases._page
        state = {"n": 0}

        def page(ctx, machine, bus, timeout=240):
            result = original_page(ctx, machine, bus, timeout)
            if any(node["role"] == "progress" for node in result):
                try:
                    machine.screenshot(out / "slideshow" / f"slide-{state['n']:03d}.png")
                    state["n"] += 1
                except Exception as error:  # noqa: BLE001
                    print(f"slideshow capture: {error}", flush=True)
            return result

        cases._page = page

    evidence = EvidenceSet(REPO, out / "evidence")
    ctx = cases.Context(
        name="install", repo_root=REPO, run_dir=out, evidence=evidence,
        artifact={"path": str(args.iso.resolve()), "name": args.iso.name,
                  "sha256": sha256_file(args.iso)},
        options={"version": args.version, "firmware": args.firmware,
                 "disk_gib": args.disk_gib, "boot_timeout": 900,
                 "desktop_settle": 120, "installer_settle": 180,
                 "install_timeout": 5400},
        work_root=out,
    )
    verdict = "ERROR"
    try:
        cases.case_install(ctx)
        failed = [c for c in ctx.checks if c["state"] == "FAILED"]
        verdict = "FAIL" if failed else ("OK" if ctx.checks else "NOTHING-CHECKED")
    except cases.Blocked as blocked:
        verdict = f"BLOCKED: {blocked}"
    except BaseException as error:  # noqa: BLE001
        import traceback
        verdict = f"ERROR: {type(error).__name__}: {error}"
        traceback.print_exc()
    finally:
        for machine in ctx.guests:
            if machine.is_running():
                try:
                    machine.shutdown(timeout=60)
                except Exception:  # noqa: BLE001
                    machine.kill_hard()
    (out / "transcript.log").write_text("\n".join(ctx.transcript) + "\n")
    (out / "result.json").write_text(json.dumps({
        "verdict": verdict, "checks": ctx.checks, "observations": ctx.observations,
        "iso": ctx.artifact, "version": args.version, "firmware": args.firmware,
        "account": {k: v for k, v in account.items() if k != "password"},
        "disk": ctx.observations.get("installed_disk"),
    }, indent=2, default=str) + "\n")
    disk = ctx.observations.get("installed_disk")
    if disk and Path(disk).is_file():
        target = out / "disk.qcow2"
        shutil.move(disk, target)
        variables = Path(disk).parent / "OVMF_VARS_4M.fd"
        if variables.is_file():
            shutil.copyfile(variables, out / "OVMF_VARS_4M.fd")
        print(f"installed disk: {target}")
    print(f"VERDICT {verdict}")
    return 0 if verdict == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
