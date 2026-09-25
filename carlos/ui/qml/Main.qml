pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Window

ApplicationWindow {
    id: appWindow
    width: 1480
    height: 900
    minimumWidth: 1080
    minimumHeight: 680
    visible: !backgroundMode
    title: "Carlos // AUTONOMOUS DESKTOP INTELLIGENCE"
    color: "#050a0f"
    font.family: "Liberation Sans"
    property bool reducedMotion: settingValue("hud_reduce_motion", false)
    readonly property bool hudEnabled: settingValue("hud_enabled", true)
    objectName: "ev-main-window"
    property color cyan: "#70e6ff"
    property color blue: "#4d7dff"
    property color ink: "#071019"
    property color panel: "#091522"
    property color line: "#2a4558"
    property color dim: "#8da6b8"
    // Keep the futuristic motion tied to real assistant activity. A dormant or
    // resource-pressured desktop should not continuously redraw the scene.
    property bool animationsRunning: visible
                                     && !reducedMotion
                                     && visibility !== Window.Minimized
                                     && evClient.state !== "DORMANT"
                                     && get(evClient.telemetry, "resource_mode", "NORMAL") === "NORMAL"
    property int testPage: 0
    property bool testScenePicker: false
    property string inspectedPlanId: ""
    property bool commandInputExpanded: false
    property bool reviseCurrentTask: false
    readonly property var pageTitles: ["COMMAND DECK", "NEURAL INTERFACE", "COMMUNICATIONS", "MEMORY ARCHIVE", "SYSTEM TELEMETRY", "CAPABILITY MATRIX", "MISSION CONTROL", "SECURITY OPERATIONS", "EVENT STREAM", "SYSTEM CONFIGURATION", "ENVIRONMENT CONTROL"]
    readonly property var pageDetails: ["Your desktop intelligence. All systems in view.", "Perception. Memory. Reasoning. Explore the architecture.", "One conversation. Real actions. Verified outcomes.", "Your explicit memories, kept under your control.", "Live host measurements. No simulated readings.", "Discover every registered operation and its boundaries.", "Trace each request from intent to verified result.", "Local evidence, runtime inspection and access boundaries.", "A timestamped record of what actually happened.", "Tune the connection, voice and interaction pipeline.", "Your atmosphere, routines and daily essentials."]

    Connections {
        target: evClient
        function onSceneActivated(hud) {
            const pages = {CARLOS: 0, PROJECT: 6, SYSTEM: 4, MEDIA: 2, REMOTE: 10};
            if (pages[hud] !== undefined) navigation.currentIndex = pages[hud];
        }
    }

    function get(map, key, fallback) {
        return map && map[key] !== undefined && map[key] !== null ? map[key] : fallback
    }
    function settingValue(key, fallback) {
        const fields = get(get(evClient.daily, "settings", {}), "fields", []);
        for (let i = 0; i < fields.length; ++i) if (fields[i].key === key) return fields[i].value;
        return fallback;
    }
    function nested(map, first, second, fallback) {
        const inner = get(map, first, null)
        return inner ? get(inner, second, fallback) : fallback
    }
    function bytes(value) {
        if (value === undefined || value === null) return "—"
        const gib = Number(value) / 1073741824
        return gib >= 1 ? gib.toFixed(1) + " GB" : (Number(value) / 1048576).toFixed(0) + " MB"
    }
    function percent(value) { return value === undefined || value === null ? "—" : Number(value).toFixed(1) + "%" }
    function eventSummary(item) {
        const p = item && item.payload ? item.payload : {}
        if (p.tool) return p.tool
        if (p.detail) return p.detail
        if (p.message) return p.message
        if (p.response) return p.response
        return item && item.source ? item.source : "event"
    }
    function permissionColor(value) {
        if (value === "SAFE") return "#53efae"
        if (value === "LOW_RISK") return cyan
        if (value === "SENSITIVE") return "#ffca58"
        if (value === "HIGH" || value === "PRIVILEGED" || value === "DESTRUCTIVE") return "#ff6478"
        return cyan
    }
    function stateColor(value) {
        const state = String(value || "UNAVAILABLE").toUpperCase()
        if (state === "SUCCESS" || state === "SUCCEEDED" || state === "PASS" || state === "READY" || state === "ACTIVE" || state === "HEALTHY" || state === "COMPLETED" || state === "NO_FINDINGS") return "#53efae"
        if (state === "ERROR" || state === "DEVICE_ERROR" || state === "CLIPPING" || state === "INPUT_INVALID" || state === "UNAVAILABLE" || state === "RESOURCE_SUSPENDED") return "#ff6478"
        if (state === "LOADING" || state === "LISTENING" || state === "TRANSCRIBING" || state === "DETECTED" || state === "RECONNECTING") return "#ffca58"
        return "#6f91a4"
    }
    function voiceDiag(key, fallback) { return get(get(evClient.voice, "diagnostics", {}), key, fallback) }
    function voiceStatus() {
        if (!evClient.connected) return "CORE DISCONNECTED"
        if (get(evClient.voice, "privacy_mode", false)) return "MICROPHONE OFF / PRIVACY"
        if (get(evClient.voice, "wake_paused", false)) return "WAKE LISTENING PAUSED"
        if (evClient.state === "LISTENING") return voiceDiag("follow_up_state", "") === "ACTIVE" ? "YOUR TURN / KEEP TALKING" : "LISTENING TO YOU"
        if (evClient.state === "TRANSCRIBING") return "TRANSCRIBING YOUR WORDS"
        if (["THINKING", "RETRIEVING_MEMORY"].indexOf(evClient.state) >= 0) return "WORKING ON YOUR REQUEST"
        if (evClient.state === "USING_TOOL") return "EXECUTING / VERIFYING"
        if (evClient.state === "SPEAKING") return "SPEAKING / SAY STOP TO END"
        if (voiceDiag("wake_input_quality", "") === "LOUD_NON_SPEECH") return "CHECK MIC / LOUD INPUT WITHOUT CLEAR SPEECH"
        if (get(voiceDiag("wake_speech_backup", {}), "state", "") === "CHECKING") return "CHECKING YOUR NAME / LOCAL"
        return get(evClient.voice, "wake_active", false) ? "LISTENING FOR Carlos" : "VOICE NEEDS ATTENTION"
    }
    function nextChoice(current, choices) {
        const index = choices.indexOf(String(current))
        return choices[(index + 1) % choices.length]
    }
    function inspectedPlan() {
        if (get(evClient.activePlan, "id", "") !== "") return evClient.activePlan
        for (let i = evClient.plans.length - 1; i >= 0; --i) {
            if (inspectedPlanId === "" || get(evClient.plans[i], "id", "") === inspectedPlanId)
                return evClient.plans[i]
        }
        return {}
    }
    function compactJson(value) {
        if (value === undefined || value === null) return "—"
        const rendered = JSON.stringify(value, null, 2) || String(value)
        return rendered.length > 3500 ? rendered.slice(0, 3500) + "\n…TRUNCATED IN VIEW" : rendered
    }
    function latestLatency() {
        const recent = get(evClient.latency, "recent", [])
        return recent.length > 0 ? recent[recent.length - 1] : {}
    }
    function selectStepFields(plan, field) {
        const selected = []
        const steps = get(plan, "steps", [])
        for (let i = 0; i < steps.length; ++i) {
            const item = { "step": get(steps[i], "id", String(i + 1)) }
            item[field] = get(steps[i], field, null)
            selected.push(item)
        }
        return selected
    }
    function codingAgentTask(plan) {
        const steps = get(plan, "steps", [])
        for (let i = 0; i < steps.length; ++i) {
            if (String(get(steps[i], "tool", "")).indexOf("development.coding_agent") === 0)
                return steps[i]
        }
        const gaps = get(plan, "capability_gaps", [])
        return gaps.length > 0 ? {
            "status": "NOT_CREATED",
            "possible_solution": get(gaps[0], "possible_solution", "—"),
            "requires_user_approval": get(gaps[0], "requires_user_approval", true)
        } : null
    }
    function planEvidence(plan) {
        if (get(plan, "id", "") === "") return "NO PLAN SELECTED\n\nRun a multi-step desktop request or select a recent task."
        const steps = get(plan, "steps", [])
        const current = steps.length > 0 ? steps[Math.min(Number(get(plan, "current_step", 0)), steps.length - 1)] : null
        const lines = [
            "USER REQUEST\n" + get(plan, "request", "—"),
            "TRANSCRIPT / INPUT\n" + get(plan, "request", "—"),
            "RESOLVED GOAL\n" + get(plan, "goal", "—"),
            "PLAN\n" + compactJson(steps.map(function(step) { return {
                "step": get(step, "id", "—"),
                "depends_on": get(step, "dependencies", []),
                "expected": get(step, "expected_postcondition", "—")
            }})),
            "STATUS / CURRENT STEP\n" + get(plan, "status", "—") + "  //  " + compactJson(current),
            "WORLD STATE USED\n" + compactJson(get(plan, "world_state_used", {})),
            "TOOLS\n" + compactJson(selectStepFields(plan, "tool")),
            "PERMISSIONS\n" + compactJson(selectStepFields(plan, "permission_class")),
            "RESULTS\n" + compactJson(selectStepFields(plan, "actual_result")),
            "VERIFICATION\n" + compactJson(selectStepFields(plan, "verification")),
            "RECOVERY\n" + compactJson(get(plan, "recovery", [])),
            "TIMINGS\n" + compactJson(get(plan, "timings", {})),
            "CAPABILITY GAPS\n" + compactJson(get(plan, "capability_gaps", [])),
            "CODING-AGENT TASK\n" + compactJson(codingAgentTask(plan)),
            "FAILURE REPORT\n" + compactJson(get(plan, "failure_report", null)),
            "LATEST PIPELINE LATENCY\n" + compactJson(latestLatency())
        ]
        return lines.join("\n\n")
    }

    component HudButton: SciButton { accent: appWindow.cyan }

    component SectionPanel: HudPanel {
        color: appWindow.panel
        lineColor: appWindow.line
    }

    component TitleText: Text {
        color: "#dff8ff"
        font.family: "Hack"
        font.pixelSize: 11
        font.bold: true
        font.letterSpacing: 1.8
    }

    component StatusPill: HudPanel {
        property string label: "STAGE"
        property string status: "IDLE"
        implicitWidth: 126
        implicitHeight: 52
        cut: 9
        color: "#081b29"
        accent: stateColor(status)
        Column {
            anchors.centerIn: parent
            spacing: 4
            Text { anchors.horizontalCenter: parent.horizontalCenter; text: label; color: "#8da5b3"; font.pixelSize: 9; font.bold: true; font.letterSpacing: 1 }
            Text { anchors.horizontalCenter: parent.horizontalCenter; text: status; color: stateColor(status); font.pixelSize: 10; font.bold: true }
        }
    }

    component Waveform: Canvas {
        property var values: []
        property color waveColor: appWindow.cyan
        onValuesChanged: requestPaint()
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
            const ctx = getContext("2d")
            ctx.reset()
            ctx.strokeStyle = waveColor
            ctx.lineWidth = 1.5
            ctx.globalAlpha = 0.9
            ctx.beginPath()
            const baseline = height / 2
            if (!values || values.length < 2) {
                ctx.moveTo(0, baseline); ctx.lineTo(width, baseline)
            } else {
                for (let i = 0; i < values.length; ++i) {
                    const x = i * width / (values.length - 1)
                    const y = baseline - Number(values[i]) * height * 0.43
                    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y)
                }
            }
            ctx.stroke()
        }
    }

    HudBackdrop { anchors.fill: parent }
    RowLayout {
        anchors.fill: parent
        spacing: 0
        Accessible.name: "Carlos Control Center"
        Accessible.description: "Local voice assistant controls, diagnostics, tools, and task status"

        Rectangle {
            Layout.preferredWidth: appWindow.width < 1250 ? 180 : 208
            Layout.fillHeight: true
            color: "#07111d"
            Rectangle { anchors.right: parent.right; height: parent.height; width: 1; color: "#294758" }
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 16; spacing: 8
                Item {
                    Layout.fillWidth: true; Layout.preferredHeight: 126
                    Text { y: 5; text: "PERSONAL INTELLIGENCE"; color: "#7290a5"; font.family: "Hack"; font.pixelSize: 8; font.letterSpacing: 1.4 }
                    Text { y: 28; text: "Carlos"; color: "#edfaff"; font.family: "Liberation Sans"; font.pixelSize: 50; font.bold: true; font.letterSpacing: 9 }
                    Rectangle { x: 2; y: 90; width: 32; height: 2; color: appWindow.cyan }
                    Text { x: 43; y: 85; text: "NEXUS / 01"; color: appWindow.cyan; font.family: "Hack"; font.pixelSize: 9; font.letterSpacing: 2 }
                    Text { y: 111; text: "OPERATIONS"; color: "#6f889c"; font.family: "Hack"; font.pixelSize: 8; font.letterSpacing: 2 }
                }
                ListView {
                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; contentItem: Rectangle { implicitWidth: 4; radius: 2; color: "#527f98" } }
                    id: navigation
                    objectName: "navigation"
                    Layout.fillWidth: true; Layout.fillHeight: true
                    clip: true; interactive: contentHeight > height
                    currentIndex: appWindow.testPage; spacing: 5
                    model: ["DASHBOARD", "BRAIN", "COMMANDS", "MEMORY", "SYSTEM", "TOOLS", "TASKS", "SECURITY", "LOGS", "SETTINGS", "DAILY"]
                    delegate: Item {
                        id: navItem
                        objectName: "nav-" + index
                        required property string modelData
                        required property int index
                        width: navigation.width
                        height: Math.max(32, Math.min(43, (navigation.height - 50) / 11))
                        readonly property bool selected: navigation.currentIndex === index
                        HudPanel {
                            anchors.fill: parent; visible: navItem.selected || navMouse.containsMouse || navItem.activeFocus
                            cut: 8; technical: false
                            color: navItem.selected ? "#173448" : "#101e2c"
                            lineColor: navItem.activeFocus ? "#ddfaff" : navItem.selected ? "#4ea7c4" : "#2b485b"
                            accent: navItem.selected ? appWindow.cyan : "#406077"
                        }
                        Text { x: 10; anchors.verticalCenter: parent.verticalCenter; text: String(index + 1).padStart(2,"0"); color: navItem.selected ? "#70e6ff" : "#53748b"; font.family: "Hack"; font.pixelSize: 9 }
                        Text { x: 39; anchors.verticalCenter: parent.verticalCenter; text: modelData; color: navItem.selected ? "#f0fbff" : "#9bb1c1"; font.family: "Hack"; font.pixelSize: 10; font.bold: navItem.selected; font.letterSpacing: .7 }
                        Text { anchors.right: parent.right; anchors.rightMargin: 10; anchors.verticalCenter: parent.verticalCenter; text: "›"; color: appWindow.cyan; visible: navItem.selected }
                        activeFocusOnTab: true
                        Keys.onReturnPressed: navigation.currentIndex = index
                        Keys.onSpacePressed: navigation.currentIndex = index
                        MouseArea {
                            id: navMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                            Accessible.name: modelData + " page"; Accessible.role: Accessible.Button
                            Accessible.onPressAction: navigation.currentIndex = index
                            onClicked: navigation.currentIndex = index
                        }
                    }
                }
                HudPanel {
                    Layout.fillWidth: true; Layout.preferredHeight: 72; cut: 10
                    accent: evClient.connected ? "#75e8ff" : "#ff6478"
                    Column { anchors.fill: parent; anchors.margins: 12; spacing: 6
                        Text { text: evClient.connected ? "●  CORE CONNECTED" : "○  CORE OFFLINE"; color: evClient.connected ? "#70e6ff" : "#ff6478"; font.family: "Hack"; font.pixelSize: 9 }
                        Text { width: parent.width; text: appWindow.voiceStatus(); color: "#dcebf2"; font.family: "Hack"; font.pixelSize: 9; elide: Text.ElideRight }
                    }
                }
            }
        }
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            Rectangle {
                Layout.fillWidth: true; Layout.preferredHeight: 104
                color: "#08131f"
                Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: appWindow.line }
                RowLayout {
                    anchors.fill: parent; anchors.margins: 22; spacing: 16
                    Text { text: String(navigation.currentIndex + 1).padStart(2,"0"); color: "#3b738e"; font.family: "Hack"; font.pixelSize: 34 }
                    ColumnLayout {
                        Layout.fillWidth: true; spacing: 7
                        Text { objectName: "voice-status"; Layout.fillWidth: true; text: "Carlos  //  " + appWindow.voiceStatus(); color: evClient.connected ? appWindow.cyan : "#ff6478"; font.family: "Hack"; font.pixelSize: 10; font.letterSpacing: 1; elide: Text.ElideRight; Accessible.name: text }
                        Text { Layout.fillWidth: true; text: appWindow.pageTitles[navigation.currentIndex]; color: "#edfaff"; font.pixelSize: appWindow.width < 1250 ? 21 : 26; font.bold: true; font.letterSpacing: 2.5; elide: Text.ElideRight }
                        Text { Layout.fillWidth: true; text: appWindow.pageDetails[navigation.currentIndex]; color: "#91aabb"; font.pixelSize: 11; elide: Text.ElideRight }
                    }
                    Column {
                        Layout.preferredWidth: appWindow.width < 1250 ? 150 : 230; spacing: 8
                        Text { width: parent.width; text: evClient.state; horizontalAlignment: Text.AlignRight; color: appWindow.cyan; font.family: "Hack"; font.pixelSize: 10; font.letterSpacing: 1.4 }
                        Text { width: parent.width; text: evClient.statusMessage; horizontalAlignment: Text.AlignRight; color: "#839faf"; font.pixelSize: 10; elide: Text.ElideRight }
                    }
                }
            }
            StackLayout {
                id: pageStack
                objectName: "page-stack"
                onCurrentIndexChanged: if (!appWindow.reducedMotion) pageFade.restart()
                NumberAnimation { id: pageFade; target: pageStack; property: "opacity"; from: 0.3; to: 1; duration: 170; easing.type: Easing.OutCubic }
                Layout.fillWidth: true
                Layout.fillHeight: true
                currentIndex: navigation.currentIndex

                // DASHBOARD
                Item {
                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 18
                        spacing: 14
                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.minimumWidth: 400
                            Layout.fillHeight: true
                            spacing: 14
                            SectionPanel {
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                ColumnLayout {
                                    anchors.fill: parent
                                    anchors.margins: 14
                                    RowLayout {
                                        Layout.fillWidth: true
                                        TitleText { text: "NEURAL ENGINE" }
                                        Item { Layout.fillWidth: true }
                                        Text { text: "LIVE / " + evClient.state; color: "#71a7bd"; font.family: "Hack"; font.pixelSize: 9 }
                                    }
                                    ColumnLayout {
                                        id: engineeringCard
                                        Layout.fillWidth: true
                                        property string rememberedFailure: ""
                                        function rememberFailure() {
                                            const card = get(evClient.activity, "engineering", {});
                                            if (appWindow.visible && visible && card.proposal_id !== rememberedFailure && ["FAILED", "VALIDATION_FAILED"].indexOf(card.state) >= 0) {
                                                rememberedFailure = card.proposal_id;
                                                evClient.rememberFailureCard(card.proposal_id);
                                            }
                                        }
                                        onVisibleChanged: if (visible) rememberFailure()
                                        Connections { target: evClient; function onActivityChanged() { engineeringCard.rememberFailure() } }
                                        visible: !!get(get(evClient.activity, "engineering", {}), "proposal_id", "")
                                        Text { text: "CARLOS ENGINEERING / " + get(get(evClient.activity, "engineering", {}), "state", "UNKNOWN"); color: appWindow.cyan; font.bold: true }
                                        Text { Layout.fillWidth: true; text: get(get(evClient.activity, "engineering", {}), "project", ""); color: appWindow.dim; elide: Text.ElideMiddle }
                                        Text { Layout.fillWidth: true; text: get(get(evClient.activity, "engineering", {}), "message", ""); color: appWindow.dim; wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight }
                                        RowLayout {
                                            Text { text: get(get(evClient.activity, "engineering", {}), "files_changed", 0) + " files changed"; color: appWindow.dim }
                                            HudButton { text: "CANCEL JOB"; enabled: get(get(evClient.activity, "engineering", {}), "state", "") === "RUNNING"; onClicked: evClient.callTool("development.coding_agent_cancel", {proposal_id: evClient.activity.engineering.proposal_id}) }
                                        }
                                    }
                                    AgentOrb {
                                        objectName: "agent-orb"
                                        Layout.fillWidth: true; Layout.fillHeight: true
                                        phase: !evClient.connected ? "OFFLINE" : appWindow.get(evClient.activity, "phase", "IDLE")
                                        action: appWindow.get(evClient.activity, "wait_reason", "") || appWindow.get(evClient.activity, "action", "")
                                        waveform: phase === "SPEAKING" ? evClient.outputWaveform : evClient.inputWaveform
                                        completed: appWindow.get(evClient.activity, "steps_completed", 0)
                                        total: appWindow.get(evClient.activity, "steps_total", 0)
                                        animate: appWindow.animationsRunning && visible
                                        onActivated: { appWindow.commandInputExpanded = !appWindow.commandInputExpanded; if (appWindow.commandInputExpanded) dashboardCommand.forceActiveFocus() }
                                    }
                                    Text {
                                        Layout.fillWidth: true; horizontalAlignment: Text.AlignHCenter
                                        text: voiceDiag("media_focus", "") === "HELD" ? "MEDIA QUIET  ·  YOUR CONVERSATION HAS FOCUS" : "SAY Carlos  ·  HEAR THE CHIME  ·  SPEAK NATURALLY"
                                        color: appWindow.dim; font.pixelSize: 10; font.letterSpacing: 1; elide: Text.ElideRight
                                    }
                                    RowLayout {
                                        visible: appWindow.commandInputExpanded
                                        Layout.fillWidth: true
                                        spacing: 10
                                        HudButton { text: evClient.state === "LISTENING" ? "STOP CAPTURE" : "PUSH TO TALK"; accent: evClient.state === "LISTENING" ? "#ff6478" : appWindow.cyan; onClicked: evClient.state === "LISTENING" ? evClient.stopListening() : evClient.startListening() }
                                        SciField {
                                            id: dashboardCommand
                                            objectName: "orb-command-input"
                                            Layout.fillWidth: true
                                            placeholderText: "Ask anything or issue a desktop request…"
                                            color: "#e5f9ff"
                                            placeholderTextColor: "#829fb2"
                                            selectByMouse: true
                                            background: HudPanel { color: "#071320"; cut: 8; technical: false; accent: "#466b80" }
                                            function submit() {
                                                const taskId = appWindow.get(evClient.activity, "task_id", "")
                                                if (appWindow.reviseCurrentTask && taskId) evClient.steerTask(taskId, text)
                                                else evClient.sendCommand(text)
                                                text = ""
                                            }
                                            onAccepted: submit()
                                        }
                                        HudButton { text: "SEND"; onClicked: dashboardCommand.submit() }
                                    }
                                    CheckBox {
                                        objectName: "revise-current-task"
                                        visible: appWindow.commandInputExpanded && appWindow.get(evClient.activity, "task_id", "") !== ""
                                        text: "Revise displayed task (stop remaining steps and re-observe)"
                                        checked: appWindow.reviseCurrentTask
                                        onToggled: appWindow.reviseCurrentTask = checked
                                    }
                                }
                            }
                            SectionPanel {
                                Layout.fillWidth: true
                            Layout.preferredHeight: 174
                                ColumnLayout {
                                    anchors.fill: parent
                                    anchors.margins: 14
                                    RowLayout {
                                        Layout.fillWidth: true
                                        TitleText { text: "SIGNAL MONITOR" }
                                        Item { Layout.fillWidth: true }
                                        Text { text: evClient.state; color: appWindow.cyan; font.pixelSize: 9 }
                                    }
                                    Text { text: "MICROPHONE // REAL PCM"; color: appWindow.dim; font.pixelSize: 9; font.letterSpacing: 1 }
                                    Waveform { Layout.fillWidth: true; Layout.preferredHeight: 42; values: evClient.inputWaveform; waveColor: appWindow.cyan }
                                    Text { text: "SYNTHESIS OUTPUT // REAL PCM"; color: appWindow.dim; font.pixelSize: 9; font.letterSpacing: 1 }
                                    Waveform { Layout.fillWidth: true; Layout.preferredHeight: 42; values: evClient.outputWaveform; waveColor: "#7d87ff" }
                                }
                            }
                        }
                        ColumnLayout {
                            Layout.preferredWidth: appWindow.width < 1250 ? 276 : 320
                            Layout.minimumWidth: 260
                            Layout.maximumWidth: 340
                            Layout.fillHeight: true
                            spacing: 10
                            TitleText { text: "SYSTEM VITALS" }
                            GridLayout {
                                Layout.fillWidth: true
                                columns: 2
                                columnSpacing: 9
                                rowSpacing: 9
                                MetricCard { label: "CPU"; value: percent(get(evClient.telemetry, "cpu_percent", null)); Layout.fillWidth: true }
                                MetricCard { label: "THERMAL"; value: get(get(evClient.telemetry, "cpu_temperature", {}), "celsius", "—") + " °C"; accent: Number(get(get(evClient.telemetry, "cpu_temperature", {}), "celsius", 0)) >= 90 ? "#ff6478" : "#ffca58"; Layout.fillWidth: true }
                                MetricCard { label: "MEMORY"; value: percent(nested(evClient.telemetry, "memory", "percent", null)); accent: "#76a7ff"; Layout.fillWidth: true }
                                MetricCard { label: "DISK"; value: percent(nested(evClient.telemetry, "disk", "percent", null)); accent: "#a482ff"; Layout.fillWidth: true }
                                MetricCard { label: "Carlos CORE"; value: bytes(nested(evClient.telemetry, "ev_core", "rss_bytes", null)); detail: "RESIDENT MEMORY"; accent: "#53efae"; Layout.columnSpan: 2; Layout.fillWidth: true }
                            }
                            TitleText { text: "RECENT EXECUTION" }
                            SectionPanel {
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                ListView {
                                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; contentItem: Rectangle { implicitWidth: 4; radius: 2; color: "#527f98" } }
                                    anchors.fill: parent
                                    anchors.margins: 10
                                    spacing: 7
                                    clip: true
                                    model: evClient.timeline
                                    delegate: Column {
                                        required property var modelData
                                        width: ListView.view.width
                                        spacing: 3
                                        Text { text: modelData.title; color: modelData.kind === "ERROR" ? "#ff6478" : appWindow.cyan; font.pixelSize: 9; font.bold: true; font.letterSpacing: 1 }
                                        Text { width: parent.width; text: modelData.body; color: "#b8cbd5"; font.pixelSize: 11; wrapMode: Text.WordWrap }
                                        Rectangle { width: parent.width; height: 1; color: "#172935" }
                                    }
                                }
                            }
                        }
                    }
                }

                // BRAIN
                Item {
                    BrainView {
                        objectName: "brain-view"
                        anchors.fill: parent
                        client: evClient
                        animationsRunning: appWindow.animationsRunning
                        initialExplore: brainExploreMode
                    }
                }

                // COMMANDS
                Item {
                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 18; spacing: 12
                        SectionPanel {
                            Layout.fillWidth: true; Layout.fillHeight: true
                            ListView {
                                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; contentItem: Rectangle { implicitWidth: 4; radius: 2; color: "#527f98" } }
                                id: commandTimeline
                                anchors.fill: parent; anchors.margins: 18; clip: true; spacing: 10; model: evClient.timeline
                                onCountChanged: positionViewAtEnd()
                                delegate: Item {
                                    id: bubbleRow
                                    required property var modelData
                                    width: commandTimeline.width
                                    height: conversationColumn.implicitHeight + 30
                                    Rectangle {
                                    width: bubbleRow.width * 0.86; height: bubbleRow.height
                                    x: bubbleRow.modelData.kind === "USER" ? bubbleRow.width - width : 0
                                    radius: 3
                                    color: bubbleRow.modelData.kind === "USER" ? "#172c40" : bubbleRow.modelData.kind === "ERROR" ? "#2b1219" : "#0b1b2b"
                                    border.color: bubbleRow.modelData.kind === "USER" ? "#526691" : bubbleRow.modelData.kind === "ERROR" ? "#ff6478" : "#2e5267"
                                    Rectangle { anchors.left: parent.left; anchors.top: parent.top; anchors.topMargin: 9; width: 2; height: 20; color: bubbleRow.modelData.kind === "USER" ? "#a8b2ff" : "#70e6ff" }
                                    Column { id: conversationColumn; anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 12; spacing: 5
                                        Text { text: bubbleRow.modelData.title; color: bubbleRow.modelData.kind === "ERROR" ? "#ff6478" : appWindow.cyan; font.family: "Hack"; font.pixelSize: 9; font.bold: true; font.letterSpacing: 1.2 }
                                        Text { width: parent.width; text: bubbleRow.modelData.body; color: "#e0f1f5"; font.pixelSize: 14; wrapMode: Text.WordWrap; lineHeight: 1.3 }
                                    }
                                    }
                                }
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true
                            SciField {
                                id: commandInput
                                objectName: "command-input"
                                Layout.fillWidth: true
                                placeholderText: "Talk to Carlos…"
                                color: "#e5f9ff"
                                placeholderTextColor: "#829fb2"
                                background: HudPanel { color: "#071320"; cut: 8; technical: false; accent: "#466b80" }
                                onAccepted: { evClient.sendCommand(text); text = "" }
                            }
                            HudButton { objectName: "send-command"; text: "SEND"; onClicked: { evClient.sendCommand(commandInput.text); commandInput.text = "" } }
                            HudButton { text: evClient.state === "LISTENING" ? "STOP" : "PTT"; onClicked: evClient.state === "LISTENING" ? evClient.stopListening() : evClient.startListening() }
                        }
                    }
                }

                // MEMORY
                Item {
                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 18; spacing: 12
                        RowLayout {
                            Layout.fillWidth: true
                            SciField {
                                id: memoryInput
                                Layout.fillWidth: true
                                placeholderText: "Create an explicit memory…"
                                color: "#e5f9ff"
                                placeholderTextColor: "#829fb2"
                                background: HudPanel { color: "#071320"; cut: 8; technical: false; accent: "#466b80" }
                            }
                            HudButton { text: "REMEMBER"; onClicked: { evClient.remember(memoryInput.text); memoryInput.text = "" } }
                            HudButton { text: "REFRESH"; onClicked: evClient.refreshMemories() }
                        }
                        SectionPanel {
                            Layout.fillWidth: true; Layout.fillHeight: true
                            ListView {
                                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; contentItem: Rectangle { implicitWidth: 4; radius: 2; color: "#527f98" } }
                                anchors.fill: parent; anchors.margins: 16; spacing: 9; clip: true; model: evClient.memories
                                delegate: HudPanel {
                                    required property var modelData
                                    width: ListView.view.width; height: 84; color: "#0b1c2b"; lineColor: "#30526a"; cut: 10
                                    RowLayout { anchors.fill: parent; anchors.margins: 12
                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            Text { Layout.fillWidth: true; text: modelData.content; color: "#d9edf2"; font.pixelSize: 12; wrapMode: Text.WordWrap }
                                            Text { text: "EXPLICIT  //  " + modelData.id.slice(0, 8); color: appWindow.dim; font.pixelSize: 9; font.letterSpacing: 1 }
                                        }
                                        HudButton { text: "FORGET"; accent: "#ff6478"; onClicked: evClient.forget(modelData.id) }
                                    }
                                }
                                Text { anchors.centerIn: parent; visible: parent.count === 0; text: "NO EXPLICIT MEMORIES"; color: appWindow.dim; font.pixelSize: 11; font.letterSpacing: 1.3 }
                            }
                        }
                    }
                }

                // SYSTEM
                Item {
                    GridLayout {
                        anchors.fill: parent; anchors.margins: 18; columns: 3; columnSpacing: 12; rowSpacing: 12
                        MetricCard { label: "CPU LOAD"; value: percent(get(evClient.telemetry, "cpu_percent", null)); detail: "HOST TOTAL"; Layout.fillWidth: true; Layout.fillHeight: true }
                        MetricCard { label: "CPU PACKAGE"; value: get(get(evClient.telemetry, "cpu_temperature", {}), "celsius", "—") + " °C"; detail: get(get(evClient.telemetry, "cpu_temperature", {}), "sensor", "NO SENSOR"); accent: "#ffca58"; Layout.fillWidth: true; Layout.fillHeight: true }
                        MetricCard { label: "BATTERY"; value: percent(nested(evClient.telemetry, "battery", "percent", null)); detail: nested(evClient.telemetry, "battery", "plugged", false) ? "AC POWER" : "BATTERY POWER"; accent: "#53efae"; Layout.fillWidth: true; Layout.fillHeight: true }
                        MetricCard { label: "RAM USED"; value: bytes(nested(evClient.telemetry, "memory", "used_bytes", null)); detail: percent(nested(evClient.telemetry, "memory", "percent", null)); accent: "#76a7ff"; Layout.fillWidth: true; Layout.fillHeight: true }
                        MetricCard { label: "RAM AVAILABLE"; value: bytes(nested(evClient.telemetry, "memory", "available_bytes", null)); detail: "PHYSICAL MEMORY"; accent: "#76a7ff"; Layout.fillWidth: true; Layout.fillHeight: true }
                        MetricCard { label: "SWAP"; value: bytes(nested(evClient.telemetry, "swap", "used_bytes", null)); detail: bytes(nested(evClient.telemetry, "swap", "total_bytes", null)) + " TOTAL"; accent: "#a482ff"; Layout.fillWidth: true; Layout.fillHeight: true }
                        MetricCard { label: "DISK FREE"; value: bytes(nested(evClient.telemetry, "disk", "free_bytes", null)); detail: percent(nested(evClient.telemetry, "disk", "percent", null)) + " USED"; accent: "#a482ff"; Layout.fillWidth: true; Layout.fillHeight: true }
                        MetricCard { label: "NETWORK DOWN"; value: bytes(nested(evClient.telemetry, "network", "download_bytes_per_second", null)) + "/s"; detail: nested(evClient.telemetry, "network", "connected_interfaces", "—") + " INTERFACES"; Layout.fillWidth: true; Layout.fillHeight: true }
                        MetricCard { label: "NETWORK UP"; value: bytes(nested(evClient.telemetry, "network", "upload_bytes_per_second", null)) + "/s"; detail: "LIVE RATE"; Layout.fillWidth: true; Layout.fillHeight: true }
                        MetricCard { label: "Carlos RAM"; value: bytes(nested(evClient.telemetry, "ev_core", "rss_bytes", null)); detail: nested(evClient.telemetry, "ev_core", "threads", "—") + " THREADS"; accent: "#53efae"; Layout.fillWidth: true; Layout.fillHeight: true }
                        MetricCard { label: "Carlos CPU"; value: percent(nested(evClient.telemetry, "ev_core", "cpu_percent", null)); detail: "BACKGROUND CORE"; accent: "#53efae"; Layout.fillWidth: true; Layout.fillHeight: true }
                        MetricCard { label: "UPTIME"; value: (Number(get(evClient.telemetry, "uptime_seconds", 0)) / 3600).toFixed(1) + " h"; detail: "SYSTEM"; Layout.fillWidth: true; Layout.fillHeight: true }
                    }
                }

                // TOOLS
                Item {
                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 18; spacing: 10
                        RowLayout {
                            Layout.fillWidth: true
                            TitleText { text: "REGISTERED STRUCTURED TOOLS  //  " + evClient.tools.length }
                            Item { Layout.fillWidth: true }
                            HudButton { text: "REFRESH"; onClicked: evClient.refreshTools() }
                        }
                        SectionPanel {
                            Layout.fillWidth: true; Layout.fillHeight: true
                            ListView {
                                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; contentItem: Rectangle { implicitWidth: 4; radius: 2; color: "#527f98" } }
                                anchors.fill: parent; anchors.margins: 14; spacing: 7; clip: true; model: evClient.tools
                                delegate: HudPanel {
                                    required property var modelData
                                    width: ListView.view.width; height: 70; color: "#0a1b29"; lineColor: "#29495f"; cut: 9
                                    RowLayout { anchors.fill: parent; anchors.margins: 11
                                        ColumnLayout {
                                            Layout.fillWidth: true
                                            spacing: 3
                                            Text { text: modelData.name; color: "#e0f8fd"; font.pixelSize: 12; font.bold: true }
                                            Text { Layout.fillWidth: true; text: modelData.description; color: appWindow.dim; font.pixelSize: 10; elide: Text.ElideRight }
                                        }
                                        Text { text: modelData.category; color: "#7897aa"; font.pixelSize: 9; font.letterSpacing: 1 }
                                        Rectangle { Layout.preferredWidth: 92; Layout.preferredHeight: 27; radius: 5; color: "#0b202a"; border.color: permissionColor(modelData.permission); Text { anchors.centerIn: parent; text: modelData.permission; color: permissionColor(modelData.permission); font.pixelSize: 9; font.bold: true } }
                                    }
                                }
                            }
                        }
                    }
                }

                // TASKS
                Item {
                    id: tasksPage
                    property var shownPlan: appWindow.inspectedPlan()
                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 18; spacing: 10
                        RowLayout {
                            Layout.fillWidth: true
                            TitleText { text: "TASK INSPECTOR  //  VERIFIED PLANS" }
                            Item { Layout.fillWidth: true }
                            HudButton { text: "CANCEL ACTIVE"; accent: "#ffca58"; enabled: get(evClient.activePlan, "id", "") !== ""; onClicked: evClient.cancelActivePlan() }
                            HudButton { text: "REFRESH"; onClicked: evClient.refreshPhase3() }
                        }
                        SectionPanel {
                            Layout.fillWidth: true; Layout.preferredHeight: 238
                            ColumnLayout {
                                anchors.fill: parent; anchors.margins: 14; spacing: 8
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text { text: get(tasksPage.shownPlan, "goal", "NO ACTIVE TASK"); color: "#e6f9fd"; font.pixelSize: 13; font.bold: true; Layout.fillWidth: true; elide: Text.ElideRight }
                                    Text { text: get(tasksPage.shownPlan, "status", "DORMANT"); color: stateColor(text); font.pixelSize: 10; font.bold: true }
                                }
                                Text { Layout.fillWidth: true; text: get(tasksPage.shownPlan, "request", "Carlos is ready for a typed or spoken multi-step request."); color: appWindow.dim; font.pixelSize: 10; elide: Text.ElideRight }
                                ListView {
                                    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; contentItem: Rectangle { implicitWidth: 4; radius: 2; color: "#527f98" } }
                                    Layout.fillWidth: true; Layout.fillHeight: true; spacing: 5; clip: true
                                    model: get(tasksPage.shownPlan, "steps", [])
                                    delegate: HudPanel {
                                        required property var modelData
                                        required property int index
                                        width: ListView.view.width; height: 48; color: "#0a1b29"; accent: stateColor(get(modelData, "status", "PENDING")); cut: 8
                                        RowLayout { anchors.fill: parent; anchors.margins: 9
                                            Text { text: String(index + 1).padStart(2, "0"); color: "#607f90"; font.pixelSize: 9; Layout.preferredWidth: 28 }
                                            Text { text: get(modelData, "tool", "unknown"); color: "#dff8ff"; font.pixelSize: 10; font.bold: true; Layout.preferredWidth: 235 }
                                            Text { Layout.fillWidth: true; text: get(modelData, "expected_postcondition", ""); color: appWindow.dim; font.pixelSize: 9; elide: Text.ElideRight }
                                            Text { text: get(modelData, "status", "PENDING"); color: stateColor(text); font.pixelSize: 9; font.bold: true }
                                        }
                                    }
                                    Text { anchors.centerIn: parent; visible: parent.count === 0; text: "NO PLAN IS RUNNING"; color: appWindow.dim; font.pixelSize: 10; font.letterSpacing: 1.2 }
                                }
                            }
                        }
                        RowLayout {
                            Layout.fillWidth: true; Layout.fillHeight: true; spacing: 10
                            SectionPanel {
                                Layout.fillWidth: true; Layout.fillHeight: true
                                ColumnLayout { anchors.fill: parent; anchors.margins: 13
                                    Text { text: "RECENT TASKS"; color: appWindow.cyan; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.3 }
                                    ListView {
                                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; contentItem: Rectangle { implicitWidth: 4; radius: 2; color: "#527f98" } }
                                        Layout.fillWidth: true; Layout.fillHeight: true; spacing: 6; clip: true; model: evClient.plans
                                        delegate: HudPanel {
                                            required property var modelData
                                            required property int index
                                            width: ListView.view.width; height: 78; color: "#0a1b29"; lineColor: "#29495f"; cut: 9
                                            ColumnLayout { anchors.fill: parent; anchors.margins: 9; spacing: 3
                                                RowLayout { Layout.fillWidth: true
                                                    Text { Layout.fillWidth: true; text: get(modelData, "goal", "Task"); color: "#dff8ff"; font.pixelSize: 10; font.bold: true; elide: Text.ElideRight }
                                                    Text { text: get(modelData, "status", "UNKNOWN"); color: stateColor(text); font.pixelSize: 9; font.bold: true }
                                                }
                                                Text { Layout.fillWidth: true; text: get(modelData, "request", ""); color: appWindow.dim; font.pixelSize: 9; elide: Text.ElideRight }
                                                Text { text: Number(get(modelData, "duration_ms", 0)).toFixed(1) + " ms  //  " + get(modelData, "steps", []).length + " STEPS  //  " + get(modelData, "capability_gaps", []).length + " GAPS"; color: "#688696"; font.pixelSize: 8 }
                                            }
                                            MouseArea {
                                                anchors.fill: parent
                                                cursorShape: Qt.PointingHandCursor
                                                Accessible.name: "Inspect " + get(modelData, "goal", "recent task")
                                                Accessible.role: Accessible.Button
                                                Accessible.onPressAction: appWindow.inspectedPlanId = get(modelData, "id", "")
                                                onClicked: appWindow.inspectedPlanId = get(modelData, "id", "")
                                            }
                                        }
                                    }
                                }
                            }
                            SectionPanel {
                                Layout.preferredWidth: 420; Layout.fillHeight: true
                                ColumnLayout { anchors.fill: parent; anchors.margins: 13; spacing: 7
                                    Text { text: "PLAN EVIDENCE  //  RECOVERY  //  TIMINGS"; color: appWindow.cyan; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.1 }
                                    ScrollView {
                                        Layout.fillWidth: true
                                        Layout.fillHeight: true
                                        clip: true
                                        TextArea {
                                            text: appWindow.planEvidence(tasksPage.shownPlan)
                                            readOnly: true
                                            selectByMouse: true
                                            wrapMode: TextEdit.Wrap
                                            color: "#afc8d2"
                                            font.family: "monospace"
                                            font.pixelSize: 9
                                            background: Rectangle { color: "#071019"; radius: 6; border.color: "#18313f" }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                // SECURITY
                Item {
                    ColumnLayout {
                        anchors.fill: parent; anchors.margins: 18; spacing: 10
                        RowLayout {
                            Layout.fillWidth: true
                            TitleText { text: "SECURITY CENTER  //  READ-ONLY OBSERVATION" }
                            Item { Layout.fillWidth: true }
                            Text { text: get(evClient.security, "local_only", true) ? "LOCAL ONLY" : "NETWORKED"; color: get(evClient.security, "local_only", true) ? "#53efae" : "#ff6478"; font.pixelSize: 9; font.bold: true }
                            HudButton { text: "SCAN NOW"; onClicked: evClient.refreshPhase3() }
                            HudButton { text: "RUNTIME / ADMIN READ"; onClicked: evClient.sendCommand("inspect runtime firewall as admin") }
                        }
                        GridLayout {
                            Layout.fillWidth: true; columns: 4; columnSpacing: 10
                            MetricCard { Layout.fillWidth: true; label: "SECURITY STATUS"; value: get(evClient.security, "status", "NOT SCANNED"); detail: get(evClient.security, "host", "LOCAL HOST"); accent: stateColor(value) }
                            MetricCard { Layout.fillWidth: true; label: "FIREWALL"; value: nested(evClient.security, "firewall", "status", "UNKNOWN"); detail: nested(evClient.security, "firewall", "confidence", "—") + " CONFIDENCE"; accent: stateColor(value) }
                            MetricCard { Layout.fillWidth: true; label: "EXPOSED LISTENERS"; value: String(nested(evClient.security, "network", "network_accessible_count", "—")); detail: String(nested(evClient.security, "network", "listener_count", "—")) + " TOTAL"; accent: Number(value) > 0 ? "#ffca58" : "#53efae" }
                            MetricCard { Layout.fillWidth: true; label: "SSH"; value: nested(evClient.security, "ssh", "running", false) ? "RUNNING" : "STOPPED"; detail: "PORT " + nested(evClient.security, "ssh", "configured_port", "—"); accent: nested(evClient.security, "ssh", "running", false) ? "#ffca58" : "#53efae" }
                        }
                        RowLayout {
                            Layout.fillWidth: true; Layout.fillHeight: true; spacing: 10
                            SectionPanel {
                                Layout.fillWidth: true; Layout.fillHeight: true
                                ColumnLayout { anchors.fill: parent; anchors.margins: 13
                                    Text { text: "FINDINGS WITH EVIDENCE"; color: appWindow.cyan; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.2 }
                                    ListView {
                                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; contentItem: Rectangle { implicitWidth: 4; radius: 2; color: "#527f98" } }
                                        Layout.fillWidth: true; Layout.fillHeight: true; spacing: 6; clip: true; model: get(evClient.security, "findings", [])
                                        delegate: HudPanel {
                                            required property var modelData
                                            width: ListView.view.width; height: 80; color: get(modelData, "severity", "INFO") === "HIGH" ? "#261017" : "#0b1c2b"; accent: permissionColor(get(modelData, "severity", "INFO")); cut: 10
                                            ColumnLayout { anchors.fill: parent; anchors.margins: 9; spacing: 3
                                                RowLayout { Layout.fillWidth: true
                                                    Text { text: get(modelData, "kind", "OBSERVATION"); color: "#dff8ff"; font.pixelSize: 10; font.bold: true }
                                                    Item { Layout.fillWidth: true }
                                                    Text { text: get(modelData, "severity", "INFO") + "  //  " + get(modelData, "confidence", "MEASURED"); color: "#ffca58"; font.pixelSize: 8; font.bold: true }
                                                }
                                                Text { Layout.fillWidth: true; text: get(modelData, "detail", ""); color: appWindow.dim; font.pixelSize: 9; wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight }
                                            }
                                        }
                                        Text { anchors.centerIn: parent; visible: parent.count === 0; text: "NO FINDINGS FROM THE LAST LOCAL SCAN"; color: appWindow.dim; font.pixelSize: 9 }
                                    }
                                }
                            }
                            SectionPanel {
                                Layout.preferredWidth: 430; Layout.fillHeight: true
                                ColumnLayout { anchors.fill: parent; anchors.margins: 13
                                    Text { text: "Carlos SELF-DIAGNOSTICS"; color: appWindow.cyan; font.pixelSize: 10; font.bold: true; font.letterSpacing: 1.2 }
                                    ListView {
                                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; contentItem: Rectangle { implicitWidth: 4; radius: 2; color: "#527f98" } }
                                        Layout.fillWidth: true; Layout.fillHeight: true; spacing: 5; clip: true; model: get(evClient.diagnostics, "checks", [])
                                        delegate: HudPanel {
                                            required property var modelData
                                            width: ListView.view.width; height: 54; color: "#0a1b29"; cut: 8; technical: false
                                            RowLayout { anchors.fill: parent; anchors.margins: 8
                                                ColumnLayout { Layout.fillWidth: true; spacing: 2
                                                    Text { text: get(modelData, "component", "COMPONENT").toUpperCase(); color: "#dff8ff"; font.pixelSize: 9; font.bold: true }
                                                    Text { Layout.fillWidth: true; text: get(modelData, "evidence", ""); color: appWindow.dim; font.pixelSize: 8; elide: Text.ElideMiddle }
                                                }
                                                Text { text: get(modelData, "status", "UNKNOWN"); color: stateColor(text); font.pixelSize: 9; font.bold: true }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                // LOGS
                Item {
                    SectionPanel {
                        anchors.fill: parent; anchors.margins: 18
                        ListView {
                            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; contentItem: Rectangle { implicitWidth: 4; radius: 2; color: "#527f98" } }
                            anchors.fill: parent; anchors.margins: 14; spacing: 5; clip: true; model: evClient.events
                            delegate: HudPanel {
                                required property var modelData
                                width: ListView.view.width; height: 54; cut: 8; technical: false; accent: modelData.type.indexOf("failed") >= 0 ? "#ff6478" : "#456c85"; color: modelData.type === "system.error" || modelData.type === "tool.failed" ? "#261017" : "#0b1b2a"
                                RowLayout { anchors.fill: parent; anchors.margins: 9
                                    Text { text: "#" + modelData.sequence; color: "#8ca4b4"; font.pixelSize: 9; Layout.preferredWidth: 48 }
                                    Text { text: modelData.type; color: modelData.type.indexOf("failed") >= 0 || modelData.type.indexOf("error") >= 0 ? "#ff6478" : appWindow.cyan; font.pixelSize: 10; font.bold: true; Layout.preferredWidth: 215 }
                                    Text { text: modelData.source; color: "#829dac"; font.pixelSize: 9; Layout.preferredWidth: 100 }
                                    Text { Layout.fillWidth: true; text: eventSummary(modelData); color: "#a9bdc7"; font.pixelSize: 10; elide: Text.ElideRight }
                                    Text { text: modelData.duration_ms !== undefined ? Number(modelData.duration_ms).toFixed(1) + " ms" : ""; color: "#8daabd"; font.pixelSize: 9 }
                                }
                            }
                        }
                    }
                }

                // SETTINGS
                Item {
                    Flickable {
                        anchors.fill: parent; anchors.margins: 18; contentHeight: settingsColumn.implicitHeight; clip: true
                        ColumnLayout {
                            id: settingsColumn; width: parent.width; spacing: 12
                            Component.onCompleted: evClient.refreshDaily()
                            TitleText { text: "CARLOS SETTINGS" }
                            Flow {
                                Layout.fillWidth: true; spacing: 6
                                Repeater {
                                    model: [
                                        {label: "Memory", page: 3}, {label: "Permissions", page: 5},
                                        {label: "Scenes and Power", page: 10}, {label: "Security", page: 7},
                                        {label: "Diagnostics", page: 4}, {label: "Engineering", page: 6}
                                    ]
                                    delegate: HudButton {
                                        required property var modelData
                                        text: modelData.label
                                        onClicked: navigation.currentIndex = modelData.page
                                    }
                                }
                            }
                            Repeater {
                                model: get(get(evClient.daily, "settings", {}), "fields", [])
                                delegate: RowLayout {
                                    required property var modelData
                                    Layout.fillWidth: true
                                    Text { Layout.fillWidth: true; text: modelData.section + " / " + modelData.label; color: appWindow.dim }
                                    HudButton {
                                        text: modelData.value ? "ON" : "OFF"
                                        onClicked: evClient.callTool("carlos.settings.set", {key: modelData.key, value: !modelData.value})
                                    }
                                }
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                Text { Layout.fillWidth: true; text: "HoloHand gesture control"; color: appWindow.dim }
                                HudButton { text: "STATUS"; onClicked: evClient.callTool("holohand.status", {}) }
                                HudButton { text: "PAUSE"; onClicked: evClient.callTool("holohand.set_paused", {paused: true}) }
                                HudButton { text: "RESUME"; onClicked: evClient.callTool("holohand.set_paused", {paused: false}) }
                            }
                            Text {
                                Layout.fillWidth: true; color: appWindow.dim; wrapMode: Text.WordWrap
                                text: "AI: local first. To request cloud reasoning, begin with ‘use cloud:’ in NORMAL privacy mode.\nPresence uses lock and explicit interaction evidence; camera identity is not inferred.\nMobile access uses the paired HoloHand app and Tailscale."
                            }
                            TitleText { text: "SYSTEM READINESS" }
                            Repeater {
                                model: Object.keys(get(get(evClient.daily, "readiness", {}), "components", {}))
                                delegate: Text {
                                    required property string modelData
                                    Layout.fillWidth: true; color: appWindow.dim
                                    text: modelData + " / " + evClient.daily.readiness.components[modelData]
                                }
                            }
                            TitleText { text: "CORE LINK" }
                            SectionPanel {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 105
                                RowLayout {
                                    anchors.fill: parent
                                    anchors.margins: 16
                                    ColumnLayout {
                                        Layout.fillWidth: true
                                        Text { text: evClient.connected ? "CONNECTED OVER PRIVATE UNIX SOCKET" : "DISCONNECTED"; color: evClient.connected ? "#53efae" : "#ff6478"; font.pixelSize: 13; font.bold: true }
                                        Text { text: evClient.statusMessage; color: appWindow.dim; font.pixelSize: 10 }
                                    }
                                    HudButton { text: "RECONNECT"; onClicked: evClient.retryCore() }
                                    HudButton { text: "REFRESH"; onClicked: evClient.refreshSnapshot() }
                                }
                            }
                            TitleText { text: "AI PROVIDER" }
                            SectionPanel {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 125
                                Column {
                                    anchors.fill: parent
                                    anchors.margins: 16
                                    spacing: 8
                                    Text { text: get(evClient.provider, "active", "offline").toUpperCase(); color: appWindow.cyan; font.pixelSize: 16; font.bold: true; font.letterSpacing: 1.2 }
                                    Text { text: "MODEL  //  " + get(evClient.provider, "model", "not configured"); color: "#b3c8d2"; font.pixelSize: 11 }
                                    Text { text: get(evClient.provider, "reason", ""); color: appWindow.dim; font.pixelSize: 10 }
                                }
                            }
                            TitleText { text: "PERSONALITY // LIVE DELIVERY" }
                            SectionPanel {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 205
                                ColumnLayout {
                                    anchors.fill: parent
                                    anchors.margins: 16
                                    spacing: 10
                                    Text {
                                        Layout.fillWidth: true
                                        text: "These settings change local conversation immediately. Tools report execution evidence; code execution and existing-file text replacement require approval."
                                        color: appWindow.dim
                                        font.pixelSize: 10
                                        wrapMode: Text.WordWrap
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        HudButton {
                                            Layout.fillWidth: true
                                            text: "RESPONSE // " + String(get(evClient.personality, "response_length", "normal")).toUpperCase()
                                            onClicked: evClient.updatePersonality("response_length", appWindow.nextChoice(get(evClient.personality, "response_length", "normal"), ["minimal", "normal", "detailed"]))
                                        }
                                        HudButton {
                                            Layout.fillWidth: true
                                            text: "TONE // " + String(get(evClient.personality, "tone", "natural")).toUpperCase()
                                            onClicked: evClient.updatePersonality("tone", appWindow.nextChoice(get(evClient.personality, "tone", "natural"), ["calm", "natural", "professional", "custom"]))
                                        }
                                        HudButton {
                                            Layout.fillWidth: true
                                            text: "TECH // " + String(get(evClient.personality, "technical_language", "balanced")).toUpperCase()
                                            onClicked: evClient.updatePersonality("technical_language", appWindow.nextChoice(get(evClient.personality, "technical_language", "balanced"), ["simple", "balanced", "technical"]))
                                        }
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        HudButton {
                                            Layout.fillWidth: true
                                            text: "WORKING // " + String(get(evClient.personality, "working_verbosity", "minimal")).toUpperCase()
                                            onClicked: evClient.updatePersonality("working_verbosity", appWindow.nextChoice(get(evClient.personality, "working_verbosity", "minimal"), ["silent", "minimal", "conversational"]))
                                        }
                                        HudButton {
                                            Layout.fillWidth: true
                                            text: "ACK // " + String(get(evClient.personality, "acknowledgements", "important_only")).replace("_", " ").toUpperCase()
                                            onClicked: evClient.updatePersonality("acknowledgements", appWindow.nextChoice(get(evClient.personality, "acknowledgements", "important_only"), ["off", "important_only", "normal"]))
                                        }
                                        HudButton {
                                            Layout.fillWidth: true
                                            text: "VOICE COLOR // " + Math.round(Number(get(evClient.personality, "voice_expressiveness", 0.62)) * 100) + "%"
                                            onClicked: {
                                                const current = Number(get(evClient.personality, "voice_expressiveness", 0.62))
                                                const next = current < 0.45 ? 0.5 : current < 0.58 ? 0.62 : current < 0.72 ? 0.8 : 0.35
                                                evClient.updatePersonality("voice_expressiveness", next)
                                            }
                                        }
                                    }
                                }
                            }
                            TitleText { text: "VOICE PIPELINE" }
                            SectionPanel {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 205
                                ColumnLayout {
                                    anchors.fill: parent
                                    anchors.margins: 16
                                    RowLayout {
                                        Layout.fillWidth: true
                                        StatusPill { Layout.fillWidth: true; label: "MIC"; status: get(evClient.voice, "capture_available", false) ? voiceDiag("capture_state", "IDLE") : "UNAVAILABLE" }
                                        Text { text: "›"; color: appWindow.cyan; font.pixelSize: 18 }
                                        StatusPill { Layout.fillWidth: true; label: "VAD"; status: voiceDiag("voice_activity", false) ? "ACTIVE" : "IDLE" }
                                        Text { text: "›"; color: appWindow.cyan; font.pixelSize: 18 }
                                        StatusPill { Layout.fillWidth: true; label: "STT"; status: voiceDiag("stt_state", get(evClient.voice, "stt_available", false) ? "READY" : "UNAVAILABLE") }
                                        Text { text: "›"; color: appWindow.cyan; font.pixelSize: 18 }
                                        StatusPill { Layout.fillWidth: true; label: "INTENT"; status: voiceDiag("detected_intent", "") !== "" ? "SUCCESS" : "IDLE" }
                                        Text { text: "›"; color: appWindow.cyan; font.pixelSize: 18 }
                                        StatusPill { Layout.fillWidth: true; label: "TOOL"; status: voiceDiag("selected_tool", "") !== "" ? voiceDiag("tool_result", "ACTIVE") : "IDLE" }
                                        Text { text: "›"; color: appWindow.cyan; font.pixelSize: 18 }
                                        StatusPill { Layout.fillWidth: true; label: "TTS"; status: voiceDiag("tts_state", get(evClient.voice, "tts_available", false) ? "READY" : "UNAVAILABLE") }
                                    }
                                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: appWindow.line }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        Text { text: "WAKE  //  " + voiceDiag("wake_state", "IDLE"); color: stateColor(voiceDiag("wake_state", "IDLE")); font.pixelSize: 10; font.bold: true }
                                        Text { text: voiceDiag("wake_guard", "READY") === "MEDIA_REQUIRES_HEY" ? "SAY HEY Carlos / MEDIA GUARD" : "SAY HEY Carlos"; color: appWindow.dim; font.pixelSize: 9 }
                                        Item { Layout.fillWidth: true }
                                        Text { text: "STT  //  " + get(evClient.voice, "stt_model", "—"); color: appWindow.dim; font.pixelSize: 9 }
                                        Text { text: "VOICE  //  " + get(get(evClient.voice, "tts", {}), "voice", "—"); color: appWindow.dim; font.pixelSize: 9 }
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        HudButton { text: "TEST MICROPHONE"; onClicked: evClient.testMicrophone() }
                                        HudButton { text: "TEST WAKE WORD"; onClicked: evClient.testWakeWord() }
                                        HudButton { text: "TEST TRANSCRIPTION"; onClicked: evClient.testTranscription() }
                                        HudButton { text: "TEST VOICE"; onClicked: evClient.testVoice() }
                                        HudButton { text: "FULL PIPELINE"; accent: "#53efae"; onClicked: evClient.testFullVoicePipeline() }
                                    }
                                    Text { text: "Full pipeline: say a read-only request such as ‘what’s my RAM usage?’; unsafe or unmatched intents fail closed."; color: appWindow.dim; font.pixelSize: 9 }
                                }
                            }
                            TitleText { text: "VOICE DIAGNOSTICS // REAL VALUES" }
                            SectionPanel {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 395
                                ColumnLayout {
                                    anchors.fill: parent; anchors.margins: 16; spacing: 10
                                    RowLayout {
                                        Layout.fillWidth: true
                                        ColumnLayout { Layout.fillWidth: true; Text { text: "MICROPHONE"; color: appWindow.dim; font.pixelSize: 9 } Text { Layout.fillWidth: true; text: voiceDiag("microphone", "NO DEVICE"); color: "#d7eef5"; font.pixelSize: 11; elide: Text.ElideMiddle } }
                                        ColumnLayout { Layout.preferredWidth: 140; Text { text: "PIPEWIRE"; color: appWindow.dim; font.pixelSize: 9 } Text { text: voiceDiag("pipewire", "UNAVAILABLE"); color: stateColor(voiceDiag("pipewire", "UNAVAILABLE")); font.pixelSize: 11; font.bold: true } }
                                        ColumnLayout { Layout.preferredWidth: 190; Text { text: "FORMAT"; color: appWindow.dim; font.pixelSize: 9 } Text { text: voiceDiag("sample_rate", "—") + " Hz / " + voiceDiag("channels", "—") + " ch / " + voiceDiag("pcm_format", "—"); color: "#d7eef5"; font.pixelSize: 11 } }
                                    }
                                    RowLayout {
                                        Layout.fillWidth: true
                                        MetricCard { Layout.fillWidth: true; label: "INPUT RMS"; value: Number(voiceDiag("input_rms", 0)).toFixed(4); detail: "LIVE" }
                                        MetricCard { Layout.fillWidth: true; label: "INPUT PEAK"; value: Number(voiceDiag("input_peak", 0)).toFixed(4); detail: voiceDiag("capture_health", "UNTESTED"); accent: stateColor(voiceDiag("capture_health", "IDLE")) }
                                        MetricCard { Layout.fillWidth: true; label: "NOISE FLOOR"; value: Number(voiceDiag("noise_floor", 0)).toFixed(5); detail: voiceDiag("voice_activity", false) ? "VOICE ACTIVE" : "NO VOICE" }
                                        MetricCard { Layout.fillWidth: true; label: "STT LATENCY"; value: voiceDiag("stt_latency_ms", null) === null ? "—" : Number(voiceDiag("stt_latency_ms", 0)).toFixed(0) + " ms"; detail: Number(voiceDiag("stt_audio_duration_ms", 0)).toFixed(0) + " ms AUDIO"; accent: "#76a7ff" }
                                    }
                                    Waveform { Layout.fillWidth: true; Layout.preferredHeight: 58; values: evClient.inputWaveform }
                                    Text {
                                        objectName: "wake-worker-health"
                                        Layout.fillWidth: true
                                        text: "MIC STREAM / " + (get(evClient.voice, "microphone_active", false) ? "LIVE" : "NO AUDIO")
                                            + "    AUDIO PENDING ACK / " + get(voiceDiag("wake_worker_health", {}), "backlog_ms", "—") + " ms"
                                            + "    LOCAL BACKUP / " + get(voiceDiag("wake_speech_backup", {}), "state", "IDLE")
                                            + "    SPEECH / " + Math.round(Number(get(voiceDiag("wake_speech_backup", {}), "speech_probability", 0)) * 100) + "%"
                                        color: get(evClient.voice, "microphone_active", false) ? appWindow.cyan : "#ffca58"
                                        font.family: "Hack"; font.pixelSize: 9; wrapMode: Text.WordWrap
                                    }
                                    GridLayout {
                                        Layout.fillWidth: true; columns: 2; columnSpacing: 16; rowSpacing: 5
                                        Text { text: "RAW TRANSCRIPT"; color: appWindow.dim; font.pixelSize: 9 }
                                        Text { Layout.fillWidth: true; text: voiceDiag("raw_transcript", "—"); color: "#e4f7fb"; font.pixelSize: 11; wrapMode: Text.WordWrap }
                                        Text { text: "NORMALIZED"; color: appWindow.dim; font.pixelSize: 9 }
                                        Text { Layout.fillWidth: true; text: voiceDiag("normalized_transcript", "—"); color: appWindow.cyan; font.pixelSize: 11; wrapMode: Text.WordWrap }
                                        Text { text: "INTENT"; color: appWindow.dim; font.pixelSize: 9 }
                                        Text { Layout.fillWidth: true; text: voiceDiag("detected_intent", "—"); color: "#bcd0da"; font.pixelSize: 10; wrapMode: Text.WordWrap }
                                        Text { text: "TOOL / PERMISSION"; color: appWindow.dim; font.pixelSize: 9 }
                                        Text { Layout.fillWidth: true; text: voiceDiag("selected_tool", "—") + " // " + voiceDiag("permission_class", "—") + " // " + voiceDiag("tool_result", "—"); color: "#bcd0da"; font.pixelSize: 10 }
                                    }
                                }
                            }
                            TitleText { text: "VOICE & INTERACTION" }
                            RowLayout {
                                Text { text: "PRIVACY"; color: appWindow.dim }
                                ComboBox {
                                    model: ["NORMAL", "LOCAL ONLY", "PRIVATE SESSION", "DO NOT LISTEN", "GUEST"]
                                    currentIndex: Math.max(0, model.indexOf(get(evClient.voice, "privacy_profile", "NORMAL")))
                                    onActivated: evClient.setPrivacyProfile(currentText)
                                }
                                Text { text: "Private conversations stay in RAM. Guest restricts personal tools."; color: appWindow.dim; font.pixelSize: 11 }
                            }
                            SectionPanel {
                                Layout.fillWidth: true; Layout.preferredHeight: 130
                                RowLayout {
                                    anchors.fill: parent; anchors.margins: 16
                                    ColumnLayout { Layout.fillWidth: true; Text { text: get(evClient.voice, "privacy_mode", false) ? "MICROPHONE OFF" : get(evClient.voice, "wake_active", false) ? "WAKE LISTENING" : "WAKE INACTIVE"; color: get(evClient.voice, "privacy_mode", false) ? "#ffca58" : "#53efae"; font.pixelSize: 12; font.bold: true } Text { text: get(evClient.voice, "wake_reason", ""); color: appWindow.dim; font.pixelSize: 10 } }
                                    HudButton { text: get(evClient.voice, "wake_paused", false) ? "RESUME WAKE" : "PAUSE WAKE"; onClicked: evClient.setWakePaused(!get(evClient.voice, "wake_paused", false)) }
                                    HudButton { text: get(evClient.voice, "privacy_mode", false) ? "DISABLE PRIVACY" : "ENABLE PRIVACY"; accent: "#ffca58"; onClicked: evClient.setPrivacyMode(!get(evClient.voice, "privacy_mode", false)) }
                                    HudButton { text: "STOP SPEAKING"; accent: "#ff6478"; onClicked: evClient.stopSpeaking() }
                                }
                            }
                            TitleText { text: "PRIVACY GUARANTEES" }
                            SectionPanel { Layout.fillWidth: true; Layout.preferredHeight: 120; Text { anchors.fill: parent; anchors.margins: 16; text: "• Ambient audio is processed locally and only a short in-memory wake buffer is retained.\n• PCM is discarded immediately after each interaction and is never uploaded or permanently saved.\n• Privacy mode stops capture, wake detection, follow-up listening, and releases audio buffers.\n• Tools report execution evidence; code execution and existing-file text replacement require approval."; color: "#aac1cc"; font.pixelSize: 11; lineHeight: 1.45 } }
                        }
                    }
                }
                DailyView { client: evClient; initialPicker: appWindow.testScenePicker }
            }
            Rectangle {
                Layout.fillWidth: true; Layout.preferredHeight: 34; color: "#07121d"
                Rectangle { width: parent.width; height: 1; color: "#2a4558" }
                RowLayout {
                    anchors.fill: parent; anchors.leftMargin: 20; anchors.rightMargin: 16; spacing: 20
                    Text { text: get(evClient.voice,"privacy_profile","NORMAL") + " / " + (get(evClient.voice,"privacy_mode",false) ? "MIC OFF" : get(evClient.voice,"wake_active",false) ? "WAKE ONLINE" : "WAKE INACTIVE"); color: "#8cb5c7"; font.family: "Hack"; font.pixelSize: 9 }
                    Text { text: "TOOLS / " + evClient.tools.length; color: "#6d93aa"; font.family: "Hack"; font.pixelSize: 9 }
                    Item { Layout.fillWidth: true }
                    SciButton { objectName: "motion-toggle"; text: appWindow.reducedMotion ? "MOTION OFF" : "MOTION AUTO"; implicitHeight: 25; topPadding: 5; bottomPadding: 5; onClicked: evClient.callTool("carlos.settings.set", {key: "hud_reduce_motion", value: !appWindow.reducedMotion}) }
                    SciButton { text: "STOP VOICE"; implicitHeight: 25; topPadding: 5; bottomPadding: 5; accent: "#f2b970"; onClicked: evClient.stopSpeaking() }
                    SciButton { objectName: "stop-all-actions"; text: "STOP ALL"; implicitHeight: 25; topPadding: 5; bottomPadding: 5; accent: "#ff6478"; onClicked: evClient.sendCommand("stop everything") }
                }
            }
        }
    }

    HudWindow {
        id: voiceHud
        objectName: "voice-hud"
        property bool interactionActive: ["AWAKE", "LISTENING", "TRANSCRIBING", "THINKING", "RETRIEVING_MEMORY", "USING_TOOL", "WAITING_FOR_CONFIRMATION", "SPEAKING"].indexOf(evClient.state) >= 0
        property var engineering: appWindow.get(evClient.activity, "engineering", {})
        property string rememberedFailure: ""
        property bool showFailure: false
        readonly property bool engineeringActive: engineering.state === "RUNNING" || showFailure
        function observeEngineering() {
            if (appWindow.hudEnabled && !interactionActive && ["FAILED", "VALIDATION_FAILED"].indexOf(engineering.state) >= 0 && engineering.proposal_id !== rememberedFailure) {
                rememberedFailure = engineering.proposal_id;
                showFailure = true;
                failureTimer.restart();
                evClient.rememberFailureCard(engineering.proposal_id);
            }
        }
        onEngineeringChanged: observeEngineering()
        onInteractionActiveChanged: if (!interactionActive) observeEngineering()
        Timer { id: failureTimer; interval: 12000; onTriggered: voiceHud.showFailure = false }
        width: 510
        height: 142
        visible: appWindow.hudEnabled && (interactionActive || engineeringActive)
        // Do not make this status-only surface a transient of the control
        // center: some Wayland compositors activate the transient's parent
        // when it is mapped, stealing focus from a desktop action target.
        transientParent: null
        // Wayland xdg-toplevel cannot promise no activation just from Qt's
        // WindowDoesNotAcceptFocus hint. Give ONLY this overlay a native layer
        // surface, with keyboard interactivity disabled at compositor level.
        // Zero exclusion leaves the user's panels/work area completely intact.
        color: "transparent"
        flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
        x: Screen.virtualX + Math.round((Screen.width - width) / 2)
        y: Screen.virtualY + Screen.desktopAvailableHeight - height - 18

        function titleForState() {
            if (!interactionActive && engineeringActive) return "Carlos Engineering // " + engineering.state
            if (evClient.state === "WAITING_FOR_CONFIRMATION") return "Carlos  //  NEEDS CONFIRMATION"
            if (evClient.state === "USING_TOOL") return "Carlos  //  " + evClient.detail.toUpperCase()
            if (evClient.state === "TRANSCRIBING" || evClient.state === "THINKING" || evClient.state === "RETRIEVING_MEMORY") return "Carlos  //  THINKING"
            return "Carlos  //  " + evClient.state
        }

        HudPanel {
            anchors.fill: parent
            anchors.bottomMargin: 18
            cut: 20
            color: "#f0081724"
            accent: evClient.state === "WAITING_FOR_CONFIRMATION" ? "#ffca58" : appWindow.cyan
            Rectangle { anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom; width: 4; radius: 2; color: evClient.state === "WAITING_FOR_CONFIRMATION" ? "#ffca58" : appWindow.cyan }
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 15; spacing: 6
                RowLayout {
                    Layout.fillWidth: true
                    Text { text: voiceHud.titleForState(); color: "#e8fbff"; font.pixelSize: 12; font.bold: true; font.letterSpacing: 1.5 }
                    Item { Layout.fillWidth: true }
                    Text { text: evClient.state === "LISTENING" && voiceDiag("follow_up_state", "IDLE") === "ACTIVE" ? "FOLLOW-UP" : voiceDiag("capture_health", ""); color: stateColor(text); font.pixelSize: 9; font.bold: true }
                }
                Waveform {
                    visible: voiceHud.interactionActive
                    Layout.fillWidth: true; Layout.preferredHeight: 38
                    values: evClient.state === "SPEAKING" ? evClient.outputWaveform : evClient.inputWaveform
                    waveColor: evClient.state === "WAITING_FOR_CONFIRMATION" ? "#ffca58" : appWindow.cyan
                }
                Text {
                    Layout.fillWidth: true
                    text: !voiceHud.interactionActive && voiceHud.engineeringActive ? appWindow.get(voiceHud.engineering, "message", "Working in an isolated project checkout…")
                          : evClient.state === "LISTENING" ? "Listening on " + voiceDiag("microphone", "microphone")
                          : evClient.state === "TRANSCRIBING" ? "Processing real microphone audio locally…"
                          : voiceDiag("normalized_transcript", "") !== "" ? voiceDiag("normalized_transcript", "") : evClient.detail
                    color: "#a9c3ce"; font.pixelSize: 10; elide: Text.ElideRight
                }
            }
            MouseArea { anchors.fill: parent; onClicked: { appWindow.show(); appWindow.raise(); appWindow.requestActivate() } }
        }
    }

    Rectangle {
        anchors.fill: parent
        color: "#b0060b10"
        visible: evClient.confirmation.id !== undefined && evClient.confirmation.id !== ""
        z: 100
        MouseArea { anchors.fill: parent }
        HudPanel {
            width: Math.min(600, parent.width - 60)
            height: 300
            anchors.centerIn: parent
            cut: 20
            color: "#0b1720"
            accent: permissionColor(get(evClient.confirmation, "permission", "SENSITIVE"))
            ColumnLayout {
                anchors.fill: parent; anchors.margins: 24; spacing: 13
                Text { text: "PERMISSION GATE"; color: permissionColor(get(evClient.confirmation, "permission", "SENSITIVE")); font.pixelSize: 11; font.bold: true; font.letterSpacing: 2 }
                Text { text: get(evClient.confirmation, "tool", "UNKNOWN TOOL"); color: "#e6f9fd"; font.pixelSize: 20; font.bold: true }
                Text { Layout.fillWidth: true; text: get(evClient.confirmation, "reason", "This action requires explicit approval."); color: "#a9c0cb"; font.pixelSize: 12; wrapMode: Text.WordWrap }
                Text { text: "CLASSIFICATION  //  " + get(evClient.confirmation, "permission", "SENSITIVE"); color: "#748f9f"; font.pixelSize: 10; font.letterSpacing: 1 }
                Item { Layout.fillHeight: true }
                RowLayout {
                    Layout.fillWidth: true
                    Item { Layout.fillWidth: true }
                    HudButton { text: "DENY"; accent: "#ff6478"; onClicked: evClient.respondToConfirmation(false) }
                    HudButton { text: "APPROVE ONCE"; accent: "#53efae"; onClicked: evClient.respondToConfirmation(true) }
                }
            }
        }
    }
}
