#pragma once
#include <cmath>

namespace holohand {
// Receipt time isn't capture time. Old frames don't get a fresh birthday.
inline bool freshCapture(double captured, double now, double maximumAge = .15) {
    return std::isfinite(captured) && std::isfinite(now) && captured > 0 && now >= captured &&
           now - captured <= maximumAge;
}
inline double captureTimestamp(double deviceTime, bool monotonic, double readStarted) {
    // Without a monotonic driver timestamp, use the conservative start of read,
    // never the later decode completion. The caller still checks frame age.
    return monotonic ? deviceTime : readStarted;
}
} // namespace holohand
