#!/bin/sh
# Shadowfetch Linux 5.0 -- move this user's desktop onto the ShadowCode look.
#
# 3.1-4.1 shipped two looks, Fire and Ice. 5.0 ships one, ShadowCode, and the
# Ice assets and the retired wallpapers are no longer installed:
#
#   colour scheme   ShadowfetchIce                 (ShadowfetchDark stays)
#   Konsole scheme  ShadowfetchGlacier             (ShadowfetchUmbra stays)
#   look-and-feel   org.shadowfetch.ice            (org.shadowfetch.dark stays)
#   wallpapers      UmbraFire UmbraIce UmbraFrost UmbraDrift UmbraGold
#                   UmbraEmblem UmbraVault, backgrounds/shadowfetch/umbra-4k.jpg
#                   and umbra-ice-4k.jpg
#
# A setting that still names one of those renders as a KDE fallback (Breeze
# colours, a blank wallpaper, Konsole's default palette). This script repoints
# ONLY such settings at the ShadowCode equivalent. Anything that names an asset
# which still exists -- a custom wallpaper, another colour scheme, a user's own
# Konsole scheme -- is a choice, and is left exactly as it is. The same rule
# gives Konsole a default profile only when none is set (step 5b).
#
# The one exception is re-applying ShadowfetchDark when it is ALREADY the
# user's scheme: KDE copies a scheme's colours into kdeglobals when it is
# applied, so without a re-apply a 4.1 Fire desktop keeps the 4.1 gold baked
# in. The scheme name the user chose does not change.
#
# Runs once per user from XDG autostart (shadowfetch-look-migrate.desktop). The
# stamp is written only when every step it attempted succeeded, so a session
# where plasmashell was not ready yet retries at the next login; every step is
# conditional on a removed asset, so a retry cannot undo a later choice.
set -u

CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
STAMP="$CONFIG/shadowfetch/.look-migrated"
OLD_STAMP="$CONFIG/shadowfetch/.element-applied"

SCHEME=ShadowfetchDark
KONSOLE_SCHEME=ShadowfetchUmbra
LOOK=org.shadowfetch.dark
WALLPAPER=/usr/share/backgrounds/shadowfetch/shadowcode-4k.jpg
ACCENT=242,179,61
# The accents the retired looks wrote: Ice azure #4AA2D8 and Fire's 4.1
# default #D8A24A. Either one overrides the scheme's accent, so a migrated
# desktop would otherwise keep a 4.1 accent over the ShadowCode scheme.
RETIRED_ACCENTS="74,162,216 216,162,74"

[ -e "$STAMP" ] && exit 0

KDEGLOBALS="$CONFIG/kdeglobals"
APPLETSRC="$CONFIG/plasma-org.kde.plasma.desktop-appletsrc"
LOCKRC="$CONFIG/kscreenlockerrc"
SPLASHRC="$CONFIG/ksplashrc"

failed=0
log() { printf 'shadowfetch-look-migrate: %s\n' "$*" >&2; }

# ini_get FILE '[Group][Sub]' KEY -> the key's last value in that exact group.
ini_get() {
  [ -f "$1" ] || return 0
  awk -v g="$2" -v k="$3" '
    BEGIN { n = length(k) + 1 }
    /^\[/ { cur = $0; next }
    cur == g && substr($0, 1, n) == k "=" { v = substr($0, n + 1) }
    END { if (v != "") print v }
  ' "$1"
}

# Every Image= of an image-plugin wallpaper, one per desktop, from appletsrc.
wallpaper_images() {
  [ -f "$APPLETSRC" ] || return 0
  awk '
    /^\[/ { inwall = ($0 ~ /\[Wallpaper\]\[org\.kde\.image\]\[General\]$/); next }
    inwall && /^Image=/ { print substr($0, 7) }
  ' "$APPLETSRC"
}

removed_wallpaper() {
  case "$1" in
    */wallpapers/UmbraFire|*/wallpapers/UmbraFire/*) return 0 ;;
    */wallpapers/UmbraIce|*/wallpapers/UmbraIce/*) return 0 ;;
    */wallpapers/UmbraFrost|*/wallpapers/UmbraFrost/*) return 0 ;;
    */wallpapers/UmbraDrift|*/wallpapers/UmbraDrift/*) return 0 ;;
    */wallpapers/UmbraGold|*/wallpapers/UmbraGold/*) return 0 ;;
    */wallpapers/UmbraEmblem|*/wallpapers/UmbraEmblem/*) return 0 ;;
    */wallpapers/UmbraVault|*/wallpapers/UmbraVault/*) return 0 ;;
    */backgrounds/shadowfetch/umbra-4k.jpg) return 0 ;;
    */backgrounds/shadowfetch/umbra-ice-4k.jpg) return 0 ;;
  esac
  return 1
}

have() { command -v "$1" >/dev/null 2>&1; }

# Run a command, retrying while plasmashell/kded finish starting.
attempt() {
  tries=0
  while [ "$tries" -lt 5 ]; do
    "$@" >/dev/null 2>&1 && return 0
    tries=$((tries + 1))
    sleep 2
  done
  log "failed: $*"
  failed=1
  return 1
}

set_key() {  # set_key FILE KEY VALUE GROUP...
  file=$1 key=$2 value=$3
  shift 3
  if ! have kwriteconfig6; then
    log "kwriteconfig6 not found; cannot set $key in $file"
    failed=1
    return 1
  fi
  set -- --file "$file" "$@"
  # kwriteconfig6 takes --group once per nesting level; the caller passes them.
  attempt kwriteconfig6 "$@" --key "$key" "$value"
}

# 1) Look-and-feel. Applied first: it also sets the splash, window decoration
#    and colour scheme, which the steps below then only check.
if [ "$(ini_get "$KDEGLOBALS" '[KDE]' LookAndFeelPackage)" = "org.shadowfetch.ice" ]; then
  if have plasma-apply-lookandfeel; then
    attempt plasma-apply-lookandfeel -a "$LOOK" && log "look-and-feel: $LOOK"
  else
    failed=1
  fi
fi
if [ "$(ini_get "$SPLASHRC" '[KSplash]' Theme)" = "org.shadowfetch.ice" ]; then
  set_key "$SPLASHRC" Theme "$LOOK" --group KSplash && log "splash: $LOOK"
fi

# 2a) Accent first. Applying a scheme bakes the CURRENT AccentColor into the
#     [Colors:*] groups, so a retired 4.1 accent still set at that moment
#     survives the migration inside every selection colour.
accent_replaced=0
accent=$(ini_get "$KDEGLOBALS" '[General]' AccentColor)
for retired in $RETIRED_ACCENTS; do
  if [ "$accent" = "$retired" ]; then
    set_key "$KDEGLOBALS" AccentColor "$ACCENT" --group General \
      && { log "accent: $ACCENT (was $accent)"; accent_replaced=1; }
  fi
done

# 2) Colour scheme: Ice or missing -> ShadowfetchDark. ShadowfetchDark whose
#    baked selection colour is not the ShadowCode gold -> re-applied so the new
#    colours reach kdeglobals (see header). The bounce through BreezeDark is
#    needed because plasma-apply-colorscheme declines to apply the scheme that
#    is already current.
scheme=$(ini_get "$KDEGLOBALS" '[General]' ColorScheme)
baked=$(ini_get "$KDEGLOBALS" '[Colors:Selection]' BackgroundNormal)
apply_scheme=0
case "$scheme" in
  ""|ShadowfetchIce) apply_scheme=1 ;;
  "$SCHEME") [ "$baked" = "$ACCENT" ] || apply_scheme=1 ;;
esac
[ "$accent_replaced" -eq 1 ] && apply_scheme=1
if [ "$apply_scheme" -eq 1 ]; then
  if have plasma-apply-colorscheme; then
    if [ "$scheme" = "$SCHEME" ]; then
      plasma-apply-colorscheme BreezeDark >/dev/null 2>&1 || true
    fi
    attempt plasma-apply-colorscheme "$SCHEME" && log "colour scheme: $SCHEME (was ${scheme:-unset})"
  else
    failed=1
  fi
fi


# 3) Desktop wallpaper, per desktop. When every image wallpaper names a
#    removed asset, plasma-apply-wallpaperimage sets them all. When only some
#    do, a plasmashell script repoints just those, so a custom wallpaper on
#    another screen survives.
total=0 stale=0
images=$(wallpaper_images)
if [ -n "$images" ]; then
  old_ifs=$IFS
  IFS='
'
  for image in $images; do
    total=$((total + 1))
    removed_wallpaper "$image" && stale=$((stale + 1))
  done
  IFS=$old_ifs
fi
if [ "$stale" -gt 0 ] && [ "$stale" -eq "$total" ]; then
  if have plasma-apply-wallpaperimage; then
    attempt plasma-apply-wallpaperimage "$WALLPAPER" && log "wallpaper: $WALLPAPER"
  else
    failed=1
  fi
elif [ "$stale" -gt 0 ]; then
  script="var removed = /\\/wallpapers\\/Umbra(Fire|Ice|Frost|Drift|Gold|Emblem|Vault)(\\/|\$)|\\/backgrounds\\/shadowfetch\\/umbra(-ice)?-4k\\.jpg\$/;
desktops().forEach(function (d) {
  if (d.wallpaperPlugin != 'org.kde.image') return;
  d.currentConfigGroup = ['Wallpaper', 'org.kde.image', 'General'];
  if (removed.test(String(d.readConfig('Image', '')))) d.writeConfig('Image', 'file://$WALLPAPER');
});"
  done_js=1
  for q in qdbus6 qdbus-qt6 qdbus; do
    if have "$q"; then
      attempt "$q" org.kde.plasmashell /PlasmaShell org.kde.PlasmaShell.evaluateScript "$script" \
        && log "wallpaper: $WALLPAPER on $stale of $total desktops"
      done_js=0
      break
    fi
  done
  [ "$done_js" -eq 0 ] || { log "no qdbus tool; cannot repoint $stale desktops"; failed=1; }
fi

# 4) Lock screen wallpaper.
lock=$(ini_get "$LOCKRC" '[Greeter][Wallpaper][org.kde.image][General]' Image)
if [ -n "$lock" ] && removed_wallpaper "$lock"; then
  set_key "$LOCKRC" Image "$WALLPAPER" --group Greeter --group Wallpaper \
    --group org.kde.image --group General && log "lock screen: $WALLPAPER"
fi

# 5) Konsole profiles that name the removed Glacier scheme.
for profile in "$DATA"/konsole/*.profile; do
  [ -f "$profile" ] || continue
  if [ "$(ini_get "$profile" '[Appearance]' ColorScheme)" = "ShadowfetchGlacier" ]; then
    set_key "$profile" ColorScheme "$KONSOLE_SCHEME" --group Appearance \
      && log "konsole: $(basename "$profile") -> $KONSOLE_SCHEME"
  fi
done

# 5b) Konsole's default profile. Skel shipped Shadowfetch.profile but no
#     konsolerc naming it before 5.0, so Konsole opened on its built-in profile
#     (Breeze colours). Point it at Shadowfetch.profile only when the user has
#     that profile and has not picked a default; an existing DefaultProfile is
#     a choice and stays.
if [ -f "$DATA/konsole/Shadowfetch.profile" ] \
  && [ -z "$(ini_get "$CONFIG/konsolerc" '[Desktop Entry]' DefaultProfile)" ]; then
  set_key "$CONFIG/konsolerc" DefaultProfile Shadowfetch.profile --group "Desktop Entry" \
    && log "konsole: default profile Shadowfetch.profile"
fi

# 6) Menu launchers written by the retired shadowfetch-codex and
#    shadowfetch-code-agent helpers (ShadowCode connects the vendor CLIs in
#    5.0). Only a launcher that still runs one of those helpers is removed; the
#    CLIs themselves in ~/.local/bin stay, because ShadowCode and Mission
#    Control use them.
for name in codex-cli claude-code grok-build cursor-agent; do
  launcher="$DATA/applications/$name.desktop"
  [ -f "$launcher" ] || continue
  exec_line=$(ini_get "$launcher" '[Desktop Entry]' Exec)
  case "$exec_line" in
    "/usr/bin/shadowfetch-codex open"|"/usr/bin/shadowfetch-code-agent "*" open")
      rm -f "$launcher" && log "removed retired launcher: $name.desktop" ;;
  esac
done

if [ "$failed" -ne 0 ]; then
  log "incomplete; will retry at next login"
  exit 0
fi
mkdir -p "$(dirname "$STAMP")"
printf '%s\n' "shadowcode" > "$STAMP"
rm -f "$OLD_STAMP"
exit 0
