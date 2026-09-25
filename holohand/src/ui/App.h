#pragma once
#include "HandPreview.h"
#include "core/CursorMotion.h"
#include "core/DwellClick.h"
#include "core/Gesture.h"
#include "core/PointerMapping.h"
#include "input/Input.h"
#include "input/ManualOverride.h"
#include <QCheckBox>
#include <QComboBox>
#include <QDoubleSpinBox>
#include <QImage>
#include <QLabel>
#include <QLocalServer>
#include <QSystemTrayIcon>
#include <QTimer>
#include <QWidget>
#include <atomic>
#include <mutex>
#include <thread>
namespace holohand {
class App : public QWidget {
    Q_OBJECT
  public:
    App();
    ~App();
    QString status() const;
    void command(const QString &);

  private:
    void tick();
    void startWorkers();
    void save();
    void pause(bool);
    void load();
    void release();
    void moveCursor(double);
    QRect pointerBounds() const;
    struct Result;
    void updatePresentation(const QString &guide, const QString &mode, double progress,
                            const Result &result, double time, bool urgent = false);
    struct Result {
        Hand hand;
        std::shared_ptr<void> sourceFrame;
        double time = 0, ms = 0;
        uint64_t seq = 0;
        QString error;
        QString backend;
    };
    std::atomic<bool> stop_{false}, paused_{false};
    std::atomic<int> dominant_{0};
    std::thread capture_, inference_, alignment_;
    std::mutex frameMutex_;
    mutable std::mutex resultMutex_;
    std::shared_ptr<void> frame_;
    QImage videoFrame_;
    uint64_t previewSeq_ = 0;
    uint64_t previewUpdates_ = 0;
    uint64_t frameSeq_ = 0, processed_ = 0, dropped_ = 0;
    uint64_t alignedFrames_ = 0;
    double frameTime_ = 0;
    Result latest_;
    struct Presentation {
        QImage video;
        Hand hand;
        uint64_t seq = 0;
        double time = 0, alignmentMs = 0;
    };
    mutable std::mutex presentationMutex_;
    Presentation presentation_;
    QString device_ = "/dev/video0";
    ManualOverride manual_;
    std::unique_ptr<Input> input_;
    GestureEngine engine_;
    OneEuro fx_, fy_;
    CursorMotion cursor_;
    DwellClick dwellClick_;
    QCheckBox *dwellChoice_;
    QSystemTrayIcon *tray_;
    HandPreview *preview_ = nullptr;
    QLabel *info_;
    QLabel *guide_;
    QComboBox *handChoice_;
    QComboBox *cameraChoice_;
    QComboBox *pointerMode_;
    QComboBox *effectsChoice_;
    QComboBox *screenChoice_;
    bool directPointer_ = true;
    int pointerStyle_ = 0;
    QString targetScreen_;
    QDoubleSpinBox *margin_;
    QDoubleSpinBox *topMargin_;
    QDoubleSpinBox *bottomMargin_;
    QDoubleSpinBox *smoothing_;
    QDoubleSpinBox *speed_;
    QDoubleSpinBox *pinch_;
    QDoubleSpinBox *scroll_;
    QTimer timer_;
    QLocalServer server_;
    QString configPath_;
    bool calibrated_ = false;
    double lastTime_ = 0;
    uint64_t lastSeq_ = 0;
    uint64_t lastPointerSeq_ = 0;
    double clickHoldUntil_ = 0;
    QString pendingGuide_, shownGuide_, shownMode_;
    double guideSince_ = 0, lastTelemetry_ = 0;
};
} // namespace holohand
