#pragma once
#include <array>
#include <cmath>
#include <cstdint>
#include <string>
#include <vector>
namespace holohand {
struct Point {
    double x{}, y{}, z{};
};
struct Hand {
    std::array<Point, 21> p{};
    double confidence{};
    double rightProbability{};
    bool valid{};
    double yScale = 1;
    bool imageBoundsKnown = false;
    uint32_t observedMask = (1u << 21) - 1;
    bool visible(int i) const {
        return !imageBoundsKnown || ((observedMask & (1u << i)) && p[i].x >= 0 && p[i].x <= 1 &&
                                     p[i].y >= 0 && p[i].y <= 1);
    }
    bool partial() const {
        for (int i = 0; i < 21; ++i)
            if (!visible(i))
                return true;
        return false;
    }
};
enum class Action { Move, LeftClick, RightClick, Down, Up, Scroll, Maximize };
struct Event {
    Action action;
    double x{}, y{};
};
struct Settings {
    double confidence = .8, enter = .27, exit = .40, tap = .6, rightDwell = .6, dragDwell = .3,
           maxDwell = .65, scrollDwell = .18, cooldown = .18, scrollGain = 45, scrollDead = .003;
    double scrollEnter = .65, scrollExit = .9;
};
class GestureEngine {
  public:
    explicit GestureEngine(Settings s = {}) : s_(s) {}
    std::vector<Event> update(const Hand &, double now);
    std::vector<Event> reset();
    std::string state() const { return state_; }
    double progress() const { return progress_; }

  private:
    Settings s_;
    std::string state_ = "IDLE";
    double since_ = 0, until_ = 0, progress_ = 0, last_ = 0;
    bool held_ = false, latched_ = false, armed_ = false;
    bool hasTime_ = false;
    Point scroll_{};
    double releaseSince_ = 0, weakSince_ = 0;
    double poseExitSince_ = 0;
};
class OneEuro {
  public:
    double filter(double x, double time, double minCutoff = 1.4, double beta = .035);
    void reset() { ready_ = false; }

  private:
    bool ready_ = false;
    double x_ = 0, raw_ = 0, dx_ = 0, time_ = 0;
};
double distance(Point a, Point b);
} // namespace holohand
