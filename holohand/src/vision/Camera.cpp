#include "Camera.h"
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
namespace holohand {
static int call(int fd, unsigned long request, void *arg) {
    int r;
    do {
        r = ioctl(fd, request, arg);
    } while (r < 0 && errno == EINTR);
    return r;
}
void Camera::close() {
    if (fd_ >= 0) {
        if (streaming_) {
            int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
            call(fd_, VIDIOC_STREAMOFF, &type);
        }
        for (auto b : buffers_)
            munmap(b.data, b.size);
#ifdef V4L2_CID_EXPOSURE_AUTO_PRIORITY
        if (originalExposurePriority_ >= 0) {
            v4l2_control control{};
            control.id = V4L2_CID_EXPOSURE_AUTO_PRIORITY;
            control.value = originalExposurePriority_;
            call(fd_, VIDIOC_S_CTRL, &control);
        }
#endif
        ::close(fd_);
    }
    buffers_.clear();
    fd_ = -1;
    streaming_ = false;
    originalExposurePriority_ = -1;
}
bool Camera::open(const std::string &device) {
    close();
    fd_ = ::open(device.c_str(), O_RDWR | O_NONBLOCK | O_CLOEXEC);
    if (fd_ < 0) {
        error = strerror(errno);
        return false;
    }
    auto fail = [&]() {
        error = strerror(errno);
        close();
        return false;
    };
    v4l2_capability caps{};
    if (call(fd_, VIDIOC_QUERYCAP, &caps) < 0)
        return fail();
    auto cap = caps.capabilities & V4L2_CAP_DEVICE_CAPS ? caps.device_caps : caps.capabilities;
    if (!(cap & V4L2_CAP_VIDEO_CAPTURE) || !(cap & V4L2_CAP_STREAMING)) {
        error = "Camera lacks capture/streaming";
        close();
        return false;
    }
    v4l2_format f{};
    f.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    f.fmt.pix.width = 640;
    f.fmt.pix.height = 480;
    f.fmt.pix.pixelformat = V4L2_PIX_FMT_MJPEG;
    f.fmt.pix.field = V4L2_FIELD_ANY;
    if (call(fd_, VIDIOC_S_FMT, &f) < 0)
        return fail();
    width_ = f.fmt.pix.width;
    height_ = f.fmt.pix.height;
    format_ = f.fmt.pix.pixelformat;
    if (format_ != V4L2_PIX_FMT_MJPEG && format_ != V4L2_PIX_FMT_YUYV) {
        error = "Unsupported camera pixel format";
        close();
        return false;
    }
    v4l2_streamparm parm{};
    parm.type = f.type;
    parm.parm.capture.timeperframe = {1, 30};
    call(fd_, VIDIOC_S_PARM, &parm);
#ifdef V4L2_CID_EXPOSURE_AUTO_PRIORITY
    // Keep auto exposure, but ask it to respect the selected capture interval.
    // This webcam otherwise drops from 30 to 15 FPS. Restore on normal close.
    v4l2_control control{};
    control.id = V4L2_CID_EXPOSURE_AUTO_PRIORITY;
    if (call(fd_, VIDIOC_G_CTRL, &control) == 0 && control.value != 0) {
        const int previous = control.value;
        control.value = 0;
        if (call(fd_, VIDIOC_S_CTRL, &control) == 0)
            originalExposurePriority_ = previous;
    }
#endif
    v4l2_requestbuffers req{};
    req.count = 3;
    req.type = f.type;
    req.memory = V4L2_MEMORY_MMAP;
    if (call(fd_, VIDIOC_REQBUFS, &req) < 0)
        return fail();
    for (unsigned i = 0; i < req.count; i++) {
        v4l2_buffer b{};
        b.type = f.type;
        b.memory = req.memory;
        b.index = i;
        if (call(fd_, VIDIOC_QUERYBUF, &b) < 0)
            return fail();
        void *data = mmap(nullptr, b.length, PROT_READ | PROT_WRITE, MAP_SHARED, fd_, b.m.offset);
        if (data == MAP_FAILED)
            return fail();
        buffers_.push_back({data, b.length});
        if (call(fd_, VIDIOC_QBUF, &b) < 0)
            return fail();
    }
    int type = f.type;
    if (call(fd_, VIDIOC_STREAMON, &type) < 0)
        return fail();
    streaming_ = true;
    error.clear();
    return true;
}
bool Camera::read(cv::Mat &frame) {
    if (fd_ < 0)
        return false;
    pollfd p{fd_, POLLIN, 0};
    int r = poll(&p, 1, 500);
    if (r <= 0) {
        error = r == 0 ? "Camera timeout" : strerror(errno);
        return false;
    }
    v4l2_buffer b{};
    b.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    b.memory = V4L2_MEMORY_MMAP;
    if (call(fd_, VIDIOC_DQBUF, &b) < 0) {
        error = strerror(errno);
        return false;
    }
    // After a scheduling stall, discard older queued frames before decoding.
    // Never stamp old buffered imagery as if it had just been captured.
    for (size_t i = 1; i < buffers_.size(); ++i) {
        v4l2_buffer newest{};
        newest.type = b.type;
        newest.memory = b.memory;
        if (call(fd_, VIDIOC_DQBUF, &newest) < 0)
            break;
        call(fd_, VIDIOC_QBUF, &b);
        b = newest;
    }
    bool ok = false;
    if (b.index < buffers_.size() && b.bytesused <= buffers_[b.index].size) {
        auto data = static_cast<unsigned char *>(buffers_[b.index].data);
        if (format_ == V4L2_PIX_FMT_MJPEG)
            frame = cv::imdecode(cv::Mat(1, b.bytesused, CV_8U, data), cv::IMREAD_COLOR);
        else if (b.bytesused >= unsigned(width_ * height_ * 2))
            cv::cvtColor(cv::Mat(height_, width_, CV_8UC2, data), frame, cv::COLOR_YUV2BGR_YUYV);
        ok = !frame.empty();
    }
    call(fd_, VIDIOC_QBUF, &b);
    return ok;
}
} // namespace holohand
