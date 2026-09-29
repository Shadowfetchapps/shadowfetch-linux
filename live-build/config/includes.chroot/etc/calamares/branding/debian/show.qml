/* === This file is part of Calamares - <http://github.com/calamares> ===
 *
 *   Copyright 2015, Teo Mrnjavac <teo@kde.org>
 *   Copyright 2018-2019, Jonathan Carter <jcc@debian.org>
 *
 *   Calamares is free software: you can redistribute it and/or modify
 *   it under the terms of the GNU General Public License as published by
 *   the Free Software Foundation, or (at your option) any later version.
 *
 *   Calamares is distributed in the hope that it will be useful,
 *   but WITHOUT ANY WARRANTY; without even the implied warranty of
 *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
 *   GNU General Public License for more details.
 *
 *   You should have received a copy of the GNU General Public License
 *   along with Calamares. If not, see <http://www.gnu.org/licenses/>.
 */

import QtQuick 2.15;
import calamares.slideshow 1.0;

Presentation
{
    id: presentation

    // slideshowAPI: 2 (branding.desc). Calamares sets activatedInCalamares
    // while the install page is showing; a Timer without `running` never
    // starts, which is why 5.0.0's first builds stayed on slide one.
    Timer {
        interval: 12000
        repeat: true
        running: presentation.activatedInCalamares
        onTriggered: presentation.goToNextSlide()
    }

    function onActivate() { presentation.currentSlide = 0; }
    function onLeave() { }

    // The ShadowCode artwork (1000x563) carries the SHADOWFETCH LINUX wordmark
    // and tagline down to ~78% of its height; below that is only reflection.
    // The captioned slides show the top 82% of it, fitted ABOVE the caption
    // band, so the caption never covers the wordmark (it did at the 920x640
    // window, where a full-bleed crop put the wordmark under the caption).
    readonly property real artAspect: 1000 / 563
    readonly property real artShown: 0.82
    readonly property int captionHeight: 116

    // ShadowCode (5.0.0): gold / brushed dark steel on near-black. Colours are
    // tools/truth/palette.json roles: ink #0A0D11, accent #F2B33D, silver #BCC0C6.

    Slide {
        Rectangle { anchors.fill: parent; color: "#0A0D11" }
        Item {
            anchors.top: parent.top; anchors.horizontalCenter: parent.horizontalCenter
            height: parent.height - presentation.captionHeight
            width: Math.min(parent.width, height / presentation.artShown * presentation.artAspect)
            clip: true
            Image {
                anchors.top: parent.top
                width: parent.width; height: width / presentation.artAspect
                source: "slide-shadowcode.jpg"
                fillMode: Image.PreserveAspectFit
            }
        }
        Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: presentation.captionHeight; color: "#0A0D11" }
        Text {
            anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
            anchors.margins: 24; height: 82
            color: "#FFFFFF"; wrapMode: Text.WordWrap; textFormat: Text.RichText
            font.pixelSize: 17; horizontalAlignment: Text.AlignLeft
            text: qsTr("<b>Shadowfetch Linux 5.0.0</b> &mdash; <font color=\"#F2B33D\">One harness. All models.</font><br/>ShadowCode is the desktop's coding agent. Pick a model from a subscription you already have, an API key, or one that runs on this computer.")
        }
    }

    Slide {
        Rectangle { anchors.fill: parent; color: "#0A0D11" }
        Image {
            anchors.fill: parent
            source: "slide-agents.jpg"
            fillMode: Image.PreserveAspectCrop
        }
        Text {
            anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
            anchors.rightMargin: 40
            width: parent.width * 0.46
            color: "#FFFFFF"; wrapMode: Text.WordWrap; textFormat: Text.RichText
            font.pixelSize: 19; horizontalAlignment: Text.AlignLeft
            lineHeight: 1.2
            text: qsTr("<b><font color=\"#F2B33D\">Agents only if you choose them</font></b><br/><br/>Grok Bot, Hermes and OpenClaw are optional. Nothing is installed until you pick it after installation.<br/><br/><font color=\"#BCC0C6\">Agent sandboxes can start offline.</font>")
        }
    }

    Slide {
        Rectangle { anchors.fill: parent; color: "#0A0D11" }
        Item {
            anchors.top: parent.top; anchors.horizontalCenter: parent.horizontalCenter
            height: parent.height - presentation.captionHeight
            width: Math.min(parent.width, height / presentation.artShown * presentation.artAspect)
            clip: true
            Image {
                anchors.top: parent.top
                width: parent.width; height: width / presentation.artAspect
                source: "slide-shadowcode.jpg"
                fillMode: Image.PreserveAspectFit
                opacity: 0.55
            }
        }
        Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: presentation.captionHeight; color: "#0A0D11" }
        Text {
            anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
            anchors.margins: 24; height: 82
            color: "#FFFFFF"; wrapMode: Text.WordWrap; textFormat: Text.RichText
            font.pixelSize: 17; horizontalAlignment: Text.AlignLeft
            text: qsTr("<b>Built to come back</b><br/>Fireproof simulates updates first. Phoenix Points protect system changes. Firebreak confines agent writes to the project you select.")
        }
    }

}
