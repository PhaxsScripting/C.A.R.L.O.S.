#pragma once
#include <vector>
namespace holohand {
// Non-exclusive mouse/touchpad observation only; no keyboard contents collected.
class ManualOverride {
  public:
    ManualOverride();
    ~ManualOverride();
    bool active();

  private:
    std::vector<int> fds_;
    double until_ = 0;
    void scan();
    double lastScan_ = 0;
};
} // namespace holohand
