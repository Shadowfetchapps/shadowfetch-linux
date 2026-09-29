# ShadowCode: WebKitWebProcess idles at 120-180% CPU when the machine has no DRM render node

**Draft upstream issue, from Shadowfetch Linux 5.0.0 VM qualification.**
ShadowCode 0.34.2 (upstream-signed `shadow-code` .deb), Debian testing,
WebKitGTK as shipped by Debian, KDE Plasma 6.7 Wayland.

## Summary

On a machine without a GPU render node (`/dev/dri/renderD*` absent: QEMU
std-VGA or virtio-gpu without virgl, many cloud desktops, some broken driver
setups) ShadowCode's first-run screen keeps `WebKitWebProcess` busy while
nothing on screen changes. Setting `WEBKIT_DISABLE_DMABUF_RENDERER=1` (or
`WEBKIT_DISABLE_COMPOSITING_MODE=1`) before launch brings it to idle.

## Measurements

Each cycle: start ShadowCode via `systemd-run --user`, settle 15 s, then read
the unit's `CPUUsageNSec` over 45 s with per-process attribution. Display on,
no input.

| Environment                             | unit CPU (5 cycles)         |
|-----------------------------------------|-----------------------------|
| default                                 | 125, 138, 165, 162, 178 %   |
| of which `WebKitWebProcess`             | 122 % (cycle 1)             |
| `WEBKIT_DISABLE_DMABUF_RENDERER=1`      | 3 %                         |
| `WEBKIT_DISABLE_COMPOSITING_MODE=1`     | 4 %                         |

Reproduce: boot any Linux VM with no render node, check `ls /dev/dri`
(a `card0` and no `renderD128`), launch ShadowCode, leave it on the first-run
screen, watch `top` for `WebKitWebProcess`.

## What we did downstream (and why it is not the right place)

The distro must not modify the signed `/usr/bin/shadowcode`. Shadowfetch now
ships a systemd user environment generator
(`/usr/lib/systemd/user-environment-generators/60-shadowfetch-webkit-software-rendering`)
that exports `WEBKIT_DISABLE_DMABUF_RENDERER=1` for the whole session only when
no render node exists. That works for the Shadowfetch desktop but does nothing
for ShadowCode users on other distributions, and it applies to every WebKitGTK
app in the session rather than just the one that needs it.

## Requested fix

At startup, before the first WebKit view is created, ShadowCode should:

```rust
// or the equivalent in the launcher
let has_render_node = std::fs::read_dir("/dev/dri")
    .map(|d| d.flatten().any(|e| e.file_name().to_string_lossy().starts_with("renderD")))
    .unwrap_or(false);
if !has_render_node && std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
    std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
}
```

* Only when there is no render node, so GPU machines keep hardware rendering.
* Never override a value the user set.
* Must run before any thread starts (setting the environment later is unsafe
  and too late for the WebKit web process).

Tauri/wry-based apps commonly carry the same workaround; a release note line
("idle CPU on machines without GPU acceleration") would help downstreams know
when they can drop theirs.

## Evidence

`work/qa-5.0.0/diag/shadowcode-idle-165211/probe.log`,
`work/qa-5.0.0/diag/shadowcode-webkit-env.txt` (Shadowfetch QA tree), probe
script `tools/qa_5_0_0/shadowcode_idle_probe.sh`.
