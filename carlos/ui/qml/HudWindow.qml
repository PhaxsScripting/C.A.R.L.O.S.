import QtQuick
import org.kde.layershell as LayerShell
Window {
 LayerShell.Window.scope: "ev-voice-hud"
 LayerShell.Window.layer: LayerShell.Window.LayerOverlay
 LayerShell.Window.anchors: LayerShell.Window.AnchorBottom
 LayerShell.Window.exclusionZone: 0
 LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
 LayerShell.Window.activateOnShow: false
 readonly property bool passiveSurface: LayerShell.Window.keyboardInteractivity === LayerShell.Window.KeyboardInteractivityNone && !LayerShell.Window.activateOnShow
}
