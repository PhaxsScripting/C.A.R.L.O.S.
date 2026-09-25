pragma ComponentBehavior: Bound

import QtQuick

Item {
    id: root
    property var activeNodes: []
    property string coreState: "DORMANT"
    property bool animateAmbient: true
    property color cyan: "#39dcff"
    property color blue: "#427dff"
    property color muted: "#294255"

    readonly property var nodes: [
        { name: "VOICE", x: 0.50, y: 0.08 },
        { name: "LANGUAGE", x: 0.80, y: 0.20 },
        { name: "REASONING", x: 0.92, y: 0.50 },
        { name: "MEMORY", x: 0.79, y: 0.80 },
        { name: "SECURITY", x: 0.50, y: 0.92 },
        { name: "FILES", x: 0.20, y: 0.80 },
        { name: "SYSTEM", x: 0.08, y: 0.50 },
        { name: "APPLICATIONS", x: 0.20, y: 0.20 },
        { name: "TOOL ROUTER", x: 0.72, y: 0.50 }
    ]

    function isActive(name) { return activeNodes && activeNodes.indexOf(name) >= 0 }

    Canvas {
        id: links
        anchors.fill: parent
        antialiasing: true
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            const cx = width * 0.5
            const cy = height * 0.5
            for (let i = 0; i < root.nodes.length; ++i) {
                const node = root.nodes[i]
                const active = root.isActive(node.name)
                ctx.beginPath()
                ctx.moveTo(cx, cy)
                ctx.lineTo(width * node.x, height * node.y)
                ctx.strokeStyle = active ? root.cyan : root.muted
                ctx.globalAlpha = active ? 0.9 : 0.30
                ctx.lineWidth = active ? 2.0 : 0.8
                ctx.stroke()
            }
            ctx.globalAlpha = 1
        }
    }

    Connections {
        target: root
        function onActiveNodesChanged() { links.requestPaint() }
        function onWidthChanged() { links.requestPaint() }
        function onHeightChanged() { links.requestPaint() }
    }

    Rectangle {
        id: orbit
        width: Math.min(root.width, root.height) * 0.37
        height: width
        anchors.centerIn: parent
        radius: width / 2
        color: "transparent"
        border.width: 1
        border.color: "#2a88a7"
        opacity: 0.5
        Rectangle { width: 8; height: 8; radius: 4; color: root.cyan; anchors.horizontalCenter: parent.horizontalCenter; y: -4 }
        RotationAnimator on rotation { from: 0; to: 360; duration: 18000; loops: Animation.Infinite; running: root.animateAmbient }
    }

    Rectangle {
        width: Math.min(root.width, root.height) * 0.25
        height: width
        anchors.centerIn: parent
        radius: width / 2
        color: root.coreState === "DORMANT" ? "#101f2b" : "#11374a"
        border.width: root.coreState === "DORMANT" ? 1 : 2
        border.color: root.coreState === "ERROR" ? "#ff586d" : root.cyan
        scale: root.coreState === "DORMANT" ? 1.0 : 1.04
        Behavior on scale { NumberAnimation { duration: 220 } }
        Behavior on color { ColorAnimation { duration: 240 } }

        Rectangle {
            anchors.centerIn: parent
            width: parent.width * 0.68
            height: width
            radius: width / 2
            color: "#07111a"
            border.color: "#31576b"
        }
        Column {
            anchors.centerIn: parent
            spacing: 3
            Text { anchors.horizontalCenter: parent.horizontalCenter; text: "Carlos"; color: "#effcff"; font.pixelSize: 30; font.bold: true; font.letterSpacing: 4 }
            Text { anchors.horizontalCenter: parent.horizontalCenter; text: root.coreState; color: root.cyan; font.pixelSize: 9; font.letterSpacing: 1.4 }
        }
    }

    Repeater {
        model: root.nodes
        delegate: Rectangle {
            required property var modelData
            width: modelData.name.length > 10 ? 116 : 92
            height: 34
            x: root.width * modelData.x - width / 2
            y: root.height * modelData.y - height / 2
            radius: 7
            color: root.isActive(modelData.name) ? "#123b4c" : "#0b151e"
            border.width: root.isActive(modelData.name) ? 2 : 1
            border.color: root.isActive(modelData.name) ? root.cyan : "#294255"
            Behavior on color { ColorAnimation { duration: 140 } }
            Text {
                anchors.centerIn: parent
                text: modelData.name
                color: root.isActive(modelData.name) ? "#eaffff" : "#718da0"
                font.pixelSize: 9
                font.bold: true
                font.letterSpacing: 0.9
            }
        }
    }

    Text {
        anchors.left: parent.left
        anchors.bottom: parent.bottom
        anchors.margins: 8
        text: "AMBIENT ORBIT  //  EVENT NODES ARE LIVE"
        color: "#42677a"
        font.pixelSize: 8
        font.letterSpacing: 1.1
    }
}
