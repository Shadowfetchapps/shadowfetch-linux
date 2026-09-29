#!/usr/bin/env bash
# Post-upgrade checks the harness's `upgrade` case does not make, run on a
# copy-on-write clone of the disk that case upgraded (4.1.0 Ice -> 5.0.0):
#   * shadowfetch-agent-network resolves to offline, from the system file the
#     5.0 postinst wrote out of /etc/shadowfetch/element (which must be gone);
#   * the per-user look migration ran at a real login and moved the desktop
#     to ShadowCode (colour scheme, look-and-feel, wallpaper, accent, Konsole);
#   * no Fire/Ice asset remains installed;
#   * ShadowCode (shadow-code) is installed and answers --version.
# Usage: verify_upgrade.sh UPGRADED_DISK.qcow2
# Output: work/qa-5.0.0/upgrade/verify/{verify.log,checks.txt,*.png}
set -uo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
vm="$root/tools/qa_5_0_0/sfvm.py"
disk="${1:?upgraded disk}"
user="${QA_USER:-demo}"
out="$root/work/qa-5.0.0/upgrade/verify"
name=upg50-verify
mkdir -p "$out"
exec > >(tee -a "$out/verify.log") 2>&1
gx() { "$vm" exec "$name" "$1" --timeout "${2:-300}"; }
ux() { "$vm" uexec "$name" "$user" "$1" --timeout "${2:-300}"; }
pass=0; fail=0
check() { # name, command (0 = pass)
  if eval "$2" >/dev/null 2>&1; then echo "PASSED $1" | tee -a "$out/checks.txt"; pass=$((pass+1));
  else echo "FAILED $1" | tee -a "$out/checks.txt"; fail=$((fail+1)); fi
}
: > "$out/checks.txt"
"$vm" clone "$name" --base "$disk"
"$vm" start "$name" --medium installed
"$vm" wait "$name"
for _ in $(seq 60); do gx "pgrep -u $user -x plasmashell >/dev/null" 30 && break; sleep 5; done
sleep 90   # login autostarts (look-migrate) and Plasma settle
"$vm" shot "$name" "$out/50-after-upgrade-desktop.png"

gx 'cat /usr/share/shadowfetch/version; grep VERSION_ID /etc/os-release' > "$out/version.txt"
gx 'ls -la /etc/shadowfetch/; cat /etc/shadowfetch/agent-network 2>&1; test -e /etc/shadowfetch/element && echo ELEMENT_FILE_PRESENT || echo element-file-absent' > "$out/etc-shadowfetch.txt"
ux 'shadowfetch-agent-network; shadowfetch-agent-network status' > "$out/agent-network.txt" 2>&1
ux 'cd ~/.config; echo "--- stamp"; cat shadowfetch/.look-migrated 2>&1; ls -la shadowfetch; echo "--- kdeglobals"; grep -E "^(ColorScheme|LookAndFeelPackage|AccentColor)=" kdeglobals; echo "--- wallpaper"; grep -A3 "Wallpaper\]\[org.kde.image\]\[General\]" plasma-org.kde.plasma.desktop-appletsrc; echo "--- lock"; grep -A3 "org.kde.image" kscreenlockerrc 2>&1; echo "--- splash"; cat ksplashrc 2>&1; echo "--- konsole"; grep -H ColorScheme ~/.local/share/konsole/*.profile 2>&1; echo "--- journal"; journalctl --user -b --no-pager 2>/dev/null | grep -i look-migrate' > "$out/look.txt" 2>&1
gx 'for p in /usr/share/color-schemes/ShadowfetchIce.colors /usr/share/konsole/ShadowfetchGlacier.colorscheme /usr/share/plasma/look-and-feel/org.shadowfetch.ice /usr/share/wallpapers/UmbraFire /usr/share/wallpapers/UmbraIce /usr/share/wallpapers/UmbraFrost /usr/share/wallpapers/UmbraDrift /usr/share/wallpapers/UmbraGold /usr/share/backgrounds/shadowfetch/umbra-4k.jpg /usr/share/backgrounds/shadowfetch/umbra-ice-4k.jpg /usr/bin/shadowfetch-element /usr/lib/shadowfetch/element-boot.sh /usr/lib/shadowfetch/element-session.sh /usr/lib/systemd/system/shadowfetch-element-boot.service /etc/xdg/autostart/shadowfetch-element-session.desktop; do test -e "$p" && echo "PRESENT $p"; done; echo "--- search"; find /usr/share /etc -xdev \( -iname "*ice*" -a -ipath "*shadowfetch*" -o -iname "*glacier*" -o -iname "umbra*ice*" -o -iname "UmbraFire*" \) 2>/dev/null | grep -v -i -E "device|service|notice|slice|office|voice|price|choice" | head -50; echo "--- enable links"; ls -la /etc/systemd/system/*/ 2>/dev/null | grep -i element' > "$out/retired-assets.txt" 2>&1
gx 'dpkg-query -W -f="\${Package}\t\${Version}\t\${db:Status-Abbrev}\n" shadow-code "shadowfetch-*"; dpkg --audit; systemctl --failed --no-legend' > "$out/packages.txt" 2>&1
ux 'shadowcode --version' > "$out/shadowcode-version.txt" 2>&1

check "release is 5.0.0" "grep -qx 5.0.0 <(head -1 '$out/version.txt')"
check "/etc/shadowfetch/element removed by the upgrade" "grep -q element-file-absent '$out/etc-shadowfetch.txt'"
check "system agent-network file says offline" "grep -qx offline <(sed -n '/^offline\$\|^online\$/p' '$out/etc-shadowfetch.txt')"
check "shadowfetch-agent-network resolves to offline for the user" "head -1 '$out/agent-network.txt' | grep -qx offline"
check "look migration stamp written (shadowcode)" "grep -A1 -- '--- stamp' '$out/look.txt' | grep -qx shadowcode"
check "colour scheme is ShadowfetchDark" "grep -qx ColorScheme=ShadowfetchDark '$out/look.txt'"
check "look-and-feel is not the removed Ice package" "! grep -q 'LookAndFeelPackage=org.shadowfetch.ice' '$out/look.txt'"
check "retired Ice accent is gone" "! grep -q 'AccentColor=74,162,216' '$out/look.txt'"
check "wallpaper is the ShadowCode wallpaper" "grep -q 'shadowcode-4k.jpg' '$out/look.txt'"
check "no wallpaper names a removed Umbra asset" "! grep -E 'Image=.*(UmbraFire|UmbraIce|UmbraFrost|UmbraDrift|UmbraGold|umbra-ice-4k|umbra-4k)' '$out/look.txt'"
check "no Konsole profile names ShadowfetchGlacier" "! grep -q 'ColorScheme=ShadowfetchGlacier' '$out/look.txt'"
check "no retired Fire/Ice asset remains installed" "! grep -q '^PRESENT' '$out/retired-assets.txt'"
check "shadow-code is installed" "grep -qP '^shadow-code\t0\.34\.2\tii' '$out/packages.txt'"
check "shadowcode --version answers 0.34.2" "grep -qx 'ShadowCode 0.34.2' '$out/shadowcode-version.txt'"
"$vm" stop "$name"
echo "verify_upgrade: $pass passed, $fail failed" | tee -a "$out/checks.txt"
exit $(( fail > 0 ))
