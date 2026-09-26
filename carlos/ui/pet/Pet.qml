import QtQuick
import QtQuick.Controls
import org.kde.layershell as LayerShell

Window {
    id: petWindow
    width: 242
    height: 202
    visible: pet.shown
    color: "transparent"
    title: "Carlos Pet"
    flags: Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus | Qt.Tool
    LayerShell.Window.scope: "carlos-pet"
    LayerShell.Window.layer: LayerShell.Window.LayerOverlay
    LayerShell.Window.anchors: LayerShell.Window.AnchorBottom | LayerShell.Window.AnchorRight
    LayerShell.Window.exclusionZone: 0
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
    LayerShell.Window.activateOnShow: false

    PetSprite {
        id: sprite
        anchors.fill: parent
        bubble: pet.bubble
        still: pet.still || !petWindow.visible
        mood: pet.mood
        dragMoved: pet.dragMoved
        onPatted: pet.pet()
        onMenuRequested: pet.menu()
        onDragStarted: pet.beginDrag()
        onDragFinished: pet.endDrag()
    }
}
