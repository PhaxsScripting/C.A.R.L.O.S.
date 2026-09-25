#pragma once
#include "Gesture.h"
#include <algorithm>
namespace holohand {
// Map a comfortable, fully visible hand region to the entire target screen.
// The outer camera border is never required for reaching a desktop edge.
struct PointerMapping {
    double side = .16, top = .18, bottom = .32;
    Point map(Point p) const {
        const double x = std::clamp(side, 0., .35);
        const double a = std::clamp(top, 0., .35), b = std::clamp(bottom, 0., .4);
        return {std::clamp((p.x - x) / (1 - 2 * x), 0., 1.),
                std::clamp((p.y - a) / (1 - a - b), 0., 1.)};
    }
};
} // namespace holohand
