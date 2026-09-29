#!/bin/sh
# Stamp /etc/shadowfetch/agent-network from the kernel command line
# (sf.agent-network=online|offline). Runs once per boot, before the display
# manager, so the whole session agrees on the choice made at the boot menu.
# Does nothing unless the cmdline names a value (the boot menu wins when used).
set -u
net=""
for tok in $(cat /proc/cmdline); do
    case "$tok" in
        sf.agent-network=online) net=online ;;
        sf.agent-network=offline) net=offline ;;
    esac
done
[ -n "$net" ] || exit 0
mkdir -p /etc/shadowfetch
printf '%s\n' "$net" > /etc/shadowfetch/agent-network
rm -f /etc/shadowfetch/element
exit 0
