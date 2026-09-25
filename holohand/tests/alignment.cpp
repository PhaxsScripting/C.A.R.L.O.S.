#include "vision/MotionAligner.h"
#include <cassert>
#include <opencv2/imgproc.hpp>
int main() {
    cv::setNumThreads(1);
    cv::Mat old(240, 320, CV_8UC3);
    cv::RNG random(7249);
    random.fill(old, cv::RNG::UNIFORM, 0, 255);
    cv::GaussianBlur(old, old, {3, 3}, .7);
    cv::Mat current;
    cv::Mat transform = (cv::Mat_<double>(2, 3) << 1, 0, 7, 0, 1, 4);
    cv::warpAffine(old, current, transform, old.size());
    holohand::Hand h;
    h.valid = true;
    h.confidence = .99;
    for (int i = 0; i < 21; ++i)
        h.p[i] = {.2 + (i % 7) * .08, .25 + (i / 7) * .2};
    auto aligned = holohand::alignHandMotion(old, current, h, .034);
    assert(aligned);
    for (int i = 0; i < 21; ++i) {
        assert(std::abs((aligned->p[i].x - h.p[i].x) * 320 - 7) < .7);
        assert(std::abs((aligned->p[i].y - h.p[i].y) * 240 - 4) < .7);
    }
    assert(!holohand::alignHandMotion(old, current, h, .2)); // stale result never corrected
    cv::Mat blank = cv::Mat::zeros(old.size(), old.type());
    assert(!holohand::alignHandMotion(blank, blank, h, .034)); // no features is not zero motion
    h.confidence = .6;
    assert(!holohand::alignHandMotion(old, current, h, .034));
}
