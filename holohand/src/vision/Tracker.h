#pragma once
#include "core/Gesture.h"
#include "core/HandFilter.h"
#include <memory>
#include <opencv2/dnn.hpp>
#include <opencv2/imgproc.hpp>
#include <optional>
namespace holohand {
struct Palm {
    cv::Rect2f box;
    std::array<cv::Point2f, 7> p;
    float score;
    bool fullHand = false;
    std::array<cv::Point2f, 21> all{};
};
class Tracker {
  public:
    explicit Tracker(const std::string &models);
    ~Tracker();
    const std::string &backend() const { return backend_; }
    Hand infer(const cv::Mat &, int dominant = 0);
    void reset() {
        tracked_.reset();
        frames_ = 0;
        locked_ = false;
        filter_.reset();
        misses_ = 0;
    }

  private:
    struct Accelerator;
    std::unique_ptr<Accelerator> accelerator_;
    std::string backend_ = "OpenCV CPU";
    std::vector<cv::Mat> forward(bool palm, const cv::Mat &input);
    cv::dnn::Net palm_, hand_;
    std::vector<cv::Point2f> anchors_;
    std::optional<Palm> tracked_;
    int frames_ = 0;
    int misses_ = 0, dominant_ = -1;
    Point last_{};
    bool locked_ = false;
    HandFilter filter_;
    std::vector<Palm> palms(const cv::Mat &);
    Hand pose(const cv::Mat &, const Palm &);
};
} // namespace holohand
