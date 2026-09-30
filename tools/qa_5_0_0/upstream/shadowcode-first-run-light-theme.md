# ShadowCode: first run opens in the light theme on a dark desktop and overwrites `ui.theme`

**Draft upstream issue, from Shadowfetch Linux 5.0.0 VM qualification.**
ShadowCode 0.34.2 (upstream-signed `shadow-code` .deb), Debian testing,
KDE Plasma 6.7 Wayland with a dark colour scheme.

Still present in ShadowCode 1.0.0 (tag `v1.0.0`, e0ab2655): the theme code is
unchanged since v0.34.2 -- first run still saves `theme: "system"`
(`ui/src/components/Onboarding.tsx`, `completeOnboarding`), "system" is still
resolved only from the web view's `prefers-color-scheme`
(`ui/src/hooks/useTheme.ts`, `resolveTheme`), and `/etc/shadowcode/policy.yaml`
still has no appearance key (`native/core/src/updates.rs`, `PolicyFile`).

## Summary

The Shadowfetch desktop is dark (Plasma colour scheme `ShadowfetchDark`, GTK
GTK `Breeze-Dark` with `gtk-application-prefer-dark-theme=1`; the KDE portal
derives `org.freedesktop.appearance color-scheme` from the dark scheme -- not
separately captured in QA, please confirm on your side). ShadowCode nevertheless opens its first-run
screen in the light theme. Pre-seeding `ui.theme` in the user's settings does
not help: the first run writes its own value over it, so a distribution cannot
set a dark default either.

## Expected

1. With no explicit user choice, follow the system preference
   (`org.freedesktop.portal.Settings` `color-scheme`, falling back to the GTK
   setting / `prefers-color-scheme` in the web view).
2. First run must not overwrite an existing `ui.theme`; write a default only
   when the key is absent.
3. Ideally honour a system-wide default (e.g. a key in the existing system
   policy file `/etc/shadowcode/policy.yaml`, which the distro already ships),
   so downstreams can match their look without patching the package.

## Steps to reproduce

1. On a Plasma or GNOME desktop set to dark, remove ShadowCode's per-user
   config directory.
2. Optionally create the settings file with `ui.theme` set to the dark theme.
3. Launch ShadowCode. Observed: light theme; `ui.theme` rewritten to light.

## Downstream status

Not worked around in Shadowfetch 5.0.0: the distro does not modify the signed
package, and a pre-seeded value is overwritten. Tracked as a known issue for
the release notes until upstream fixes it.
