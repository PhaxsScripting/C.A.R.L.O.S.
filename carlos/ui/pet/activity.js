// App id and monitor name. Window titles stay out of this.
function report() {
    const window = workspace.activeWindow;
    const cursor = workspace.cursorPos;
    const screen = workspace.screens.find(function(output) {
        const r = output.geometry;
        return cursor.x >= r.x && cursor.x < r.x + r.width &&
               cursor.y >= r.y && cursor.y < r.y + r.height;
    });
    callDBus("org.phax.CarlosPet", "/Pet", "org.phax.CarlosPet", "Observe",
             window ? String(window.resourceClass).slice(0, 160) : "",
             Boolean(window && window.fullScreen && !window.minimized),
             screen ? String(screen.name) : "");
}
function watch(window) {
    if (String(window.resourceClass) === "ev-pet") return;
    window.fullScreenChanged.connect(report);
    window.minimizedChanged.connect(report);
    window.windowClassChanged.connect(report);
}
workspace.windowList().forEach(watch);
workspace.windowAdded.connect(watch);
workspace.windowActivated.connect(report);
workspace.currentDesktopChanged.connect(report);
report();
