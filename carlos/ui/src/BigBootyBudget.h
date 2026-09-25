#pragma once
#include <cstdint>
#include <deque>

// Once exhausted, recovery requires an explicit user retry. A successful
// short-lived connection does not erase a repeated-crash history.
class BigBootyBudget {
  public:
    bool take(std::int64_t nowMs) {
        if (blocked_)
            return false;
        while (!attempts_.empty() && nowMs - attempts_.front() >= 600000)
            attempts_.pop_front();
        if (attempts_.size() >= 3) {
            blocked_ = true;
            return false;
        }
        attempts_.push_back(nowMs);
        return true;
    }
    void reset() {
        attempts_.clear();
        blocked_ = false;
    }

  private:
    std::deque<std::int64_t> attempts_;
    bool blocked_ = false;
};
