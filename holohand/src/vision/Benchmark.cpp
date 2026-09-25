#include "Camera.h"
#include "Tracker.h"
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <algorithm>
#include <chrono>
#include <iostream>
#include <numeric>
#include <opencv2/imgcodecs.hpp>
int main(int argc, char **argv) {
    if (argc < 2)
        return 1;
    try {
        std::string imagePath, reportPath;
        int frames = 150;
        for (int i = 2; i < argc; ++i) {
            const std::string arg = argv[i];
            if (i + 1 >= argc)
                throw std::runtime_error("Missing benchmark option value");
            if (arg == "--image")
                imagePath = argv[++i];
            else if (arg == "--json")
                reportPath = argv[++i];
            else if (arg == "--frames")
                frames = std::clamp(std::stoi(argv[++i]), 20, 1000);
            else
                throw std::runtime_error("Unknown benchmark option");
        }
        holohand::Tracker tracker(argv[1]);
        holohand::Camera camera;
        cv::Mat fixture;
        if (imagePath.empty()) {
            if (!camera.open("/dev/video0"))
                throw std::runtime_error(camera.error);
        } else {
            fixture = cv::imread(imagePath);
            if (fixture.empty())
                throw std::runtime_error("Unreadable public test image");
            cv::resize(fixture, fixture, {640, 480});
        }
        const auto start = std::chrono::steady_clock::now();
        std::vector<double> timings;
        QJsonArray observations;
        int hands = 0;
        cv::Mat frame;
        for (int n = 0; n < frames; n++) {
            if (fixture.empty()) {
                if (!camera.read(frame))
                    break;
                cv::flip(frame, frame, 1);
            } else {
                cv::Mat transform = (cv::Mat_<double>(2, 3) << 1, 0, 25 * std::sin(n * .1), 0, 1,
                                     15 * std::cos(n * .1));
                cv::warpAffine(fixture, frame, transform, {640, 480});
            }
            auto t = std::chrono::steady_clock::now();
            auto h = tracker.infer(frame);
            const double ms =
                std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t)
                    .count();
            if (n < 10)
                continue; // identical warmup exclusion for backend comparisons
            hands += h.valid;
            timings.push_back(ms);
            QJsonObject item{
                {"frame", n}, {"valid", h.valid}, {"confidence", h.confidence}, {"ms", ms}};
            if (!fixture.empty() && h.valid) {
                QJsonArray points;
                for (auto p : h.p)
                    points.append(QJsonArray{p.x, p.y, p.z});
                item.insert("landmarks", points); // public fixture only, never webcam coordinates
            }
            observations.append(item);
        }
        double seconds =
            std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();
        if (timings.empty())
            return 3;
        double mean = std::accumulate(timings.begin(), timings.end(), 0.) / timings.size();
        std::sort(timings.begin(), timings.end());
        QJsonObject report{{"backend", QString::fromStdString(tracker.backend())},
                           {"source", fixture.empty() ? QString("live; no images retained")
                                                      : QString::fromStdString(imagePath)},
                           {"measured_frames", int(timings.size())},
                           {"valid_hand_frames", hands},
                           {"inference_mean_ms", mean},
                           {"inference_p95_ms", timings[size_t(.95 * (timings.size() - 1))]},
                           {"elapsed_seconds", seconds},
                           {"observations", observations}};
        if (!reportPath.empty()) {
            QFile file(QString::fromStdString(reportPath));
            if (!file.open(QIODevice::WriteOnly))
                throw std::runtime_error("Cannot write benchmark report");
            file.write(QJsonDocument(report).toJson());
        }
        report.remove("observations");
        std::cout << QJsonDocument(report).toJson(QJsonDocument::Compact).toStdString()
                  << "\nNo input injected.\n";
    } catch (const std::exception &e) {
        std::cerr << e.what() << '\n';
        return 4;
    }
}
