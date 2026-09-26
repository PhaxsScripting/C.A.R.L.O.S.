// Only the app id leaves KWin. Window titles stay out of this.
function report() {
    const window = workspace.activeWindow;
    callDBus("org.phax.CarlosPet", "/Pet", "org.phax.CarlosPet", "Observe",
             window ? String(window.resourceClass).slice(0, 160) : "",
             Boolean(window && window.fullScreen && !window.minimized));
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
