import QtQuick

Item {
    id: root
    property color color: "#091521"
    property color accent: "#66e5ff"
    property color lineColor: "#294456"
    property real cut: 14
    property bool technical: true
    property real tint: 0.07
    onVisibleChanged: if (visible) frame.requestPaint()
    onColorChanged: frame.requestPaint()
    onAccentChanged: frame.requestPaint()
    onLineColorChanged: frame.requestPaint()
    onCutChanged: frame.requestPaint()
    onTechnicalChanged: frame.requestPaint()
    onTintChanged: frame.requestPaint()
    Canvas {
        id: frame
        anchors.fill: parent
        antialiasing: true
        onAvailableChanged: if (available) requestPaint()
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
            const c = getContext("2d"); c.reset()
            const w = width - 1, h = height - 1, k = Math.min(root.cut, h / 3, w / 3)
            c.beginPath(); c.moveTo(k, 0.5); c.lineTo(w, 0.5); c.lineTo(w, h-k)
            c.lineTo(w-k, h); c.lineTo(0.5, h); c.lineTo(0.5, k); c.closePath()
            c.fillStyle = root.color; c.fill(); c.strokeStyle = root.lineColor; c.lineWidth = 1; c.stroke()
            c.save(); c.clip()
            const g = c.createLinearGradient(0,0,w,h)
            g.addColorStop(0, Qt.rgba(root.accent.r,root.accent.g,root.accent.b,root.tint))
            g.addColorStop(0.6, "transparent")
            c.fillStyle = g; c.fillRect(0,0,w,h); c.restore()
            c.strokeStyle = root.accent; c.lineWidth = 2
            c.beginPath(); c.moveTo(0.5,k+22); c.lineTo(0.5,k); c.lineTo(k,0.5); c.lineTo(k+32,0.5); c.stroke()
            c.globalAlpha = 0.6
            c.beginPath(); c.moveTo(w-50,h); c.lineTo(w-k,h); c.lineTo(w,h-k); c.lineTo(w,h-k-16); c.stroke()
            if(root.technical && h > 80) {
                c.globalAlpha = 0.35; c.lineWidth = 1
                for(let i=0;i<7;++i) { c.beginPath(); c.moveTo(w-12-i*5,7); c.lineTo(w-15-i*5,11); c.stroke() }
                c.fillStyle = root.accent; c.fillRect(10,h-6,30,1); c.fillRect(44,h-6,6,1)
            }
        }
    }
}
