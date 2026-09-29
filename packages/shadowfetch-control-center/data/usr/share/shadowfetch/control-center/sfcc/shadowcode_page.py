"""ShadowCode: the coding agent preinstalled on this desktop.

ShadowCode (package shadow-code) is not built from this tree; Shadowfetch ships
the upstream-signed .deb. This page only reports what dpkg says is installed
and opens the app. Accounts, API keys and models are set up inside ShadowCode.

Both programs are named through the trusted table (sfcc.desktop.PROGRAMS).
dpkg-query runs with the fixed system PATH, because its answer is a statement
about the machine. ShadowCode itself keeps the session PATH, exactly as
Welcome's open_shadowcode() does: it runs the person's own vendor command-line
tools, which live in ~/.local/bin, and desktop.trusted_env() leaves that out.
"""
import subprocess

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QPushButton, QScrollArea, QVBoxLayout, QWidget

from sfcc import desktop
from sfcc.theme import Card, label

PACKAGE = "shadow-code"
SHORTCUT = "Meta+Shift+C"

CONNECT = ("Connect what you already have in ShadowCode's Accounts: a Codex, Claude Code, Cursor, "
           "Antigravity or Grok subscription through each vendor's own sign-in, an OpenRouter API key "
           "billed per token, or a free model that runs on this computer. Nothing is connected until you "
           "sign in there, and ShadowCode never buys credits for you.")


def installed_version():
    """The installed shadow-code version, or None when dpkg does not have it installed."""
    path = desktop.trusted_program("dpkg-query")
    if path is None:
        return None
    try:
        result = subprocess.run([path, "-W", "-f=${db:Status-Status}\t${Version}", PACKAGE],
                                capture_output=True, text=True, timeout=10, check=False,
                                env=desktop.trusted_env())
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    status, _tab, version = result.stdout.strip().partition("\t")
    return version if status == "installed" and version else None


def open_shadowcode():
    """Start ShadowCode detached with the session PATH. False when it cannot start."""
    return desktop.start_with_session_path("shadowcode")


class ShadowCodePage(QWidget):
    @classmethod
    def build(cls, context):
        return cls(context.open_route)

    def __init__(self, open_route=None):
        super().__init__()
        self.open_route = open_route
        self.version = None
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 10, 24, 18)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(12)

        hero = Card(active=True)
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(24, 20, 24, 20)
        hero_layout.setSpacing(10)
        eyebrow = QHBoxLayout()
        eyebrow.addWidget(label("ONE HARNESS. ALL MODELS.", "safety"))
        eyebrow.addStretch(1)
        eyebrow.addWidget(label("PREINSTALLED", "detail"))
        hero_layout.addLayout(eyebrow)
        hero_layout.addWidget(label("ShadowCode", "title"))
        hero_layout.addWidget(label("The coding agent built into this desktop. Pick a model for each task, "
                                    "approve what it may do, and review every change it makes.",
                                    "subtitle", wrap=True))
        hero_layout.addWidget(label(CONNECT, "detail", wrap=True))
        self.state = label("Checking this computer…", "statusWarn", wrap=True)
        hero_layout.addWidget(self.state)
        buttons = QHBoxLayout()
        self.open_button = QPushButton("Open ShadowCode  ↗")
        self.open_button.setMinimumHeight(40)
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open)
        buttons.addWidget(self.open_button)
        self.software = QPushButton("Open Software")
        self.software.setObjectName("quiet")
        self.software.clicked.connect(lambda: self._route("software"))
        buttons.addWidget(self.software)
        buttons.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.setObjectName("quiet")
        refresh.clicked.connect(self.refresh)
        buttons.addWidget(refresh)
        hero_layout.addLayout(buttons)
        layout.addWidget(hero)

        facts = QGridLayout()
        facts.setSpacing(10)
        self.network_fact = label("", "detail", wrap=True)
        for i, (heading, content) in enumerate((
            ("Updates come with the system",
             label("Shadowfetch verifies each ShadowCode release's publisher signature before shipping it, "
                   "and it arrives with your system updates in Software. ShadowCode's own GitHub update "
                   "check is turned off by /etc/shadowcode/policy.yaml.", "detail", wrap=True)),
            ("Commands run in a sandbox",
             label("Shell commands ShadowCode runs go through bubblewrap, which hides your home folder. "
                   "You approve actions, then keep or undo each change.", "detail", wrap=True)),
            ("Always one keystroke away",
             label(f"New accounts have it pinned in the dock, next to the file manager, and on {SHORTCUT}. "
                   "Set your own key in System Settings › Keyboard › Shortcuts.", "detail", wrap=True)),
            ("Network", self.network_fact),
        )):
            card = Card()
            row = QVBoxLayout(card)
            row.setContentsMargins(16, 13, 16, 13)
            row.addWidget(label(heading, "cardTitle", wrap=True))
            row.addWidget(content)
            facts.addWidget(card, i // 2, i % 2)
        layout.addLayout(facts)
        links = QHBoxLayout()
        missions = QPushButton("Open Mission Control")
        missions.setObjectName("quiet")
        missions.clicked.connect(lambda: self._route("missions"))
        links.addWidget(missions)
        links.addStretch(1)
        layout.addLayout(links)
        layout.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll)
        self.timer = QTimer(self)
        self.timer.setInterval(60_000)
        self.timer.timeout.connect(lambda: self.refresh() if self.isVisible() else None)
        self.timer.start()
        QTimer.singleShot(0, self.refresh)

    def refresh(self):
        self.show_state(installed_version(), desktop.trusted_program("shadowcode") is not None,
                        desktop.agent_network_offline())

    def show_state(self, version, launchable, offline):
        self.version = version
        if version and launchable:
            self._set_state(f"ShadowCode {version} is installed.", warn=False)
        elif version:
            self._set_state(f"ShadowCode {version} is registered, but /usr/bin/shadowcode is missing. "
                            "Reinstall the shadow-code package from Software.", warn=True)
        else:
            self._set_state("ShadowCode is not installed. Reinstall the shadow-code package from Software.",
                            warn=True)
        self.open_button.setEnabled(bool(launchable))
        self.software.setVisible(not (version and launchable))
        if offline:
            self.network_fact.setText(
                "The Shadowfetch agent network is offline. ShadowCode keeps its own network setting: "
                "choose Offline in its Settings › Permissions & network to use only models on this computer.")
        else:
            self.network_fact.setText(
                "ShadowCode has its own network mode (Online, Web tools off or Offline) in its "
                "Settings › Permissions & network. It sends no telemetry.")

    def _set_state(self, text, warn):
        self.state.setText(text)
        self.state.setObjectName("statusWarn" if warn else "status")
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)

    def _open(self):
        if not open_shadowcode():
            self._set_state("ShadowCode could not be started. Reinstall the shadow-code package from Software.",
                            warn=True)
            self.software.setVisible(True)

    def _route(self, route):
        if self.open_route:
            self.open_route(route)
