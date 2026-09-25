#include "App.h"
#include "core/CaptureTiming.h"
#include "vision/Camera.h"
#include "vision/MotionAligner.h"
#include "vision/Tracker.h"
#ifdef __linux__
#include "input/LinuxInput.h"
#endif
#include <QApplication>
#include <QDir>
#include <QFile>
#include <QFormLayout>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLocalSocket>
#include <QMenu>
#include <QMessageBox>
#include <QPainter>
#include <QPushButton>
#include <QSaveFile>
#include <QScreen>
#include <QScrollArea>
#include <QStandardPaths>
#include <QVBoxLayout>
#include <chrono>
#include <limits>
#include <sys/stat.h>
#include <unistd.h>
namespace holohand {
static double now() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch())
        .count();
}
static QString runtime() {
    QString d = qEnvironmentVariable("XDG_RUNTIME_DIR");
    if (d.isEmpty())
        d = QString("/tmp/holohand-%1").arg(getuid());
    QDir().mkpath(d);
    struct stat s{};
    if (lstat(d.toLocal8Bit(), &s) || !S_ISDIR(s.st_mode) || s.st_uid != getuid() ||
        (s.st_mode & 0077))
        throw std::runtime_error("Runtime directory must be owned by you with mode 0700");
    return d + "/holohand.sock";
}
App::App() {
    setWindowTitle("HoloHand // LOCAL GESTURE CONTROL");
    resize(740, 690);
    setStyleSheet("QWidget{background:#080d12;color:#cfecf4;} "
                  "QPushButton,QComboBox,QDoubleSpinBox{background:#142632;border:1px solid "
                  "#246374;padding:7px;border-radius:4px;} "
                  "QPushButton:hover{border-color:#35e7ff;} QLabel{padding:3px;}");
    configPath_ = QStandardPaths::writableLocation(QStandardPaths::GenericConfigLocation) +
                  "/holohand/config.json";
    load();
    input_ = makeInput();
#ifdef __linux__
    if (auto *linuxInput = dynamic_cast<LinuxInput *>(input_.get()))
        connect(linuxInput, &LinuxInput::pauseRequested, this, [this] { pause(!paused_); });
#endif
    auto *layout = new QVBoxLayout(this);
    auto *title = new QLabel("HOLOHAND  /  CALIBRATION & TELEMETRY");
    title->setStyleSheet("color:#35e7ff;font-size:17px;");
    layout->addWidget(title);
    guide_ = new QLabel("Point to move · pinch to click · Ctrl+Alt+H to pause");
    guide_->setWordWrap(true);
    guide_->setFixedHeight(54);
    guide_->setStyleSheet("color:#35e7ff;font-size:15px;padding:8px;");
    layout->addWidget(guide_);
    preview_ = new HandPreview;
    layout->addWidget(preview_, 1);
    info_ = new QLabel;
    info_->setTextFormat(Qt::PlainText);
    info_->setFont(QFont("monospace", 9));
    info_->setFixedHeight(54);
    info_->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);
    layout->addWidget(info_);
    auto *settingsPanel = new QWidget;
    auto *form = new QFormLayout(settingsPanel);
    auto *settingsScroll = new QScrollArea;
    settingsScroll->setWidgetResizable(true);
    settingsScroll->setWidget(settingsPanel);
    settingsScroll->setMinimumHeight(150);
    settingsScroll->setMaximumHeight(220);
    layout->addWidget(settingsScroll);
    cameraChoice_ = new QComboBox;
    for (auto name : QDir("/dev").entryList({"video*"}, QDir::System))
        cameraChoice_->addItem("/dev/" + name);
    cameraChoice_->setCurrentText(device_);
    form->addRow("Camera (applies after restart)", cameraChoice_);
    handChoice_ = new QComboBox;
    handChoice_->addItems({"Auto — stable hand lock", "Right", "Left"});
    handChoice_->setCurrentIndex(dominant_);
    connect(handChoice_, &QComboBox::currentIndexChanged, this, [this](int i) {
        dominant_ = i;
        pause(paused_);
    });
    form->addRow("Controlling hand", handChoice_);
    effectsChoice_ = new QComboBox;
    effectsChoice_->addItems({"Off", "Subtle", "Energy particles"});
    int effects = 2;
    QFile effectsConfig(configPath_);
    if (effectsConfig.open(QIODevice::ReadOnly))
        effects =
            QJsonDocument::fromJson(effectsConfig.readAll()).object().value("effects").toInt(2);
    effectsChoice_->setCurrentIndex(std::clamp(effects, 0, 2));
    preview_->setEffects(effectsChoice_->currentIndex());
    connect(effectsChoice_, &QComboBox::currentIndexChanged, this,
            [this](int level) { preview_->setEffects(level); });
    form->addRow("Hand effects", effectsChoice_);
    pointerMode_ = new QComboBox;
    pointerMode_->addItems(
        {"Responsive — steady aiming", "Direct — raw fingertip", "Smoothed — speed limited"});
    pointerMode_->setCurrentIndex(pointerStyle_);
    input_->setDirect(directPointer_);
    cursor_.setDirect(directPointer_);
    cursor_.setPrecision(pointerStyle_ == 0);
    connect(pointerMode_, &QComboBox::currentIndexChanged, this, [this](int index) {
        release();
        pointerStyle_ = index;
        directPointer_ = index != 2;
        input_->setDirect(directPointer_);
        cursor_.setDirect(directPointer_);
        cursor_.setPrecision(index == 0);
    });
    layout->insertWidget(1, pointerMode_);
    auto number = [&](double min, double max, double step, double value) {
        auto *s = new QDoubleSpinBox;
        s->setRange(min, max);
        s->setSingleStep(step);
        s->setValue(value);
        return s;
    };
    margin_ = number(0, .35, .01, .16);
    topMargin_ = number(0, .35, .01, .18);
    bottomMargin_ = number(0, .4, .01, .32);
    smoothing_ = number(.3, 5, .1, 1.4);
    speed_ = number(300, 2400, 100, 600);
    pinch_ = number(.12, .4, .01, .27);
    scroll_ = number(5, 100, 5, 45);
    QFile cfg(configPath_);
    if (cfg.open(QIODevice::ReadOnly)) {
        auto o = QJsonDocument::fromJson(cfg.readAll()).object();
        margin_->setValue(o.value("margin").toDouble(.16));
        topMargin_->setValue(o.value("top_margin").toDouble(.18));
        bottomMargin_->setValue(o.value("bottom_margin").toDouble(.32));
        smoothing_->setValue(o.value("smoothing").toDouble(1.4));
        speed_->setValue(o.value("cursor_speed").toDouble(600));
        pinch_->setValue(o.value("pinch").toDouble(.27));
        scroll_->setValue(o.value("scroll").toDouble(45));
    }
    screenChoice_ = new QComboBox;
    screenChoice_->addItem("All screens", "");
    for (auto *screen : QGuiApplication::screens())
        screenChoice_->addItem(screen->name(), screen->name());
    screenChoice_->setCurrentIndex(std::max(0, screenChoice_->findData(targetScreen_)));
    connect(screenChoice_, &QComboBox::currentIndexChanged, this, [this] {
        targetScreen_ = screenChoice_->currentData().toString();
        release();
    });
    form->addRow("Control screen", screenChoice_);
    dwellChoice_ = new QCheckBox("Hold still for 0.9 seconds to click (optional)");
    if (cfg.isOpen()) {
        cfg.seek(0);
        dwellChoice_->setChecked(
            QJsonDocument::fromJson(cfg.readAll()).object().value("dwell_click").toBool(false));
    }
    connect(dwellChoice_, &QCheckBox::toggled, this, [this] { release(); });
    form->addRow("Dwell click", dwellChoice_);
    auto *reachHelp = new QLabel(
        "Reach every edge inside the camera. Increase an inset to reach that edge sooner.");
    reachHelp->setWordWrap(true);
    form->addRow(reachHelp);
    form->addRow("Left / right inset", margin_);
    form->addRow("Top inset", topMargin_);
    form->addRow("Bottom inset", bottomMargin_);
    for (auto *control : {margin_, topMargin_, bottomMargin_})
        connect(control, &QDoubleSpinBox::valueChanged, this, [this] { release(); });
    form->addRow("Pointer responsiveness", smoothing_);
    form->addRow("Cursor speed (lower = easier aiming)", speed_);
    input_->setSpeed(speed_->value());
    connect(speed_, &QDoubleSpinBox::valueChanged, this,
            [this](double speed) { input_->setSpeed(speed); });
    auto *cursorControls = new QHBoxLayout;
    cursorControls->addWidget(new QLabel("Cursor feel"));
    auto *feel = new QComboBox;
    feel->addItem("Easy aiming — slow", 600);
    feel->addItem("Balanced", 900);
    feel->addItem("Quick travel", 1500);
    feel->addItem("Custom speed", 0);
    auto reflectSpeed = [feel](double speed) {
        const int index = feel->findData(int(speed));
        feel->setCurrentIndex(index < 0 ? 3 : index);
    };
    reflectSpeed(speed_->value());
    connect(feel, &QComboBox::activated, this, [this, feel](int index) {
        const int speed = feel->itemData(index).toInt();
        if (speed > 0)
            speed_->setValue(speed);
    });
    connect(speed_, &QDoubleSpinBox::valueChanged, this, reflectSpeed);
    cursorControls->addWidget(feel, 1);
    layout->insertLayout(1, cursorControls);
    auto modeControls = [this, feel](int index) {
        const bool smooth = index == 2;
        feel->setEnabled(smooth);
        speed_->setEnabled(smooth);
        smoothing_->setEnabled(smooth);
    };
    connect(pointerMode_, &QComboBox::currentIndexChanged, this, modeControls);
    modeControls(pointerMode_->currentIndex());
    form->addRow("Pinch / palm scale", pinch_);
    form->addRow("Scroll sensitivity", scroll_);
    Settings settings;
    settings.enter = pinch_->value();
    settings.exit = settings.enter + .22;
    settings.scrollGain = scroll_->value();
    engine_ = GestureEngine(settings);
    auto *resetButton = new QPushButton("Reset calibration (disables input)");
    connect(resetButton, &QPushButton::clicked, this, [this] {
        pause(true);
        if (QMessageBox::question(this, "Reset HoloHand calibration",
                                  "Remove saved calibration and disable hand input?",
                                  QMessageBox::Yes | QMessageBox::No,
                                  QMessageBox::No) != QMessageBox::Yes)
            return;
        release();
        calibrated_ = false;
        QFile::remove(configPath_);
        margin_->setValue(.16);
        topMargin_->setValue(.18);
        bottomMargin_->setValue(.32);
        smoothing_->setValue(1.4);
        speed_->setValue(600);
        pinch_->setValue(.27);
        scroll_->setValue(45);
    });
    resetButton->hide(); // reset belongs in the tray menu, away from the enable control
    auto *saveButton = new QPushButton("Save calibration and enable input");
    connect(saveButton, &QPushButton::clicked, this, &App::save);
    layout->insertWidget(1, saveButton);
    auto *pauseButton = new QPushButton("Pause / Resume — Ctrl+Alt+H on KDE Wayland");
    connect(pauseButton, &QPushButton::clicked, this, [this] { pause(!paused_); });
    layout->addWidget(pauseButton);
    QPixmap icon(32, 32);
    icon.fill(Qt::transparent);
    {
        QPainter p(&icon);
        p.setPen(QPen(QColor("#35e7ff"), 2));
        p.drawEllipse(6, 6, 20, 20);
        p.drawLine(16, 8, 16, 24);
    }
    tray_ = new QSystemTrayIcon(QIcon(icon), this);
    auto *menu = new QMenu(this);
    menu->addAction("Calibration / settings", this, [this] {
        show();
        raise();
        activateWindow();
    });
    menu->addAction("Pause", this, [this] { pause(true); });
    menu->addAction("Resume", this, [this] { pause(false); });
    menu->addAction("Quit", qApp, &QApplication::quit);
    tray_->setContextMenu(menu);
    menu->addAction("Reset saved calibration…", resetButton, &QPushButton::click);
    tray_->show();
    connect(tray_, &QSystemTrayIcon::activated, this, [this](auto) {
        show();
        raise();
    });
    server_.setSocketOptions(QLocalServer::UserAccessOption);
    QLocalServer::removeServer(runtime());
    if (!server_.listen(runtime()))
        throw std::runtime_error("Cannot create private control socket");
    connect(&server_, &QLocalServer::newConnection, this, [this] {
        while (auto *s = server_.nextPendingConnection()) {
            connect(s, &QLocalSocket::readyRead, this, [this, s] {
                if (s->bytesAvailable() > 1024) {
                    s->abort();
                    return;
                }
                QString cmd = QString::fromUtf8(s->readAll()).trimmed();
                command(cmd);
                s->write(status().toUtf8());
                s->disconnectFromServer();
            });
            connect(s, &QLocalSocket::disconnected, s, &QObject::deleteLater);
        }
    });
    startWorkers();
    connect(&timer_, &QTimer::timeout, this, &App::tick);
    timer_.setTimerType(Qt::PreciseTimer);
    timer_.start(16);
    if (!calibrated_)
        show();
}
void App::load() {
    QFile f(configPath_);
    if (f.open(QIODevice::ReadOnly)) {
        auto o = QJsonDocument::fromJson(f.readAll()).object();
        calibrated_ = o.value("calibrated").toBool();
        device_ = o.value("camera").toString("/dev/video0");
        dominant_ = o.value("hand").toInt();
        pointerStyle_ = std::clamp(o.value("pointer_style").toInt(0), 0, 2);
        directPointer_ = pointerStyle_ != 2;
        targetScreen_ = o.value("target_screen").toString();
    }
}
void App::save() {
    QDir().mkpath(QFileInfo(configPath_).absolutePath());
    QSaveFile f(configPath_);
    if (!f.open(QIODevice::WriteOnly)) {
        QMessageBox::warning(this, "Calibration could not be saved", f.errorString());
        return;
    }
    f.setPermissions(QFile::ReadOwner | QFile::WriteOwner);
    QJsonObject o{{"calibrated", true},
                  {"camera", cameraChoice_->currentText()},
                  {"hand", handChoice_->currentIndex()},
                  {"margin", margin_->value()},
                  {"top_margin", topMargin_->value()},
                  {"bottom_margin", bottomMargin_->value()},
                  {"target_screen", targetScreen_},
                  {"pointer_style", pointerStyle_},
                  {"dwell_click", dwellChoice_->isChecked()},
                  {"smoothing", smoothing_->value()},
                  {"cursor_speed", speed_->value()},
                  {"direct_pointer", directPointer_},
                  {"effects", effectsChoice_->currentIndex()},
                  {"pinch", pinch_->value()},
                  {"scroll", scroll_->value()}};
    f.write(QJsonDocument(o).toJson());
    if (f.commit()) {
        Settings s;
        s.enter = pinch_->value();
        s.exit = s.enter + .22;
        s.scrollGain = scroll_->value();
        release();
        engine_ = GestureEngine(s);
        calibrated_ = true;
        pause(false);
    } else {
        QMessageBox::warning(this, "Calibration could not be saved", f.errorString());
    }
}
void App::release() {
    dwellClick_.reset();
    cursor_.freeze();
    lastPointerSeq_ = 0;
    engine_.reset();
    if (input_)
        input_->release();
    fx_.reset();
    fy_.reset();
}
void App::pause(bool p) {
    paused_ = p;
    ++trackingGeneration_;
    {
        std::lock_guard lock(frameMutex_);
        frame_.reset();
    }
    {
        std::lock_guard lock(resultMutex_);
        latest_ = {};
    }
    {
        std::lock_guard lock(presentationMutex_);
        presentation_ = {};
    }
    frameReady_.notify_all();
    release();
    if (preview_)
        preview_->clearTracking();
}
QString App::status() const {
    QString tracking;
    {
        std::lock_guard lock(resultMutex_);
        tracking = QString("Hand: %1; confidence %2; inference %3 ms; age %4 ms; %5\n")
                       .arg(latest_.hand.valid ? "visible" : "not detected")
                       .arg(latest_.hand.confidence, 0, 'f', 2)
                       .arg(latest_.ms, 0, 'f', 1)
                       .arg((now() - latest_.time) * 1000, 0, 'f', 0)
                       .arg(latest_.error);
        tracking += QString("Backend: %1; pixel-aligned results: %2\n")
                        .arg(latest_.backend)
                        .arg(alignedFrames_);
    }
    {
        std::lock_guard lock(presentationMutex_);
        tracking += QString("Preview hand: %1; alignment %2 ms; video age %3 ms\n")
                        .arg(presentation_.hand.valid ? "matched" : "none")
                        .arg(presentation_.alignmentMs, 0, 'f', 1)
                        .arg((now() - presentation_.time) * 1000, 0, 'f', 0);
    }
    return QString("%1 | %2 | %3\n")
               .arg(paused_        ? "PAUSED"
                    : !calibrated_ ? "CALIBRATION REQUIRED"
                                   : "READY",
                    input_->reason(), QString::fromStdString(engine_.state())) +
           QString("Saved calibration: %1\n").arg(QFile::exists(configPath_) ? "yes" : "no") +
           tracking +
           QString("Preview frames: %1; camera sequence: %2; pointer: %3\n")
               .arg(previewUpdates_)
               .arg(previewSeq_)
               .arg(directPointer_ ? "direct fingertip" : "smoothed");
}
void App::command(const QString &c) {
    if (c == "--pause")
        pause(true);
    else if (c == "--resume")
        pause(false);
    else if (c == "--toggle")
        pause(!paused_);
    else if (c == "--calibrate") {
        show();
        raise();
        activateWindow();
    } else if (c == "--quit")
        qApp->quit();
}
void App::startWorkers() {
    capture_ = std::thread([this] {
        Camera camera;
        uint64_t seq = 0;
        while (!stop_) {
            if (paused_) {
                camera.close();
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
                continue;
            }
            cv::Mat frame;
            const auto generation = trackingGeneration_.load();
            if (!camera.read(frame)) {
                camera.close();
                if (!camera.open(device_.toStdString())) {
                    {
                        std::lock_guard l(resultMutex_);
                        latest_.error = "NO CAMERA: " + QString::fromStdString(camera.error);
                        latest_.time = now();
                        latest_.hand = {};
                        latest_.seq++;
                    }
                    for (int i = 0; i < 20 && !stop_; i++)
                        std::this_thread::sleep_for(std::chrono::milliseconds(100));
                }
                continue;
            }
            cv::flip(frame, frame, 1);
            cv::Mat rgb;
            cv::cvtColor(frame, rgb, cv::COLOR_BGR2RGB);
            QImage video =
                QImage(rgb.data, rgb.cols, rgb.rows, int(rgb.step), QImage::Format_RGB888).copy();
            {
                std::lock_guard l(frameMutex_);
                frame_ = std::make_shared<cv::Mat>(std::move(frame));
                videoFrame_ = std::move(video);
                frameTime_ = camera.capturedAt();
                frameGeneration_ = generation;
                frameSeq_ = ++seq;
            }
            frameReady_.notify_all();
        }
    });
    inference_ = std::thread([this] {
        try {
            QString dir = qEnvironmentVariable("HOLOHAND_MODELS");
            if (dir.isEmpty())
                dir = QCoreApplication::applicationDirPath() + "/../share/holohand/models";
            Tracker tracker(dir.toStdString());
            uint64_t seen = 0;
            uint64_t generationSeen = trackingGeneration_;
            while (!stop_) {
                std::shared_ptr<void> frame;
                double timestamp = 0;
                uint64_t seq = 0;
                uint64_t generation = 0;
                {
                    std::unique_lock l(frameMutex_);
                    frameReady_.wait_for(l, std::chrono::milliseconds(100), [this, seen] {
                        return stop_ || (!paused_ && frame_ && frameSeq_ != seen);
                    });
                    if (stop_)
                        break;
                    if (frameSeq_ != seen && !paused_ && frame_) {
                        frame = frame_;
                        timestamp = frameTime_;
                        seq = frameSeq_;
                        generation = frameGeneration_;
                    }
                }
                if (!frame)
                    continue;
                seen = seq;
                if (generation != trackingGeneration_ || !freshCapture(timestamp, now()))
                    continue;
                if (generationSeen != generation) {
                    tracker.reset();
                    generationSeen = generation;
                }
                auto image = std::static_pointer_cast<cv::Mat>(frame);
                auto start = now();
                auto h = tracker.infer(*image, dominant_);
                Result r;
                r.hand = h;
                r.backend = QString::fromStdString(tracker.backend());
                r.sourceFrame = frame;
                r.time = timestamp;
                r.ms = (now() - start) * 1000;
                r.seq = seq;
                r.generation = generation;
                {
                    std::lock_guard l(resultMutex_);
                    if (generation != trackingGeneration_ || paused_)
                        continue;
                    dropped_ += latest_.seq && seq > latest_.seq ? seq - latest_.seq - 1 : 0;
                    processed_++;
                    latest_ = std::move(r);
                }
            }
        } catch (const std::exception &e) {
            std::lock_guard l(resultMutex_);
            latest_.error = "ERROR: " + QString::fromUtf8(e.what());
            latest_.hand = {};
            latest_.seq++;
            latest_.time = now();
        }
    });
    alignment_ = std::thread([this] {
        uint64_t seen = 0;
        while (!stop_) {
            std::shared_ptr<void> frame;
            Presentation display;
            {
                std::unique_lock lock(frameMutex_);
                frameReady_.wait_for(lock, std::chrono::milliseconds(100), [this, seen] {
                    return stop_ || (!paused_ && frame_ && frameSeq_ != seen);
                });
                if (stop_)
                    break;
                if (!paused_ && frame_ && frameSeq_ != seen) {
                    frame = frame_;
                    display.video = videoFrame_;
                    display.seq = frameSeq_;
                    display.time = frameTime_;
                    display.generation = frameGeneration_;
                }
            }
            if (!frame)
                continue;
            seen = display.seq;
            if (display.generation != trackingGeneration_)
                continue;
            Result model;
            {
                std::lock_guard lock(resultMutex_);
                model = latest_;
            }
            if (model.generation == display.generation && model.hand.valid && model.sourceFrame &&
                model.hand.confidence >= .8) {
                if (display.seq == model.seq)
                    display.hand = model.hand;
                else {
                    try {
                        const double alignStart = now();
                        auto hand =
                            alignHandMotion(*std::static_pointer_cast<cv::Mat>(model.sourceFrame),
                                            *std::static_pointer_cast<cv::Mat>(frame), model.hand,
                                            display.time - model.time);
                        display.alignmentMs = (now() - alignStart) * 1000;
                        if (hand) {
                            display.hand = *hand;
                            std::lock_guard lock(resultMutex_);
                            ++alignedFrames_;
                        }
                    } catch (const cv::Exception &) {
                        // An alignment failure must never stall normal camera video.
                    }
                }
            }
            seen = display.seq;
            {
                std::lock_guard lock(presentationMutex_);
                if (display.generation == trackingGeneration_ && !paused_)
                    presentation_ = std::move(display);
            }
        }
    });
}
void App::updatePresentation(const QString &guide, const QString &mode, double progress,
                             const Result &r, double t, bool urgent) {
    // Display hysteresis never delays input cancellation or button releases.
    if (guide != pendingGuide_) {
        pendingGuide_ = guide;
        guideSince_ = t;
    }
    if (urgent || shownGuide_.isEmpty() || t - guideSince_ >= .14) {
        if (shownGuide_ != guide) {
            shownGuide_ = guide;
            guide_->setText(guide);
        }
        shownMode_ = mode;
    }
    preview_->setState(shownMode_, progress, calibrated_ && !paused_ && input_->available());
    if (t - lastTelemetry_ >= .25) {
        lastTelemetry_ = t;
        const QString ready = paused_ ? "PAUSED" : !calibrated_ ? "INPUT OFF" : "READY";
        info_->setText(QString("%1  |  confidence %2  |  inference %3 ms\n"
                               "Frame age %4 ms  |  %5  |  Ctrl+Alt+H pauses")
                           .arg(ready)
                           .arg(r.hand.confidence, 0, 'f', 2)
                           .arg(r.ms, 0, 'f', 0)
                           .arg(std::max(0., (t - r.time) * 1000), 0, 'f', 0)
                           .arg(input_->available() ? "Input connected" : "Input unavailable"));
        const QString tip = "HoloHand — " + ready;
        if (tray_->toolTip() != tip)
            tray_->setToolTip(tip);
    }
}
void App::tick() {
    input_->heartbeat();
    if (input_->takePauseRequest())
        pause(!paused_);
    Result r;
    {
        std::lock_guard l(resultMutex_);
        r = latest_;
    }
    const double t = now();
    Presentation display;
    {
        std::lock_guard lock(presentationMutex_);
        display = presentation_;
    }
    const bool newVideo = display.generation == trackingGeneration_ && display.seq != previewSeq_;
    if (newVideo) {
        previewSeq_ = display.seq;
        ++previewUpdates_;
        // Hand and video are from the same captured frame; neither waits for DNN.
        preview_->setFrame(display.video, display.hand);
    }
    const bool fresh = r.seq != lastSeq_;
    if (fresh)
        lastSeq_ = r.seq;
    if (paused_ || !r.error.isEmpty() || r.generation != trackingGeneration_ ||
        !freshCapture(r.time, t)) {
        release();
        const QString text = paused_              ? "PAUSED — resume when ready"
                             : !r.error.isEmpty() ? "Camera or model unavailable — input stopped"
                                                  : "Waiting for fresh tracking — input held";
        updatePresentation(text, paused_ ? "PAUSED" : "TRACKING HELD", 0, r, t, paused_);
        return;
    }
    if (manual_.active() || input_->manualActive()) {
        release();
        updatePresentation("Your mouse has control — hand input temporarily held", "MOUSE CONTROL",
                           0, r, t);
        return;
    }
    auto submitObservedPointer = [&] {
        if (!r.hand.valid || r.hand.confidence < .8 || !r.hand.visible(8))
            return false;
        if (r.seq == lastPointerSeq_)
            return true;
        lastPointerSeq_ = r.seq;
        const Point tip = r.hand.p[8];
        const PointerMapping mapping{margin_->value(), topMargin_->value(), bottomMargin_->value()};
        cursor_.submit(mapping.map(tip), r.time, smoothing_->value());
        return true;
    };
    if (!fresh) {
        moveCursor(t);
        return;
    }
    // Preview optical flow is visual only. Input and gesture dwell share the
    // actual model observation and its camera timestamp.
    auto events = engine_.update(r.hand, r.time);
    const auto mode = engine_.state();
    if (dwellChoice_->isChecked() && calibrated_ && input_->available() && mode == "POINT" &&
        r.hand.valid && !r.hand.partial() && r.hand.confidence >= .8 && t - r.time < .1 &&
        t >= clickHoldUntil_) {
        const PointerMapping mapping{margin_->value(), topMargin_->value(), bottomMargin_->value()};
        const Point mapped = mapping.map(r.hand.p[8]);
        const QRect bounds = pointerBounds();
        if (dwellClick_.update(
                Point{mapped.x * (bounds.width() - 1), mapped.y * (bounds.height() - 1)}, r.time))
            events.push_back({Action::LeftClick});
    } else
        dwellClick_.update({}, t);
    const QString guide =
        !calibrated_              ? "Click Save calibration and enable input above"
        : !r.hand.valid           ? "Show your hand — input held while tracking recovers"
        : r.hand.confidence < .8  ? "Tracking is uncertain — input held safely"
        : mode == "PARTIAL POINT" ? "Partial hand — pointing only; reveal the hand for gestures"
        : mode == "PALM CANCEL"   ? "Open palm — pointer held and drag released"
        : mode == "PINCH PREP"    ? "AIM LOCKED — pinch thumb + index to click"
        : mode == "PINCH"         ? "Release for left click · keep holding for right click"
        : mode == "DRAG"          ? "Hold thumb + middle to drag · open to drop"
        : mode == "SCROLL"        ? "SCROLL — move joined index + middle"
        : mode == "MAXIMIZE"      ? "Hold ring + pinky to maximize / restore"
        : mode == "POINT" ? (dwellChoice_->isChecked()
                                 ? "POINTING — hold still for 0.9 seconds to click"
                                 : "POINTING — bring thumb toward index to steady your click")
                          : "Hand found — extend index to point";
    bool moving = false;
    if (calibrated_ && input_->available()) {
        const QRect bounds = pointerBounds();
        cursor_.setViewport(bounds.width(), bounds.height());
        for (auto e : events) {
            switch (e.action) {
            case Action::Move: {
                moving = submitObservedPointer();
                break;
            }
            case Action::LeftClick:
                clickHoldUntil_ = t + .18;
                cursor_.freeze();
                input_->stopMotion();
                input_->click(false);
                preview_->trigger(e, display.hand.valid ? display.hand : r.hand);
                break;
            case Action::RightClick:
                input_->stopMotion();
                input_->click(true);
                preview_->trigger(e, display.hand.valid ? display.hand : r.hand);
                break;
            case Action::Down:
                input_->button(true);
                preview_->trigger(e, display.hand.valid ? display.hand : r.hand);
                break;
            case Action::Up:
                input_->button(false);
                preview_->trigger(e, display.hand.valid ? display.hand : r.hand);
                break;
            case Action::Scroll:
                input_->scroll(e.x, e.y);
                preview_->trigger(e, display.hand.valid ? display.hand : r.hand);
                break;
            case Action::Maximize:
                input_->maximize();
                preview_->trigger(e, display.hand.valid ? display.hand : r.hand);
                break;
            }
        }
    }
    if (!moving) {
        cursor_.hold();
        input_->stopMotion();
    }
    moveCursor(t);
    updatePresentation(guide, QString::fromStdString(mode),
                       std::max(engine_.progress(), dwellClick_.progress()), r, t);
}
void App::moveCursor(double t) {
    if (!calibrated_ || !input_->available() || paused_ || t < clickHoldUntil_)
        return;
    auto p = cursor_.step(t);
    if (!p)
        return;
    const QRect bounds = pointerBounds();
    if (!bounds.isEmpty()) {
        QPointF target(bounds.x() + p->x * (bounds.width() - 1),
                       bounds.y() + p->y * (bounds.height() - 1));
        // Unequal-height monitors leave holes in the bounding rectangle. Target
        // the nearest real screen pixel instead of feeding an unreachable point.
        QPointF nearest = target;
        double best = std::numeric_limits<double>::max();
        for (auto *screen : QGuiApplication::screens()) {
            const QRect rect = screen->geometry();
            if (!rect.intersects(bounds))
                continue;
            QPointF candidate(std::clamp(target.x(), double(rect.left()), double(rect.right())),
                              std::clamp(target.y(), double(rect.top()), double(rect.bottom())));
            const double distance =
                std::hypot(candidate.x() - target.x(), candidate.y() - target.y());
            if (distance < best) {
                best = distance;
                nearest = candidate;
            }
        }
        input_->move(nearest);
    }
}
QRect App::pointerBounds() const {
    QRect bounds;
    for (auto *s : QGuiApplication::screens()) {
        if (!targetScreen_.isEmpty() && s->name() == targetScreen_)
            return s->geometry();
        bounds = bounds.united(s->geometry());
    }
    return bounds; // A disconnected selected screen must not strand the pointer.
}
App::~App() {
    stop_ = true;
    frameReady_.notify_all();
    release();
    if (capture_.joinable())
        capture_.join();
    if (inference_.joinable())
        inference_.join();
    if (alignment_.joinable())
        alignment_.join();
    server_.close();
}
} // namespace holohand
