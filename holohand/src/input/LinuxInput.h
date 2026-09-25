#pragma once
#include "Input.h"
#include <QDBusConnection>
#include <QDBusContext>
#include <QDBusMessage>
#include <QElapsedTimer>
#include <QObject>
#include <QTimer>
#include <algorithm>
namespace holohand {
class LinuxInput final : public QObject, public Input, protected QDBusContext {
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface", "org.phax.HoloHand.Input")
  public:
    LinuxInput();
    ~LinuxInput() override;
    bool available() const override { return fd_ >= 0 && bridge_ && flat_ && probed_; }
    QString reason() const override;
    void move(QPointF) override;
    void setSpeed(double speed) override { speed_ = std::clamp(speed, 300., 2400.); }
    void setDirect(bool direct) override {
        direct_ = direct;
        stopMotion();
    }
    void stopMotion() override {
        ++epoch_;
        inFlight_ = false;
        movePending_ = false;
        lastMotionTime_ = -1;
    }
    void button(bool) override;
    void click(bool) override;
    void scroll(double, double) override;
    void maximize() override;
    void release() override;
  public slots:
    QString Next();
    bool Position(int epoch, int x, int y);
    bool Probe(int x, int y);
    void TogglePause();
  signals:
    void pauseRequested();

  private:
    void emitEvent(unsigned short type, unsigned short code, int value);
    void sync();
    void deliver();
    void configurePointer();
    bool trusted() const;
    int fd_ = -1;
    bool held_ = false, bridge_ = false, flat_ = false, inFlight_ = false, probed_ = false;
    double sx_ = 0, sy_ = 0;
    double speed_ = 900;
    bool direct_ = false;
    QPointF target_;
    bool movePending_ = false, maxPending_ = false;
    QDBusMessage pending_;
    bool waiting_ = false;
    QString kwinOwner_;
    QTimer heartbeat_;
    QTimer motionTimer_, deviceTimer_;
    QElapsedTimer clock_;
    qint64 targetTime_ = 0, requestTime_ = 0;
    qint64 lastMotionTime_ = -1;
    int epoch_ = 1;
    quint64 positionReplies_ = 0;
    int scriptId_ = -1;
};
} // namespace holohand
