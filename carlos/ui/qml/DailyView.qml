pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    required property var client
    property var dashboardData: client.daily || ({})
    property bool initialPicker: false
    onInitialPickerChanged: if (initialPicker) Qt.callLater(function() { scenePicker.open() })
    Connections {
        target: root.client
        function onToolResultChanged() {
            if (root.client.toolResult.preview_id) previewId.text = root.client.toolResult.preview_id
        }
    }
    component ActionButton: SciButton { }
    component Field: SciField { }
    onVisibleChanged: if (visible) client.refreshDaily()
    Component.onCompleted: if (visible) client.refreshDaily()
    Timer { interval: 15000; running: root.visible; repeat: true; onTriggered: root.client.refreshDaily() }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 24; spacing: 16
        RowLayout {
            Layout.fillWidth: true
            Column {
                Text { text: "PERSONAL SYSTEMS"; color: "#e8f8ff"; font.pixelSize: 22; font.bold: true; font.letterSpacing: 2 }
                Text { text: "LIBRARY  /  SETTINGS  /  SCENES  /  ROUTINES"; color: "#8caabd"; font.pixelSize: 10; font.letterSpacing: 1.5 }
            }
            Item { Layout.fillWidth: true }
            ActionButton { objectName: "open-scene-picker"; text: "CHANGE SCENE"; onClicked: { root.client.refreshDaily(); scenePicker.open() } }
            ActionButton { text: "REFRESH"; onClicked: root.client.refreshDaily() }
        }
        Text { Layout.fillWidth: true; text: root.client.statusMessage; color: "#68dbe8"; wrapMode: Text.WordWrap; maximumLineCount: 3 }
        RowLayout {
            Layout.fillWidth: true
            Field { id: command; Layout.fillWidth: true; placeholderText: 'Try: remind me to stretch every 30 minutes'; onAccepted: run.clicked() }
            ActionButton { id: run; text: "DO IT"; onClicked: { root.client.sendCommand(command.text); command.clear() } }
        }
        HudPanel {
            Layout.fillWidth: true; Layout.fillHeight: true; color: "#081725"; lineColor: "#305168"
            ScrollView {
                id: dailyScroll
                anchors.fill: parent; anchors.margins: 18; clip: true
                contentWidth: availableWidth
                ColumnLayout {
                    width: dailyScroll.availableWidth; spacing: 18
                    Text { visible: (root.client.insights || []).length > 0; text: "SYSTEM INSIGHTS / LOCAL"; color: "#ffca58"; font.pixelSize: 14; font.bold: true }
                    Repeater {
                        objectName: "system-insights"
                        model: root.client.insights || []
                        delegate: ColumnLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            Text { text: modelData.title; color: "#e8f8ff"; font.bold: true }
                            Text { Layout.fillWidth: true; text: modelData.detail; color: "#91adbf"; wrapMode: Text.WordWrap }
                            RowLayout {
                                ActionButton { objectName: "insight-inspect-" + modelData.id; text: "INSPECT"; onClicked: root.client.callTool(modelData.action.tool, modelData.action.arguments) }
                                ActionButton { objectName: "insight-dismiss-" + modelData.id; text: "HIDE FOR 1 HOUR"; onClicked: root.client.callTool("agent.insights.dismiss", {id: modelData.id}) }
                                Text { text: "Nothing runs automatically"; color: "#6a8799"; font.pixelSize: 10 }
                            }
                        }
                    }
                    Text { text: "CARLOS SCENES"; color: "#b5eaff"; font.pixelSize: 14; font.bold: true }
                    Text { Layout.fillWidth: true; wrapMode: Text.WordWrap; color: "#91adbf"; text: "Choose a scene to run, or edit its commands below. Active: " + ((root.dashboardData.active_scene || {}).name || "none") }
                    RowLayout {
                        Layout.fillWidth: true
                        ComboBox {
                            id: assistantScene; Layout.fillWidth: true
                            model: Object.keys(root.dashboardData.assistant_scenes || {})
                            onActivated: {
                                const scene = root.dashboardData.assistant_scenes[currentText];
                                assistantSceneName.text = currentText;
                                assistantSceneDescription.text = scene.description || "";
                                assistantSceneCommands.text = (scene.commands || []).join("\n");
                                assistantSceneHud.currentIndex = assistantSceneHud.model.indexOf(scene.hud);
                                assistantSceneQuiet.checked = !!scene.quiet;
                            }
                        }
                        ActionButton { text: "RUN SCENE"; enabled: assistantScene.currentText.length > 0; onClicked: root.client.sendCommand("activate " + assistantScene.currentText + " scene") }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Field { id: assistantSceneName; Layout.fillWidth: true; placeholderText: "Scene name" }
                        ComboBox { id: assistantSceneHud; model: ["CARLOS", "PROJECT", "SYSTEM", "MEDIA", "REMOTE"] }
                        CheckBox { id: assistantSceneQuiet; text: "Quiet notifications" }
                    }
                    Field { id: assistantSceneDescription; Layout.fillWidth: true; placeholderText: "Description" }
                    TextArea {
                        id: assistantSceneCommands; Layout.fillWidth: true; Layout.preferredHeight: 80
                        placeholderText: "One command per line, up to eight. Example: open Firefox"
                        color: "#d7f2fa"; placeholderTextColor: "#71909f"; wrapMode: TextEdit.Wrap
                        background: HudPanel { color: "#071320"; lineColor: "#29485d"; cut: 8 }
                    }
                    ActionButton {
                        text: "SAVE SCENE"; enabled: assistantSceneName.text.trim().length > 0
                        onClicked: root.client.callTool("carlos.scenes.save", {
                            name: assistantSceneName.text.trim(), description: assistantSceneDescription.text,
                            commands: assistantSceneCommands.text.split("\n").map(s => s.trim()).filter(s => s.length > 0),
                            hud: assistantSceneHud.currentText, quiet: assistantSceneQuiet.checked
                        })
                    }
                    Text { text: "PRIVACY PROFILE"; color: "#b5eaff"; font.pixelSize: 14; font.bold: true }
                    ComboBox {
                        model: ["NORMAL", "LOCAL ONLY", "PRIVATE SESSION", "DO NOT LISTEN", "GUEST"]
                        currentIndex: Math.max(0, model.indexOf(root.dashboardData.privacy_mode || "NORMAL"))
                        onActivated: root.client.setPrivacyProfile(currentText)
                    }
                    Text { text: "COMPONENT HEALTH"; color: "#b5eaff"; font.pixelSize: 14; font.bold: true }
                    Repeater {
                        model: Object.keys(root.dashboardData.component_health || {})
                        delegate: Text {
                            required property string modelData
                            Layout.fillWidth: true; wrapMode: Text.WordWrap; color: "#91adbf"
                            text: modelData + " — " + root.dashboardData.component_health[modelData].state
                        }
                    }
                    Text { text: "HANDS-FREE LIBRARY"; color: "#b5eaff"; font.pixelSize: 14; font.bold: true }
                    Text { Layout.fillWidth: true; wrapMode: Text.WordWrap; color: "#91adbf"; text: 'Say “add task buy groceries”, “save a note called ideas saying …”, “list my bookmarks”, then “open the second one”. Saved items stay local. Archive is reversible.' }
                    RowLayout {
                        Layout.fillWidth: true
                        ComboBox { id: libraryKind; objectName: "personal-library-kind"; model: ["note", "task", "bookmark", "snippet"]; palette.button: "#0d2532"; palette.buttonText: "#b5efff"; palette.text: "#b5efff"; palette.base: "#0d2532" }
                        Field { id: libraryTitle; objectName: "personal-library-title"; Layout.fillWidth: true; placeholderText: "Unique title" }
                        ActionButton { objectName: "personal-library-save"; text: "SAVE"; enabled: libraryTitle.text.trim().length > 0 && (libraryKind.currentText === "task" || libraryContent.text.trim().length > 0); onClicked: {
                            const prefixes = {note: "notes", task: "tasks", bookmark: "bookmarks", snippet: "snippets"};
                            root.client.callTool(prefixes[libraryKind.currentText] + ".create", {title: libraryTitle.text, content: libraryContent.text});
                        } }
                    }
                    TextArea { id: libraryContent; Layout.fillWidth: true; Layout.preferredHeight: 72; placeholderText: libraryKind.currentText === "bookmark" ? "https://…" : "Note, snippet, or optional task details"; color: "#d7f2fa"; placeholderTextColor: "#71909f"; padding: 10; wrapMode: TextEdit.Wrap; background: HudPanel { color: "#071320"; lineColor: "#29485d"; cut: 8 } }
                    Repeater {
                        model: (root.dashboardData.personal || {})[libraryKind.currentText] || []
                        delegate: RowLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            Text { Layout.fillWidth: true; text: modelData.title; color: "#c5dce5"; elide: Text.ElideRight }
                            ActionButton { text: libraryKind.currentText === "bookmark" ? "OPEN" : "READ"; onClicked: {
                                const prefixes = {note: "notes", task: "tasks", bookmark: "bookmarks", snippet: "snippets"};
                                root.client.callTool(prefixes[libraryKind.currentText] + (libraryKind.currentText === "bookmark" ? ".open" : ".read"), {identifier: modelData.id});
                            } }
                            ActionButton { text: libraryKind.currentText === "task" ? "DONE" : libraryKind.currentText === "snippet" ? "COPY" : "ARCHIVE"; onClicked: {
                                const tools = {note: "notes.archive", task: "tasks.complete", bookmark: "bookmarks.archive", snippet: "snippets.copy"};
                                root.client.callTool(tools[libraryKind.currentText], {identifier: modelData.id});
                            } }
                        }
                    }
                    Text { text: "NATIVE SETTINGS"; color: "#b5eaff"; font.pixelSize: 14; font.bold: true }
                    Text { Layout.fillWidth: true; wrapMode: Text.WordWrap; color: "#91adbf"; text: 'Say “set brightness to fifty”, “turn brightness down by ten”, “set Firefox volume to thirty”, or “open mouse settings”. Turning Wi-Fi/Bluetooth off can disconnect devices. Native authentication still applies.' }
                    Flow {
                        Layout.fillWidth: true; spacing: 8
                        Repeater {
                            objectName: "native-settings-pages"
                            model: ["display", "audio", "network", "bluetooth", "power", "keyboard", "mouse", "touchpad", "notifications", "accessibility", "default apps", "night light"]
                            delegate: ActionButton { required property string modelData; objectName: "settings-page-" + modelData; text: modelData.toUpperCase(); onClicked: root.client.callTool("settings.open", {page: modelData}) }
                        }
                    }
                    Text { text: "QUICK EXAMPLES"; color: "#b5eaff"; font.pixelSize: 14; font.bold: true }
                    Text { Layout.fillWidth: true; wrapMode: Text.WordWrap; color: "#91adbf"; text: '“Calculate 18 percent of 85” · “Convert 10 miles to kilometers” · “What time is it in Tokyo?” · “Show recent downloads” · “Search lo-fi music on YouTube” · “Go to tab three” · “Select all” · “Undo typing” · “Switch to next workspace” · “Snooze reminder stretch for ten minutes”' }
                    Text { text: "REMINDERS & ALARMS"; color: "#b5eaff"; font.pixelSize: 14; font.bold: true }
                    Text { visible: !(root.dashboardData.reminders || []).length; text: "No pending reminders. Alarms run while Carlos is open; overdue reminders appear when it restarts."; color: "#91adbf"; Layout.fillWidth: true; wrapMode: Text.WordWrap }
                    Repeater {
                        model: root.dashboardData.reminders || []
                        delegate: RowLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            Text { Layout.fillWidth: true; text: modelData.label + "  ·  " + new Date(modelData.due * 1000).toLocaleString(); color: "#d6e9ee"; wrapMode: Text.WordWrap }
                            ActionButton { text: "CANCEL"; onClicked: root.client.callTool("reminders.cancel", {identifier: modelData.id}) }
                        }
                    }
                    Text { text: "APP NICKNAMES"; color: "#b5eaff"; font.pixelSize: 14; font.bold: true }
                    RowLayout {
                        Layout.fillWidth: true
                        Field { id: aliasName; Layout.fillWidth: true; placeholderText: "Nickname, e.g. editor" }
                        Field { id: aliasTarget; Layout.fillWidth: true; placeholderText: "Actual app, e.g. code" }
                        ActionButton { text: "SAVE"; enabled: aliasName.text.trim() && aliasTarget.text.trim(); onClicked: root.client.callTool("applications.alias.save", {name: aliasName.text, target: aliasTarget.text}) }
                    }
                    Repeater {
                        model: Object.keys(root.dashboardData.aliases || {})
                        delegate: RowLayout {
                            required property string modelData
                            Layout.fillWidth: true
                            Text { Layout.fillWidth: true; text: modelData + " → " + root.dashboardData.aliases[modelData]; color: "#c5dce5" }
                            ActionButton { text: "EDIT"; onClicked: { aliasName.text = modelData; aliasTarget.text = root.dashboardData.aliases[modelData] } }
                            ActionButton { text: "REMOVE"; onClicked: root.client.callTool("applications.alias.remove", {name: modelData}) }
                        }
                    }
                    Text { text: "WORKSPACE ROUTINES"; color: "#b5eaff"; font.pixelSize: 14; font.bold: true }
                    Field { id: routineName; Layout.fillWidth: true; placeholderText: "Routine name" }
                    TextArea { id: routineCommands; Layout.fillWidth: true; placeholderText: "One command per line, e.g.\nopen Firefox on the second monitor\nmaximize Firefox"; color: "#d7f2fa"; placeholderTextColor: "#71909f"; padding: 12; background: HudPanel { color: "#071320"; lineColor: "#29485d"; cut: 8 } wrapMode: TextEdit.Wrap; Layout.preferredHeight: 95 }
                    ActionButton { text: "SAVE ROUTINE"; enabled: routineName.text.trim() && routineCommands.text.trim(); onClicked: root.client.callTool("routines.save", {name: routineName.text, commands: routineCommands.text.split("\n").filter(function(s) { return s.trim().length > 0 })}) }
                    Repeater {
                        model: Object.keys(root.dashboardData.routines || {})
                        delegate: RowLayout {
                            required property string modelData
                            Layout.fillWidth: true
                            Text { Layout.fillWidth: true; text: modelData; color: "#c5dce5" }
                            ActionButton { text: "RUN"; onClicked: root.client.sendCommand("run routine " + modelData) }
                            ActionButton { text: "EDIT"; onClicked: { routineName.text = modelData; routineCommands.text = root.dashboardData.routines[modelData].join("\n") } }
                            ActionButton { text: "REMOVE"; onClicked: root.client.callTool("routines.remove", {name: modelData}) }
                        }
                    }
                    Text { text: "SPOTIFY"; color: "#b5eaff"; font.pixelSize: 14; font.bold: true }
                    Text { Layout.fillWidth: true; text: (root.dashboardData.spotify || {}).message || "Connection not checked"; color: "#91adbf"; wrapMode: Text.WordWrap }
                    RowLayout {
                        Layout.fillWidth: true
                        Field { id: song; Layout.fillWidth: true; placeholderText: "Song, artist, album or playlist" }
                        ComboBox { id: kind; model: ["track", "artist", "album", "playlist"]; palette.button: "#0d2532"; palette.buttonText: "#b5efff"; palette.text: "#b5efff"; palette.base: "#0d2532" }
                        ActionButton { text: "SEARCH"; enabled: song.text.trim(); onClicked: root.client.callTool("spotify.search", {query: song.text, kind: kind.currentText}) }
                        ActionButton { text: "PLAY"; enabled: song.text.trim(); onClicked: root.client.callTool("spotify.play", {query: song.text, kind: kind.currentText}) }
                    }
                    Repeater {
                        model: (root.client.toolResult.items || []).filter(function(item) { return item.uri && item.uri.indexOf("spotify:") === 0 })
                        delegate: RowLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            Text { Layout.fillWidth: true; text: modelData.name + (modelData.artists ? " · " + modelData.artists.join(", ") : ""); color: "#c4e5ed"; elide: Text.ElideRight }
                            ActionButton { text: "PLAY THIS"; onClicked: root.client.callTool("spotify.play", {query: modelData.uri}) }
                        }
                    }
                    ActionButton { text: "READ RUNTIME FIREWALL AS ADMIN"; onClicked: root.client.sendCommand("inspect runtime firewall as admin") }
                    Text { Layout.fillWidth: true; text: "The OS may request administrator authentication. This reads firewall rules only; it never changes them."; color: "#91adbf"; wrapMode: Text.WordWrap }
                    Text { text: "FILE ORGANIZATION / PREVIEW FIRST"; color: "#b5eaff"; font.pixelSize: 14; font.bold: true }
                    RowLayout {
                        Layout.fillWidth: true
                        Field { id: folder; Layout.fillWidth: true; placeholderText: "Full path to a non-project folder" }
                        ActionButton { text: "PREVIEW"; enabled: folder.text.trim(); onClicked: root.client.callTool("files.organize.preview", {path: folder.text}) }
                    }
                    Text { text: "The result lists exact moves and an ID in Activity. Only unchanged files are eligible; duplicates are reported, never deleted."; color: "#91adbf"; wrapMode: Text.WordWrap; Layout.fillWidth: true }
                    RowLayout {
                        Layout.fillWidth: true
                        Field { id: previewId; Layout.fillWidth: true; placeholderText: "Exact preview ID from Activity" }
                        ActionButton { text: "APPLY"; enabled: /^[a-f0-9]{32}$/.test(previewId.text); onClicked: root.client.callTool("files.organize.apply", {preview_id: previewId.text}) }
                        ActionButton { text: "UNDO"; enabled: /^[a-f0-9]{32}$/.test(previewId.text); onClicked: root.client.callTool("files.organize.undo", {preview_id: previewId.text}) }
                    }
                    Repeater {
                        model: root.client.toolResult.moves || []
                        delegate: Text { required property var modelData; Layout.fillWidth: true; text: modelData.source + "\n  → " + modelData.destination; color: "#aacbd8"; wrapMode: Text.WrapAnywhere; font.pixelSize: 11 }
                    }
                }
            }
        }
    }
    Popup {
        id: scenePicker
        objectName: "scene-picker"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(parent.width - 50, 1250); height: Math.min(parent.height - 70, 490)
        modal: true; focus: true; padding: 22
        background: HudPanel { color: "#07121f"; lineColor: "#3e7d97"; cut: 24 }
        ColumnLayout {
            anchors.fill: parent; spacing: 16
            RowLayout {
                Text { text: "CHOOSE YOUR ATMOSPHERE"; color: "#e0f7ff"; font.pixelSize: 18; font.letterSpacing: 2 }
                Item { Layout.fillWidth: true }
                ActionButton { objectName: "close-scene-picker"; text: "CLOSE"; onClicked: scenePicker.close() }
            }
            Text { text: "Scroll sideways · Preview the wallpaper and matching palette · Click to apply"; color: "#9cbdcd" }
            ListView {
                objectName: "scene-list"
                Layout.fillWidth: true; Layout.fillHeight: true; orientation: ListView.Horizontal; spacing: 24; clip: true
                model: root.dashboardData.scenes || []
                ScrollBar.horizontal: ScrollBar { policy: ScrollBar.AlwaysOn }
                delegate: Item {
                    required property var modelData
                    readonly property color cardBackground: modelData.background
                    readonly property bool lightCard: cardBackground.r * 0.2126 + cardBackground.g * 0.7152 + cardBackground.b * 0.0722 > 0.55
                    width: 320; height: 295
                    Canvas {
                        anchors.fill: parent; anchors.margins: 2
                        property color sceneBackground: modelData.background
                        property color sceneAccent: modelData.accent
                        onPaint: {
                            const c = getContext("2d"); c.reset()
                            c.beginPath(); c.moveTo(2, height / 2); c.lineTo(32, 3); c.lineTo(width - 32, 3)
                            c.lineTo(width - 2, height / 2); c.lineTo(width - 32, height - 3); c.lineTo(32, height - 3); c.closePath()
                            c.fillStyle = sceneBackground; c.fill(); c.lineWidth = 2; c.strokeStyle = sceneAccent; c.stroke()
                        }
                    }
                    Column {
                        anchors.fill: parent; anchors.margins: 36; spacing: 10
                        Image { width: parent.width; height: 165; source: modelData.preview; fillMode: Image.PreserveAspectCrop; asynchronous: true; sourceSize.width: 600 }
                        Text { width: parent.width; text: modelData.name; color: parent.parent.lightCard ? "#152938" : "#eaf8ff"; font.pixelSize: 15; font.bold: true; elide: Text.ElideRight }
                        Row {
                            spacing: 7
                            Repeater { model: [modelData.background, modelData.accent, modelData.secondary]; delegate: Rectangle { required property string modelData; width: 34; height: 16; radius: 4; color: modelData; border.color: "#507080" } }
                            Text { text: modelData.live ? "LIVE" : "STILL"; color: parent.parent.parent.lightCard ? "#365669" : "#87adbb"; font.pixelSize: 10 }
                        }
                    }
                    MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: { root.client.callTool("scenes.apply", {name: modelData.id}); scenePicker.close() } }
                }
            }
        }
    }
}
