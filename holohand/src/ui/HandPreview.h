#pragma once
#include "core/Gesture.h"
#include <QColor>
#include <QImage>
#include <QPixmap>
#include <QTimer>
#include <QWidget>

namespace holohand {
// Presentation only. This widget has no input backend and cannot issue actions.
class HandPreview final : public QWidget {
  public:
    explicit HandPreview(QWidget *parent = nullptr);
    QSize sizeHint() const override { return {640, 340}; }
    QSize minimumSizeHint() const override { return {320, 200}; }
    void setFrame(const QImage &, const Hand &);
    void setVideo(const QImage &image) {
        image_ = image;
        update();
    }
    void setHand(const Hand &);
    void setState(const QString &mode, double progress, bool enabled);
    void trigger(const Event &, const Hand &);
    void clearTracking();
    void setEffects(int level) {
        effects_ = level;
        particles_.clear();
        pulses_.clear();
    }
    size_t particleCount() const { return particles_.size(); }

  protected:
    void paintEvent(QPaintEvent *) override;

  private:
    struct Particle {
        QPointF position, velocity;
        QColor color;
        double born, life;
        bool gesture = false;
    };
    struct Pulse {
        QPointF position;
        QColor color;
        double born;
        QString label;
        int finger = 8;
    };
    void burst(Point, QColor, int, double, QPointF direction = {}, bool gesture = false);
    QImage image_;
    Hand hand_;
    QString mode_ = "IDLE";
    double handTime_ = -10, progress_ = 0, lastScroll_ = -10;
    bool enabled_ = false;
    int effects_ = 2;
    std::vector<std::pair<Hand, double>> trails_;
    std::vector<Particle> particles_;
    std::vector<Pulse> pulses_;
    QTimer animation_;
    std::array<std::array<QPixmap, 8>, 2> sparkSprites_;
};
} // namespace holohand
