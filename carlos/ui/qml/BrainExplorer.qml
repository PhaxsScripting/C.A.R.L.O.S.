pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root

    required property var client
    property bool animationsRunning: true
    property string selectedNode: "EV"
    property int dataRevision: 0
    property real zoomLevel: 1.0
    property string searchText: ""
    component ZoomButton: SciButton { }
    onSelectedNodeChanged: edgeCanvas.requestPaint()
    onSearchTextChanged: edgeCanvas.requestPaint()
    function related(id) {
        if (selectedNode === "EV" || selectedNode === id) return true
        return edges.some(function(edge) { return edge.indexOf(id) >= 0 && edge.indexOf(root.selectedNode) >= 0 })
    }
    function matches(id) { return !searchText || (node(id).label + " " + id + " " + node(id).description).toLowerCase().indexOf(searchText.toLowerCase()) >= 0 }
    onVisibleChanged: {
        if (visible) {
            dataRevision += 1
            edgeCanvas.requestPaint()
        }
    }
    signal exitRequested()

    readonly property color neon: "#6fe5ff"
    readonly property color neonHot: "#c5f5ff"
    readonly property color cyan: "#4ce6ff"
    readonly property color muted: "#376078"
    readonly property color panel: "#081522"

    readonly property var nodes: [
        { id: "EV", label: "Carlos", x: 0.50, y: 0.50, size: 28, description: "The live Carlos core state and event coordinator." },
        { id: "CORE", label: "Core State", x: 0.50, y: 0.38, size: 18, description: "The explicit finite-state machine: dormant, listening, thinking, tools, and speech." },
        { id: "LANGUAGE", label: "Language", x: 0.34, y: 0.44, size: 17, description: "Receives normalized text and emits the final user-visible response." },
        { id: "REASONING", label: "Reasoning", x: 0.38, y: 0.61, size: 20, description: "Declared task, plan, provider, usage, and measured inference timing." },
        { id: "LOCAL MODEL", label: "Local Model", x: 0.25, y: 0.69, size: 14, description: "Loopback-only Qwen reasoning for general English conversation." },
        { id: "PLAN", label: "Execution Plan", x: 0.52, y: 0.73, size: 14, description: "The provider's explicit high-level execution plan, never hidden chain-of-thought." },
        { id: "MEMORY", label: "Memory", x: 0.68, y: 0.64, size: 20, description: "User-approved durable memories and bounded recent conversation context." },
        { id: "CONTEXT", label: "Context", x: 0.79, y: 0.74, size: 13, description: "Bounded conversation and relevant explicit memories supplied to reasoning." },
        { id: "SECURITY", label: "Security", x: 0.64, y: 0.37, size: 19, description: "Runtime firewall inspection, schema validation, redaction, permission policy and safety events." },
        { id: "PERMISSIONS", label: "Permissions", x: 0.77, y: 0.29, size: 13, description: "Codex-only approvals and audited tool-policy decisions." },
        { id: "TOOL ROUTER", label: "Tool Router", x: 0.50, y: 0.20, size: 20, description: "Routes only registered structured tools; no unrestricted model shell." },
        { id: "VOICE", label: "Voice", x: 0.23, y: 0.24, size: 18, description: "Local wake, capture, transcription, synthesis, and conversation flow." },
        { id: "WAKE", label: "Wake Word", x: 0.09, y: 0.16, size: 12, description: "Ambient local Carlos keyword detection on the current default microphone." },
        { id: "STT", label: "Speech to Text", x: 0.16, y: 0.38, size: 13, description: "On-demand local Whisper transcription and close-target verification." },
        { id: "TTS", label: "Speech Output", x: 0.09, y: 0.53, size: 13, description: "On-demand local Piper speech with real playback waveform and barge-in." },
        { id: "APPLICATIONS", label: "Applications", x: 0.67, y: 0.17, size: 16, description: "Installed-app discovery, launch, focus, and exact process identity checks." },
        { id: "SPOTIFY", label: "Spotify", x: 0.84, y: 0.11, size: 12, description: "Desktop media controls plus authorized Spotify search and verified Web API playback." },
        { id: "AUDIO", label: "Audio", x: 0.88, y: 0.27, size: 15, description: "PipeWire volume, mute, output, and registered media controls." },
        { id: "FILES", label: "Files", x: 0.92, y: 0.44, size: 14, description: "Bounded operations under configured roots with canonical path checks." },
        { id: "DESKTOP", label: "Desktop", x: 0.87, y: 0.59, size: 14, description: "Explicit desktop information and internal Carlos notification events." },
        { id: "SYSTEM", label: "System", x: 0.73, y: 0.85, size: 18, description: "CPU, RAM, thermal, disk, network, battery, and process inspection." },
        { id: "TELEMETRY", label: "Telemetry", x: 0.55, y: 0.91, size: 12, description: "Bounded live host metrics sampled by the Carlos core." },
        { id: "DEVELOPMENT", label: "Development", x: 0.32, y: 0.88, size: 13, description: "Allowlisted project discovery, build, Git, and error inspection tools." },
        { id: "EVENTS", label: "Event Stream", x: 0.14, y: 0.80, size: 14, description: "Sequenced, timestamped, redacted software events shown by this map." },
        { id: "IPC", label: "Private IPC", x: 0.09, y: 0.66, size: 12, description: "The mode-0600 local socket connecting this UI to the core." },
        { id: "REMINDERS", label: "Reminders", x: 0.38, y: 0.10, size: 13, description: "Persistent timers, clock alarms and recurring spoken reminders." },
        { id: "SCENES", label: "Scenes", x: 0.92, y: 0.79, size: 13, description: "Your installed live and still wallpapers with their exact desktop palettes." },
        { id: "VISION", label: "Perception", x: 0.64, y: 0.52, size: 15, description: "Requested screen captures, OCR and accessibility evidence. Not continuous hidden surveillance." }
    ]

    readonly property var edges: [
        ["EV", "CORE"], ["EV", "LANGUAGE"], ["EV", "REASONING"], ["EV", "MEMORY"], ["EV", "SECURITY"], ["EV", "TOOL ROUTER"], ["EV", "EVENTS"],
        ["CORE", "VOICE"], ["CORE", "TELEMETRY"], ["LANGUAGE", "VOICE"], ["LANGUAGE", "REASONING"], ["REASONING", "LOCAL MODEL"], ["REASONING", "PLAN"],
        ["REASONING", "MEMORY"], ["MEMORY", "CONTEXT"], ["CONTEXT", "LOCAL MODEL"], ["SECURITY", "PERMISSIONS"], ["SECURITY", "TOOL ROUTER"],
        ["TOOL ROUTER", "APPLICATIONS"], ["TOOL ROUTER", "AUDIO"], ["TOOL ROUTER", "FILES"], ["TOOL ROUTER", "DESKTOP"], ["TOOL ROUTER", "SYSTEM"], ["TOOL ROUTER", "DEVELOPMENT"],
        ["VOICE", "WAKE"], ["VOICE", "STT"], ["VOICE", "TTS"], ["STT", "LANGUAGE"], ["TTS", "LANGUAGE"], ["APPLICATIONS", "SPOTIFY"], ["SPOTIFY", "AUDIO"],
        ["SYSTEM", "TELEMETRY"], ["EVENTS", "TELEMETRY"], ["EVENTS", "IPC"], ["IPC", "CORE"], ["PERMISSIONS", "APPLICATIONS"], ["PERMISSIONS", "FILES"], ["PLAN", "TOOL ROUTER"],
        ["REMINDERS", "CORE"], ["REMINDERS", "VOICE"], ["SCENES", "DESKTOP"], ["VISION", "DESKTOP"], ["VISION", "REASONING"]
    ]

    function value(map, key, fallback) {
        return map && map[key] !== undefined && map[key] !== null ? map[key] : fallback
    }

    function node(id) {
        for (let i = 0; i < nodes.length; ++i) {
            if (nodes[i].id === id)
                return nodes[i]
        }
        return nodes[0]
    }

    function stateNode() {
        const state = String(client.state || "")
        if (state === "LISTENING" || state === "AWAKE") return "VOICE"
        if (state === "TRANSCRIBING") return "STT"
        if (state === "THINKING") return "REASONING"
        if (state === "RETRIEVING_MEMORY") return "MEMORY"
        if (state === "USING_TOOL") return "TOOL ROUTER"
        if (state === "WAITING_FOR_CONFIRMATION") return "PERMISSIONS"
        if (state === "SPEAKING") return "TTS"
        return "CORE"
    }

    function nodeActive(id) {
        const active = client.activeNodes || []
        if (active.indexOf(id) >= 0)
            return true
        if (id === "EV")
            return client.connected
        if (id === stateNode())
            return true
        if ((id === "WAKE" || id === "STT" || id === "TTS") && active.indexOf("VOICE") >= 0)
            return id === stateNode()
        return false
    }

    function eventMatches(item, id) {
        if (!item) return false
        if (id === "EV" || id === "EVENTS") return true
        const type = String(item.type || "")
        const source = String(item.source || "")
        const payload = item.payload || {}
        const tool = String(payload.tool || "")
        const category = String(payload.category || "").toUpperCase()
        if (id === "CORE") return type.indexOf("core.") === 0
        if (id === "LANGUAGE") return source === "language" || type.indexOf("command.") === 0
        if (id === "REASONING" || id === "LOCAL MODEL") return source === "reasoning" || type.indexOf("ai.") === 0
        if (id === "MEMORY" || id === "CONTEXT") return source === "memory" || type.indexOf("memory.") === 0
        if (id === "SECURITY" || id === "PERMISSIONS") return source === "security" || type === "tool.permission_check" || type.indexOf("voice.close_verification") === 0
        if (id === "TOOL ROUTER") return source === "tools" || type.indexOf("tool.") === 0
        if (id === "VOICE") return source === "voice" || type.indexOf("wake.") === 0 || type.indexOf("tts.") === 0
        if (id === "WAKE") return type.indexOf("wake.") === 0
        if (id === "STT") return type.indexOf("voice.transcription") === 0 || type.indexOf("voice.close_verification") === 0
        if (id === "TTS") return type.indexOf("tts.") === 0
        if (id === "SPOTIFY") return tool === "audio.media" || tool.indexOf("spotify.") === 0
        if (id === "REMINDERS") return type.indexOf("reminder.") === 0 || tool.indexOf("reminders.") === 0
        if (id === "PLAN") return type.indexOf("plan.") === 0
        if (id === "TELEMETRY") return type === "system.telemetry"
        if (id === "IPC") return type === "core.started" || type === "core.stopping"
        return category === id || tool.indexOf(id.toLowerCase() + ".") === 0
    }

    function recentEvents(id, limit) {
        const revision = dataRevision
        const source = client.events || []
        const result = []
        let previousKey = ""
        for (let i = source.length - 1; i >= 0 && result.length < limit; --i) {
            if (!eventMatches(source[i], id))
                continue
            const key = String(source[i].type || "event") + "|" + eventSummary(source[i])
            if ((id === "EV" || id === "EVENTS") && key === previousKey)
                continue
            previousKey = key
            result.push(source[i])
        }
        return result
    }

    function relatedTools(id) {
        const revision = dataRevision
        const source = client.tools || []
        const result = []
        for (let i = 0; i < source.length; ++i) {
            const tool = source[i]
            const name = String(tool.name || "")
            const category = String(tool.category || "").toUpperCase()
            if (id === "TOOL ROUTER" || category === id || name.indexOf(id.toLowerCase() + ".") === 0 || (id === "SPOTIFY" && name === "audio.media"))
                result.push(tool)
        }
        return result
    }

    function eventSummary(item) {
        const payload = item && item.payload ? item.payload : {}
        if (payload.tool) return String(payload.tool)
        if (payload.detail) return String(payload.detail)
        if (payload.message) return String(payload.message)
        if (payload.response) return String(payload.response)
        if (payload.keyword) return "keyword " + String(payload.keyword)
        return item && item.source ? String(item.source) : "event"
    }

    function statusFor(id) {
        if (id === "EV" || id === "CORE") return String(client.state) + " // " + String(client.detail)
        if (id === "LOCAL MODEL") return String(value(client.provider, "active", "offline")) + " // " + String(value(client.provider, "model", "no model"))
        if (id === "MEMORY" || id === "CONTEXT") return String(client.memories.length) + " explicit memories loaded"
        if (id === "PLAN") {
            const plan = value(client.cognition, "plan", [])
            return String(plan.length) + " declared plan steps"
        }
        const latest = recentEvents(id, 1)
        return latest.length ? String(latest[0].type) : "No matching event in the bounded history"
    }

    Connections {
        target: root.client
        enabled: root.visible
        function onEventsChanged() { root.dataRevision += 1; edgeCanvas.requestPaint() }
        function onToolsChanged() { root.dataRevision += 1 }
        function onMemoriesChanged() { root.dataRevision += 1 }
        function onActiveNodesChanged() { root.dataRevision += 1; edgeCanvas.requestPaint() }
        function onStateChanged() { root.dataRevision += 1; edgeCanvas.requestPaint() }
        function onCognitionChanged() { root.dataRevision += 1 }
    }

    HudBackdrop { anchors.fill: parent }
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 14
        spacing: 10

        RowLayout {
            Layout.fillWidth: true
            TextField { Layout.fillWidth: true; placeholderText: "Find a node: voice, memory, firewall…"; color: "#daf3ff"; placeholderTextColor: "#8aaabc"; padding: 10; background: Rectangle { color: "#0b1a28"; radius: 7; border.color: "#2c4e64" } onTextChanged: root.searchText = text }
            ZoomButton { text: "−"; onClicked: root.zoomLevel = Math.max(0.7, root.zoomLevel - 0.15) }
            ZoomButton { text: Math.round(root.zoomLevel * 100) + "% / RESET"; onClicked: { root.zoomLevel = 1; graphFlick.contentX = 0; graphFlick.contentY = 0; root.selectedNode = "EV" } }
            ZoomButton { objectName: "brain-zoom-in"; text: "+"; onClicked: root.zoomLevel = Math.min(2.5, root.zoomLevel + 0.15) }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 42
            spacing: 12

            Button {
                id: backButton
                objectName: "brain-back"
                text: "‹  BRAIN"
                leftPadding: 15
                rightPadding: 15
                contentItem: Text {
                    text: backButton.text
                    color: backButton.hovered ? root.neonHot : "#97cbe0"
                    font.pixelSize: 10
                    font.bold: true
                    font.letterSpacing: 1.3
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    radius: 6
                    color: backButton.hovered ? "#163449" : "#0c1c2b"
                    border.color: backButton.hovered ? root.neon : "#34566c"
                }
                onClicked: root.exitRequested()
            }

            Column {
                spacing: 2
                Text { text: "LIVE COGNITIVE NETWORK"; color: "#e5f9ff"; font.pixelSize: 13; font.bold: true; font.letterSpacing: 2.2 }
                Text { text: "ARCHITECTURE MAP // REAL EVENT ACTIVITY OVERLAY"; color: "#8aa4b7"; font.pixelSize: 8; font.letterSpacing: 1.25 }
            }
            Item { Layout.fillWidth: true }
            Rectangle {
                Layout.preferredWidth: liveRow.implicitWidth + 22
                Layout.preferredHeight: 28
                radius: 14
                color: "#0a1b29"
                border.color: root.client.connected ? "#3989a6" : "#72303c"
                Row {
                    id: liveRow
                    anchors.centerIn: parent
                    spacing: 7
                    Rectangle { width: 7; height: 7; radius: 4; color: root.client.connected ? root.neon : "#ff6478" }
                    Text { text: root.client.connected ? "LIVE CORE" : "CORE OFFLINE"; color: root.client.connected ? "#abddec" : "#ff8d9e"; font.pixelSize: 9; font.bold: true; font.letterSpacing: 1 }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 10

            HudPanel {
                Layout.fillWidth: true
                Layout.fillHeight: true
                color: "#060f1a"
                lineColor: "#234457"
                clip: true

                Flickable {
                    id: graphFlick
                    anchors.fill: parent
                    anchors.margins: 1
                    clip: true
                    interactive: true
                    boundsBehavior: Flickable.StopAtBounds
                    contentWidth: Math.max(width, 760) * root.zoomLevel
                    contentHeight: Math.max(height, 600) * root.zoomLevel
                    ScrollBar.horizontal: ScrollBar { policy: ScrollBar.AsNeeded }
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                    Timer {
                        // Layout dimensions are not final at Component.onCompleted
                        // on every Plasma scale factor. Center once after layout,
                        // then leave contentX/Y free for normal user panning.
                        interval: 60
                        running: true
                        repeat: false
                        onTriggered: {
                            graphFlick.contentX = Math.max(0, (graphFlick.contentWidth - graphFlick.width) / 2)
                            graphFlick.contentY = Math.max(0, (graphFlick.contentHeight - graphFlick.height) / 2)
                        }
                    }

                    Item {
                        id: graphSurface
                        width: graphFlick.contentWidth
                        height: graphFlick.contentHeight

                        Repeater {
                            model: 64
                            delegate: Rectangle {
                                required property int index
                                width: index % 9 === 0 ? 2 : 1
                                height: width
                                radius: width / 2
                                x: (index * 173 + 61) % Math.max(1, graphSurface.width - width)
                                y: (index * 97 + 29) % Math.max(1, graphSurface.height - height)
                                color: root.neon
                                opacity: 0.08 + (index % 5) * 0.018
                            }
                        }

                        Canvas {
                            id: edgeCanvas
                            anchors.fill: parent
                            antialiasing: true
                            onPaint: {
                                const ctx = getContext("2d")
                                ctx.reset()
                                const r = Math.min(width,height)*.35
                                ctx.strokeStyle = "#376078"; ctx.lineWidth = .8; ctx.globalAlpha = .25
                                for(let ring=0;ring<3;++ring) { ctx.beginPath(); ctx.arc(width*.5,height*.5,r+ring*26,0,Math.PI*2); ctx.stroke() }
                                for(let i=0;i<72;++i) {
                                    const a=i*Math.PI/36
                                    ctx.beginPath(); ctx.moveTo(width*.5+Math.cos(a)*(r+55),height*.5+Math.sin(a)*(r+55))
                                    ctx.lineTo(width*.5+Math.cos(a)*(r+60),height*.5+Math.sin(a)*(r+60)); ctx.stroke()
                                }
                                ctx.globalAlpha = 1
                                for (let i = 0; i < root.edges.length; ++i) {
                                    const from = root.node(root.edges[i][0])
                                    const to = root.node(root.edges[i][1])
                                    const active = root.nodeActive(from.id) && root.nodeActive(to.id)
                                    const selected = root.selectedNode === from.id || root.selectedNode === to.id
                                    const visibleMatch = root.matches(from.id) || root.matches(to.id)
                                    ctx.beginPath()
                                    ctx.moveTo(width * from.x, height * from.y)
                                    const midX = width * (from.x + to.x) / 2
                                    ctx.bezierCurveTo(midX, height * from.y, midX, height * to.y, width * to.x, height * to.y)
                                    ctx.strokeStyle = active ? root.neon : selected ? root.cyan : root.muted
                                    ctx.globalAlpha = !visibleMatch ? 0.07 : active ? 0.9 : selected ? 0.72 : 0.55
                                    ctx.lineWidth = active || selected ? 1.6 : 0.8
                                    ctx.stroke()
                                }
                                ctx.globalAlpha = 1.0
                            }
                            onWidthChanged: requestPaint()
                            onHeightChanged: requestPaint()
                        }

                        Repeater {
                            model: root.nodes
                            delegate: Item {
                                id: nodeDelegate
                                required property var modelData
                                width: 128
                                height: 54
                                x: graphSurface.width * modelData.x - width / 2
                                y: graphSurface.height * modelData.y - height / 2
                                z: 2
                                opacity: !root.matches(modelData.id) ? 0.14 : root.related(modelData.id) ? 1 : 0.48
                                Behavior on opacity { NumberAnimation { duration: 140 } }

                                readonly property bool active: root.nodeActive(modelData.id)
                                readonly property bool selected: root.selectedNode === modelData.id

                                Rectangle {
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    anchors.verticalCenter: parent.verticalCenter
                                    anchors.verticalCenterOffset: -7
                                    width: nodeDelegate.modelData.size + (nodeDelegate.active ? 14 : 8)
                                    height: width
                                    radius: 2
                                    rotation: 45
                                    color: nodeDelegate.active ? "#153949" : "transparent"
                                    border.color: nodeDelegate.active ? root.neon : "#30586e"
                                    opacity: nodeDelegate.selected ? 0.95 : 0.62
                                }
                                Rectangle {
                                    id: nodeDot
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    anchors.verticalCenter: parent.verticalCenter
                                    anchors.verticalCenterOffset: -7
                                    width: nodeDelegate.modelData.size
                                    height: width
                                    radius: width / 2
                                    color: nodeDelegate.active ? root.neon : nodeDelegate.selected ? "#66b8d1" : "#204458"
                                    border.width: nodeDelegate.selected ? 2 : 1
                                    border.color: nodeDelegate.selected ? "#e4fbff" : nodeDelegate.active ? root.neonHot : "#42768f"
                                    scale: nodeMouse.containsMouse ? 1.20 : 1.0
                                    Behavior on scale { NumberAnimation { duration: 130 } }

                                    SequentialAnimation on opacity {
                                        running: root.visible && root.animationsRunning && nodeDelegate.active
                                        loops: Animation.Infinite
                                        NumberAnimation { to: 0.64; duration: 520; easing.type: Easing.InOutSine }
                                        NumberAnimation { to: 1.0; duration: 520; easing.type: Easing.InOutSine }
                                    }
                                }
                                Text {
                                    anchors.horizontalCenter: parent.horizontalCenter
                                    anchors.top: nodeDot.bottom
                                    anchors.topMargin: 5
                                    text: nodeDelegate.modelData.label
                                    color: nodeDelegate.selected ? "#eefaff" : nodeDelegate.active ? root.neonHot : "#a2bacb"
                                    font.family: "Hack"
                                    font.pixelSize: nodeDelegate.modelData.id === "EV" ? 13 : 10
                                    font.bold: nodeDelegate.selected || nodeDelegate.active
                                    font.letterSpacing: 0.4
                                }
                                MouseArea {
                                    id: nodeMouse
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    cursorShape: Qt.PointingHandCursor
                                    Accessible.name: "Inspect " + nodeDelegate.modelData.label
                                    Accessible.description: nodeDelegate.modelData.description
                                    Accessible.role: Accessible.Button
                                    Accessible.onPressAction: root.selectedNode = nodeDelegate.modelData.id
                                    onClicked: root.selectedNode = nodeDelegate.modelData.id
                                }
                            }
                        }

                        Text {
                            x: graphFlick.contentX + 12
                            y: graphFlick.contentY + graphFlick.height - height - 12
                            text: "DRAG TO PAN  //  CLICK ANY NODE TO INSPECT REAL DATA"
                            color: "#799ab0"
                            font.pixelSize: 8
                            font.letterSpacing: 1.1
                        }
                    }
                }
            }

            HudPanel {
                Layout.preferredWidth: root.width < 1000 ? 265 : 310
                Layout.minimumWidth: 250
                Layout.maximumWidth: 350
                Layout.fillHeight: true
                color: root.panel
                lineColor: "#315369"

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 14
                    spacing: 9

                    RowLayout {
                        Layout.fillWidth: true
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Text { text: root.node(root.selectedNode).label.toUpperCase(); color: "#ecfaff"; font.pixelSize: 15; font.bold: true; font.letterSpacing: 1.7 }
                            Text { text: "NODE INSPECTOR"; color: "#719bb2"; font.pixelSize: 8; font.letterSpacing: 1.3 }
                        }
                        Rectangle {
                            Layout.preferredWidth: 10
                            Layout.preferredHeight: 10
                            radius: 5
                            color: root.nodeActive(root.selectedNode) ? root.neon : "#345b71"
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        text: root.node(root.selectedNode).description
                        color: "#b4cbd9"
                        font.pixelSize: 10
                        wrapMode: Text.WordWrap
                    }

                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: "#244559" }
                    Text { text: "CURRENT SIGNAL"; color: root.neon; font.pixelSize: 8; font.bold: true; font.letterSpacing: 1.4 }
                    Text {
                        Layout.fillWidth: true
                        text: root.statusFor(root.selectedNode)
                        color: "#daeff9"
                        font.pixelSize: 10
                        wrapMode: Text.WordWrap
                        maximumLineCount: 4
                        elide: Text.ElideRight
                    }

                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: "#244559" }
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: "RECENT REAL EVENTS"; color: root.neon; font.pixelSize: 8; font.bold: true; font.letterSpacing: 1.3 }
                        Item { Layout.fillWidth: true }
                        Text { text: String(root.recentEvents(root.selectedNode, 10).length); color: "#86aabc"; font.pixelSize: 9 }
                    }
                    ListView {
                        id: nodeEvents
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        Layout.minimumHeight: 130
                        clip: true
                        spacing: 5
                        model: root.recentEvents(root.selectedNode, 10)
                        delegate: Rectangle {
                            id: eventDelegate
                            required property var modelData
                            width: nodeEvents.width
                            height: eventColumn.implicitHeight + 13
                            radius: 5
                            color: "#0b1d2c"
                            border.color: "#315369"
                            Column {
                                id: eventColumn
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.top: parent.top
                                anchors.margins: 7
                                spacing: 2
                                Text { width: parent.width; text: String(eventDelegate.modelData.type || "event"); color: "#a4e8ff"; font.pixelSize: 9; font.bold: true; elide: Text.ElideRight }
                                Text { width: parent.width; text: root.eventSummary(eventDelegate.modelData); color: "#94b1c4"; font.pixelSize: 10; elide: Text.ElideRight }
                            }
                        }
                    }

                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: "#244559" }
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: "REGISTERED TOOLS"; color: root.neon; font.pixelSize: 8; font.bold: true; font.letterSpacing: 1.3 }
                        Item { Layout.fillWidth: true }
                        Text { text: String(root.relatedTools(root.selectedNode).length); color: "#86aabc"; font.pixelSize: 9 }
                    }
                    ListView {
                        id: nodeTools
                        Layout.fillWidth: true
                        Layout.preferredHeight: Math.min(116, contentHeight)
                        visible: count > 0
                        clip: true
                        spacing: 3
                        model: root.relatedTools(root.selectedNode)
                        delegate: Text {
                            required property var modelData
                            width: nodeTools.width
                            text: "•  " + String(modelData.name || "tool")
                            color: "#9fbaca"
                            font.pixelSize: 9
                            elide: Text.ElideRight
                        }
                    }
                }
            }
        }
    }
}
