#!/bin/sh
# Run INSIDE the guest as the screenshot user: install a fully transparent
# Xcursor theme "sf-qa-blank" in ~/.icons and apply it, so framebuffer captures
# contain no mouse pointer (KWin draws the pointer into the framebuffer on the
# QEMU std-VGA device). Undo: plasma-apply-cursortheme breeze_cursors.
set -eu
dir="$HOME/.icons/sf-qa-blank/cursors"
mkdir -p "$dir"
python3 - "$dir/default" <<'PY'
import struct, sys
w = h = 32
chunk = struct.pack("<IIIIIIIII", 36, 0xfffd0002, 32, 1, w, h, 0, 0, 0) + b"\0" * (w * h * 4)
header = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 1) + struct.pack("<III", 0xfffd0002, 32, 16 + 12)
open(sys.argv[1], "wb").write(header + chunk)
PY
for src in /usr/share/icons/breeze_cursors/cursors/*; do
  n=$(basename "$src"); [ "$n" = default ] || ln -sf default "$dir/$n"
done
printf '[Icon Theme]\nName=sf-qa-blank\nInherits=breeze_cursors\n' > "$HOME/.icons/sf-qa-blank/index.theme"
plasma-apply-cursortheme sf-qa-blank
