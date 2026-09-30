#!/bin/sh
# Shadowfetch Linux 5.0 -- tell the desktop user, once, that this machine still
# has the DKMS signing key a 4.x ISO shipped to every install.
#
# 4.x images contained /var/lib/dkms/mok.key and mok.pub, so every install from
# the same ISO shares one module-signing key, and an upgrade keeps it. Rotating
# it here would stop DKMS modules loading on a Secure Boot machine that
# enrolled it, so this only points at the remedy: shadowfetch-doctor
# (sec.dkms_mok) and the 5.0.0 release notes. It never blocks the session and
# never asks for anything.
#
# Runs from XDG autostart (shadowfetch-shared-mok-notice.desktop). The stamp is
# written once the certificate has been checked: after the notice was shown,
# or at once when the key is this machine's own. A session whose notification
# service was not up yet retries at the next login.
set -u

CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
STAMP="$CONFIG/shadowfetch/.shared-mok-notice"
CERT=/var/lib/dkms/mok.pub
LIST=/usr/share/shadowfetch/security/shared-dkms-mok.sha256

[ -e "$STAMP" ] && exit 0
# No certificate yet: DKMS makes a per-machine one; nothing to say.
[ -r "$CERT" ] && [ -r "$LIST" ] || exit 0
command -v sha256sum >/dev/null 2>&1 || exit 0

stamp() {
  mkdir -p "$(dirname "$STAMP")" && printf '%s\n' "$1" > "$STAMP"
}

# The listed fingerprints are of the DER certificate as the ISOs shipped it.
fingerprint=$(sha256sum < "$CERT" | cut -d' ' -f1)
if ! grep -q "^${fingerprint}[[:space:]]" "$LIST"; then
  stamp own-key
  exit 0
fi

command -v notify-send >/dev/null 2>&1 || exit 0
if notify-send --app-name=Shadowfetch --icon=dialog-warning \
  "Shared Secure Boot signing key" \
  "This computer still has the DKMS signing key that came with the Shadowfetch 4.x ISO, which every 4.x install shares. Run shadowfetch-doctor for the steps, or see Known issues in the 5.0.0 release notes." \
  >/dev/null 2>&1; then
  stamp shown
fi
exit 0
