#include "ManualOverride.h"
#include <chrono>
#include <cmath>
#include <filesystem>
#include <unistd.h>
#ifdef __linux__
#include <cstring>
#include <fcntl.h>
#include <linux/input.h>
#include <sys/ioctl.h>
#endif
namespace holohand {
static double clockNow() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch())
        .count();
}
ManualOverride::ManualOverride() { scan(); }
ManualOverride::~ManualOverride() {
    for (int f : fds_)
        close(f);
}
void ManualOverride::scan() {
    lastScan_ = clockNow();
#ifdef __linux__
    for (int f : fds_)
        close(f);
    fds_.clear();
    std::error_code ec;
    for (const auto &e : std::filesystem::directory_iterator("/dev/input", ec)) {
        if (e.path().filename().string().rfind("event", 0) != 0)
            continue;
        int fd = open(e.path().c_str(), O_RDONLY | O_NONBLOCK | O_CLOEXEC);
        if (fd < 0)
            continue;
        char name[256]{};
        ioctl(fd, EVIOCGNAME(sizeof(name)), name);
        unsigned long keys[(KEY_MAX + 8 * sizeof(long)) / (8 * sizeof(long))]{};
        ioctl(fd, EVIOCGBIT(EV_KEY, sizeof(keys)), keys);
        auto bit = [&](int k) {
            return keys[k / (8 * sizeof(long))] & (1UL << (k % (8 * sizeof(long))));
        };
        if (strstr(name, "HoloHand") || (!bit(BTN_MOUSE) && !bit(BTN_TOOL_FINGER))) {
            close(fd);
            continue;
        }
        fds_.push_back(fd);
    }
#endif
}
bool ManualOverride::active() {
    double t = clockNow();
    if (t - lastScan_ > 10)
        scan();
#ifdef __linux__
    for (int fd : fds_) {
        input_event e{};
        while (read(fd, &e, sizeof(e)) == sizeof(e)) {
            if ((e.type == EV_REL && (e.code == REL_X || e.code == REL_Y) &&
                 std::abs(e.value) > 1) ||
                (e.type == EV_KEY && e.code >= BTN_MOUSE && e.code <= BTN_TASK && e.value) ||
                (e.type == EV_ABS && (e.code == ABS_MT_POSITION_X || e.code == ABS_MT_POSITION_Y)))
                until_ = t + .5;
        }
    }
#endif
    return t < until_;
}
} // namespace holohand
