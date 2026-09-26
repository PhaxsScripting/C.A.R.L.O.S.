// Only loaded while you're holding the buddy. Qt guesses global positions on Wayland.
function pointerMoved() {
    const point = workspace.cursorPos;
    callDBus("org.phax.CarlosPet", "/Pet", "org.phax.CarlosPet", "DragPointer",
             point.x, point.y, @SERIAL@);
}
const buddy = workspace.windowList().find(function(window) { return window.pid === @PID@; });
if (buddy) {
    workspace.cursorPosChanged.connect(pointerMoved);
    workspace.windowRemoved.connect(function(window) {
        if (window === buddy) workspace.cursorPosChanged.disconnect(pointerMoved);
    });
    pointerMoved();
}
