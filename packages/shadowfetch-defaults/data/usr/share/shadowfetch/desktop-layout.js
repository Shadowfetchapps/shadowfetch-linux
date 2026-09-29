// Shadowfetch Linux "Umbra" — premium floating panel + centered creative dock.
// Applied via `plasmashell evaluateScript` at first login (see first-login.sh).
// Placeholders @@FAVORITES@@ / @@LAUNCHERS@@ / @@LAUNCHER_ICON@@ are substituted
// at runtime with .desktop IDs that actually exist on the installed system, so
// the dock never shows a broken icon.

// Clean slate: remove any auto-generated default panels.
try {
    var existing = panels();
    for (var i = 0; i < existing.length; i++) {
        existing[i].remove();
    }
} catch (e) {}

var panel = new Panel;
panel.location = "bottom";
panel.height = 46;
// Floating + adaptive opacity = the high-end "designed" look (Plasma 6).
try { panel.floating = true; } catch (e) {}

// --- App launcher (Kickoff) with curated favorites: ShadowCode, then the creative apps ---
var kickoff = panel.addWidget("org.kde.plasma.kickoff");
kickoff.currentConfigGroup = ["General"];
kickoff.writeConfig("favoritesPortedToKAstats", true);
kickoff.writeConfig("favorites", "@@FAVORITES@@");
kickoff.writeConfig("icon", "@@LAUNCHER_ICON@@");
kickoff.writeConfig("alphaSort", true);

// --- Expanding spacer: centers the dock ---
var sp1 = panel.addWidget("org.kde.plasma.panelspacer");
sp1.currentConfigGroup = ["General"];
sp1.writeConfig("expanding", true);

// --- Curated dock: icon-only task manager; files, ShadowCode, browser,
//     terminal, then the pinned creative apps (order set in first-login.sh) ---
var tasks = panel.addWidget("org.kde.plasma.icontasks");
tasks.currentConfigGroup = ["General"];
tasks.writeConfig("launchers", "@@LAUNCHERS@@");
tasks.writeConfig("showOnlyCurrentDesktop", false);
tasks.writeConfig("showOnlyCurrentActivity", false);
tasks.writeConfig("indicateAudioStreams", true);
tasks.writeConfig("fill", false);
tasks.writeConfig("maxStripes", 1);

// --- Expanding spacer ---
var sp2 = panel.addWidget("org.kde.plasma.panelspacer");
sp2.currentConfigGroup = ["General"];
sp2.writeConfig("expanding", true);

// --- System tray ---
panel.addWidget("org.kde.plasma.systemtray");

// --- Clock ---
var clock = panel.addWidget("org.kde.plasma.digitalclock");
clock.currentConfigGroup = ["Appearance"];
clock.writeConfig("showDate", true);
clock.writeConfig("dateFormat", "shortDate");

// Reload so the layout is persisted immediately.
try { panel.reloadConfig(); } catch (e) {}
