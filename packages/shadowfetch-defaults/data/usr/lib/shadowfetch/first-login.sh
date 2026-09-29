#!/bin/sh
# Shadowfetch Linux "Umbra" — first-login setup.
# Applies wallpaper + global theme + color scheme, then builds the premium
# floating panel / curated creative dock via plasmashell scripting. Self-disables.
set -u
mkdir -p "$HOME/.cache"
LOG="$HOME/.cache/shadowfetch-first-login.log"
exec >>"$LOG" 2>&1
echo "=== shadowfetch first-login $(date) ==="

# 1) Appearance: the ShadowCode look -- wallpaper + global theme + color scheme
plasma-apply-wallpaperimage /usr/share/backgrounds/shadowfetch/shadowcode-4k.jpg || true
plasma-apply-lookandfeel -a org.shadowfetch.dark || true
plasma-apply-colorscheme ShadowfetchDark || true

# 2) Resolve .desktop IDs that ACTUALLY exist (dock never shows a broken icon)
APPDIRS="/usr/share/applications $HOME/.local/share/applications /var/lib/flatpak/exports/share/applications"
pick() {
  for id in "$@"; do
    for d in $APPDIRS; do
      if [ -f "$d/$id" ]; then printf '%s' "$id"; return 0; fi
    done
  done
  return 1
}

FILES=$(pick org.kde.dolphin.desktop)
BROWSER=$(pick firefox-esr.desktop firefox.desktop org.mozilla.firefox.desktop chromium.desktop)
TERMA=$(pick org.kde.konsole.desktop)
KRITA=$(pick org.kde.krita.desktop krita.desktop)
GIMP=$(pick org.gimp.GIMP.desktop gimp.desktop)
CALIBRE=$(pick calibre-gui.desktop com.calibre_ebook.calibre.desktop)
SHOTCUT=$(pick org.shotcut.Shotcut.desktop shotcut.desktop)
SETTINGS=$(pick systemsettings.desktop org.kde.systemsettings.desktop)
DISCOVER=$(pick org.kde.discover.desktop plasma-discover.desktop)

# Dock (icon task manager pins): files, browser, terminal, then the creative cluster
DOCK=""
for id in "$FILES" "$BROWSER" "$TERMA" "$KRITA" "$GIMP" "$CALIBRE" "$SHOTCUT"; do
  [ -n "$id" ] && DOCK="${DOCK}${DOCK:+,}applications:$id"
done

# Kickoff favorites: creative cluster first, then essentials + store
FAV=""
for id in "$KRITA" "$GIMP" "$SHOTCUT" "$CALIBRE" "$BROWSER" "$FILES" "$TERMA" "$SETTINGS" "$DISCOVER"; do
  [ -n "$id" ] && FAV="${FAV}${FAV:+,}applications:$id"
done

# Launcher icon: branded if present, else a sensible default
ICON="start-here-kde-symbolic"
for n in shadowfetch start-here-shadowfetch distributor-logo-shadowfetch; do
  if [ -f "/usr/share/icons/hicolor/scalable/apps/$n.svg" ]; then ICON="$n"; break; fi
done

# 3) Build the layout script with resolved values
SRC=/usr/share/shadowfetch/desktop-layout.js
OUT="$HOME/.cache/sf-desktop-layout.js"
sed -e "s|@@LAUNCHERS@@|$DOCK|g" \
    -e "s|@@FAVORITES@@|$FAV|g" \
    -e "s|@@LAUNCHER_ICON@@|$ICON|g" \
    "$SRC" > "$OUT"
echo "dock=$DOCK"
echo "fav=$FAV"
echo "icon=$ICON"

# 4) Wait for plasmashell on the session bus, then apply the panel layout
apply_layout() {
  SCRIPT=$(cat "$OUT")
  for q in qdbus6 qdbus-qt6 qdbus; do
    if command -v "$q" >/dev/null 2>&1; then
      "$q" org.kde.plasmashell /PlasmaShell org.kde.PlasmaShell.evaluateScript "$SCRIPT" && return 0
    fi
  done
  dbus-send --session --print-reply --dest=org.kde.plasmashell \
    /PlasmaShell org.kde.PlasmaShell.evaluateScript "string:$SCRIPT" && return 0
  return 1
}

i=0
while [ $i -lt 60 ]; do
  if dbus-send --session --dest=org.freedesktop.DBus --type=method_call --print-reply \
       /org/freedesktop/DBus org.freedesktop.DBus.NameHasOwner string:org.kde.plasmashell 2>/dev/null \
       | grep -q "boolean true"; then
    sleep 2
    if apply_layout; then echo "layout applied"; break; fi
  fi
  i=$((i+1)); sleep 1
done

# 5) Self-disable
rm -f "$HOME/.config/autostart/shadowfetch-first-login.desktop"
echo "=== shadowfetch first-login done $(date) ==="
