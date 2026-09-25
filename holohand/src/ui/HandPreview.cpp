#include "HandPreview.h"
#include <QPainter>
#include <QPainterPath>
#include <QRadialGradient>
#include <QRandomGenerator>
#include <algorithm>
#include <chrono>

namespace holohand {
static double clockSeconds() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch())
        .count();
}
HandPreview::HandPreview(QWidget *parent) : QWidget(parent) {
    // Cache soft spark shapes/fades once; drawing hundreds of antialiased
    // ellipses and changing pens per particle was consuming most of a UI frame.
    for (int kind = 0; kind < 2; ++kind) {
        for (int fade = 0; fade < 8; ++fade) {
            QPixmap sprite(16, 16);
            sprite.fill(Qt::transparent);
            QPainter brush(&sprite);
            QRadialGradient glow(8, 8, kind ? 8 : 5);
            const int alpha = (fade + 1) * 255 / 8;
            glow.setColorAt(0, QColor(kind ? 220 : 100, 245, 255, alpha));
            glow.setColorAt(.18, QColor(70, 190, 255, alpha * 3 / 4));
            glow.setColorAt(.5, QColor(20, 100, 255, alpha / 4));
            glow.setColorAt(1, QColor(15, 70, 255, 0));
            brush.fillRect(sprite.rect(), glow);
            brush.end();
            sparkSprites_[kind][fade] = sprite;
        }
    }
    setMinimumSize(320, 200);
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
    connect(&animation_, &QTimer::timeout, this, [this] {
        if (isVisible() &&
            (clockSeconds() - handTime_ < .35 || !particles_.empty() || !pulses_.empty()))
            update();
    });
    animation_.start(16);
}
void HandPreview::setFrame(const QImage &image, const Hand &hand) {
    image_ = image;
    setHand(hand);
}
void HandPreview::setHand(const Hand &hand) {
    if (hand.valid) {
        const double time = clockSeconds();
        if (effects_ == 2) {
            if (hand_.valid && time - handTime_ < .12) {
                trails_.push_back({hand_, time});
                if (trails_.size() > 6)
                    trails_.erase(trails_.begin());
            }
            // Keep gesture bursts, but do not shed ambient particles at rest.
            std::vector<double> motion;
            if (hand_.valid && time - handTime_ < .12)
                for (int i = 0; i < 21; ++i)
                    if (hand.visible(i) && hand_.visible(i))
                        motion.push_back(distance(hand.p[i], hand_.p[i]));
            std::sort(motion.begin(), motion.end());
            const bool moving = motion.size() >= 5 && motion[motion.size() / 2] > .004;
            if (!moving)
                std::erase_if(particles_, [](const Particle &p) { return !p.gesture; });
            if (moving) {
                for (int i = 1; i < 21; ++i) {
                    if (particles_.size() >= 650)
                        break; // reserve room for gesture bursts
                    if (!hand.visible(i))
                        continue;
                    const Point old = hand_.valid ? hand_.p[i] : hand.p[i];
                    QPointF stream(std::clamp((old.x - hand.p[i].x) * 3., -.25, .25),
                                   std::clamp((old.y - hand.p[i].y) * 3., -.25, .25));
                    burst(hand.p[i], i % 4 == 0 ? QColor("#a8f6ff") : QColor("#238dff"), 2, time,
                          stream);
                }
                const double radius = distance(hand.p[5], hand.p[17]) * .035;
                for (int i = 1; i < 21 && particles_.size() < 650; ++i) {
                    int base = i % 4 == 1 ? 0 : i - 1;
                    if (!hand.visible(i) || !hand.visible(base))
                        continue;
                    double f = QRandomGenerator::global()->generateDouble();
                    Point a = hand.p[base], b = hand.p[i];
                    Point at{a.x + (b.x - a.x) * f +
                                 (QRandomGenerator::global()->generateDouble() - .5) * radius,
                             a.y + (b.y - a.y) * f +
                                 (QRandomGenerator::global()->generateDouble() - .5) * radius};
                    burst(at, QColor("#52d7ff"), 2, time);
                }
                const int palm[][3] = {{0, 5, 9}, {0, 9, 13}, {0, 13, 17}};
                for (const auto &triangle : palm) {
                    if (particles_.size() >= 650 || !hand.visible(triangle[0]) ||
                        !hand.visible(triangle[1]) || !hand.visible(triangle[2]))
                        continue;
                    for (int i = 0; i < 4; ++i) {
                        double u = QRandomGenerator::global()->generateDouble();
                        double v = QRandomGenerator::global()->generateDouble();
                        if (u + v > 1) {
                            u = 1 - u;
                            v = 1 - v;
                        }
                        Point a = hand.p[triangle[0]], b = hand.p[triangle[1]],
                              c = hand.p[triangle[2]];
                        burst({a.x + (b.x - a.x) * u + (c.x - a.x) * v,
                               a.y + (b.y - a.y) * u + (c.y - a.y) * v},
                              QColor("#287eff"), 1, time);
                    }
                }
            }
        }
        hand_ = hand;
        handTime_ = time;
    } else
        hand_ = {};
    update();
}
void HandPreview::setState(const QString &mode, double progress, bool enabled) {
    mode_ = mode;
    progress_ = std::clamp(progress, 0., 1.);
    enabled_ = enabled;
}
void HandPreview::clearTracking() {
    hand_ = {};
    particles_.clear();
    pulses_.clear();
    trails_.clear();
    update();
}
void HandPreview::burst(Point point, QColor color, int count, double time, QPointF direction,
                        bool gesture) {
    if (effects_ == 0)
        return;
    for (int i = 0; i < count && particles_.size() < (effects_ == 2 ? 1200u : 180u); ++i) {
        double angle = QRandomGenerator::global()->generateDouble() * 6.283185307;
        double speed =
            .025 + QRandomGenerator::global()->generateDouble() * (effects_ == 2 ? .22 : .06);
        particles_.push_back(
            {{point.x, point.y},
             QPointF(std::cos(angle) * speed, std::sin(angle) * speed) + direction,
             color,
             time,
             .3 + QRandomGenerator::global()->generateDouble() * (effects_ == 2 ? .65 : .3),
             gesture});
    }
}
void HandPreview::trigger(const Event &event, const Hand &hand) {
    if (!hand.valid || event.action == Action::Move || effects_ == 0)
        return;
    const double t = clockSeconds();
    std::erase_if(particles_, [t](const auto &p) { return t - p.born > p.life; });
    std::erase_if(pulses_, [t](const auto &p) { return t - p.born > .8; });
    QColor color("#8cefff");
    Point anchor = hand.p[8];
    int finger = 8;
    QString label;
    switch (event.action) {
    case Action::LeftClick:
        label = "CLICK";
        break;
    case Action::RightClick:
        label = "RIGHT CLICK";
        color = QColor("#d5fbff");
        break;
    case Action::Down:
        label = "DRAG";
        finger = 12;
        break;
    case Action::Up:
        label = "RELEASE";
        finger = 12;
        break;
    case Action::Maximize:
        label = "MAXIMIZE";
        finger = 16;
        break;
    case Action::Scroll: {
        if (t - lastScroll_ < .07)
            return;
        lastScroll_ = t;
        // Keep gesture jets distinct from the slower ambient dust.
        std::erase_if(particles_, [](const auto &p) { return !p.gesture; });
        QPointF direction(event.x, -event.y);
        const double length = std::hypot(direction.x(), direction.y());
        if (length > 0)
            direction *= .5 / length;
        for (int i : {8, 12}) {
            burst(hand.p[i], color, effects_ == 2 ? 34 : 3, t, direction, true);
            if (pulses_.size() < 8)
                pulses_.push_back({{hand.p[i].x, hand.p[i].y}, color, t, "SCROLL", i});
        }
        return;
    }
    default:
        return;
    }
    anchor = hand.p[finger];
    // Retain action sparks, clear background dust, then reserve a visible burst.
    std::erase_if(particles_, [](const auto &p) { return !p.gesture; });
    if (particles_.size() > 700)
        particles_.erase(particles_.begin(), particles_.end() - 700);
    burst(anchor, QColor("#278cff"), effects_ == 2 ? 120 : 6, t, {}, true);
    burst(anchor, color, effects_ == 2 ? 120 : 6, t, {}, true);
    if (event.action == Action::Maximize)
        burst(hand.p[20], color, 80, t, {}, true);
    if (pulses_.size() >= 8)
        pulses_.erase(pulses_.begin());
    pulses_.push_back({{anchor.x, anchor.y}, color, t, label, finger});
    update();
}
void HandPreview::paintEvent(QPaintEvent *) {
    const double t = clockSeconds();
    std::erase_if(particles_, [t](const auto &p) { return t - p.born > p.life; });
    std::erase_if(pulses_, [t](const auto &p) { return t - p.born > .8; });
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing);
    p.fillRect(rect(), QColor("#060d16"));
    QSize frame = image_.isNull() ? QSize(640, 480) : image_.size();
    frame.scale(size(), Qt::KeepAspectRatio);
    QRectF area((width() - frame.width()) / 2., (height() - frame.height()) / 2., frame.width(),
                frame.height());
    p.setClipRect(area);
    if (!image_.isNull()) {
        p.drawImage(area, image_);
    }
    auto mapped = [&](Point v) {
        return QPointF(area.left() + v.x * area.width(), area.top() + v.y * area.height());
    };
    // Interpolation is presentation-only; gestures use the latest real landmarks.
    const Hand &display = hand_; // no additional animation delay on tracked coordinates
    const double age = t - handTime_;
    const double opacity = hand_.valid ? std::clamp(1. - std::max(0., age - .12) / .18, 0., 1.) : 0;
    if (opacity > 0) {
        p.save();
        p.setOpacity(opacity);
        QColor cyan(hand_.confidence >= .8 ? "#42eaff" : "#ffca7c");
        if (effects_ == 2) {
            p.save();
            p.setCompositionMode(QPainter::CompositionMode_Plus);
            for (const auto &[previous, time] : trails_) {
                const double fade = std::clamp(1. - (t - time) / .18, 0., 1.);
                p.setPen(QPen(QColor(40, 130, 255, int(70 * fade)), 1));
                for (int i = 0; i < 21; ++i)
                    if (hand_.visible(i) && previous.visible(i))
                        p.drawLine(mapped(previous.p[i]), mapped(display.p[i]));
            }
            p.restore();
        }
        QPainterPath skeleton;
        const int chains[][5] = {{0, 1, 2, 3, 4},
                                 {0, 5, 6, 7, 8},
                                 {5, 9, 10, 11, 12},
                                 {9, 13, 14, 15, 16},
                                 {13, 17, 18, 19, 20}};
        for (const auto &chain : chains)
            for (int j = 1; j < 5; ++j)
                if (hand_.visible(chain[j - 1]) && hand_.visible(chain[j])) {
                    skeleton.moveTo(mapped(display.p[chain[j - 1]]));
                    skeleton.lineTo(mapped(display.p[chain[j]]));
                }
        p.setBrush(Qt::NoBrush);
        for (int width : {5, 2}) {
            QColor glow = cyan;
            glow.setAlpha(width == 2 ? 230 : 24);
            p.setPen(QPen(glow, width, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
            p.drawPath(skeleton);
        }
        if (effects_ == 2) {
            p.setCompositionMode(QPainter::CompositionMode_Plus);
            p.setPen(QPen(QColor(15, 115, 255, 35), 12, Qt::SolidLine, Qt::RoundCap));
            p.drawPath(skeleton);
            p.setPen(QPen(QColor(130, 235, 255, 180), 1));
            for (const auto &chain : chains)
                for (int j = 1; j < 5; ++j) {
                    if (!hand_.visible(chain[j - 1]) || !hand_.visible(chain[j]))
                        continue;
                    QPointF a = mapped(display.p[chain[j - 1]]), b = mapped(display.p[chain[j]]);
                    QPointF delta = b - a;
                    double length = std::hypot(delta.x(), delta.y());
                    if (length < 1)
                        continue;
                    QPointF normal(-delta.y() / length, delta.x() / length);
                    QPointF previous = a;
                    for (int k = 1; k <= 5; ++k) {
                        double f = k / 5.;
                        double wave = std::sin(f * 6.283 + t * 4 + j) * std::sin(f * 3.14159) * 4.;
                        QPointF at = a + delta * f + normal * wave;
                        p.drawLine(previous, at);
                        p.setBrush(QColor("#b3f7ff"));
                        p.drawEllipse(at, 1.2, 1.2);
                        previous = at;
                    }
                }
            p.setCompositionMode(QPainter::CompositionMode_SourceOver);
        }
        for (int i = 0; i < 21; ++i) {
            if (!hand_.visible(i))
                continue;
            const bool tip = i == 4 || i == 8 || i == 12 || i == 16 || i == 20;
            auto at = mapped(display.p[i]);
            p.setPen(QPen(cyan, 1));
            p.setBrush(QColor("#d6faff"));
            p.drawEllipse(at, tip ? 2.5 : 1.5, tip ? 2.5 : 1.5);
        }
        auto link = [&](int a, int b, QColor color) {
            if (!hand_.visible(a) || !hand_.visible(b))
                return;
            p.setPen(QPen(color, 2));
            p.drawLine(mapped(display.p[a]), mapped(display.p[b]));
            p.setBrush(Qt::NoBrush);
            for (int i : {a, b}) {
                auto at = mapped(display.p[i]);
                p.drawEllipse(at, 9, 9);
                if (progress_ > 0)
                    p.drawArc(QRectF(at.x() - 12, at.y() - 12, 24, 24), 90 * 16,
                              -int(progress_ * 360 * 16));
            }
        };
        if (mode_ == "PINCH" || mode_ == "PINCH PREP")
            link(4, 8, QColor("#b8f6ff"));
        if (mode_ == "SCROLL")
            link(8, 12, QColor("#41eaff"));
        if (mode_ == "DRAG")
            link(4, 12, QColor("#5faeff"));
        if (mode_ == "MAXIMIZE")
            link(16, 20, QColor("#90e8ff"));
        p.restore();
    }
    p.setCompositionMode(QPainter::CompositionMode_Plus);
    std::array<QPainterPath, 8> sparkStreaks;
    for (const auto &spark : particles_) {
        double a = t - spark.born;
        QPointF normalized = spark.position + spark.velocity * a;
        QPointF at = mapped({normalized.x(), normalized.y()});
        const int fade = std::clamp(int((1 - a / spark.life) * 8), 0, 7);
        auto &streak = sparkStreaks[fade];
        streak.moveTo(at);
        streak.lineTo(
            at -
            QPointF(spark.velocity.x() * area.width(), spark.velocity.y() * area.height()) * .035);
        p.drawPixmap(QPoint(int(at.x()) - 8, int(at.y()) - 8),
                     sparkSprites_[spark.gesture ? 1 : 0][fade]);
    }
    p.setBrush(Qt::NoBrush);
    for (int fade = 0; fade < 8; ++fade) {
        p.setPen(QPen(QColor(130, 230, 255, (fade + 1) * 220 / 8), 1, Qt::SolidLine, Qt::RoundCap));
        p.drawPath(sparkStreaks[fade]);
    }
    p.setFont(QFont("monospace", 9, QFont::DemiBold));
    for (const auto &pulse : pulses_) {
        double a = (t - pulse.born) / .8;
        QColor c = pulse.color;
        c.setAlphaF(std::clamp(1 - a, 0., 1.));
        p.setPen(QPen(c, 2));
        p.setBrush(Qt::NoBrush);
        QPointF at = mapped({pulse.position.x(), pulse.position.y()});
        const double radius = 6 + a * (effects_ == 2 ? 65 : 22);
        p.drawEllipse(at, radius, radius);
        p.setPen(QPen(c, 3 * (1 - a) + .5));
        p.drawArc(QRectF(at.x() - radius * .7, at.y() - radius * .7, radius * 1.4, radius * 1.4),
                  int(a * 240 * 16), 240 * 16);
        if (hand_.valid && hand_.visible(pulse.finger) && a < .3) {
            QColor flash("#dcfcff");
            flash.setAlphaF((1 - a / .3) * .7);
            p.setPen(Qt::NoPen);
            p.setBrush(flash);
            p.drawEllipse(mapped(hand_.p[pulse.finger]), 8 * (1 - a / .3), 8 * (1 - a / .3));
        }
    }
    p.setClipping(false);
    p.setCompositionMode(QPainter::CompositionMode_SourceOver);
    p.setPen(QPen(QColor("#276777"), 1));
    p.setBrush(Qt::NoBrush);
    p.drawRoundedRect(rect().adjusted(1, 1, -2, -2), 8, 8);
}
} // namespace holohand
