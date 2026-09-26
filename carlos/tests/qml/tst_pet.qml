import QtQuick
import QtTest
import "../../ui/pet"

TestCase {
    name: "CarlosPetSprite"
    when: windowShown
    visible: true
    width: 300; height: 240
    PetSprite { id: buddy; width: 242; height: 202; still: true }
    SignalSpy { id: pats; target: buddy; signalName: "patted" }
    SignalSpy { id: menus; target: buddy; signalName: "menuRequested" }
    SignalSpy { id: movement; target: buddy; signalName: "dragged" }
    function init() { pats.clear(); menus.clear(); movement.clear(); buddy.bubble = ""; buddy.dragMoved = false }
    function test_pat_and_menu() {
        mouseClick(buddy, 164, 135, Qt.LeftButton)
        compare(pats.count, 1)
        mouseClick(buddy, 164, 135, Qt.RightButton)
        compare(menus.count, 1)
    }
    function test_drag_does_not_pet() {
        mousePress(buddy, 164, 135, Qt.LeftButton)
        mouseMove(buddy, 180, 148, 30)
        verify(movement.count > 0)
        const pointer = movement.signalArguments[movement.count - 1][0]
        compare(pointer.x, 180)
        compare(pointer.y, 148)
        compare(pats.count, 0)
        mouseRelease(buddy, 180, 148, Qt.LeftButton)
        verify(movement.count > 0)
        compare(pats.count, 0)
    }
    function test_reduced_motion_keeps_eyes_open() {
        buddy.blink = true
        buddy.still = false
        buddy.still = true
        compare(buddy.blink, false)
    }
    function test_compositor_drag_does_not_pat() {
        mousePress(buddy, 164, 135, Qt.LeftButton)
        buddy.dragMoved = true
        mouseRelease(buddy, 164, 135, Qt.LeftButton)
        compare(pats.count, 0)
    }
}
