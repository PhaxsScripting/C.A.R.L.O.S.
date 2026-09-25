import QtQuick
import QtQuick.Controls

TextField {
    id: control
    color: "#e2f5fc"; placeholderTextColor: "#8099a9"
    font.family: "Liberation Sans"; font.pixelSize: 13
    padding: 12; selectByMouse: true
    selectionColor: "#275a71"; selectedTextColor: "#ffffff"
    background: HudPanel {
        color: "#070f19"; cut: 7; technical: false
        accent: control.activeFocus ? "#70e7ff" : "#375569"
        lineColor: control.activeFocus ? "#70e7ff" : "#294455"
    }
}
