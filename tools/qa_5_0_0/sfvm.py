#!/usr/bin/env python3
"""Interactive QEMU/KVM control for 5.0.0 QA and website screenshots.

A small, persistent companion to tools/acceptance/vm.py. The acceptance
harness owns its QEMU process for the length of one case; screenshots and the
upgrade/stress preparation need a machine that stays up between commands and
can be clicked. This tool starts a daemonised QEMU with the SAME display device
the harness uses (VGA with a 1920x1080 EDID) plus a QMP socket, and speaks the
guest-agent protocol through the harness's own GuestAgent class.

Nothing here reads or copies anything from the host home into a guest. Every
disk is a fresh qcow2 or a copy-on-write overlay; networking is QEMU user-mode
(no host bridge, no host name, no port forwards).

  sfvm.py create NAME --disk-gib 40
  sfvm.py clone NAME --base DISK.qcow2 [--vars-from OVMF_VARS.fd]
  sfvm.py start NAME --medium live --iso X.iso [--firmware uefi] [--mem 8192]
  sfvm.py wait NAME [--timeout 900]
  sfvm.py exec NAME 'command' [--timeout 300]
  sfvm.py uexec NAME USER 'command'          # inside USER's graphical session
  sfvm.py shot NAME out.png
  sfvm.py keys NAME ctrl-alt-t ret ...
  sfvm.py type NAME 'text'
  sfvm.py move NAME X Y | click NAME X Y [--right] [--double]
  sfvm.py push NAME LOCAL REMOTE | pull NAME REMOTE LOCAL
  sfvm.py stop NAME | kill NAME | status NAME
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))
from acceptance.vm import GuestAgent, GuestError, ppm_to_png  # noqa: E402

VM_ROOT = REPO / "work" / "qa-5.0.0" / "vm"
QEMU = "/usr/bin/qemu-system-x86_64"
QEMU_IMG = "/usr/bin/qemu-img"
OVMF_CODE = Path("/usr/share/OVMF/OVMF_CODE_4M.fd")
OVMF_VARS = Path("/usr/share/OVMF/OVMF_VARS_4M.fd")


def vm_dir(name: str) -> Path:
    if not name.replace("-", "").isalnum():
        raise SystemExit(f"invalid VM name {name!r}")
    return VM_ROOT / name


def sock_dir(name: str) -> Path:
    path = Path("/tmp") / f"sfq-{os.getuid()}-{name}"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def meta(name: str) -> dict:
    path = vm_dir(name) / "meta.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def save_meta(name: str, data: dict) -> None:
    (vm_dir(name) / "meta.json").write_text(json.dumps(data, indent=2))


def agent(name: str) -> GuestAgent:
    return GuestAgent(sock_dir(name) / "qga.sock")


# --- QMP --------------------------------------------------------------------


class QMP:
    def __init__(self, name: str, timeout: float = 30.0) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(sock_dir(name) / "qmp.sock"))
        self.buf = b""
        self._read()  # greeting
        self.cmd("qmp_capabilities")

    def _read(self) -> dict:
        while b"\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise GuestError("QMP closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def cmd(self, execute: str, **arguments):
        request = {"execute": execute}
        if arguments:
            request["arguments"] = arguments
        self.sock.sendall(json.dumps(request).encode() + b"\n")
        while True:
            reply = self._read()
            if "event" in reply:
                continue
            if "error" in reply:
                raise GuestError(f"QMP {execute}: {reply['error']}")
            return reply.get("return")

    def close(self) -> None:
        self.sock.close()


def qmp(name: str, execute: str, **arguments):
    connection = QMP(name)
    try:
        return connection.cmd(execute, **arguments)
    finally:
        connection.close()


# --- lifecycle ----------------------------------------------------------------


def running_pid(name: str) -> int | None:
    pidfile = sock_dir(name) / "qemu.pid"
    if not pidfile.is_file():
        return None
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError, PermissionError):
        return None
    cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    if str(vm_dir(name) / "disk.qcow2").encode() not in b" ".join(cmdline):
        return None
    return pid


def cmd_create(args) -> None:
    directory = vm_dir(args.name)
    directory.mkdir(parents=True, exist_ok=True)
    disk = directory / "disk.qcow2"
    if disk.exists():
        raise SystemExit(f"refusing to replace {disk}")
    subprocess.run([QEMU_IMG, "create", "-f", "qcow2", str(disk), f"{args.disk_gib}G"],
                   check=True, stdout=subprocess.DEVNULL)
    save_meta(args.name, {"created": time.time(), "disk_gib": args.disk_gib})
    print(disk)


def cmd_clone(args) -> None:
    directory = vm_dir(args.name)
    directory.mkdir(parents=True, exist_ok=True)
    disk = directory / "disk.qcow2"
    if disk.exists():
        raise SystemExit(f"refusing to replace {disk}")
    base = Path(args.base).resolve()
    subprocess.run([QEMU_IMG, "create", "-f", "qcow2", "-F", "qcow2", "-b", str(base),
                    str(disk)], check=True, stdout=subprocess.DEVNULL)
    if args.vars_from:
        shutil.copyfile(args.vars_from, directory / "OVMF_VARS_4M.fd")
    save_meta(args.name, {"created": time.time(), "base": str(base)})
    print(disk)


def cmd_start(args) -> None:
    name = args.name
    if running_pid(name):
        raise SystemExit(f"{name} is already running")
    directory = vm_dir(name)
    disk = directory / "disk.qcow2"
    if not disk.is_file():
        raise SystemExit(f"no disk: {disk}")
    sockets = sock_dir(name)
    for stale in ("qga.sock", "qmp.sock", "hmp.sock", "qemu.pid"):
        (sockets / stale).unlink(missing_ok=True)
    argv = [
        QEMU, "-name", f"sf-qa-{name}", "-enable-kvm", "-machine", "q35,accel=kvm",
        "-cpu", "host", "-smp", str(args.cpus), "-m", str(args.mem),
        "-drive", f"file={disk},format=qcow2,if=virtio,cache=writeback,discard=unmap",
        "-device", f"VGA,edid=on,xres={args.xres},yres={args.yres},vgamem_mb=64",
        "-device", "qemu-xhci", "-device", "usb-tablet", "-device", "usb-kbd",
        "-display", "none",
        "-device", "virtio-serial-pci",
        "-chardev", f"socket,path={sockets}/qga.sock,server=on,wait=off,id=qga0",
        "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
        "-qmp", f"unix:{sockets}/qmp.sock,server=on,wait=off",
        "-monitor", f"unix:{sockets}/hmp.sock,server=on,wait=off",
        "-serial", f"file:{directory}/serial.log",
        "-netdev", "user,id=net0", "-device", "virtio-net-pci,netdev=net0",
        "-rtc", "base=utc",
        "-daemonize", "-pidfile", f"{sockets}/qemu.pid",
    ]
    if args.firmware == "uefi":
        variables = directory / "OVMF_VARS_4M.fd"
        if not variables.exists():
            shutil.copyfile(OVMF_VARS, variables)
        argv += ["-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
                 "-drive", f"if=pflash,format=raw,file={variables}"]
    if args.medium == "live":
        iso = Path(args.iso).resolve()
        argv += ["-drive", f"file={iso},media=cdrom,readonly=on", "-boot", "order=d"]
    else:
        argv += ["-boot", "order=c"]
    with (directory / "qemu.log").open("ab") as log:
        log.write(f"\n=== start {time.strftime('%FT%TZ', time.gmtime())} {args.medium}\n".encode())
        log.flush()
        subprocess.run(argv, check=True, stdout=log, stderr=log,
                       env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C.UTF-8"})
    data = meta(name)
    data.update({"firmware": args.firmware, "medium": args.medium, "iso": args.iso,
                 "started": time.time()})
    save_meta(name, data)
    print(f"started {name} pid={running_pid(name)}")


def cmd_wait(args) -> None:
    deadline = time.monotonic() + args.timeout
    started = time.monotonic()
    while time.monotonic() < deadline:
        if not running_pid(args.name):
            raise SystemExit(f"{args.name} is not running")
        if (sock_dir(args.name) / "qga.sock").exists() and agent(args.name).ping():
            print(f"agent up after {time.monotonic() - started:.0f}s")
            return
        time.sleep(3)
    raise SystemExit("guest agent did not answer")


def cmd_exec(args) -> None:
    result = agent(args.name).execute(args.command, timeout=args.timeout)
    sys.stdout.write(result["stdout"])
    sys.stderr.write(result["stderr"])
    raise SystemExit(result["exitcode"] if isinstance(result["exitcode"], int) else 1)


def session_command(user: str, command: str) -> str:
    """Wrap a command so it runs as USER inside that user's graphical session."""
    probe = (
        f"u={shlex.quote(user)}; uid=$(id -u $u); home=$(getent passwd $u | cut -d: -f6); "
        "w=$(ls /run/user/$uid | grep -m1 '^wayland-[0-9]$' || echo wayland-0); "
        "exec /usr/sbin/runuser -u $u -- /usr/bin/env HOME=$home USER=$u LOGNAME=$u "
        "XDG_RUNTIME_DIR=/run/user/$uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus "
        "WAYLAND_DISPLAY=$w QT_QPA_PLATFORM=wayland XDG_SESSION_TYPE=wayland "
        "XDG_CURRENT_DESKTOP=KDE KDE_FULL_SESSION=true "
        "PATH=$home/.local/bin:/usr/local/bin:/usr/bin:/bin "
        f"/bin/sh -c {shlex.quote(command)}"
    )
    return probe


def cmd_uexec(args) -> None:
    result = agent(args.name).execute(session_command(args.user, args.command),
                                      timeout=args.timeout)
    sys.stdout.write(result["stdout"])
    sys.stderr.write(result["stderr"])
    raise SystemExit(result["exitcode"] if isinstance(result["exitcode"], int) else 1)


def screenshot(name: str, output: Path) -> tuple[int, int]:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f".{output.stem}.{int(time.time()*1000)}.ppm")
    qmp(name, "screendump", filename=str(tmp))
    deadline = time.monotonic() + 15
    previous = None
    while time.monotonic() < deadline:
        try:
            size = tmp.stat().st_size
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        if size > 20 and size == previous:
            try:
                width, height = ppm_to_png(tmp, output)
                tmp.unlink(missing_ok=True)
                return width, height
            except GuestError:
                pass
        previous = size
        time.sleep(0.1)
    raise SystemExit(f"screendump did not complete: {tmp}")


def cmd_shot(args) -> None:
    width, height = screenshot(args.name, Path(args.output))
    print(f"{args.output} {width}x{height}")


def cmd_keys(args) -> None:
    for combo in args.keys:
        keys = [{"type": "qcode", "data": part} for part in combo.split("-")]
        qmp(args.name, "send-key", keys=keys, **{"hold-time": 80})
        time.sleep(args.gap)


KEYMAP = {" ": "spc", "-": "minus", ".": "dot", "_": "shift-minus", "/": "slash",
          "@": "shift-2", ":": "shift-semicolon", "=": "equal", ",": "comma",
          "'": "apostrophe", '"': "shift-apostrophe", "|": "shift-backslash",
          "\n": "ret", ";": "semicolon", "(": "shift-9", ")": "shift-0",
          "$": "shift-4", "&": "shift-7", ">": "shift-dot", "<": "shift-comma",
          "~": "shift-grave_accent", "*": "shift-8", "+": "shift-equal",
          "!": "shift-1", "?": "shift-slash", "#": "shift-3", "%": "shift-5"}


def cmd_type(args) -> None:
    combos = []
    for char in args.text:
        if char.islower() or char.isdigit():
            combos.append(char)
        elif char.isupper():
            combos.append("shift-" + char.lower())
        elif char in KEYMAP:
            combos.append(KEYMAP[char])
        else:
            raise SystemExit(f"no key for {char!r}")
    connection = QMP(args.name)
    try:
        for combo in combos:
            keys = [{"type": "qcode", "data": part} for part in combo.split("-")]
            connection.cmd("send-key", keys=keys, **{"hold-time": 60})
            time.sleep(args.gap)
    finally:
        connection.close()


def _abs(value: int, extent: int) -> int:
    return max(0, min(0x7FFF, round(value * 0x7FFF / max(1, extent - 1))))


def cmd_move(args) -> None:
    # Nudge first: an absolute event equal to the device's last position is not
    # reported again, so a pointer the compositor re-centred would not move.
    qmp(args.name, "input-send-event", events=[
        {"type": "abs", "data": {"axis": "x", "value": _abs(max(0, args.x - 3), args.width)}},
        {"type": "abs", "data": {"axis": "y", "value": _abs(max(0, args.y - 3), args.height)}},
    ])
    time.sleep(0.05)
    qmp(args.name, "input-send-event", events=[
        {"type": "abs", "data": {"axis": "x", "value": _abs(args.x, args.width)}},
        {"type": "abs", "data": {"axis": "y", "value": _abs(args.y, args.height)}},
    ])


def cmd_click(args) -> None:
    cmd_move(args)
    time.sleep(0.15)
    button = "right" if args.right else "left"
    for _ in range(2 if args.double else 1):
        for down in (True, False):
            qmp(args.name, "input-send-event", events=[
                {"type": "btn", "data": {"down": down, "button": button}}])
            time.sleep(0.05)
        time.sleep(0.08)


def cmd_scroll(args) -> None:
    cmd_move(args)
    button = "wheel-down" if args.clicks > 0 else "wheel-up"
    for _ in range(abs(args.clicks)):
        for down in (True, False):
            qmp(args.name, "input-send-event", events=[
                {"type": "btn", "data": {"down": down, "button": button}}])
        time.sleep(0.05)


def _file_rpc(name: str, calls):
    guest = agent(name)
    connection = guest._connect(30.0)
    buffer = bytearray()
    try:
        guest._sync(connection, buffer)
        return calls(lambda execute, arguments=None: guest._rpc(connection, buffer, execute, arguments))
    finally:
        connection.close()


def cmd_push(args) -> None:
    data = Path(args.local).read_bytes()

    def calls(rpc):
        handle = rpc("guest-file-open", {"path": args.remote, "mode": "wb"})
        for offset in range(0, len(data), 48 * 1024):
            rpc("guest-file-write", {"handle": handle,
                                     "buf-b64": base64.b64encode(data[offset:offset + 48 * 1024]).decode()})
        rpc("guest-file-close", {"handle": handle})
    _file_rpc(args.name, calls)
    if args.mode:
        agent(args.name).execute(f"chmod {args.mode} {shlex.quote(args.remote)}")
    print(f"pushed {len(data)} bytes -> {args.remote}")


def cmd_pull(args) -> None:
    chunks = []

    def calls(rpc):
        handle = rpc("guest-file-open", {"path": args.remote, "mode": "rb"})
        while True:
            part = rpc("guest-file-read", {"handle": handle, "count": 1024 * 1024})
            chunks.append(base64.b64decode(part.get("buf-b64", "")))
            if part.get("eof"):
                break
        rpc("guest-file-close", {"handle": handle})
    _file_rpc(args.name, calls)
    Path(args.local).parent.mkdir(parents=True, exist_ok=True)
    Path(args.local).write_bytes(b"".join(chunks))
    print(f"pulled {sum(map(len, chunks))} bytes -> {args.local}")


def cmd_stop(args) -> None:
    pid = running_pid(args.name)
    if not pid:
        print("not running")
        return
    try:
        agent(args.name).execute_detached("/usr/bin/systemctl poweroff")
    except (GuestError, OSError):
        pass
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if not running_pid(args.name):
            print("powered off")
            return
        time.sleep(2)
    try:
        qmp(args.name, "quit")
    except (GuestError, OSError):
        os.kill(pid, 9)
    print("quit")


def cmd_kill(args) -> None:
    pid = running_pid(args.name)
    if pid:
        os.kill(pid, 9)
    print("killed" if pid else "not running")


def cmd_status(args) -> None:
    pid = running_pid(args.name)
    print(json.dumps({"name": args.name, "pid": pid, "meta": meta(args.name),
                      "agent": bool(pid) and agent(args.name).ping()}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create"); p.add_argument("name"); p.add_argument("--disk-gib", type=int, default=40)
    p.set_defaults(func=cmd_create)
    p = sub.add_parser("clone"); p.add_argument("name"); p.add_argument("--base", required=True)
    p.add_argument("--vars-from"); p.set_defaults(func=cmd_clone)
    p = sub.add_parser("start"); p.add_argument("name")
    p.add_argument("--medium", choices=("live", "installed"), required=True)
    p.add_argument("--iso"); p.add_argument("--firmware", choices=("bios", "uefi"), default="bios")
    p.add_argument("--mem", type=int, default=8192); p.add_argument("--cpus", type=int, default=6)
    p.add_argument("--xres", type=int, default=1920); p.add_argument("--yres", type=int, default=1080)
    p.set_defaults(func=cmd_start)
    p = sub.add_parser("wait"); p.add_argument("name"); p.add_argument("--timeout", type=float, default=900)
    p.set_defaults(func=cmd_wait)
    p = sub.add_parser("exec"); p.add_argument("name"); p.add_argument("command")
    p.add_argument("--timeout", type=float, default=300); p.set_defaults(func=cmd_exec)
    p = sub.add_parser("uexec"); p.add_argument("name"); p.add_argument("user"); p.add_argument("command")
    p.add_argument("--timeout", type=float, default=300); p.set_defaults(func=cmd_uexec)
    p = sub.add_parser("shot"); p.add_argument("name"); p.add_argument("output"); p.set_defaults(func=cmd_shot)
    p = sub.add_parser("keys"); p.add_argument("name"); p.add_argument("keys", nargs="+")
    p.add_argument("--gap", type=float, default=0.15); p.set_defaults(func=cmd_keys)
    p = sub.add_parser("type"); p.add_argument("name"); p.add_argument("text")
    p.add_argument("--gap", type=float, default=0.12); p.set_defaults(func=cmd_type)
    for verb, func in (("move", cmd_move), ("click", cmd_click)):
        p = sub.add_parser(verb); p.add_argument("name"); p.add_argument("x", type=int); p.add_argument("y", type=int)
        p.add_argument("--width", type=int, default=1920); p.add_argument("--height", type=int, default=1080)
        if verb == "click":
            p.add_argument("--right", action="store_true"); p.add_argument("--double", action="store_true")
        p.set_defaults(func=func)
    p = sub.add_parser("scroll"); p.add_argument("name"); p.add_argument("x", type=int); p.add_argument("y", type=int)
    p.add_argument("clicks", type=int); p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080); p.set_defaults(func=cmd_scroll)
    p = sub.add_parser("push"); p.add_argument("name"); p.add_argument("local"); p.add_argument("remote")
    p.add_argument("--mode"); p.set_defaults(func=cmd_push)
    p = sub.add_parser("pull"); p.add_argument("name"); p.add_argument("remote"); p.add_argument("local")
    p.set_defaults(func=cmd_pull)
    p = sub.add_parser("stop"); p.add_argument("name"); p.add_argument("--timeout", type=float, default=180)
    p.set_defaults(func=cmd_stop)
    p = sub.add_parser("kill"); p.add_argument("name"); p.set_defaults(func=cmd_kill)
    p = sub.add_parser("status"); p.add_argument("name"); p.set_defaults(func=cmd_status)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
