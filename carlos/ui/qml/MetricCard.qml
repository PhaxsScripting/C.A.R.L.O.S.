pragma ComponentBehavior: Bound
import QtQuick

HudPanel {
    id: root
    required property string label
    property string value: "—"
    property string detail: "LIVE SENSOR"
    readonly property bool percentage: value.indexOf("%") >= 0 && isFinite(parseFloat(value))
    readonly property real proportion: percentage ? Math.max(0,Math.min(1,parseFloat(value)/100)) : 0
    implicitWidth: 150; implicitHeight: 106
    color: "#0a1927"; lineColor: "#2c475b"; cut: 12
    Column {
        anchors.fill: parent; anchors.margins: 15; spacing: root.height > 140 ? 12 : 7
        Text { width: parent.width; text: root.label; color: "#8caabd"; font.family: "Hack"; font.pixelSize: 9; font.letterSpacing: 1.2; elide: Text.ElideRight }
        Text { width: parent.width; height: root.height > 140 ? 44 : 29; text: root.value; color: "#edfaff"; font.family: "Hack"; font.pixelSize: root.height > 140 ? 34 : 24; minimumPixelSize: 12; fontSizeMode: Text.HorizontalFit; font.weight: Font.Medium; elide: Text.ElideRight }
        Text { width: parent.width; text: root.detail; color: root.accent; opacity: .85; font.family: "Hack"; font.pixelSize: 8; elide: Text.ElideRight }
    }
    Row {
        id: meter
        anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; anchors.margins: 14
        spacing: 3; visible: root.percentage && root.height > 120
        Repeater {
            model: 24
            Rectangle {
                required property int index
                width: (meter.width-23*3)/24; height: 8
                color: root.proportion * 24 > index ? root.accent : "#193447"
                opacity: root.proportion * 24 > index ? .75 : .55
            }
        }
    }
}
