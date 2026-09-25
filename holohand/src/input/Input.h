#pragma once
#include <QPointF>
#include <QRect>
#include <QString>
#include <memory>
namespace holohand {
class Input {
  public:
    virtual ~Input() = default;
    virtual bool available() const = 0;
    virtual QString reason() const = 0;
    virtual void heartbeat() {};
    virtual bool takePauseRequest() { return false; }
    virtual bool manualActive() { return false; }
    virtual void move(QPointF) = 0;
    virtual void stopMotion() {}
    virtual void setSpeed(double) {}
    virtual void setDirect(bool) {}
    virtual void button(bool) = 0;
    virtual void click(bool right) = 0;
    virtual void scroll(double, double) = 0;
    virtual void maximize() = 0;
    virtual void release() = 0;
};
std::unique_ptr<Input> makeInput();
} // namespace holohand
