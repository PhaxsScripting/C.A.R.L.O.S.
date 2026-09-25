pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasma5support as Plasma5Support
import org.kde.plasma.plasmoid

PlasmoidItem {
    id: root

    implicitWidth: 148
    implicitHeight: 34
    Layout.minimumWidth: 124
    Layout.preferredWidth: 148
    Layout.maximumWidth: 176
    Layout.fillHeight: true

    property bool coreConnected: false
    property string coreState: "OFFLINE"
    property string stateDetail: "Waiting for E.V. core"
    property string microphone: ""
    property real inputRms: 0.0
    property real inputPeak: 0.0
    property var waveform: []
    property bool voiceActive: false
    property bool wakeActive: false
    property double lastUpdateMs: 0
    readonly property string pollCommand: "$HOME/.local/bin/ev-panel-state"

    readonly property bool listening: coreState === "AWAKE" || coreState === "LISTENING"
    readonly property bool processing: coreState === "TRANSCRIBING" || coreState === "THINKING" || coreState === "RETRIEVING_MEMORY"
    readonly property bool executing: coreState === "USING_TOOL"
    readonly property bool speaking: coreState === "SPEAKING"
    readonly property color accent: {
        if (!coreConnected) return "#ff5874"
        if (listening) return "#a8ff3e"
        if (processing) return "#a889ff"
        if (executing) return "#ffcb55"
        if (speaking) return "#49e8ff"
        return wakeActive ? "#31d9ff" : "#6d8290"
    }
    readonly property string shortState: {
        if (!coreConnected) return "OFFLINE"
        if (coreState === "AWAKE") return "WAKE"
        if (coreState === "LISTENING") return voiceActive ? "HEARING" : "LISTEN"
        if (coreState === "TRANSCRIBING") return "STT"
        if (coreState === "THINKING" || coreState === "RETRIEVING_MEMORY") return "THINK"
        if (coreState === "USING_TOOL") return "EXEC"
        if (coreState === "SPEAKING") return "VOICE"
        return wakeActive ? "ARMED" : "DORMANT"
    }

    Plasmoid.title: "E.V. Neural Voice"
    toolTipMainText: "E.V.  //  " + shortState
    toolTipSubText: stateDetail + (microphone.length > 0 ? "\nInput: " + microphone : "")
    // This is an inline monitor like Plasma's Network Speed widget, not an
    // icon that opens a popup.
    preferredRepresentation: fullRepresentation

    function consumeOutput(output) {
        const lines = String(output || "").trim().split("\n")
        for (let i = lines.length - 1; i >= 0; --i) {
            try {
                const message = JSON.parse(lines[i])
                if (message.type !== "response" || !message.payload)
                    continue
                const payload = message.payload
                coreConnected = Boolean(payload.connected)
                coreState = String(payload.state || "OFFLINE")
                stateDetail = String(payload.detail || "")
                microphone = String(payload.microphone || "")
                inputRms = Number(payload.rms || 0.0)
                inputPeak = Number(payload.peak || 0.0)
                waveform = Array.isArray(payload.waveform) ? payload.waveform : []
                voiceActive = Boolean(payload.voice_active)
                wakeActive = Boolean(payload.wake_active)
                lastUpdateMs = Date.now()
                return
            } catch (error) {
                // The executable engine may include the one-line IPC hello.
                // Ignore it and keep looking for the correlated response.
            }
        }
    }

    Plasma5Support.DataSource {
        id: stateSource
        engine: "executable"
        onNewData: function(sourceName, data) {
            root.consumeOutput(data["stdout"])
            disconnectSource(sourceName)
        }
    }

    Timer {
        interval: 250
        repeat: true
        running: true
        triggeredOnStart: true
        onTriggered: stateSource.connectSource(root.pollCommand)
    }

    Timer {
        interval: 900
        repeat: true
        running: true
        onTriggered: {
            if (root.lastUpdateMs > 0 && Date.now() - root.lastUpdateMs > 1600) {
                root.coreConnected = false
                root.coreState = "OFFLINE"
                root.stateDetail = "E.V. core link lost"
            }
        }
    }

    fullRepresentation: Item {
        id: compactRoot
        implicitWidth: 148
        implicitHeight: 34
        Layout.minimumWidth: 124
        Layout.preferredWidth: 148
        Layout.maximumWidth: 176
        Layout.fillHeight: true

        Rectangle {
            anchors.fill: parent
            anchors.margins: 2
            radius: 8
            color: "#d9081117"
            border.width: 1
            border.color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, root.listening || root.processing || root.executing || root.speaking ? 0.78 : 0.34)

            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 1
                height: 1
                color: root.accent
                opacity: 0.34
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 7
                anchors.rightMargin: 7
                spacing: 6

                Item {
                    Layout.preferredWidth: 25
                    Layout.fillHeight: true

                    Rectangle {
                        id: outerRing
                        anchors.centerIn: parent
                        width: 23
                        height: 23
                        radius: 12
                        color: "transparent"
                        border.width: 1
                        border.color: root.accent
                        opacity: root.coreConnected ? 0.72 : 0.34

                        SequentialAnimation on scale {
                            running: root.listening
                            loops: Animation.Infinite
                            NumberAnimation { to: 1.16; duration: 430; easing.type: Easing.OutSine }
                            NumberAnimation { to: 1.0; duration: 430; easing.type: Easing.InSine }
                        }
                    }
                    Rectangle {
                        anchors.centerIn: parent
                        width: 17
                        height: 17
                        radius: 9
                        color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.16)
                        border.width: 1
                        border.color: root.accent
                    }
                    Text {
                        anchors.centerIn: parent
                        text: "EV"
                        color: root.accent
                        font.pixelSize: 8
                        font.bold: true
                        font.letterSpacing: 0.3
                    }
                }

                Item {
                    id: waveArea
                    Layout.fillWidth: true
                    Layout.minimumWidth: 50
                    Layout.fillHeight: true

                    Row {
                        anchors.centerIn: parent
                        spacing: 2

                        Repeater {
                            model: 11
                            delegate: Rectangle {
                                id: waveBar
                                required property int index
                                readonly property real sample: {
                                    if (root.waveform.length === 0)
                                        return root.inputRms
                                    const position = Math.min(root.waveform.length - 1, Math.floor(index * root.waveform.length / 11))
                                    return Math.abs(Number(root.waveform[position] || 0.0))
                                }
                                width: 2
                                height: Math.max(2, Math.min(waveArea.height - 8, 2 + sample * 185))
                                radius: 1
                                color: root.accent
                                opacity: root.coreConnected ? 0.55 + Math.min(0.45, sample * 5) : 0.22
                                Behavior on height { NumberAnimation { duration: 70 } }
                                Behavior on color { ColorAnimation { duration: 150 } }
                            }
                        }
                    }

                    Rectangle {
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        height: 1
                        color: root.accent
                        opacity: 0.12
                    }
                }

                Column {
                    Layout.preferredWidth: 43
                    Layout.alignment: Qt.AlignVCenter
                    spacing: 0

                    Text {
                        width: parent.width
                        text: root.shortState
                        color: root.accent
                        font.pixelSize: 8
                        font.bold: true
                        font.letterSpacing: 0.65
                        horizontalAlignment: Text.AlignRight
                        elide: Text.ElideRight
                    }
                    Text {
                        width: parent.width
                        text: root.listening ? Math.round(root.inputPeak * 100) + "%" : root.coreConnected ? "NEURAL" : "NO LINK"
                        color: "#718996"
                        font.pixelSize: 7
                        font.letterSpacing: 0.45
                        horizontalAlignment: Text.AlignRight
                        elide: Text.ElideRight
                    }
                }
            }

            Rectangle {
                id: scanLine
                visible: root.processing || root.executing
                width: 18
                height: 1
                y: parent.height - 3
                color: root.accent
                opacity: 0.9
                SequentialAnimation on x {
                    running: scanLine.visible
                    loops: Animation.Infinite
                    NumberAnimation { from: 3; to: compactRoot.width - 21; duration: 650; easing.type: Easing.InOutQuad }
                    NumberAnimation { from: compactRoot.width - 21; to: 3; duration: 650; easing.type: Easing.InOutQuad }
                }
            }
        }

        MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: Qt.openUrlExternally("applications:ev-control-center.desktop")
        }
    }
}
