import QtQuick
import QtQuick.Controls

Button {
    id: control
    property color accent: "#67e4ff"
    Accessible.name: text
    Accessible.role: Accessible.Button
    implicitHeight: 39
    leftPadding: 16; rightPadding: 16; topPadding: 10; bottomPadding: 10
    font.family: "Hack"; font.pixelSize: 10; font.bold: true; font.letterSpacing: 0.7
    contentItem: Text {
        text: control.text; color: control.enabled ? "#dff9ff" : "#728491"
        font: control.font
        horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    background: HudPanel {
        color: control.down ? "#254557" : control.hovered ? "#183242" : "#0c1c29"
        accent: control.enabled ? control.accent : "#526574"
        lineColor: control.activeFocus ? "#e6fcff" : control.hovered ? control.accent : "#345363"
        cut: 8; technical: false; tint: control.hovered ? 0.15 : 0.06
    }
}
