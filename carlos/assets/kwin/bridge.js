/* E.V. runtime KWin bridge. Loaded only while the local E.V. core is running. */

const EV_SERVICE = "com.ev.Core";
const EV_PATH = "/com/ev/KWinBridge";
const EV_INTERFACE = "com.ev.KWinBridge";

function rectangle(value) {
    return {
        x: Math.round(value.x),
        y: Math.round(value.y),
        width: Math.round(value.width),
        height: Math.round(value.height)
    };
}

function isMaximized(window) {
    try {
        const area = workspace.clientArea(KWin.MaximizeArea, window);
        const geometry = window.frameGeometry;
        return Math.abs(geometry.x - area.x) <= 1 &&
            Math.abs(geometry.y - area.y) <= 1 &&
            Math.abs(geometry.width - area.width) <= 1 &&
            Math.abs(geometry.height - area.height) <= 1;
    } catch (error) {
        return false;
    }
}

function serializeOutput(output) {
    if (!output) return null;
    return {
        name: String(output.name || ""),
        manufacturer: String(output.manufacturer || ""),
        model: String(output.model || ""),
        serial_number: String(output.serialNumber || ""),
        scale: Number(output.devicePixelRatio || 1),
        geometry: rectangle(output.geometry)
    };
}

function serializeWindow(window) {
    const desktops = [];
    for (let i = 0; i < window.desktops.length; ++i) {
        desktops.push(String(window.desktops[i].id));
    }
    return {
        id: String(window.internalId),
        pid: Number(window.pid || 0),
        title: String(window.caption || ""),
        app_id: String(window.desktopFileName || ""),
        resource_class: String(window.resourceClass || ""),
        resource_name: String(window.resourceName || ""),
        role: String(window.windowRole || ""),
        geometry: rectangle(window.frameGeometry),
        client_geometry: rectangle(window.clientGeometry),
        output: window.output ? String(window.output.name || "") : "",
        desktops: desktops,
        active: Boolean(window.active),
        minimized: Boolean(window.minimized),
        fullscreen: Boolean(window.fullScreen),
        maximized: isMaximized(window),
        tiled: Boolean(window.tile),
        normal: Boolean(window.normalWindow),
        dialog: Boolean(window.dialog),
        special: Boolean(window.specialWindow),
        closeable: Boolean(window.closeable),
        moveable: Boolean(window.moveable),
        resizeable: Boolean(window.resizeable),
        maximizable: Boolean(window.maximizable),
        on_all_desktops: Boolean(window.onAllDesktops),
        unresponsive: Boolean(window.unresponsive),
        stacking_order: Number(window.stackingOrder || 0)
    };
}

function findWindow(id) {
    const windows = workspace.stackingOrder;
    for (let i = 0; i < windows.length; ++i) {
        if (String(windows[i].internalId) === String(id)) return windows[i];
    }
    throw new Error("Window no longer exists: " + id);
}

function findOutput(name) {
    const outputs = workspace.screens;
    for (let i = 0; i < outputs.length; ++i) {
        if (String(outputs[i].name) === String(name)) return outputs[i];
    }
    throw new Error("Output is unavailable: " + name);
}

function findDesktop(id) {
    const desktops = workspace.desktops;
    for (let i = 0; i < desktops.length; ++i) {
        if (String(desktops[i].id) === String(id)) return desktops[i];
    }
    throw new Error("Virtual desktop is unavailable: " + id);
}

function snapshot() {
    const windows = [];
    const outputs = [];
    const desktops = [];
    const stack = workspace.stackingOrder;
    for (let i = 0; i < stack.length; ++i) windows.push(serializeWindow(stack[i]));
    for (let j = 0; j < workspace.screens.length; ++j) outputs.push(serializeOutput(workspace.screens[j]));
    for (let k = 0; k < workspace.desktops.length; ++k) {
        desktops.push({id: String(workspace.desktops[k].id), name: String(workspace.desktops[k].name || "")});
    }
    return {
        windows: windows,
        outputs: outputs,
        desktops: desktops,
        active_window_id: workspace.activeWindow ? String(workspace.activeWindow.internalId) : "",
        active_output: workspace.activeScreen ? String(workspace.activeScreen.name || "") : "",
        current_desktop: workspace.currentDesktop ? String(workspace.currentDesktop.id) : "",
        cursor: {x: Math.round(workspace.cursorPos.x), y: Math.round(workspace.cursorPos.y)}
    };
}

function execute(command) {
    if (command.deadline_unix_ms && Date.now() > Number(command.deadline_unix_ms))
        throw new Error("E.V. request expired before execution");
    const action = String(command.action || "");
    const args = command.arguments || {};
    if (action === "ping") return {pong: true};
    if (action === "snapshot") return snapshot();
    if (action === "workspace_switch") {
        workspace.currentDesktop = findDesktop(String(args.desktop_id));
        return {desktop_id: String(workspace.currentDesktop.id)};
    }
    const window = findWindow(args.window_id);
    let details = {};
    if (action === "activate") {
        window.minimized = false;
        workspace.activeWindow = window;
        workspace.raiseWindow(window);
    } else if (action === "move_resize") {
        const targetGeometry = {
            x: Number(args.x), y: Number(args.y),
            width: Number(args.width), height: Number(args.height)
        };
        window.tile = null;
        // KWin's native method accepts an explicit restore rectangle.  Supplying
        // it is important for windows that were launched maximized and therefore
        // have no useful historic restore geometry.
        window.setMaximize(false, false, targetGeometry);
        window.frameGeometry = targetGeometry;
    } else if (action === "minimize") {
        window.minimized = true;
    } else if (action === "maximize") {
        window.minimized = false;
        window.fullScreen = false;
        window.setMaximize(true, true);
    } else if (action === "restore") {
        window.minimized = false;
        window.fullScreen = false;
        if (isMaximized(window)) {
            workspace.activeWindow = window;
            workspace.raiseWindow(window);
            workspace.slotWindowMaximize();
        } else {
            window.setMaximize(false, false);
        }
        // KWin 6 represents quick/custom tiled placement with Window.tile.
        // A tiled window ignores arbitrary frameGeometry until detached.
        window.tile = null;
    } else if (action === "fullscreen") {
        window.minimized = false;
        window.fullScreen = Boolean(args.enabled);
    } else if (action === "close") {
        if (!window.closeable) throw new Error("Window is not closeable");
        window.closeWindow();
    } else if (action === "move_to_output") {
        const output = findOutput(args.output);
        // This native primitive preserves KWin's own maximize/tile semantics and
        // is more reliable than synthesizing cross-output coordinates.
        workspace.sendClientToScreen(window, output);
    } else if (action === "move_to_desktop") {
        window.desktops = [findDesktop(args.desktop_id)];
    } else if (action === "layout") {
        const layout = String(args.layout || "");
        const area = workspace.clientArea(KWin.MaximizeArea, window);
        const current = window.frameGeometry;
        let targetGeometry = null;
        if (layout === "center") {
            const width = Math.min(Math.round(current.width), Math.round(area.width));
            const height = Math.min(Math.round(current.height), Math.round(area.height));
            targetGeometry = {
                x: Math.round(area.x + (area.width - width) / 2),
                y: Math.round(area.y + (area.height - height) / 2),
                width: width,
                height: height
            };
        } else if (layout === "left" || layout === "right") {
            const width = Math.floor(area.width / 2);
            targetGeometry = {
                x: layout === "left" ? Math.round(area.x) : Math.round(area.x + area.width - width),
                y: Math.round(area.y), width: width, height: Math.round(area.height)
            };
        } else if (layout === "top" || layout === "bottom") {
            const height = Math.floor(area.height / 2);
            targetGeometry = {
                x: Math.round(area.x),
                y: layout === "top" ? Math.round(area.y) : Math.round(area.y + area.height - height),
                width: Math.round(area.width), height: height
            };
        } else if (["top-left", "top-right", "bottom-left", "bottom-right"].indexOf(layout) !== -1) {
            const width = Math.floor(area.width / 2);
            const height = Math.floor(area.height / 2);
            targetGeometry = {
                x: layout.endsWith("left") ? Math.round(area.x) : Math.round(area.x + area.width - width),
                y: layout.startsWith("top") ? Math.round(area.y) : Math.round(area.y + area.height - height),
                width: width, height: height
            };
        } else {
            throw new Error("Unsupported window layout: " + layout);
        }
        window.tile = null;
        window.fullScreen = false;
        window.setMaximize(false, false, targetGeometry);
        window.frameGeometry = targetGeometry;
        details = {layout: layout, target_geometry: rectangle(targetGeometry)};
    } else {
        throw new Error("Unsupported KWin action: " + action);
    }
    details.requested = action;
    details.window = serializeWindow(window);
    return details;
}

function poll() {
    callDBus(EV_SERVICE, EV_PATH, EV_INTERFACE, "NextCommand", function(raw) {
        if (!raw) {
            poll();
            return;
        }
        let command = null;
        let reply = null;
        try {
            command = JSON.parse(String(raw));
            reply = {id: String(command.id), ok: true, result: execute(command)};
        } catch (error) {
            reply = {id: command ? String(command.id) : "invalid", ok: false, error: String(error)};
        }
        callDBus(EV_SERVICE, EV_PATH, EV_INTERFACE, "Report", JSON.stringify(reply), function() { poll(); });
    });
}

poll();
