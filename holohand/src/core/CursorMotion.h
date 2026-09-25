#pragma once
#include "Gesture.h"
#include <algorithm>
#include <optional>

namespace holohand {
// Camera updates and display updates are intentionally separate. One bad camera
// coordinate cannot teleport the cursor, and reacquisition keeps the old position.
class CursorMotion {
  public:
    void setPrecision(bool precision) {
        precision_ = precision;
        freeze();
    }
    void setDirect(bool direct) {
        direct_ = direct;
        freeze();
    }
    void setViewport(double width, double height) {
        width_ = std::max(1., width);
        height_ = std::max(1., height);
    }
    void freeze() {
        reacquiring_ = positioned_;
        active_ = false;
        samples_ = 0;
        lastSample_ = 0;
        suspect_.reset();
        velocity_ = {};
    }
    void hold() {
        // Stop output immediately while retaining the filter for a brief pose
        // transition. submit() still resets after a gap, and rejects spikes.
        active_ = false;
        velocity_ = {};
        suspect_.reset();
    }
    void submit(Point p, double t, double responsiveness = 1.4) {
        if (!std::isfinite(p.x) || !std::isfinite(p.y) || !std::isfinite(t)) {
            freeze();
            return;
        }
        p.x = std::clamp(p.x, 0., 1.);
        p.y = std::clamp(p.y, 0., 1.);
        // Duplicate/out-of-order observations cannot advance or reset the filter.
        if (samples_ && t <= lastSample_)
            return;
        if (samples_ && t - lastSample_ > .25)
            freeze();
        double dt = lastSample_ ? std::clamp(t - lastSample_, .01, .15) : .067;
        if (samples_ && distance(p, raw_) > (direct_ ? .35 : .18)) {
            if (!suspect_ || t - suspectTime_ > .1 || distance(p, *suspect_) > .045) {
                suspect_ = p;
                suspectTime_ = t;
                return;
            }
        }
        suspect_.reset();
        if (!samples_) {
            raw_ = p;
            filtered_ = p;
            samples_ = 1;
            lastSample_ = t;
            return;
        }
        if (direct_) {
            // Direct mode maps the accepted fingertip itself, without a trailing
            // target or prediction. Two observations still arm reacquisition.
            raw_ = p;
            if (reacquiring_) {
                filtered_ = p;
            } else if (precision_ && positioned_) {
                // Sub-pixel noise is held, small corrections are damped, and
                // deliberate travel remains direct. No velocity prediction.
                const double d = pixels(p, output_);
                const double alpha = d <= 1.2 ? 0. : std::clamp((d - 1.2) / 9., .18, 1.);
                output_.x += (p.x - output_.x) * alpha;
                output_.y += (p.y - output_.y) * alpha;
                // Preserve exact screen boundaries even in precision mode.
                if (p.x == 0 || p.x == 1)
                    output_.x = p.x;
                if (p.y == 0 || p.y == 1)
                    output_.y = p.y;
                filtered_ = output_;
            } else
                filtered_ = output_ = p;
            lastSample_ = t;
            ++samples_;
            active_ = positioned_ = true;
            return;
        }
        const double speed = distance(p, raw_) / dt;
        if (pixels(p, raw_) > 2.) {
            velocity_.x = .65 * velocity_.x + .35 * (p.x - raw_.x) / dt;
            velocity_.y = .65 * velocity_.y + .35 * (p.y - raw_.y) / dt;
        } else {
            velocity_.x *= .25;
            velocity_.y *= .25;
        }
        const double cutoff = std::clamp(responsiveness * .65, .3, 3.) + 7 * speed;
        const double alpha = 1 - std::exp(-2 * 3.141592653589793 * cutoff * dt);
        // Small stationary landmark noise stays inside this precision dead zone.
        if (pixels(p, filtered_) > .75) {
            filtered_.x += alpha * (p.x - filtered_.x);
            filtered_.y += alpha * (p.y - filtered_.y);
        }
        raw_ = p;
        lastSample_ = t;
        ++samples_;
        active_ = true;
        if (!positioned_) {
            output_ = filtered_;
            positioned_ = true;
        }
    }
    std::optional<Point> step(double t) {
        if (!std::isfinite(t) || (lastStep_ && t < lastStep_))
            return {};
        const double dt = lastStep_ ? std::clamp(t - lastStep_, 0., .032) : .016;
        lastStep_ = t;
        if (!active_ || t < lastSample_ || t - lastSample_ > .15)
            return {};
        if (suspect_)
            return output_; // Don't chase a previous target while rejecting a new observation.
        if (direct_ && reacquiring_) {
            Point delta{filtered_.x - output_.x, filtered_.y - output_.y};
            const double length = std::hypot(delta.x, delta.y);
            const double gain = length > 0 ? std::min(1., 1.8 * dt / length) : 1.;
            output_.x += delta.x * gain;
            output_.y += delta.y * gain;
            if (gain == 1.)
                reacquiring_ = false;
            return output_;
        }
        if (direct_)
            return output_;
        // At most 20 ms / half a screen pixel of pointer-only prediction. It never
        // enters GestureEngine, survives no loss, and cannot generate a click.
        double horizon = suspect_ ? 0 : std::clamp(t - lastSample_, 0., .02);
        Point prediction{velocity_.x * horizon, velocity_.y * horizon};
        double predicted = pixels(prediction, {});
        if (predicted > .5) {
            prediction.x *= .5 / predicted;
            prediction.y *= .5 / predicted;
        }
        Point delta{std::clamp(filtered_.x + prediction.x, 0., 1.) - output_.x,
                    std::clamp(filtered_.y + prediction.y, 0., 1.) - output_.y};
        const double length = std::hypot(delta.x, delta.y);
        if (pixels(delta, {}) > .1) {
            double gain = 1 - std::exp(-dt / .028);
            if (length * gain > 1.3 * dt)
                gain = 1.3 * dt / length;
            output_.x += delta.x * gain;
            output_.y += delta.y * gain;
        }
        return output_;
    }

  private:
    double pixels(Point a, Point b) const {
        return std::hypot((a.x - b.x) * width_, (a.y - b.y) * height_);
    }
    double width_ = 1000, height_ = 1000;
    Point raw_{}, filtered_{}, output_{};
    Point velocity_{};
    std::optional<Point> suspect_;
    int samples_ = 0;
    bool active_ = false, positioned_ = false;
    bool direct_ = false;
    bool precision_ = false;
    bool reacquiring_ = false;
    double lastSample_ = 0, lastStep_ = 0, suspectTime_ = 0;
};
} // namespace holohand
