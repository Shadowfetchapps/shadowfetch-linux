# Hermes Agent on Shadowfetch Linux

Hermes Agent is an open-source terminal AI agent by Nous Research (MIT
licence). It is optional: it is not in the ISO, and nothing is downloaded
unless you choose it in Welcome, on the Control Center's **Hermes & OpenClaw**
page, or with `shadowfetch-hermes setup`.

## What setup does

Shadowfetch 5.0 pins Hermes Agent **0.21.5** (tag `v2026.9.24`). Setup:

- downloads the official installer that shipped with that release, checks its
  SHA-256, and runs it as you, never as root;
- installs Hermes into `~/.hermes/hermes-agent` at the pinned commit, links
  `~/.local/bin/hermes`, and adds `~/.local/bin` to `PATH` in your shell
  startup files;
- downloads its Python dependencies and toolchain; plan for about 2 GB of
  downloads and disk;
- installs no messaging gateway service and runs no setup wizard.

`~/.hermes` holds Hermes's config, the `.env` file with your API keys,
sessions and logs. Shadowfetch does not store your keys.

## Before you use it

Hermes is cloud-connected: it needs your own model provider account or API
key, entered with `hermes setup`. Provider charges and data handling apply.

Hermes runs commands and edits files **as you**. It is not placed in a
Firebreak sandbox. Give it only the work you would give a script running under
your account.

While the agent network is offline (`shadowfetch-agent-network`), setup,
update and launch are paused.

## Commands

    shadowfetch-hermes setup        # review and install the pinned release
    shadowfetch-hermes open         # open Hermes in a terminal
    shadowfetch-hermes status --json
    shadowfetch-hermes doctor
    shadowfetch-hermes update       # show the latest verified release, install after consent
    shadowfetch-hermes uninstall    # add --purge-data to also remove ~/.hermes
    shadowfetch-hermes info

`update` resolves the latest GitHub release, cross-checks its commit and
installer with the GitHub API, shows the old and new version, and installs only
after you confirm. Keep Hermes current this way.

Docs: https://hermes-agent.nousresearch.com/docs
Source: https://github.com/NousResearch/hermes-agent
