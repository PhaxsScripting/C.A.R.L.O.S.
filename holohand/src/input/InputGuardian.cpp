// Owns only XTest button state. No camera, models, privileges, or EV connection.
// EOF releases a held drag after a parent crash; a 500 ms lease handles hangs.
#include <X11/Xlib.h>
#include <X11/extensions/XTest.h>
#include <chrono>
#include <poll.h>
#include <unistd.h>
namespace holohand {
int inputGuardian() {
    Display *d = XOpenDisplay(nullptr);
    if (!d)
        return 2;
    bool held = false;
    auto last = std::chrono::steady_clock::now();
    auto release = [&] {
        if (held) {
            XTestFakeButtonEvent(d, 1, False, 0);
            XFlush(d);
            held = false;
        }
    };
    for (;;) {
        pollfd p{STDIN_FILENO, POLLIN, 0};
        int ready = poll(&p, 1, 100);
        if (ready < 0)
            break;
        if (ready > 0) {
            char commands[128];
            auto n = read(STDIN_FILENO, commands, sizeof(commands));
            if (n <= 0)
                break;
            for (int i = 0; i < n; i++) {
                char c = commands[i];
                last = std::chrono::steady_clock::now();
                if (c == 'D' && !held) {
                    XTestFakeButtonEvent(d, 1, True, 0);
                    held = true;
                }
                if (c == 'U')
                    release();
                if (c == 'L' || c == 'R') {
                    unsigned b = c == 'R' ? 3 : 1;
                    XTestFakeButtonEvent(d, b, True, 0);
                    XTestFakeButtonEvent(d, b, False, 0);
                }
            }
            XFlush(d);
        }
        if (std::chrono::steady_clock::now() - last > std::chrono::milliseconds(500))
            release();
    }
    release();
    XCloseDisplay(d);
    return 0;
}
} // namespace holohand
