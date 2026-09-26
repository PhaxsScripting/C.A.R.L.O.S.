import QtQuick

Item {
    id: root
    property string bubble: ""
    property string mood: "happy"
    property bool still: false
    property bool blink: false
    property bool patting: false
    property bool dragMoved: false
    signal patted()
    signal menuRequested()
    signal dragStarted()
    signal dragged(point pointer)
    signal dragFinished()

    Rectangle {
        id: speech
        x: 8; y: 8; width: parent.width - 16
        height: Math.max(58, caption.implicitHeight + 26)
        visible: root.bubble.length > 0
        radius: 15
        color: "#f4eee8"
        border.color: "#483f55"
        border.width: 2
        Rectangle {
            width: 13; height: 13; rotation: 45
            anchors.bottom: parent.bottom; anchors.bottomMargin: -5
            anchors.right: parent.right; anchors.rightMargin: 51
            color: parent.color
        }
        Text {
            id: caption
            anchors.fill: parent; anchors.margins: 13
            text: root.bubble
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: "#352d43"
            font.family: "sans-serif"
            font.pixelSize: 13
            font.weight: Font.Medium
            verticalAlignment: Text.AlignVCenter
        }
        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.RightButton
            onClicked: root.menuRequested()
        }
    }

    Item {
        id: buddy
        width: 96; height: 100
        anchors.right: parent.right; anchors.rightMargin: 25
        anchors.bottom: parent.bottom; anchors.bottomMargin: 10
        transformOrigin: Item.Bottom
        scale: root.patting ? 1.08 : 1
        Behavior on scale { NumberAnimation { duration: root.still ? 0 : 120 } }

        Rectangle { x: 13; y: 90; width: 72; height: 7; radius: 4; color: "#40372a49" }
        Rectangle { x: 44; y: 1; width: 5; height: 15; color: "#4b3e5c" }
        Rectangle { x: 39; y: 0; width: 15; height: 10; radius: 4; color: "#99ebcd"; border.color: "#463b54"; border.width: 2 }
        Rectangle { x: 6; y: 34; width: 12; height: 21; radius: 4; color: "#a997c3"; border.color: "#463b54"; border.width: 2 }
        Rectangle { x: 78; y: 34; width: 12; height: 21; radius: 4; color: "#a997c3"; border.color: "#463b54"; border.width: 2 }
        Rectangle {
            x: 13; y: 15; width: 70; height: 58; radius: 15
            color: "#c9b8df"; border.color: "#463b54"; border.width: 3
            Rectangle { x: 8; y: 6; width: 36; height: 4; radius: 2; color: "#e7dcef" }
            Rectangle {
                x: 8; y: 17; width: 54; height: 29; radius: 9
                color: "#302d3e"
                Rectangle { x: 12; y: root.blink ? 14 : 9; width: 7; height: root.blink ? 2 : 10; radius: 3; color: "#a6f3d3" }
                Rectangle { x: 35; y: root.blink ? 14 : 9; width: 7; height: root.blink ? 2 : 10; radius: 3; color: "#a6f3d3" }
                Rectangle { x: 24; y: 20; width: 8; height: 2; radius: 1; color: "#a6f3d3" }
                Rectangle { x: 6; y: 20; width: 7; height: 3; radius: 1; color: "#c78fa8" }
                Rectangle { x: 43; y: 20; width: 7; height: 3; radius: 1; color: "#c78fa8" }
            }
        }
        Rectangle { x: 28; y: 71; width: 40; height: 17; radius: 6; color: "#a997c3"; border.color: "#463b54"; border.width: 2 }
        Rectangle { x: 44; y: 76; width: 8; height: 5; radius: 2; color: root.mood === "thinking" ? "#f1cd86" : "#a6f3d3" }
        Rectangle { x: 24; y: 83; width: 18; height: 10; radius: 4; color: "#5f5074"; border.color: "#463b54"; border.width: 2 }
        Rectangle { x: 54; y: 83; width: 18; height: 10; radius: 4; color: "#5f5074"; border.color: "#463b54"; border.width: 2 }

        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.LeftButton | Qt.RightButton
            cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
            property point start
            property bool moved: false
            onPressed: function(mouse) {
                start = Qt.point(mouse.x, mouse.y); moved = false
                if (mouse.button === Qt.LeftButton) root.dragStarted()
            }
            onPositionChanged: function(mouse) {
                if (pressed && (pressedButtons & Qt.LeftButton)) {
                    if (Math.abs(mouse.x - start.x) + Math.abs(mouse.y - start.y) > 5) moved = true
                    if (moved) root.dragged(mapToItem(root, mouse.x, mouse.y))
                }
            }
            onReleased: function(mouse) {
                root.dragFinished()
                if (mouse.button === Qt.RightButton) root.menuRequested()
                else if (!moved && !root.dragMoved) { root.patting = true; patTimer.restart(); root.patted() }
            }
            onCanceled: root.dragFinished()
        }
    }
    Timer { id: patTimer; interval: 350; onTriggered: root.patting = false }
    Timer {
        interval: 4800; repeat: true; running: root.visible && !root.still
        onTriggered: { root.blink = true; blinkEnd.restart() }
    }
    Timer { id: blinkEnd; interval: 150; onTriggered: root.blink = false }
    onStillChanged: { if (still) { blinkEnd.stop(); blink = false; patting = false } }
}
