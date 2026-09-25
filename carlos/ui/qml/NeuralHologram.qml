pragma ComponentBehavior: Bound
import QtQuick

Item {
    id: root
    property bool active: false
    property bool animate: false
    property bool compact: false
    property string state: "DORMANT"
    property color accent: "#75e8ff"
    readonly property real diameter: Math.min(width * .84, height * .93)
    Canvas {
        id: reticle
        anchors.fill: parent; antialiasing: true
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
            const c = getContext("2d"); c.reset()
            const cx=width/2, cy=height*.47, r=root.diameter*.47
            c.strokeStyle="#54bbdc"
            for(let ring=0;ring<5;++ring) {
                c.globalAlpha=ring===0?.55:.2; c.lineWidth=ring===0?1.5:.6
                c.beginPath(); c.arc(cx,cy,r*(.79+ring*.065),0,Math.PI*2); c.stroke()
            }
            for(let i=0;i<120;++i) {
                const a=i*Math.PI/60, major=i%10===0
                c.globalAlpha=major?.85:.24; c.lineWidth=major?2:1
                c.beginPath(); c.moveTo(cx+Math.cos(a)*r*1.065,cy+Math.sin(a)*r*1.065)
                c.lineTo(cx+Math.cos(a)*r*(major?1.11:1.08),cy+Math.sin(a)*r*(major?1.11:1.08)); c.stroke()
            }
            c.globalAlpha=.22
            c.beginPath(); c.moveTo(cx-r*1.3,cy); c.lineTo(cx+r*1.3,cy); c.stroke()
            c.beginPath(); c.moveTo(cx,cy-r*1.17); c.lineTo(cx,cy+r*1.17); c.stroke()
            for(let row=0;row<4;++row) {
                c.globalAlpha=.2-row*.035
                c.beginPath(); c.ellipse(cx-r*.9, height*.86+row*6, r*1.8,r*.15); c.stroke()
            }
        }
    }
    Item {
        anchors.horizontalCenter: parent.horizontalCenter
        y: parent.height*.47-height/2
        width: root.diameter; height: width
        Canvas {
            anchors.fill: parent; antialiasing: true
            onWidthChanged: requestPaint(); onHeightChanged: requestPaint()
            onPaint: {
                const c=getContext("2d"); c.reset(); const r=width*.475
                c.strokeStyle=root.accent; c.lineWidth=3; c.globalAlpha=.75
                for(let i=0;i<3;++i) { c.beginPath(); c.arc(width/2,height/2,r,i*2.094+.1,i*2.094+.8); c.stroke() }
                c.lineWidth=1; c.globalAlpha=.4; c.beginPath(); c.arc(width/2,height/2,r*.9,1,2.7); c.stroke()
            }
        }
        RotationAnimator on rotation { from: 0; to: 360; duration: 70000; loops: Animation.Infinite; running: root.visible && root.animate }
    }
    Image {
        id: anatomy
        width: parent.width * .94; height: parent.height * .79
        anchors.centerIn: parent; anchors.verticalCenterOffset: -parent.height*.03
        source: Qt.resolvedUrl(".").toString().indexOf("file:") === 0 ? Qt.resolvedUrl("../assets/ev-neural-brain.png") : "qrc:/qt/qml/EV/ControlCenter/assets/ev-neural-brain.png"
        fillMode: Image.PreserveAspectFit; sourceSize.width: root.compact ? 780 : 1180
        smooth: true; mipmap: true; opacity: root.active ? .92 : .72
        Behavior on opacity { NumberAnimation { duration: 250 } }
    }
    // Static optical scan texture: no shader, particles, or per-frame JS.
    Canvas {
        anchors.fill: anatomy; antialiasing: true
        onWidthChanged: requestPaint(); onHeightChanged: requestPaint()
        onPaint: {
            const c=getContext("2d"); c.reset(); c.save()
            c.beginPath(); c.ellipse(width*.19,height*.2,width*.62,height*.64); c.clip()
            c.strokeStyle="#9df3ff"; c.lineWidth=.6; c.globalAlpha=.16
            for(let y=0;y<height;y+=7) { c.beginPath(); c.moveTo(0,y); c.lineTo(width,y); c.stroke() }
            c.restore()
            c.strokeStyle="#6bc9e1"; c.globalAlpha=.55
            for(let i=0;i<6;++i) {
                const x=width*(.28+(i%3)*.19), y=height*(.28+Math.floor(i/3)*.33)
                c.beginPath(); c.arc(x,y,3,0,Math.PI*2); c.stroke()
                c.beginPath(); c.moveTo(x-7,y); c.lineTo(x+7,y); c.moveTo(x,y-7); c.lineTo(x,y+7); c.stroke()
            }
        }
    }
    Text {
        anchors.horizontalCenter: parent.horizontalCenter; y: parent.height*.94
        text: root.compact ? root.state : "NEURAL ASSEMBLY    /    " + root.state
        color: root.accent; font.family: "Hack"; font.pixelSize: root.compact ? 9 : 11; font.letterSpacing: 2
    }
}
