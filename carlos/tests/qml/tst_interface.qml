import QtQuick
import QtQuick.Controls
import QtTest
import "../../ui/qml" as EV

TestCase {
    id: testCase
    name: "NexusInterface"
    when: windowShown
    width: 1080; height: 680
    property bool backgroundMode: false
    property bool brainExploreMode: false
    property var evClient: mock
    property var ui
    QtObject {
        id: mock
        signal sceneActivated(string hud)
        property bool connected: true
        property string state: "DORMANT"
        property string detail: "Isolated UI test; no desktop connection"
        property string statusMessage: "TEST CONNECTION"
        property var voice: ({wake_active:true})
        property var telemetry: ({resource_mode:"NORMAL"})
        property var provider: ({active:"test",model:"fixture"})
        property var cognition: ({})
        property var confirmation: ({})
        property var events: []
        property var timeline: [{kind:"USER",title:"ME",body:"Fixture request"},{kind:"ASSISTANT",title:"E.V.",body:"Fixture response, never executed."}]
        property var memories: []
        property var tools: []
        property var plans: []
        property var activePlan: ({})
        property var activity: ({phase:"IDLE"})
        property var insights: []
        property var security: ({})
        property var latency: ({})
        property var diagnostics: ({})
        property var personality: ({})
        property var daily: ({reminders:[],aliases:{},routines:{},scenes:[],spotify:{}})
        property var toolResult: ({})
        property var inputWaveform: []
        property var outputWaveform: []
        property var activeNodes: []
        property var commands: []
        property var calls: []
        function refreshDaily() {}
        function retryCore() {}
        function rememberFailureCard(id) {}
        function refreshMemories() {}
        function refreshPhase3() {}
        function refreshTools() {}
        function refreshSnapshot() {}
        function sendCommand(text) { commands = commands.concat([text]) }
        function steerTask(id,text) { calls = calls.concat([{task_id:id,text:text}]) }
        function callTool(name,args) { calls = calls.concat([{name:name,args:args}]) }
        function stopSpeaking() {}
    }
    Component { id: appComponent; EV.Main { width:1080; height:680 } }
    function init() {
        mock.commands=[]; mock.calls=[]
        mock.voice={wake_active:true}
        mock.daily={reminders:[],aliases:{},routines:{},scenes:[{id:"test-scene",name:"Fixture scene",live:false,preview:Qt.resolvedUrl("../../ui/assets/ev-neural-brain.png"),accent:"#70e6ff",background:"#061320",secondary:"#b1baff"}],spotify:{}}
        ui=createTemporaryObject(appComponent,null)
        verify(ui !== null)
        ui.reducedMotion=true
        waitForRendering(ui.contentItem)
    }
    function cleanup() { ui.close() }
    function test_insight_never_executes_until_explicit_click() {
        mock.insights = [{id:"thermal", title:"CPU hot", detail:"Inspect only", action:{tool:"system.get_temperature", arguments:{}}}]
        click("nav-10")
        const repeater = item("system-insights")
        tryVerify(function() { return repeater.itemAt(0) !== null })
        const inspectButton = findChild(repeater.itemAt(0), "insight-inspect-thermal")
        const dismissButton = findChild(repeater.itemAt(0), "insight-dismiss-thermal")
        verify(inspectButton !== null)
        verify(dismissButton !== null)
        compare(mock.calls.length, 0)
        inspectButton.clicked()
        compare(mock.calls.length, 1)
        compare(mock.calls[0].name, "system.get_temperature")
        dismissButton.clicked()
        compare(mock.calls[1].name, "agent.insights.dismiss")
        compare(mock.calls[1].args.id, "thermal")
        mock.insights = []
    }
    function test_voice_overlay_never_requests_keyboard_focus_or_reserves_panel_space() {
        const hud = findChild(ui, "voice-hud")
        verify(hud !== null)
        verify(hud.passiveSurface)
        compare(hud.transientParent, null)
        verify((hud.flags & Qt.WindowDoesNotAcceptFocus) !== 0)
        for (const state of ["LISTENING", "USING_TOOL", "SPEAKING", "DORMANT"]) {
            mock.state = state
            tryCompare(hud, "visible", state !== "DORMANT")
            verify(hud.passiveSurface)
        }
    }
    function test_voice_status_explains_idle_listening_and_privacy() {
        mock.voice = {wake_active:true,diagnostics:{}}
        compare(ui.voiceStatus(), "LISTENING FOR Carlos")
        mock.voice = {wake_active:false,privacy_mode:true}
        compare(ui.voiceStatus(), "MICROPHONE OFF / PRIVACY")
        mock.voice = {wake_active:true,diagnostics:{wake_speech_backup:{state:"CHECKING"}}}
        compare(ui.voiceStatus(), "CHECKING YOUR NAME / LOCAL")
        mock.voice = {wake_active:true,diagnostics:{wake_input_quality:"LOUD_NON_SPEECH"}}
        compare(ui.voiceStatus(), "CHECK MIC / LOUD INPUT WITHOUT CLEAR SPEECH")
        mock.voice = {wake_active:true,diagnostics:{}}
    }
    function test_orb_shows_real_wait_state_and_expands_input() {
        mock.activity = {phase:"WAITING",task_id:"exact-task",wait_reason:"Observing declared conditions"}
        const orb = item("agent-orb")
        tryCompare(orb, "phase", "WAITING")
        compare(ui.commandInputExpanded,false)
        orb.activated()
        tryVerify(function() { return item("orb-command-input").visible })
        item("orb-command-input").text = "use another folder"
        ui.reviseCurrentTask = true
        item("orb-command-input").submit()
        compare(mock.calls.length,1)
        compare(mock.calls[0].task_id,"exact-task")
        compare(mock.commands.length,0)
        mock.activity = {phase:"IDLE"}
    }
    function test_stop_all_sends_only_the_explicit_stop_request() {
        click("stop-all-actions")
        compare(mock.commands.length,1)
        compare(mock.commands[0],"stop everything")
        compare(mock.calls.length,0)
    }
    function test_listener_health_is_readable_without_executing_actions() {
        click("nav-9")
        mock.voice={wake_active:true,microphone_active:true,diagnostics:{wake_worker_health:{backlog_ms:250},wake_speech_backup:{state:"LISTENING"}}}
        const label=item("wake-worker-health")
        tryVerify(function() { return label.text.indexOf("MIC STREAM / LIVE") >= 0 && label.text.indexOf("250 ms") >= 0 })
        mock.voice={wake_active:false,microphone_active:false,diagnostics:{}}
        tryVerify(function() { return label.text.indexOf("NO AUDIO") >= 0 })
        compare(mock.commands.length,0)
        compare(mock.calls.length,0)
    }
    function item(name) {
        let found
        if(name.indexOf("nav-") === 0) {
            const nav=findChild(ui,"navigation")
            const index=Number(name.slice(4))
            tryVerify(function() { return nav.itemAtIndex(index) !== null })
            found=nav.itemAtIndex(index)
        } else found=findChild(ui,name)
        verify(found !== null,"Missing " + name)
        return found
    }
    function click(name) { const target=item(name); mouseClick(target,target.width/2,target.height/2) }
    function test_all_navigation_pages_fit_and_do_not_execute() {
        const stack=item("page-stack")
        for(let i=0;i<11;++i) {
            click("nav-"+i)
            compare(stack.currentIndex,i)
            verify(stack.width>600 && stack.height>450)
        }
        compare(mock.commands.length,0); compare(mock.calls.length,0)
    }
    function test_brain_entry_zoom_and_return() {
        click("nav-1")
        const brain=item("brain-view")
        compare(brain.stage,0)
        click("enter-brain-network")
        compare(brain.stage,1)
        const graph=item("brain-explorer")
        click("brain-zoom-in")
        verify(graph.zoomLevel>1)
        click("brain-back")
        compare(brain.stage,0)
        compare(mock.commands.length,0)
    }
    function test_wallpaper_picker_opens_closes_without_applying() {
        click("nav-10")
        click("open-scene-picker")
        tryCompare(item("scene-picker"),"opened",true)
        click("close-scene-picker")
        tryCompare(item("scene-picker"),"opened",false)
        compare(mock.calls.length,0)
    }
    function test_personal_library_save_is_explicit_and_keeps_title() {
        click("nav-10")
        item("personal-library-kind").currentIndex=1
        item("personal-library-title").text="Fixture task, not a command"
        const save=item("personal-library-save")
        verify(save.enabled)
        save.clicked()
        compare(mock.calls.length,1)
        compare(mock.calls[0].name,"tasks.create")
        compare(mock.calls[0].args.title,"Fixture task, not a command")
        compare(mock.commands.length,0)
    }
    function test_native_settings_button_requests_only_selected_page() {
        click("nav-10")
        const pages=item("native-settings-pages")
        tryVerify(function() { return pages.itemAt(7) !== null })
        compare(pages.itemAt(7).modelData,"touchpad")
        pages.itemAt(7).clicked()
        compare(mock.calls.length,1)
        compare(mock.calls[0].name,"settings.open")
        compare(mock.calls[0].args.page,"touchpad")
        compare(mock.commands.length,0)
    }
    function test_command_button_keeps_exact_payload_and_clears_field() {
        click("nav-2")
        item("command-input").text="test fixture only"
        click("send-command")
        compare(mock.commands,["test fixture only"])
        compare(item("command-input").text,"")
    }
    function test_scene_card_sends_only_selected_fixture_id() {
        click("nav-10")
        click("open-scene-picker")
        tryCompare(item("scene-picker"),"opened",true)
        const list=item("scene-list")
        tryVerify(function() { return list.itemAtIndex(0) !== null })
        const card=list.itemAtIndex(0)
        mouseClick(card,card.width/2,card.height/2)
        compare(mock.calls.length,1)
        compare(mock.calls[0].name,"scenes.apply")
        compare(mock.calls[0].args.name,"test-scene")
    }
    function test_reduced_motion_switch_stops_ambient_animation() {
        click("motion-toggle")
        compare(mock.calls[0].name,"carlos.settings.set")
        compare(mock.calls[0].args.key,"hud_reduce_motion")
        compare(mock.calls[0].args.value,false)
        ui.reducedMotion = Qt.binding(function() { return ui.settingValue("hud_reduce_motion", false) })
        mock.daily = {settings:{fields:[{key:"hud_reduce_motion",value:false}]}}
        tryCompare(ui,"reducedMotion",false)
        click("motion-toggle")
        compare(mock.calls[1].args.value,true)
        mock.daily = {settings:{fields:[{key:"hud_reduce_motion",value:true}]}}
        tryCompare(ui,"reducedMotion",true)
        compare(ui.animationsRunning,false)
    }
}
