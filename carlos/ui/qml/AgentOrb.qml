pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls

Item {
    id: orb
    property string phase: "IDLE"
    property string action: ""
    property var waveform: []
    property bool animate: false
    property int completed: 0
    property int total: 0
    readonly property color tint: phase === "FAILED" || phase === "BLOCKED" ? "#ff7786" :
        phase === "WAITING" || phase === "WAITING_FOR_USER" ? "#ebbf76" :
        phase === "COMPLETED" ? "#80ebbd" : "#70dbf2"
    readonly property real amplitude: {
        if (phase !== "LISTENING" && phase !== "SPEAKING") return 0
        let value = 0
        for (let n = 0; n < waveform.length; n++) value = Math.max(value, Math.abs(Number(waveform[n]) || 0))
        return Math.min(1, value)
    }
    signal activated()
    implicitWidth: 420
    implicitHeight: 320
    onTintChanged: rings.requestPaint()
    onCompletedChanged: rings.requestPaint()
    onTotalChanged: rings.requestPaint()

    Item {
        id: core
        anchors.centerIn: parent
        anchors.verticalCenterOffset: -22
        width: Math.min(parent.width - 36, parent.height - 78)
        height: width
        Rectangle {
            anchors.centerIn: parent
            width: parent.width * (.49 + orb.amplitude * .10); height: width; radius: width / 2
            color: "#0c2533"; border.width: 1; border.color: orb.tint
            gradient: Gradient {
                GradientStop { position: 0; color: "#214255" }
                GradientStop { position: .55; color: "#0a202d" }
                GradientStop { position: 1; color: "#102f40" }
            }
            Text { anchors.centerIn: parent; text: "Carlos"; color: "#d9faff"; font.pixelSize: parent.width * .22; font.letterSpacing: 5; font.weight: Font.Light }
        }
        Canvas {
            id: rings
            anchors.fill: parent
            onWidthChanged: requestPaint()
            onHeightChanged: requestPaint()
            onPaint: {
                const c = getContext("2d"), x = width / 2, y = height / 2, r = width * .45
                c.reset()
                c.lineWidth = 1; c.strokeStyle = "#284451"
                for (let k = 0; k < 3; k++) {
                    c.beginPath(); c.arc(x, y, r - k * 11, 0, Math.PI * 2); c.stroke()
                }
                c.strokeStyle = orb.tint; c.lineWidth = 2
                for (let n = 0; n < 4; n++) {
                    const a = n * Math.PI / 2 + .12
                    c.beginPath(); c.arc(x, y, r - 11, a, a + .55); c.stroke()
                }
                // This is observed step progress, never estimated whole-goal completion.
                if (orb.total > 0 && orb.completed > 0) {
                    c.lineWidth = 3; c.beginPath()
                    c.arc(x, y, r + 7, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * Math.min(1, orb.completed / orb.total))
                    c.stroke()
                }
                c.lineWidth = 1; c.strokeStyle = "#547381"
                for (let n = 0; n < 48; n++) {
                    const a = n * Math.PI / 24, inner = r + 12, outer = inner + (n % 4 === 0 ? 7 : 3)
                    c.beginPath(); c.moveTo(x + Math.cos(a) * inner, y + Math.sin(a) * inner)
                    c.lineTo(x + Math.cos(a) * outer, y + Math.sin(a) * outer); c.stroke()
                }
            }
        }
        Item {
            anchors.fill: parent
            visible: ["UNDERSTANDING", "PLANNING", "OBSERVING", "EXECUTING", "VERIFYING", "RECOVERING", "TRANSCRIBING"].indexOf(orb.phase) >= 0
            RotationAnimator on rotation { running: orb.animate && parent.visible; from: 0; to: 360; duration: 14000; loops: Animation.Infinite }
            Rectangle { x: parent.width * .85; y: parent.height * .19; width: 5; height: 5; radius: 3; color: orb.tint }
        }
        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: orb.activated() }
    }
    Column {
        anchors.bottom: parent.bottom; anchors.bottomMargin: 4; width: parent.width; spacing: 6
        Text { objectName: "agent-orb-phase"; width: parent.width; text: orb.phase.replace(/_/g, " "); horizontalAlignment: Text.AlignHCenter; color: orb.tint; font.pixelSize: 12; font.letterSpacing: 3 }
        Text { width: parent.width; text: orb.action || "CLICK CORE TO TYPE"; horizontalAlignment: Text.AlignHCenter; elide: Text.ElideMiddle; color: "#97b3c0"; font.pixelSize: 10 }
        Text { width: parent.width; text: orb.total > 0 ? orb.completed + " / " + orb.total + " OBSERVED PLAN STEPS · NOT WHOLE-GOAL PROGRESS" : ""; horizontalAlignment: Text.AlignHCenter; color: "#6b8998"; font.pixelSize: 8 }
    }
}
