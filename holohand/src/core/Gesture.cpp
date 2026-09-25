#include "Gesture.h"
#include <algorithm>
namespace holohand {
double distance(Point a, Point b) { return std::hypot(a.x - b.x, a.y - b.y); }
static bool extended(const Hand &h, int tip, double ratio = 1.07) {
    auto d = [&](int a, int b) {
        return std::hypot(h.p[a].x - h.p[b].x, (h.p[a].y - h.p[b].y) * h.yScale);
    };
    return d(tip, 0) > ratio * d(tip - 2, 0);
}
std::vector<Event> GestureEngine::reset() {
    std::vector<Event> e;
    if (held_)
        e.push_back({Action::Up});
    held_ = false;
    latched_ = false;
    armed_ = false;
    state_ = "IDLE";
    progress_ = 0;
    releaseSince_ = weakSince_ = 0;
    poseExitSince_ = 0;
    return e;
}
std::vector<Event> GestureEngine::update(const Hand &h, double now) {
    if (!std::isfinite(now)) {
        hasTime_ = false;
        return reset();
    }
    if (hasTime_ && (now < last_ || now - last_ > .3)) {
        auto e = reset();
        last_ = now;
        return e;
    }
    last_ = now;
    hasTime_ = true;
    for (auto p : h.p)
        if (!std::isfinite(p.x) || !std::isfinite(p.y) || !std::isfinite(p.z))
            return reset();
    if (!h.valid || !std::isfinite(h.confidence) || h.confidence < .65 ||
        !std::isfinite(h.yScale) || h.yScale <= 0)
        return reset();
    if (h.confidence < s_.confidence) {
        if (held_)
            return reset(); // never retain a held button on weak observations
        if (!weakSince_)
            weakSince_ = now;
        if (now - weakSince_ > .12)
            return reset();
        return {}; // no input or discrete action is inferred from low confidence
    }
    if (weakSince_) {
        since_ += now - weakSince_;
        weakSince_ = 0;
        scroll_ = {(h.p[8].x + h.p[12].x) / 2, (h.p[8].y + h.p[12].y) / 2};
    }
    if (h.partial()) {
        // A cropped hand can still point with a clearly visible extended index.
        // Never use model extrapolations outside the image to infer a click.
        auto out = reset();
        bool fingerVisible = true;
        for (int i : {5, 6, 7, 8})
            fingerVisible = fingerVisible && h.visible(i);
        auto d = [&](int a, int b) {
            return std::hypot(h.p[a].x - h.p[b].x, (h.p[a].y - h.p[b].y) * h.yScale);
        };
        if (fingerVisible && d(6, 5) > .008 && d(8, 5) > 1.5 * d(6, 5)) {
            state_ = "PARTIAL POINT";
            out.push_back({Action::Move, h.p[8].x, h.p[8].y});
        }
        return out;
    }
    auto metric = [&](int a, int b) {
        return std::hypot(h.p[a].x - h.p[b].x, (h.p[a].y - h.p[b].y) * h.yScale);
    };
    const double scale = std::max(metric(5, 17), .65 * metric(0, 9));
    if (scale < .015)
        return reset();
    auto d = [&](int a, int b) { return metric(a, b) / scale; };
    bool index = extended(h, 8, state_ == "POINT" || state_ == "SCROLL" ? 1.0 : 1.07),
         middle = extended(h, 12, state_ == "SCROLL" ? 1.0 : 1.07), ring = extended(h, 16),
         pinky = extended(h, 20);
    // A fully open palm is a clutch/cancel, never a pinch release click.
    // Require a separated thumb so an ordinary pinch cannot look like cancel.
    if (index && middle && ring && pinky && d(4, 8) > .65 && d(4, 12) > .65) {
        auto out = reset();
        state_ = "PALM CANCEL";
        return out;
    }
    bool pi = d(4, 8) < (state_ == "PINCH" ? s_.exit : s_.enter);
    bool drag = d(4, 12) < (state_ == "DRAG" ? s_.exit : s_.enter);
    // During a curled pinch the adjacent fingertips can overlap in the image.
    // Preserve the established gesture, otherwise require a clear nearest tip.
    if (pi && drag) {
        if (state_ == "PINCH")
            drag = false;
        else if (state_ == "DRAG")
            pi = false;
        else if (d(4, 8) + .08 < d(4, 12))
            drag = false;
        else if (d(4, 12) + .08 < d(4, 8))
            pi = false;
    }
    // Two extended fingers have a natural gap; do not require a pinch between
    // them. Curled outer fingers distinguish deliberate scrolling from a palm.
    bool scroll = index && middle && (state_ == "SCROLL" || (!ring && !pinky)) &&
                  d(8, 12) < (state_ == "SCROLL" ? s_.scrollExit : s_.scrollEnter) &&
                  d(4, 8) > .5 && d(4, 12) > .5;
    // Both outer fingers deliberately extended with inner fingers curled; natural proximity is
    // insufficient.
    bool maximize = ring && pinky && !index && !middle &&
                    d(16, 20) < (state_ == "MAXIMIZE" ? s_.exit : s_.enter) && d(4, 16) > .5;
    int candidates = int(pi) + int(drag) + int(scroll) + int(maximize);
    if (candidates > 1)
        return reset();
    const Point scrollCenter{(h.p[8].x + h.p[12].x) / 2, (h.p[8].y + h.p[12].y) / 2};
    if (state_ == "SCROLL" && !scroll && candidates == 0 && d(8, 12) < s_.scrollExit * 1.3) {
        if (!poseExitSince_)
            poseExitSince_ = now;
        scroll_ = scrollCenter;
        if (now - poseExitSince_ < .1)
            return {}; // pause wheel without losing engagement
    } else
        poseExitSince_ = 0;
    std::vector<Event> out;
    // Require a visible neutral/pointing frame after loss before arming any discrete gesture.
    if (!armed_) {
        if (candidates == 0)
            armed_ = true;
        return out;
    }
    std::string desired = pi                                                        ? "PINCH"
                          : drag                                                    ? "DRAG"
                          : scroll                                                  ? "SCROLL"
                          : maximize                                                ? "MAXIMIZE"
                          : index && d(4, 8) < (state_ == "PINCH PREP" ? .68 : .55) ? "PINCH PREP"
                          : index                                                   ? "POINT"
                                                                                    : "CLUTCH";
    double releaseAt = now;
    if (state_ == "PINCH" && desired != "PINCH") {
        if (!releaseSince_)
            releaseSince_ = now;
        // Two real open frames confirm release; a single noisy separation cannot click.
        if (now - releaseSince_ < .045)
            return {};
        releaseAt = releaseSince_;
    } else
        releaseSince_ = 0;
    if (desired != state_) {
        if (state_ == "PINCH" &&
            (desired == "POINT" || desired == "PINCH PREP" || desired == "CLUTCH") && !latched_ &&
            releaseAt - since_ >= .04 && releaseAt - since_ <= std::min(s_.tap, s_.rightDwell) &&
            now >= until_) {
            out.push_back({Action::LeftClick});
            until_ = now + s_.cooldown;
        }
        if (held_) {
            out.push_back({Action::Up});
            held_ = false;
        }
        state_ = desired;
        releaseSince_ = 0;
        since_ = now;
        progress_ = 0;
        latched_ = false;
        scroll_ = scrollCenter;
    }
    double elapsed = now - since_;
    if (state_ == "PINCH") {
        progress_ = std::clamp(elapsed / s_.rightDwell, 0., 1.);
        if (elapsed >= s_.rightDwell && !latched_ && now >= until_) {
            out.push_back({Action::RightClick});
            latched_ = true;
            until_ = now + s_.cooldown;
        }
    } else if (state_ == "DRAG") {
        progress_ = std::clamp(elapsed / s_.dragDwell, 0., 1.);
        if (elapsed >= s_.dragDwell && !held_ && now >= until_) {
            out.push_back({Action::Down});
            held_ = true;
        }
        if (held_)
            out.push_back({Action::Move, h.p[8].x, h.p[8].y});
    } else if (state_ == "SCROLL") {
        double dx = scrollCenter.x - scroll_.x, dy = scrollCenter.y - scroll_.y;
        if (elapsed >= s_.scrollDwell) {
            if (std::abs(dx) < s_.scrollDead)
                dx = 0;
            else
                scroll_.x = scrollCenter.x;
            if (std::abs(dy) < s_.scrollDead)
                dy = 0;
            else
                scroll_.y = scrollCenter.y;
            // Small movements accumulate instead of vanishing at higher FPS.
            if (std::abs(dy) > 1.6 * std::abs(dx))
                dx = 0;
            else if (std::abs(dx) > 1.6 * std::abs(dy))
                dy = 0;
            if (dx || dy)
                out.push_back({Action::Scroll, std::clamp(dx, -.03, .03) * s_.scrollGain,
                               -std::clamp(dy, -.03, .03) * s_.scrollGain});
        } else
            scroll_ = scrollCenter;
    } else if (state_ == "MAXIMIZE") {
        progress_ = std::clamp(elapsed / s_.maxDwell, 0., 1.);
        if (elapsed >= s_.maxDwell && !latched_ && now >= until_) {
            out.push_back({Action::Maximize});
            latched_ = true;
            until_ = now + s_.cooldown;
        }
    } else if (state_ == "POINT")
        out.push_back({Action::Move, h.p[8].x, h.p[8].y});
    return out;
}
double OneEuro::filter(double x, double t, double cutoff, double beta) {
    if (!ready_ || t <= time_ || t - time_ > .3) {
        ready_ = true;
        x_ = raw_ = x;
        dx_ = 0;
        time_ = t;
        return x;
    }
    double dt = t - time_;
    auto alpha = [&](double c) { return 1 / (1 + 1 / (2 * 3.141592653589793 * c * dt)); };
    dx_ += alpha(1) * ((x - raw_) / dt - dx_);
    x_ += alpha(cutoff + beta * std::abs(dx_)) * (x - x_);
    raw_ = x;
    time_ = t;
    return x_;
}
} // namespace holohand
