#pragma once
#include "Gesture.h"
#include <algorithm>
#include <optional>
namespace holohand {
// Optional accessibility alternative to pinching. Only real POINT observations
// may enter here, in screen pixels. One click until the user moves away.
class DwellClick {
  public:
    void reset() {
        ready_ = latched_ = false;
        progress_ = 0;
    }
    double progress() const { return progress_; }
    bool update(std::optional<Point> point, double t) {
        if (!point || !std::isfinite(t) || !std::isfinite(point->x) || !std::isfinite(point->y)) {
            ready_ = false;
            progress_ = 0;
            return false;
        }
        if (latched_) {
            if (distance(*point, anchor_) <= 24)
                return false;
            latched_ = false;
            ready_ = false;
        }
        if (!ready_ || t <= last_ || t - last_ > .15 || distance(*point, anchor_) > 10) {
            anchor_ = *point;
            since_ = t;
            ready_ = true;
        }
        last_ = t;
        progress_ = std::clamp((t - since_) / .9, 0., 1.);
        if (progress_ < 1)
            return false;
        latched_ = true;
        ready_ = false;
        progress_ = 0;
        return true;
    }

  private:
    bool ready_ = false, latched_ = false;
    Point anchor_{};
    double since_ = 0, last_ = 0, progress_ = 0;
};
} // namespace holohand
