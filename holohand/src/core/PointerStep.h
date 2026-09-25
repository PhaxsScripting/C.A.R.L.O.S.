#pragma once
#include "Gesture.h"
#include <algorithm>
namespace holohand {
// Screen-pixel velocity limit shared by the native input backends. A delayed
// callback cannot accumulate a large catch-up jump.
inline Point pointerStep(Point delta, double speed, double elapsed, double gain = 1.) {
    if (!std::isfinite(delta.x) || !std::isfinite(delta.y) || !std::isfinite(speed) ||
        !std::isfinite(elapsed))
        return {};
    delta.x *= gain;
    delta.y *= gain;
    const double maximum = std::clamp(speed, 300., 2400.) * std::clamp(elapsed, 0., .032);
    const double length = std::hypot(delta.x, delta.y);
    if (length > maximum && length > 0) {
        delta.x *= maximum / length;
        delta.y *= maximum / length;
    }
    return delta;
}
} // namespace holohand
