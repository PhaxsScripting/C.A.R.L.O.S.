// Pre/postprocessing adapted from OpenCV Zoo (Apache-2.0); see vendor licenses.
#include "Tracker.h"
#include <QCryptographicHash>
#include <QFile>
#include <algorithm>
#include <iostream>
#ifdef HOLOHAND_OPENVINO
#include <openvino/openvino.hpp>
#endif
namespace holohand {
struct Tracker::Accelerator {
#ifdef HOLOHAND_OPENVINO
    ov::Core core;
    ov::InferRequest palm, hand;
    explicit Accelerator(const std::string &dir) {
        const ov::AnyMap config{
            {ov::hint::performance_mode.name(), ov::hint::PerformanceMode::LATENCY},
            {ov::inference_num_threads.name(), 2},
            {ov::num_streams.name(), 1},
            {ov::hint::inference_precision.name(), ov::element::f32}};
        palm = core.compile_model(dir + "/palm_detection_mediapipe_2023feb.onnx", "CPU", config)
                   .create_infer_request();
        hand =
            core.compile_model(dir + "/handpose_estimation_mediapipe_2023feb.onnx", "CPU", config)
                .create_infer_request();
    }
#endif
};
Tracker::~Tracker() = default;
std::vector<cv::Mat> Tracker::forward(bool palm, const cv::Mat &input) {
    auto &net = palm ? palm_ : hand_;
    std::vector<cv::Mat> out;
#ifdef HOLOHAND_OPENVINO
    if (accelerator_) {
        auto &request = palm ? accelerator_->palm : accelerator_->hand;
        ov::Shape shape;
        for (int i = 0; i < input.dims; ++i)
            shape.push_back(input.size[i]);
        request.set_input_tensor(
            ov::Tensor(ov::element::f32, shape, const_cast<float *>(input.ptr<float>())));
        request.infer();
        for (const auto &name : net.getUnconnectedOutLayersNames()) {
            auto tensor = request.get_tensor(name);
            if (tensor.get_element_type() != ov::element::f32)
                throw std::runtime_error("Unexpected accelerated model output type");
            std::vector<int> dims;
            for (auto d : tensor.get_shape())
                dims.push_back(int(d));
            out.push_back(
                cv::Mat(int(dims.size()), dims.data(), CV_32F, tensor.data<float>()).clone());
        }
        return out;
    }
#endif
    net.setInput(input);
    net.forward(out, net.getUnconnectedOutLayersNames());
    return out;
}
static cv::Mat nhwc(const cv::Mat &bgr, int side) {
    cv::Mat rgb, f;
    cv::resize(bgr, rgb, {side, side});
    cv::cvtColor(rgb, rgb, cv::COLOR_BGR2RGB);
    rgb.convertTo(f, CV_32FC3, 1. / 255);
    int dims[] = {1, side, side, 3};
    return cv::Mat(4, dims, CV_32F, f.data).clone();
}
Tracker::Tracker(const std::string &dir) {
    for (auto asset :
         {std::pair{"palm_detection_mediapipe_2023feb.onnx",
                    "78ff51c38496b7fc8b8ebdb6cc8c1abb02fa6c38427c6848254cdaba57fcce7c"},
          std::pair{"handpose_estimation_mediapipe_2023feb.onnx",
                    "db0898ae717b76b075d9bf563af315b29562e11f8df5027a1ef07b02bef6d81c"}}) {
        QFile file(QString::fromStdString(dir + "/" + asset.first));
        QCryptographicHash hash(QCryptographicHash::Sha256);
        if (!file.open(QIODevice::ReadOnly) || !hash.addData(&file) ||
            hash.result().toHex() != asset.second)
            throw std::runtime_error("Model missing or SHA256 mismatch; input remains disabled");
    }
    const int requested = qEnvironmentVariableIntValue("HOLOHAND_THREADS");
    cv::setNumThreads(requested == 2 ? 2 : 1);
    palm_ = cv::dnn::readNet(dir + "/palm_detection_mediapipe_2023feb.onnx");
    hand_ = cv::dnn::readNet(dir + "/handpose_estimation_mediapipe_2023feb.onnx");
    const QString backend = qEnvironmentVariable("HOLOHAND_BACKEND", "auto");
    if (backend != "auto" && backend != "opencv" && backend != "openvino")
        throw std::runtime_error("Unknown tracking backend");
#ifdef HOLOHAND_OPENVINO
    if (backend != "opencv") {
        try {
            accelerator_ = std::make_unique<Accelerator>(dir);
            backend_ = "OpenVINO CPU (FP32, 2 threads)";
        } catch (const std::exception &e) {
            if (backend == "openvino")
                throw;
            backend_ = "OpenCV CPU (OpenVINO unavailable)";
            std::cerr << "OpenVINO initialization failed: " << e.what() << '\n';
        }
    }
#else
    if (backend == "openvino")
        throw std::runtime_error("OpenVINO is not compiled into this build");
#endif
    for (int grid : {24, 12})
        for (int y = 0; y < grid; y++)
            for (int x = 0; x < grid; x++)
                for (int k = 0; k < (grid == 24 ? 2 : 6); k++)
                    anchors_.push_back({float(x + .5f) / grid, float(y + .5f) / grid});
}
std::vector<Palm> Tracker::palms(const cv::Mat &image) {
    const int side = std::max(image.cols, image.rows);
    cv::Mat square;
    int left = (side - image.cols) / 2, top = (side - image.rows) / 2;
    cv::copyMakeBorder(image, square, top, side - image.rows - top, left, side - image.cols - left,
                       cv::BORDER_CONSTANT);
    auto out = forward(true, nhwc(square, 192));
    if (out.size() != 2 || out[0].total() != anchors_.size() * 18 ||
        out[1].total() != anchors_.size())
        throw std::runtime_error("Unexpected palm model tensors");
    auto boxes = out[0].ptr<float>();
    auto scores = out[1].ptr<float>();
    std::vector<Palm> candidates;
    std::vector<cv::Rect> rects;
    std::vector<float> confidence;
    for (size_t i = 0; i < anchors_.size(); i++) {
        float s = 1.f / (1.f + std::exp(-std::clamp(scores[i], -80.f, 80.f)));
        if (s < .7)
            continue;
        const float *b = boxes + i * 18;
        float x = (b[0] / 192 + anchors_[i].x) * side - left,
              y = (b[1] / 192 + anchors_[i].y) * side - top, w = b[2] / 192 * side,
              h = b[3] / 192 * side;
        if (w <= 0 || h <= 0)
            continue;
        Palm p;
        p.box = {x - w / 2, y - h / 2, w, h};
        p.score = s;
        for (int j = 0; j < 7; j++)
            p.p[j] = {(b[4 + j * 2] / 192 + anchors_[i].x) * side - left,
                      (b[5 + j * 2] / 192 + anchors_[i].y) * side - top};
        candidates.push_back(p);
        rects.push_back(p.box);
        confidence.push_back(s);
    }
    std::vector<int> keep;
    cv::dnn::NMSBoxes(rects, confidence, .7, .3, keep);
    std::vector<Palm> result;
    for (int i : keep) {
        result.push_back(candidates[i]);
        if (result.size() == 2)
            break;
    }
    return result;
}
Hand Tracker::pose(const cv::Mat &image, const Palm &p) {
    cv::Point2f delta = p.p[2] - p.p[0];
    double angle = 90 - std::atan2(-delta.y, delta.x) * 180 / CV_PI;
    cv::Point2f center(p.box.x + p.box.width / 2, p.box.y + p.box.height / 2);
    cv::Mat R = cv::getRotationMatrix2D(center, angle, 1);
    auto transform = [&](cv::Point2f v) {
        return cv::Point2f(R.at<double>(0, 0) * v.x + R.at<double>(0, 1) * v.y + R.at<double>(0, 2),
                           R.at<double>(1, 0) * v.x + R.at<double>(1, 1) * v.y +
                               R.at<double>(1, 2));
    };
    float minx = 1e9, miny = 1e9, maxx = -1e9, maxy = -1e9;
    std::vector<cv::Point2f> region;
    if (p.fullHand)
        region.assign(p.all.begin(), p.all.end());
    else
        region.assign(p.p.begin(), p.p.end());
    for (auto v : region) {
        auto q = transform(v);
        minx = std::min(minx, q.x);
        maxx = std::max(maxx, q.x);
        miny = std::min(miny, q.y);
        maxy = std::max(maxy, q.y);
    }
    double side = std::max(maxx - minx, maxy - miny) * (p.fullHand ? 1.65 : 3.0);
    if (side < 8)
        return {};
    double cx = (minx + maxx) / 2, cy = (miny + maxy) / 2 - (p.fullHand ? .1 : .4) * (maxy - miny),
           scale = 224 / side;
    R.at<double>(0, 2) -= cx - side / 2;
    R.at<double>(1, 2) -= cy - side / 2;
    R *= scale;
    cv::Mat crop;
    cv::warpAffine(image, crop, R, {224, 224}, cv::INTER_LINEAR, cv::BORDER_CONSTANT);
    auto out = forward(false, nhwc(crop, 224));
    if (out.size() != 4 || out[0].total() != 63)
        throw std::runtime_error("Unexpected landmark model tensors");
    Hand h;
    h.confidence = out[1].ptr<float>()[0];
    h.rightProbability = out[2].ptr<float>()[0];
    h.yScale = double(image.rows) / image.cols;
    h.imageBoundsKnown = true;
    if (h.confidence < (p.fullHand ? .65 : .8))
        return h;
    cv::Mat inv;
    cv::invertAffineTransform(R, inv);
    const float *points = out[0].ptr<float>();
    for (int i = 0; i < 21; i++) {
        double x = points[i * 3], y = points[i * 3 + 1];
        h.p[i] = {(inv.at<double>(0, 0) * x + inv.at<double>(0, 1) * y + inv.at<double>(0, 2)) /
                      image.cols,
                  (inv.at<double>(1, 0) * x + inv.at<double>(1, 1) * y + inv.at<double>(1, 2)) /
                      image.rows,
                  points[i * 3 + 2] / scale / image.cols};
        if (!h.visible(i))
            h.observedMask &= ~(1u << i);
    }
    h.valid = true;
    return h;
}
Hand Tracker::infer(const cv::Mat &image, int dominant) {
    if (dominant != dominant_) {
        reset();
        dominant_ = dominant;
    }
    Hand best;
    std::optional<Palm> chosen;
    if (tracked_) {
        best = pose(image, *tracked_);
        if (best.valid && locked_ && distance(last_, best.p[0]) > .18)
            best.valid = false;
        if (best.valid)
            chosen = tracked_;
    }
    if (!best.valid && (!tracked_ || misses_ % 2 == 1)) {
        double cost = 1e9;
        for (const auto &p : palms(image)) {
            auto h = pose(image, p);
            if (!h.valid)
                continue;
            if (dominant == 1 && h.rightProbability < .65)
                continue;
            if (dominant == 2 && h.rightProbability > .35)
                continue;
            double c = locked_ ? distance(last_, h.p[0]) : 1 - h.confidence;
            if (locked_ && c > .3)
                continue;
            if (c < cost) {
                cost = c;
                best = h;
                chosen = p;
            }
        }
    }
    frames_++;
    if (best.valid)
        best = filter_.update(best);
    if (best.valid) {
        misses_ = 0;
        Palm next;
        next.fullHand = true;
        for (int i = 0; i < 21; i++)
            next.all[i] = {float(best.p[i].x * image.cols), float(best.p[i].y * image.rows)};
        int ids[] = {0, 5, 9, 13, 17, 1, 2};
        std::vector<cv::Point2f> pts;
        for (int i = 0; i < 7; i++) {
            next.p[i] = {float(best.p[ids[i]].x * image.cols),
                         float(best.p[ids[i]].y * image.rows)};
            pts.push_back(next.p[i]);
        }
        next.box = cv::boundingRect(pts);
        next.score = best.confidence;
        tracked_ = next;
        last_ = best.p[0];
        locked_ = true;
    } else {
        // Keep the last real ROI through a short occlusion. Retry it on fresh
        // frames; no landmarks or input are fabricated during those misses.
        if (misses_ >= 2)
            tracked_.reset();
        if (++misses_ >= 12) {
            locked_ = false;
            filter_.reset();
        }
    }
    return best;
}
} // namespace holohand
