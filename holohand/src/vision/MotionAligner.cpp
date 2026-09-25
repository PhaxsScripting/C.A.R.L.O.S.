#include "MotionAligner.h"
#include <algorithm>
#include <opencv2/imgproc.hpp>
#include <opencv2/video/tracking.hpp>
namespace holohand {
std::optional<Hand> alignHandMotion(const cv::Mat &from, const cv::Mat &to, const Hand &hand,
                                    double gap) {
    if (!hand.valid || hand.confidence < .8 || gap <= 0 || gap > .085 || from.empty() ||
        from.size() != to.size() || from.type() != CV_8UC3 || to.type() != CV_8UC3)
        return {};
    cv::Mat a, b;
    cv::cvtColor(from, a, cv::COLOR_BGR2GRAY);
    cv::cvtColor(to, b, cv::COLOR_BGR2GRAY);
    std::vector<cv::Point2f> points;
    std::vector<int> ids;
    for (int i = 0; i < 21; ++i) {
        const auto p = hand.p[i];
        if (hand.visible(i) && std::isfinite(p.x) && std::isfinite(p.y)) {
            points.emplace_back(p.x * from.cols, p.y * from.rows);
            ids.push_back(i);
        }
    }
    if (points.size() < 10)
        return {};
    std::vector<cv::Point2f> next, back;
    std::vector<unsigned char> forwardOk, backOk;
    std::vector<float> forwardError, backError;
    const cv::TermCriteria stop(cv::TermCriteria::COUNT | cv::TermCriteria::EPS, 15, .02);
    cv::calcOpticalFlowPyrLK(a, b, points, next, forwardOk, forwardError, {21, 21}, 2, stop);
    cv::calcOpticalFlowPyrLK(b, a, next, back, backOk, backError, {21, 21}, 2, stop);
    Hand aligned = hand;
    aligned.imageBoundsKnown = true;
    aligned.observedMask = 0;
    int accepted = 0;
    bool pointerOk = false;
    for (size_t i = 0; i < ids.size(); ++i) {
        const auto p = next[i];
        if (!forwardOk[i] || !backOk[i] || !std::isfinite(p.x) || !std::isfinite(p.y) ||
            forwardError[i] > 20 || backError[i] > 20 || cv::norm(back[i] - points[i]) > 1.5 ||
            cv::norm(next[i] - points[i]) > from.cols * .12 || p.x < 0 || p.y < 0 ||
            p.x >= from.cols || p.y >= from.rows)
            continue;
        aligned.p[ids[i]].x = p.x / from.cols;
        aligned.p[ids[i]].y = p.y / from.rows;
        aligned.observedMask |= (1u << ids[i]);
        ++accepted;
        pointerOk = pointerOk || ids[i] == 8;
    }
    if (!pointerOk || accepted < std::max(10, int(points.size() * 3 / 4)))
        return {};
    return aligned;
}
} // namespace holohand
