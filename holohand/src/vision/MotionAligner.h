#pragma once
#include "core/Gesture.h"
#include <opencv2/core.hpp>
#include <optional>
namespace holohand {
// Align an already recognized hand to newer camera pixels. Output is only for
// preview/pointer placement, never for recognizing or authorizing a gesture.
std::optional<Hand> alignHandMotion(const cv::Mat &recognizedFrame, const cv::Mat &currentFrame,
                                    const Hand &, double gap);
} // namespace holohand
