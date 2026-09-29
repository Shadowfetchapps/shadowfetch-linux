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

    Timer {
        interval: 12000
        repeat: true
        onTriggered: presentation.goToNextSlide()
    }

    // ShadowCode (5.0.0): gold / brushed dark steel on near-black. Colours are
    // tools/truth/palette.json roles: ink #0A0D11, accent #F2B33D, silver #BCC0C6.

    Slide {
        Rectangle { anchors.fill: parent; color: "#0A0D11" }
        Image {
            anchors.fill: parent
            source: "slide-shadowcode.jpg"
            fillMode: Image.PreserveAspectCrop
        }
        Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: 116; color: "#E60A0D11" }
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
        Image {
            anchors.fill: parent
            source: "slide-shadowcode.jpg"
            fillMode: Image.PreserveAspectCrop
            opacity: 0.55
        }
        Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: 116; color: "#E60A0D11" }
        Text {
            anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
            anchors.margins: 24; height: 82
            color: "#FFFFFF"; wrapMode: Text.WordWrap; textFormat: Text.RichText
            font.pixelSize: 17; horizontalAlignment: Text.AlignLeft
            text: qsTr("<b>Built to come back</b><br/>Fireproof simulates updates first. Phoenix Points protect system changes. Firebreak confines agent writes to the project you select.")
        }
    }

}
