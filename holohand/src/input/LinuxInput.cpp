#include "LinuxInput.h"
#include "core/PointerStep.h"
#include <QCoreApplication>
#include <QDBusConnectionInterface>
#include <QDBusInterface>
#include <QDBusReply>
#include <QDBusVariant>
#include <QDir>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QStandardPaths>
#include <algorithm>
#include <cmath>
#include <fcntl.h>
#include <linux/uinput.h>
#include <sys/ioctl.h>
#include <unistd.h>
namespace holohand {
LinuxInput::LinuxInput() {
    clock_.start();
    fd_ = ::open("/dev/uinput", O_WRONLY | O_NONBLOCK | O_CLOEXEC);
    if (fd_ >= 0) {
        bool ok = ioctl(fd_, UI_SET_EVBIT, EV_KEY) >= 0 && ioctl(fd_, UI_SET_EVBIT, EV_REL) >= 0;
        for (int k : {BTN_LEFT, BTN_RIGHT})
            ok = ok && ioctl(fd_, UI_SET_KEYBIT, k) >= 0;
        for (int k : {REL_X, REL_Y, REL_WHEEL, REL_HWHEEL})
            ok = ok && ioctl(fd_, UI_SET_RELBIT, k) >= 0;
        uinput_setup s{};
        s.id.bustype = BUS_VIRTUAL;
        s.id.vendor = 0x1209;
        s.id.product = 0x4848;
        snprintf(s.name, sizeof(s.name), "HoloHand virtual pointer");
        if (!ok || ioctl(fd_, UI_DEV_SETUP, &s) < 0 || ioctl(fd_, UI_DEV_CREATE) < 0) {
            ::close(fd_);
            fd_ = -1;
        }
    }
    auto bus = QDBusConnection::sessionBus();
    bus.registerService("org.phax.HoloHand");
    bus.registerObject("/Input", this, QDBusConnection::ExportAllSlots);
    QDBusReply<QString> owner = bus.interface()->serviceOwner("org.kde.KWin");
    if (owner.isValid())
        kwinOwner_ = owner.value();
    QString script =
        QStandardPaths::writableLocation(QStandardPaths::AppDataLocation) + "/input-bridge.js";
    QDir().mkpath(QFileInfo(script).absolutePath());
    QFile f(script);
    if (f.open(QIODevice::WriteOnly)) {
        f.write(R"JS(
function next(){callDBus('org.phax.HoloHand','/Input','org.phax.HoloHand.Input','Next',function(raw){
 try {var c=JSON.parse(raw);if(c.action==='position')callDBus('org.phax.HoloHand','/Input','org.phax.HoloHand.Input','Position',c.epoch,Math.round(workspace.cursorPos.x),Math.round(workspace.cursorPos.y));
 if(c.action==='maximize'){var w=workspace.activeWindow;if(w&&w.normalWindow&&w.maximizable){var a=workspace.clientArea(KWin.MaximizeArea,w);var g=w.frameGeometry;var max=Math.abs(a.width-g.width)<2&&Math.abs(a.height-g.height)<2;w.setMaximize(!max,!max);}}
 }catch(e){}next();});}
registerShortcut('HoloHandPause','Pause or resume HoloHand','Ctrl+Alt+H',function(){callDBus('org.phax.HoloHand','/Input','org.phax.HoloHand.Input','TogglePause');});
callDBus('org.phax.HoloHand','/Input','org.phax.HoloHand.Input','Probe',Math.round(workspace.cursorPos.x),Math.round(workspace.cursorPos.y));
next();
)JS");
        f.close();
        QDBusInterface scripting("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", bus);
        scripting.call("unloadScript", "org.phax.holohand.runtime");
        QDBusReply<int> id = scripting.call("loadScript", script, "org.phax.holohand.runtime");
        if (id.isValid() && id.value() >= 0) {
            scriptId_ = id.value();
            QDBusInterface obj("org.kde.KWin", QString("/Scripting/Script%1").arg(scriptId_),
                               "org.kde.kwin.Script", bus);
            obj.call("run");
        }
    }
    heartbeat_.setInterval(10000);
    connect(&heartbeat_, &QTimer::timeout, this, [this] {
        if (waiting_) {
            QDBusConnection::sessionBus().send(pending_.createReply(QString("{}")));
            waiting_ = false;
        }
    });
    heartbeat_.start();
    // Device discovery is bounded polling only during udev/KWin registration.
    connect(&deviceTimer_, &QTimer::timeout, this, &LinuxInput::configurePointer);
    deviceTimer_.start(500);
    QTimer::singleShot(0, this, &LinuxInput::configurePointer);
    motionTimer_.setTimerType(Qt::PreciseTimer);
    connect(&motionTimer_, &QTimer::timeout, this, [this] {
        if (inFlight_ && clock_.elapsed() - requestTime_ > 150) {
            inFlight_ = false;
            ++epoch_;
        }
        if (clock_.elapsed() - targetTime_ > 200)
            movePending_ = false;
        deliver();
    });
    motionTimer_.start(16);
}
void LinuxInput::configurePointer() {
    QDBusInterface manager("org.kde.KWin", "/org/kde/KWin/InputDevice",
                           "org.kde.KWin.InputDeviceManager", QDBusConnection::sessionBus());
    for (const auto &name : manager.property("devicesSysNames").toStringList()) {
        QDBusInterface device("org.kde.KWin", "/org/kde/KWin/InputDevice/" + name,
                              "org.kde.KWin.InputDevice", QDBusConnection::sessionBus());
        // Never change a physical mouse, even if device enumeration order changes.
        if (device.property("name").toString() != "HoloHand virtual pointer" ||
            device.property("vendor").toUInt() != 0x1209 ||
            device.property("product").toUInt() != 0x4848 || !device.property("isVirtual").toBool())
            continue;
        device.setProperty("pointerAccelerationProfileFlat", true);
        device.setProperty("pointerAcceleration", 0.0);
        flat_ = device.property("pointerAccelerationProfileFlat").toBool() &&
                std::abs(device.property("pointerAcceleration").toDouble()) < .001;
        if (flat_)
            deviceTimer_.stop();
        return;
    }
}
LinuxInput::~LinuxInput() {
    release();
    if (waiting_)
        QDBusConnection::sessionBus().send(pending_.createReply(QString("{}")));
    QDBusInterface s("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting",
                     QDBusConnection::sessionBus());
    s.call("unloadScript", "org.phax.holohand.runtime");
    if (fd_ >= 0) {
        ioctl(fd_, UI_DEV_DESTROY);
        ::close(fd_);
    }
}
QString LinuxInput::reason() const {
    return fd_ < 0    ? "uinput permission/setup unavailable"
           : !bridge_ ? "Waiting for independent KWin input bridge"
           : !flat_   ? "Waiting for HoloHand-only flat pointer profile"
           : !probed_ ? "KWin pointer callback not verified"
                      : QString("Linux uinput + KWin (%1; %2 replies)")
                            .arg(direct_ ? "direct fingertip" : "flat, bounded motion")
                            .arg(positionReplies_);
}
bool LinuxInput::trusted() const {
    return calledFromDBus() && !kwinOwner_.isEmpty() && message().service() == kwinOwner_;
}
QString LinuxInput::Next() {
    if (!trusted())
        return "{}";
    bridge_ = true;
    setDelayedReply(true);
    if (waiting_)
        QDBusConnection::sessionBus().send(pending_.createReply(QString("{}")));
    pending_ = message();
    waiting_ = true;
    return {};
}
bool LinuxInput::Probe(int, int) {
    if (!trusted())
        return false;
    probed_ = true;
    return true;
}
bool LinuxInput::Position(int epoch, int x, int y) {
    if (!trusted() || epoch != epoch_ || !inFlight_)
        return false;
    inFlight_ = false;
    ++positionReplies_;
    if (movePending_ && flat_ && clock_.elapsed() - targetTime_ < 200) {
        // Conservative feedback gain, fixed device profile and bounded velocity
        // prevent a screen-sized correction from overshooting and reversing.
        const auto time = clock_.elapsed();
        const double dt = lastMotionTime_ < 0 ? .016 : (time - lastMotionTime_) / 1000.;
        lastMotionTime_ = time;
        const Point error{target_.x() - x, target_.y() - y};
        const Point delta =
            direct_ ? Point{error.x * .65, error.y * .65} : pointerStep(error, speed_, dt, .65);
        emitEvent(EV_REL, REL_X, int(std::lround(delta.x)));
        emitEvent(EV_REL, REL_Y, int(std::lround(delta.y)));
        sync();
    }
    return true;
}
void LinuxInput::TogglePause() {
    if (trusted())
        emit pauseRequested();
}
void LinuxInput::deliver() {
    if (!waiting_ || inFlight_ || (!movePending_ && !maxPending_) || !flat_)
        return;
    QString action = maxPending_ ? "maximize" : "position";
    maxPending_ = false;
    waiting_ = false;
    if (action == "position") {
        inFlight_ = true;
        requestTime_ = clock_.elapsed();
    }
    QDBusConnection::sessionBus().send(pending_.createReply(
        QString::fromUtf8(QJsonDocument(QJsonObject{{"action", action}, {"epoch", int(epoch_)}})
                              .toJson(QJsonDocument::Compact))));
}
void LinuxInput::emitEvent(unsigned short t, unsigned short c, int v) {
    if (fd_ < 0)
        return;
    input_event e{};
    e.type = t;
    e.code = c;
    e.value = v;
    if (::write(fd_, &e, sizeof(e)) != sizeof(e)) {
        held_ = false;
        ioctl(fd_, UI_DEV_DESTROY);
        ::close(fd_);
        fd_ = -1;
    }
}
void LinuxInput::sync() { emitEvent(EV_SYN, SYN_REPORT, 0); }
void LinuxInput::move(QPointF p) {
    target_ = p;
    targetTime_ = clock_.elapsed();
    movePending_ = true;
}
void LinuxInput::button(bool down) {
    emitEvent(EV_KEY, BTN_LEFT, down);
    sync();
    held_ = down;
}
void LinuxInput::click(bool right) {
    int b = right ? BTN_RIGHT : BTN_LEFT;
    emitEvent(EV_KEY, b, 1);
    sync();
    emitEvent(EV_KEY, b, 0);
    sync();
}
void LinuxInput::scroll(double x, double y) {
    sx_ += x;
    sy_ += y;
    int dx = std::clamp(int(sx_), -12, 12), dy = std::clamp(int(sy_), -12, 12);
    sx_ -= dx;
    sy_ -= dy;
    emitEvent(EV_REL, REL_HWHEEL, dx);
    emitEvent(EV_REL, REL_WHEEL, dy);
    sync();
}
void LinuxInput::maximize() {
    maxPending_ = true;
    deliver();
}
void LinuxInput::release() {
    lastMotionTime_ = -1;
    ++epoch_;
    inFlight_ = false;
    movePending_ = maxPending_ = false;
    if (held_)
        button(false);
    sx_ = sy_ = 0;
}
std::unique_ptr<Input> makeInput() { return std::make_unique<LinuxInput>(); }
} // namespace holohand
