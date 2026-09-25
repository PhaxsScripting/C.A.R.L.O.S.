#pragma once
#include <opencv2/core.hpp>
#include <string>
#include <vector>
namespace holohand {
class Camera {
  public:
    Camera() = default;
    ~Camera() { close(); }
    Camera(const Camera &) = delete;
    Camera &operator=(const Camera &) = delete;
    bool open(const std::string &device);
    bool read(cv::Mat &);
    double capturedAt() const { return capturedAt_; }
    void close();
    std::string error;

  private:
    struct Buffer {
        void *data;
        size_t size;
    };
    int fd_ = -1;
    std::vector<Buffer> buffers_;
    int width_ = 640, height_ = 480;
    size_t stride_ = 0;
    double capturedAt_ = 0;
    unsigned format_ = 0;
    bool streaming_ = false;
    int originalExposurePriority_ = -1;
};
} // namespace holohand
