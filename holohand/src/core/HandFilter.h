#pragma once
#include "Gesture.h"
#include <algorithm>

namespace holohand {
// Compensate whole-palm translation first, then smooth each landmark relative
// to that palm. This reduces fingertip shake without lagging deliberate arm motion.
class HandFilter {
  public:
    void reset() { ready_ = false; }
    Hand update(const Hand &raw) {
        if (!raw.valid) {
            return raw;
        }
        for (auto p : raw.p)
            if (!std::isfinite(p.x) || !std::isfinite(p.y) || !std::isfinite(p.z))
                return {};
        if (!ready_) {
            previous_ = raw;
            ready_ = true;
            return raw;
        }
        double scale = std::max(distance(raw.p[5], raw.p[17]), .65 * distance(raw.p[0], raw.p[9]));
        double oldScale = std::max(distance(previous_.p[5], previous_.p[17]),
                                   .65 * distance(previous_.p[0], previous_.p[9]));
        if (scale < .015 || scale > oldScale * 1.8 || scale < oldScale * .55) {
            ready_ = false;
            return {};
        }
        // A noisy wrist must not move every fingertip. Estimate translation from
        // the median of five palm anchors instead of trusting one landmark.
        std::array<double, 5> dx{}, dy{};
        const int anchors[] = {0, 5, 9, 13, 17};
        for (int i = 0; i < 5; ++i) {
            dx[i] = raw.p[anchors[i]].x - previous_.p[anchors[i]].x;
            dy[i] = raw.p[anchors[i]].y - previous_.p[anchors[i]].y;
        }
        std::sort(dx.begin(), dx.end());
        std::sort(dy.begin(), dy.end());
        Point translation{dx[2], dy[2]};
        if (std::hypot(translation.x, translation.y) > .22) {
            ready_ = false;
            return {};
        }
        Hand result = raw;
        for (size_t i = 0; i < raw.p.size(); i++) {
            Point expected{previous_.p[i].x + translation.x, previous_.p[i].y + translation.y,
                           previous_.p[i].z};
            double relative = distance(raw.p[i], expected) / scale;
            double blend = std::clamp(.65 + relative * 2.5, .65, 1.);
            result.p[i].x = expected.x + blend * (raw.p[i].x - expected.x);
            result.p[i].y = expected.y + blend * (raw.p[i].y - expected.y);
            // Do not turn smoothing into a multi-frame trailing hand.
            const double lag = distance(result.p[i], raw.p[i]);
            if (lag > .002) {
                result.p[i].x = raw.p[i].x + (result.p[i].x - raw.p[i].x) * .002 / lag;
                result.p[i].y = raw.p[i].y + (result.p[i].y - raw.p[i].y) * .002 / lag;
            }
        }
        previous_ = result;
        return result;
    }

  private:
    Hand previous_{};
    bool ready_ = false;
};
} // namespace holohand
