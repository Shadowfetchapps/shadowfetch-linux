"""Hermes Agent and OpenClaw: optional, user-installed cloud agents.

Nothing here is preinstalled and nothing runs as root. Every fact on the page
comes from the helper's own `status --json`; every action runs the helper by
its trusted absolute path (sfcc.desktop.PROGRAMS), never through $PATH.
"""
import subprocess

from PyQt6.QtCore import QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QHBoxLayout, QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget

from sfcc import desktop
from sfcc.mission_client import JsonCommand
from sfcc.theme import Card, ProcessDialog, label

AGENTS = (
    {
        "key": "hermes", "program": "shadowfetch-hermes", "name": "Hermes Agent",
        "vendor": "Nous Research · open source (MIT)",
        "summary": "A terminal AI agent with memory, skills and tools. Installs into your home "
                   "folder at a verified release; bring your own provider account or API key.",
        "docs": "https://hermes-agent.nousresearch.com/docs",
        "security": None,
    },
    {
        "key": "openclaw", "program": "shadowfetch-openclaw", "name": "OpenClaw",
        "vendor": "OpenClaw Foundation · open source (MIT)",
        "summary": "A personal AI agent. Installs from a Shadowfetch-pinned npm lockfile with "
                   "verified signatures and opens only inside a Firebreak sandbox, with no "
                   "network service unless you enable its loopback Gateway yourself.",
        "docs": "https://docs.openclaw.ai",
        "security": "Security note: OpenClaw has a long record of security advisories, including "
                    "critical ones. Keep it updated; the pinned release ages fast.",
    },
)


def helper_path(program):
    return desktop.trusted_program(program) or desktop.PROGRAMS[program][0]


class AgentCard(Card):
    def __init__(self, page, spec):
        super().__init__()
        self.page = page
        self.spec = spec
        self.command = helper_path(spec["program"])
        self.record = {}
        self._busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        heading.addWidget(label(spec["name"], "cardTitle"))
        heading.addStretch(1)
        heading.addWidget(label(spec["vendor"], "detail"))
        layout.addLayout(heading)
        layout.addWidget(label(spec["summary"], "detail", wrap=True))
        if spec["security"]:
            layout.addWidget(label(spec["security"], "safety", wrap=True))
        self.state = label("Checking this computer…", "statusWarn", wrap=True)
        layout.addWidget(self.state)
        self.detail = label("", "detail", wrap=True)
        layout.addWidget(self.detail)
        buttons = QHBoxLayout()
        self.install = QPushButton("Install")
        self.update_button = QPushButton("Check for update")
        self.open = QPushButton("Open")
        self.uninstall = QPushButton("Uninstall")
        self.uninstall.setObjectName("quiet")
        docs = QPushButton("Docs")
        docs.setObjectName("quiet")
        for button, callback in ((self.install, self._install), (self.update_button, self._check_update),
                                 (self.open, self._open), (self.uninstall, self._uninstall),
                                 (docs, self._docs)):
            button.setMinimumHeight(36)
            button.setEnabled(button is docs)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

    # ---- status ------------------------------------------------------------

    def refresh(self):
        if self._busy:
            return
        self._busy = True
        JsonCommand(self, self.command, ["status", "--json"], self._status).start()

    def _status(self, data, error):
        self._busy = False
        if error or not isinstance(data, dict):
            self.record = {}
            self._set_state(error or f"The {self.spec['name']} helper returned an unexpected response.", warn=True)
            self.detail.setText("")
            for button in (self.install, self.update_button, self.open, self.uninstall):
                button.setEnabled(False)
            return
        self.record = data
        offline = bool(data.get("blocked_by_offline"))
        state = data.get("state")
        installed = bool(data.get("installed"))
        version = data.get("installed_version")
        if state == "ready":
            self._set_state(f"{self.spec['name']} {version} is installed and verified.", warn=False)
        elif state == "drifted":
            self._set_state(f"{self.spec['name']} moved away from its verified release. "
                            "Update to return to a verified release.", warn=True)
        elif state == "unmanaged":
            self._set_state(f"{self.spec['name']} was installed outside Shadowfetch. "
                            "Install here to verify it at a release.", warn=True)
        elif installed or state == "needs-repair":
            self._set_state(f"{self.spec['name']} needs repair. Install again to repair it.", warn=True)
        else:
            self._set_state(f"Not installed. Pinned release: {data.get('pinned_version') or data.get('version')}.", warn=True)
        details = []
        if self.spec["key"] == "openclaw":
            if installed and version == data.get("pinned_version"):
                details.append("Still on this release's pin — check for an update.")
            details.append("Sandbox: " + ("Firebreak" if data.get("sandbox") == "shadowfetch-firebreak"
                                          else "unavailable — repair shadowfetch-fireline"))
            details.append("Gateway: " + ("enabled, loopback only" if data.get("gateway_enabled") else "off"))
        else:
            commit = data.get("installed_commit")
            if commit:
                details.append(f"Commit {commit[:12]}")
        if offline:
            details.append("The agent network is offline: setup, update and launch are paused.")
        details.append("Needs your own model provider account or API key. Shadowfetch stores no keys.")
        self.detail.setText(" · ".join(details))
        ready = state == "ready"
        self.install.setText("Installed" if ready else "Repair" if installed or state == "needs-repair" else "Install")
        # A drifted install is returned to a release by Update, never rolled back to the pin.
        self.install.setEnabled(state in ("not-installed", "needs-repair", "unmanaged") and not offline)
        self.update_button.setEnabled(installed and not offline)
        self.open.setEnabled(bool(data.get("launchable")) and not offline)
        self.uninstall.setEnabled(installed or state in ("needs-repair", "drifted", "unmanaged"))

    def _set_state(self, text, warn):
        self.state.setText(text)
        self.state.setObjectName("statusWarn" if warn else "status")
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)

    # ---- actions -----------------------------------------------------------

    def _offline(self):
        return desktop.agent_network_offline()

    def _consent_text(self):
        try:
            result = subprocess.run([self.command, "info"], capture_output=True, text=True, timeout=15,
                                    env=desktop.trusted_env(), check=False)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        return f"{self.spec['name']}: {self.spec['summary']}"

    def _run(self, title, arguments, intro):
        dialog = ProcessDialog(self, title, [self.command, *arguments], intro)
        dialog.completed.connect(lambda _code: self.refresh())
        dialog.start()
        dialog.exec()

    def _install(self):
        if self._offline():
            return
        answer = QMessageBox.question(self, f"Install {self.spec['name']}", self._consent_text() + "\n\nInstall now?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                      QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run(f"Installing {self.spec['name']}", ["setup", "--yes", "--no-open"],
                  "Downloads for your user only, verifies before activating, and never asks for an administrator password.")

    def _check_update(self):
        if self._offline():
            return
        self.update_button.setEnabled(False)
        self.update_button.setText("Checking…")
        JsonCommand(self, self.command, ["update", "--check", "--json"], self._update_checked,
                    timeout_ms=180_000).start()

    def _update_checked(self, data, error):
        self.update_button.setText("Check for update")
        self.update_button.setEnabled(bool(self.record.get("installed")) and not self._offline())
        if error or not isinstance(data, dict):
            QMessageBox.warning(self, f"{self.spec['name']} update", error or "The update check returned no answer.")
            return
        latest = data.get("latest_version")
        if not data.get("update_available"):
            QMessageBox.information(self, f"{self.spec['name']} update",
                                    f"{self.spec['name']} is current ({latest}).")
            return
        if self.spec["key"] == "hermes":
            detail = (f"{data.get('installed_version') or 'unknown'} (commit {str(data.get('installed_commit'))[:12]})\n"
                      f"→ {latest} ({data.get('latest_tag')}, commit {str(data.get('latest_commit'))[:12]})\n\n"
                      f"The release's installer is verified against GitHub's record at that commit.")
        else:
            detail = (f"{data.get('installed_version')} → {latest}\n"
                      f"Integrity: {data.get('latest_integrity')}\n\n"
                      "Dependencies are resolved at update time and every tarball is checked by SHA-512; "
                      "npm registry signatures are verified before OpenClaw's own install script runs.")
            if data.get("runtime_ok") is False:
                detail += "\n\nThis release needs a newer Node.js than this system has; the update will stop and say so."
        message = (f"Update {self.spec['name']}?\n\n{detail}\n\n"
                   "This release was published after this Shadowfetch release and has not been reviewed by Shadowfetch. "
                   "Your configuration, sessions and keys are kept.")
        answer = QMessageBox.question(self, f"Update {self.spec['name']}", message,
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                                      QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self._run(f"Updating {self.spec['name']}", ["update", "--yes", "--expect", str(latest)],
                      "Stops if the latest release changed since you reviewed it.")

    def _open(self):
        if self._offline():
            return
        self._run(f"Opening {self.spec['name']}", ["open"], "Opens in a terminal window.")

    def _uninstall(self):
        answer = QMessageBox.question(
            self, f"Uninstall {self.spec['name']}",
            f"Remove {self.spec['name']} for your user? Its configuration, sessions and API keys are kept; "
            f"run `{self.spec['program']} uninstall --purge-data` in a terminal to remove them too.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            self._run(f"Uninstalling {self.spec['name']}", ["uninstall", "--yes"],
                      "Runs the agent's own uninstaller, then removes what Shadowfetch added.")

    def _docs(self):
        if self._offline():
            QMessageBox.information(self, "Offline agent network",
                                    "This opens a public website. Turn the agent network on when you want to connect.")
            return
        QDesktopServices.openUrl(QUrl(self.spec["docs"]))


class OptionalAgentsPage(QWidget):
    @classmethod
    def build(cls, context):
        return cls(context.open_route)

    def __init__(self, open_route=None):
        super().__init__()
        self.open_route = open_route
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 10, 24, 18)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(12)
        layout.addWidget(label("Optional agents", "pageTitle"))
        layout.addWidget(label("Hermes Agent and OpenClaw are not part of the installed system. Each downloads "
                               "only if you choose it, installs for your user alone, and pauses while the agent "
                               "network is offline.", "subtitle", wrap=True))
        self.cards = {}
        for spec in AGENTS:
            card = AgentCard(self, spec)
            self.cards[spec["key"]] = card
            layout.addWidget(card)
        layout.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll)
        self.timer = QTimer(self)
        self.timer.setInterval(60_000)
        self.timer.timeout.connect(lambda: self.refresh() if self.isVisible() else None)
        self.timer.start()
        QTimer.singleShot(0, self.refresh)

    def refresh(self):
        for card in self.cards.values():
            card.refresh()

    def route(self, parts):
        if parts and parts[0] in self.cards:
            self.cards[parts[0]].setFocus()
