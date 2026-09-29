# Workbench

Shadowfetch Linux connects Mission Control to four production profiles:

- **Software Studio**: Python, TypeScript, rootless containers, database tools,
  and a Dev Container-ready project template.
- **AI Lab**: JupyterLab, the Hugging Face CLI, model provenance, and GPU
  diagnostics. Model downloads remain explicit and are not embedded in the ISO.
- **Production Ops**: Podman, Buildah, Skopeo, Ansible, runbooks, and deployment
  receipts.
- **Creative AI**: Krita, Blender, Kdenlive, OBS, FFmpeg, raw photography tools,
  and provenance templates.

The agent network setting decides whether Firebreak agent sessions start with
network access. Online is the default; offline starts every session without
network until the user grants it (`shadowfetch-agent-network set offline`).
Both keep the same boundary: the project is the only path on your disk a session can
write, the sandbox environment starts empty rather than having known secrets removed
from it, and every session takes a checkpoint and leaves a receipt.

Nothing in Workbench silently downloads a model, creates an account, copies a
credential, publishes work, or grants an agent broader access. The graphical
page and `shadowfetch-workbench plan PROFILE` state disk, network, account and
accelerator consequences before installation.

Useful commands:

    shadowfetch-workbench list
    shadowfetch-workbench plan ai-lab
    shadowfetch-workbench install ai-lab
    shadowfetch-workbench create ai-lab my-project
    shadowfetch-workbench doctor ai-lab

Profile installs use the same root-owned package catalog as Welcome. They are
one APT transaction and receive an automatic Phoenix Point on a supported
Btrfs installation. Projects live under `~/Workspaces` and contain no secrets.

## From project to mission

Open Mission Control from the application menu, or choose **New mission** in
Workbench. Give the mission a title, project folder, workflow and instructions.
Use an existing project directly inside `~/Workspaces`; create one in
Workbench first. Choose one of:

- **Code & tests**: use your signed-in Codex CLI. Enter
  the actual test program and arguments. Inspect changes and test receipts
  before accepting the result.
- **Media export**: select media paths inside the project. The engine performs
  a deterministic FFmpeg export and records validation in its receipt.

The connection selector makes external network access explicit. Codex needs
the agent network online for its cloud connection; deterministic media
workflows work offline. A queued mission persists locally. Activity, Changes and Results
show its execution evidence. Failed and cancelled missions can be retried;
completed work waits for your review. Restore changes uses the mission's local
checkpoint and reports conflicts instead of silently overwriting newer work.

In Dolphin, right-click a project folder and choose **Shadowfetch Mission** to
open the same scoped creation form. Folders outside the workspace root are not
accepted; move or copy only the files you want the agent to use into a project.

**ShadowCode** is preinstalled and connects the coding tools and models you
already have: subscriptions, API keys, or a model that runs on this computer.
Welcome offers three optional agents -- Grok Bot, Hermes and OpenClaw -- each
installed only if you choose it. No API keys or app accounts are included in the
distro.
