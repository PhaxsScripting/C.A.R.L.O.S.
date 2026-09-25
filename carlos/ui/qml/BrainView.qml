pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Layouts

Item {
    id: root
    required property var client
    property bool animationsRunning: false
    property bool initialExplore: false
    property int stage: initialExplore ? 1 : 0
    onVisibleChanged: if (!visible && !initialExplore) stage = 0
    function get(object,key,fallback) { return object && object[key] !== undefined ? object[key] : fallback }
    component Readout: HudPanel {
        id: readout
        property string label
        property string value
        property string detail
        implicitHeight: 118
        Column {
            anchors.fill: parent; anchors.margins: 15; spacing: 10
            Text { width: parent.width; text: readout.label; color: readout.accent; font.family: "Hack"; font.pixelSize: 10; elide: Text.ElideRight }
            Text { width: parent.width; text: readout.value; color: "#edfaff"; font.family: "Hack"; font.pixelSize: 25; minimumPixelSize: 18; fontSizeMode: Text.HorizontalFit }
            Text { width: parent.width; text: readout.detail; color: "#92b0c2"; wrapMode: Text.WordWrap; font.pixelSize: 10 }
        }
    }
    HudBackdrop { anchors.fill: parent; perspective: true }
    StackLayout {
        anchors.fill: parent; currentIndex: root.stage
        Item {
            id: landing
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 22; spacing: 14
                RowLayout {
                    Layout.fillWidth: true
                    Text { text: "COGNITIVE ENGINE"; color: "#d7f4ff"; font.family: "Hack"; font.pixelSize: 12; font.letterSpacing: 3 }
                    Item { Layout.fillWidth: true }
                    Text { text: "ARCHITECTURE VISUALIZATION  /  LIVE TELEMETRY"; color: "#78a6bd"; font.family: "Hack"; font.pixelSize: 8; font.letterSpacing: 1 }
                }
                RowLayout {
                    Layout.fillWidth: true; Layout.fillHeight: true; spacing: 6
                    ColumnLayout {
                        Layout.preferredWidth: landing.width < 1000 ? 144 : 184; spacing: 18
                        Layout.minimumWidth: Layout.preferredWidth
                        Layout.maximumWidth: Layout.preferredWidth
                        Text { text: "01 / INPUT ARRAY"; color: "#719bb2"; font.family: "Hack"; font.pixelSize: 9; font.letterSpacing: 1 }
                        Readout {
                            Layout.fillWidth: true; label: "VOICE LINK"
                            value: root.get(root.client.voice,"wake_active",false) ? "ONLINE" : "INACTIVE"
                            detail: root.get(root.client.voice,"privacy_mode",false) ? "Privacy mode enabled" : "Local wake-word capture"
                        }
                        Readout {
                            Layout.fillWidth: true; accent: "#a9aeff"; label: "MEMORY BANK"
                            value: String(root.client.memories.length).padStart(2,"0"); detail: "Explicit saved memories"
                        }
                        Text { Layout.fillWidth: true; text: "INPUT\n↓\nINTERPRETATION\n↓\nVERIFIED ACTION"; color: "#688da4"; font.family: "Hack"; font.pixelSize: 9; lineHeight: 1.65; font.letterSpacing: 1 }
                    }
                    Item {
                        Layout.fillWidth: true; Layout.fillHeight: true
                        Layout.minimumWidth: 240
                        NeuralHologram {
                            anchors.fill: parent; anchors.margins: -12
                            active: root.client.state !== "DORMANT"
                            animate: root.visible && root.animationsRunning
                            state: root.client.state
                            scale: enterMouse.containsMouse ? 1.025 : 1
                            Behavior on scale { NumberAnimation { duration: 220 } }
                        }
                        MouseArea {
                            id: enterMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                            Accessible.name: "Explore the neural architecture"; Accessible.role: Accessible.Button
                            Accessible.onPressAction: root.stage = 1
                            onClicked: root.stage = 1
                        }
                    }
                    ColumnLayout {
                        Layout.preferredWidth: landing.width < 1000 ? 144 : 184; spacing: 18
                        Layout.minimumWidth: Layout.preferredWidth
                        Layout.maximumWidth: Layout.preferredWidth
                        Text { text: "02 / EXECUTION ARRAY"; color: "#719bb2"; font.family: "Hack"; font.pixelSize: 9; font.letterSpacing: .7 }
                        Readout {
                            Layout.fillWidth: true; accent: "#f1bd7b"; label: "TOOL MATRIX"
                            value: root.client.tools.length; detail: "Registered capabilities"
                        }
                        Readout {
                            Layout.fillWidth: true; label: "EVENT BUFFER"
                            value: root.client.events.length; detail: "Observed software events"
                        }
                        Text { Layout.fillWidth: true; text: root.client.detail; color: "#9cbbca"; font.pixelSize: 11; wrapMode: Text.WordWrap; maximumLineCount: 5; elide: Text.ElideRight; lineHeight: 1.4 }
                    }
                }
                RowLayout {
                    Layout.fillWidth: true; spacing: 16
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#31566a" }
                    SciButton { objectName: "enter-brain-network"; text: "ENTER NEURAL NETWORK  →"; implicitHeight: 48; leftPadding: 30; rightPadding: 30; onClicked: root.stage = 1 }
                    Rectangle { Layout.fillWidth: true; height: 1; color: "#31566a" }
                }
                Text { Layout.alignment: Qt.AlignHCenter; text: "A map of connected systems and real events. Not a display of hidden thoughts."; color: "#8caabb"; font.pixelSize: 10 }
            }
        }
        BrainExplorer { objectName: "brain-explorer"; client: root.client; animationsRunning: root.animationsRunning; onExitRequested: root.stage = 0 }
    }
}
