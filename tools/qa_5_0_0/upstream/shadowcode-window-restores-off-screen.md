# ShadowCode: a saved window size is restored unclamped and runs off a smaller screen

**Draft upstream issue, from Shadowfetch Linux 5.0.0 VM qualification.**
ShadowCode 1.0.0 (upstream-signed `shadow-code` .deb, tag `v1.0.0`, e0ab2655),
Debian testing, KDE Plasma 6.7 Wayland.

## Steps

1. Open ShadowCode on a 1920x1080 display and maximise it or size it large, then
   quit.
2. Change the display to 1366x768 (or move to a smaller monitor) and open
   ShadowCode again.

## Actual

The window reopens at its saved 1920-wide size. It extends past the right and
bottom edges of the screen, and the composer at the bottom of the window sits
behind the Plasma panel, where it can't be reached without moving the window
by hand (QA screenshot `raw/r1366/defect-shadowcode-overflow.png`).

## Expected

On restore, clamp the saved geometry to the available work area of the screen
the window opens on (the screen minus panels). If the saved position is off
every current screen, centre the window on the primary screen. A saved
maximised state should restore as maximised on the current screen, not as the
old pixel size.
