#include "Input.h"
#include "core/PointerStep.h"
#include <QCoreApplication>
#include <QProcess>
#include <X11/Xatom.h>
#include <X11/Xlib.h>
#include <X11/extensions/XInput2.h>
#include <X11/extensions/XTest.h>
#include <X11/keysym.h>
#include <chrono>
#include <cmath>
#include <cstring>
#include <set>
namespace holohand {
class XInput final : public Input {
    double speed_ = 900;
    bool direct_ = false;
    std::chrono::steady_clock::time_point lastMove_{};
    Display *d_ = nullptr;
    bool held_ = false;
    double sx_ = 0, sy_ = 0;
    QProcess guard_;
    int xi_ = 0;
    bool pause_ = false;
    std::set<int> synthetic_;
    std::chrono::steady_clock::time_point manualUntil_{};
    void devices() {
        synthetic_.clear();
        int n = 0;
        auto *list = XIQueryDevice(d_, XIAllDevices, &n);
        if (!list)
            return;
        for (int i = 0; i < n; i++)
            if (list[i].name && strstr(list[i].name, "XTEST"))
                synthetic_.insert(list[i].deviceid);
        XIFreeDeviceInfo(list);
    }

  public:
    XInput() {
        d_ = XOpenDisplay(nullptr);
        if (!d_)
            return;
        int event, error, major = 2, minor = 0;
        if (!XQueryExtension(d_, "XInputExtension", &xi_, &event, &error) ||
            XIQueryVersion(d_, &major, &minor) != Success) {
            XCloseDisplay(d_);
            d_ = nullptr;
            return;
        }
        unsigned char bits[(XI_LASTEVENT + 7) / 8]{};
        XISetMask(bits, XI_RawMotion);
        XISetMask(bits, XI_RawButtonPress);
        XISetMask(bits, XI_HierarchyChanged);
        XIEventMask mask{XIAllDevices, int(sizeof(bits)), bits};
        XISelectEvents(d_, DefaultRootWindow(d_), &mask, 1);
        devices();
        for (unsigned extra :
             {0u, unsigned(LockMask), unsigned(Mod2Mask), unsigned(LockMask | Mod2Mask)})
            XGrabKey(d_, XKeysymToKeycode(d_, XK_h), ControlMask | Mod1Mask | extra,
                     DefaultRootWindow(d_), False, GrabModeAsync, GrabModeAsync);
        XFlush(d_);
        guard_.start(QCoreApplication::applicationFilePath(), {"--input-guardian"},
                     QIODevice::WriteOnly);
        guard_.waitForStarted(1500);
    }
    ~XInput() {
        release();
        guard_.closeWriteChannel();
        guard_.waitForFinished(1500);
        if (d_)
            XCloseDisplay(d_);
    }
    bool available() const override { return d_ && guard_.state() == QProcess::Running; }
    QString reason() const override {
        return available() ? "X11/XTest + button watchdog" : "X11 input/watchdog unavailable";
    }
    void heartbeat() override {
        if (guard_.state() == QProcess::Running)
            guard_.write("H", 1);
        if (!d_)
            return;
        while (XPending(d_)) {
            XEvent e;
            XNextEvent(d_, &e);
            if (e.type == KeyPress)
                pause_ = true;
            if (e.type == GenericEvent && e.xcookie.extension == xi_ &&
                XGetEventData(d_, &e.xcookie)) {
                if (e.xcookie.evtype == XI_HierarchyChanged)
                    devices();
                if (e.xcookie.evtype == XI_RawMotion || e.xcookie.evtype == XI_RawButtonPress) {
                    auto *r = static_cast<XIRawEvent *>(e.xcookie.data);
                    if (!synthetic_.contains(r->sourceid))
                        manualUntil_ =
                            std::chrono::steady_clock::now() + std::chrono::milliseconds(500);
                }
                XFreeEventData(d_, &e.xcookie);
            }
        }
    }
    bool takePauseRequest() override {
        bool p = pause_;
        pause_ = false;
        return p;
    }
    bool manualActive() override { return std::chrono::steady_clock::now() < manualUntil_; }
    void setSpeed(double speed) override { speed_ = std::clamp(speed, 300., 2400.); }
    void setDirect(bool direct) override { direct_ = direct; }
    void move(QPointF p) override {
        if (d_) {
            auto now = std::chrono::steady_clock::now();
            double dt =
                lastMove_.time_since_epoch().count()
                    ? std::clamp(std::chrono::duration<double>(now - lastMove_).count(), .001, .032)
                    : .016;
            lastMove_ = now;
            Window root, child;
            int x, y, wx, wy;
            unsigned mask;
            if (!XQueryPointer(d_, DefaultRootWindow(d_), &root, &child, &x, &y, &wx, &wy, &mask))
                return;
            QPointF delta = p - QPointF(x, y);
            const auto step = direct_ ? Point{delta.x(), delta.y()}
                                      : pointerStep({delta.x(), delta.y()}, speed_, dt);
            p = QPointF(x + step.x, y + step.y);
            XTestFakeMotionEvent(d_, -1, std::lround(p.x()), std::lround(p.y()), 0);
            XFlush(d_);
        }
    }
    void button(bool down) override {
        if (available())
            guard_.write(down ? "D" : "U", 1);
        held_ = down;
    }
    void click(bool right) override {
        if (available())
            guard_.write(right ? "R" : "L", 1);
    }
    void scroll(double x, double y) override {
        if (!d_)
            return;
        sx_ += x;
        sy_ += y;
        auto emitWheel = [&](double &v, unsigned positive, unsigned negative) {
            int n = std::clamp(int(v), -12, 12);
            v -= n;
            for (int i = 0; i < std::abs(n); i++) {
                auto b = n > 0 ? positive : negative;
                XTestFakeButtonEvent(d_, b, 1, 0);
                XTestFakeButtonEvent(d_, b, 0, 0);
            }
        };
        emitWheel(sx_, 7, 6);
        emitWheel(sy_, 4, 5);
        XFlush(d_);
    }
    void maximize() override {
        if (!d_)
            return;
        Atom active = XInternAtom(d_, "_NET_ACTIVE_WINDOW", False), type;
        int format;
        unsigned long n, after;
        unsigned char *data = nullptr;
        Window root = DefaultRootWindow(d_);
        if (XGetWindowProperty(d_, root, active, 0, 1, False, AnyPropertyType, &type, &format, &n,
                               &after, &data) != Success ||
            !data)
            return;
        Window win =
            (type == XA_WINDOW && format == 32 && n == 1) ? *reinterpret_cast<Window *>(data) : 0;
        XFree(data);
        if (!win)
            return;
        XEvent e{};
        e.xclient.type = ClientMessage;
        e.xclient.window = win;
        e.xclient.message_type = XInternAtom(d_, "_NET_WM_STATE", False);
        e.xclient.format = 32;
        e.xclient.data.l[0] = 2;
        e.xclient.data.l[1] = XInternAtom(d_, "_NET_WM_STATE_MAXIMIZED_VERT", False);
        e.xclient.data.l[2] = XInternAtom(d_, "_NET_WM_STATE_MAXIMIZED_HORZ", False);
        e.xclient.data.l[3] = 2;
        XSendEvent(d_, root, False, SubstructureRedirectMask | SubstructureNotifyMask, &e);
        XFlush(d_);
    }
    void release() override {
        if (held_)
            button(false);
        sx_ = sy_ = 0;
    }
};
std::unique_ptr<Input> makeInput() { return std::make_unique<XInput>(); }
} // namespace holohand
