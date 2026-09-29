import QtQuick 2.15

// ShadowCode splash: the two-tone wordmark (SHADOWFETCH gold, LINUX brushed
// steel, as on the emblem artwork) on the near-black window colour.
// Every colour here is a role in
// tools/truth/palette.json; tools/drift_gate.py rejects any other.
Rectangle {
    id: root
    color: "#0e1116"
    anchors.fill: parent

    property int stage: 0
    readonly property int totalStages: 6

    Column {
        anchors.centerIn: parent
        spacing: 30

        Row {
            anchors.horizontalCenter: parent.horizontalCenter

            Text {
                text: "SHADOWFETCH"
                color: "#f2b33d"
                font.family: "Inter"
                font.pixelSize: 46
                font.weight: Font.Bold
                font.letterSpacing: 3
                renderType: Text.NativeRendering
            }

            Text {
                text: " LINUX"
                color: "#bcc0c6"
                font.family: "Inter"
                font.pixelSize: 46
                font.weight: Font.Bold
                font.letterSpacing: 3
                renderType: Text.NativeRendering
            }
        }

        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "ONE HARNESS. ALL MODELS."
            color: "#9aa3ad"
            font.family: "Inter"
            font.pixelSize: 14
            font.weight: Font.Medium
            font.letterSpacing: 6
            renderType: Text.NativeRendering
        }

        Rectangle {
            anchors.horizontalCenter: parent.horizontalCenter
            width: 280
            height: 4
            radius: 2
            color: "#262b33"

            Rectangle {
                height: parent.height
                radius: 2
                color: "#f2b33d"
                width: parent.width * Math.max(0.04, Math.min(1.0, root.stage / root.totalStages))
                Behavior on width {
                    NumberAnimation { duration: 280; easing.type: Easing.OutCubic }
                }
            }
        }
    }
}
