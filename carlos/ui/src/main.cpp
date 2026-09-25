#include <QAction>
#include <QApplication>
#include <QFileInfo>
#include <QIcon>
#include <QLocalServer>
#include <QLocalSocket>
#include <QMenu>
#include <QPainter>
#include <QPixmap>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickStyle>
#include <QQuickWindow>
#include <QSystemTrayIcon>
#include <QTimer>

#include "EvClient.h"

namespace {
QIcon stateIcon(const QString &state, bool privacy) {
    QColor accent(QStringLiteral("#39dcff"));
    if (privacy)
        accent = QColor(QStringLiteral("#71838d"));
    else if (state == QStringLiteral("LISTENING"))
        accent = QColor(QStringLiteral("#53efae"));
    else if (state == QStringLiteral("SPEAKING"))
        accent = QColor(QStringLiteral("#76a7ff"));
    else if (state == QStringLiteral("THINKING") || state == QStringLiteral("TRANSCRIBING") ||
             state == QStringLiteral("RETRIEVING_MEMORY"))
        accent = QColor(QStringLiteral("#ffca58"));
    else if (state == QStringLiteral("USING_TOOL"))
        accent = QColor(QStringLiteral("#a482ff"));
    else if (state == QStringLiteral("WAITING_FOR_CONFIRMATION"))
        accent = QColor(QStringLiteral("#ff9e58"));
    else if (state == QStringLiteral("ERROR") || state == QStringLiteral("OFFLINE"))
        accent = QColor(QStringLiteral("#ff6478"));
    QPixmap pixmap(64, 64);
    pixmap.fill(Qt::transparent);
    QPainter painter(&pixmap);
    painter.setRenderHint(QPainter::Antialiasing);
    painter.setPen(QPen(accent, 4));
    painter.setBrush(QColor(QStringLiteral("#07131c")));
    painter.drawEllipse(QRectF(6, 6, 52, 52));
    painter.setPen(accent);
    QFont font = painter.font();
    font.setBold(true);
    font.setPixelSize(20);
    painter.setFont(font);
    painter.drawText(pixmap.rect(), Qt::AlignCenter, QStringLiteral("EV"));
    return QIcon(pixmap);
}
} // namespace

int main(int argc, char *argv[]) {
    QApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("Carlos"));
    app.setApplicationDisplayName(QStringLiteral("Carlos Control Center"));
    app.setOrganizationName(QStringLiteral("Carlos"));
    app.setQuitOnLastWindowClosed(false);
    QQuickStyle::setStyle(QStringLiteral("Basic"));

    const QStringList applicationArguments = app.arguments();
    const bool backgroundMode = applicationArguments.contains(QStringLiteral("--background"));
    const bool screenshotMode = !qEnvironmentVariable("EV_SCREENSHOT_PATH").isEmpty();
    QLocalServer instanceServer;
    if (!screenshotMode) {
        const QString serverName =
            QStringLiteral("ev-ui-%1").arg(static_cast<qulonglong>(getuid()));
        if (!instanceServer.listen(serverName)) {
            QLocalSocket existing;
            existing.connectToServer(serverName);
            if (existing.waitForConnected(250)) {
                existing.write(backgroundMode ? "noop\n" : "show\n");
                existing.waitForBytesWritten(250);
                return 0;
            }
            QLocalServer::removeServer(serverName);
            if (!instanceServer.listen(serverName))
                return 4;
        }
    }

    EvClient client;
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty(QStringLiteral("evClient"), &client);
    engine.rootContext()->setContextProperty(QStringLiteral("backgroundMode"), backgroundMode);
    engine.rootContext()->setContextProperty(QStringLiteral("brainExploreMode"),
                                             screenshotMode &&
                                                 qEnvironmentVariableIsSet("EV_BRAIN_EXPLORE"));
    QObject::connect(&app, &QCoreApplication::aboutToQuit, &client, &EvClient::disconnectFromCore);
    QObject::connect(
        &engine, &QQmlApplicationEngine::objectCreationFailed, &app,
        [] { QCoreApplication::exit(1); }, Qt::QueuedConnection);
    engine.loadFromModule(QStringLiteral("EV.ControlCenter"), QStringLiteral("Main"));
    if (engine.rootObjects().isEmpty())
        return 2;
    auto *window = qobject_cast<QQuickWindow *>(engine.rootObjects().constFirst());
    if (!window)
        return 2;

    QObject::connect(
        &instanceServer, &QLocalServer::newConnection, &app, [&instanceServer, window] {
            while (QLocalSocket *socket = instanceServer.nextPendingConnection()) {
                QObject::connect(socket, &QLocalSocket::readyRead, socket, [socket, window] {
                    const QByteArray command = socket->readAll().trimmed();
                    if (command == "show") {
                        window->show();
                        window->raise();
                        window->requestActivate();
                    }
                    socket->disconnectFromServer();
                });
            }
        });

    QSystemTrayIcon tray;
    QMenu trayMenu;
    QAction *openAction = trayMenu.addAction(QStringLiteral("Open Carlos"));
    QAction *pushToTalkAction = trayMenu.addAction(QStringLiteral("Push to Talk"));
    QAction *stopSpeakingAction = trayMenu.addAction(QStringLiteral("Stop Speaking"));
    trayMenu.addSeparator();
    QAction *privacyAction = trayMenu.addAction(QStringLiteral("Privacy Mode"));
    privacyAction->setCheckable(true);
    QAction *pauseWakeAction = trayMenu.addAction(QStringLiteral("Pause Wake Detection"));
    pauseWakeAction->setCheckable(true);
    QAction *settingsAction = trayMenu.addAction(QStringLiteral("Settings"));
    trayMenu.addSeparator();
    QAction *quitAction = trayMenu.addAction(QStringLiteral("Quit Carlos"));
    tray.setContextMenu(&trayMenu);
    tray.setToolTip(QStringLiteral("Carlos // connecting"));
    tray.setIcon(stateIcon(QStringLiteral("OFFLINE"), false));
    if (QSystemTrayIcon::isSystemTrayAvailable())
        tray.show();

    auto openWindow = [window] {
        window->show();
        window->raise();
        window->requestActivate();
    };
    QObject::connect(openAction, &QAction::triggered, &app, openWindow);
    QObject::connect(settingsAction, &QAction::triggered, &app, [window, openWindow] {
        window->setProperty("testPage", 9);
        openWindow();
    });
    QObject::connect(pushToTalkAction, &QAction::triggered, &client, &EvClient::startListening);
    QObject::connect(stopSpeakingAction, &QAction::triggered, &client, &EvClient::stopSpeaking);
    QObject::connect(privacyAction, &QAction::toggled, &client, &EvClient::setPrivacyMode);
    QObject::connect(pauseWakeAction, &QAction::toggled, &client, &EvClient::setWakePaused);
    QObject::connect(quitAction, &QAction::triggered, &app, [&app, &client] {
        client.stopCore();
        QTimer::singleShot(180, &app, &QCoreApplication::quit);
    });
    QObject::connect(&tray, &QSystemTrayIcon::activated, &app,
                     [window, openWindow](QSystemTrayIcon::ActivationReason reason) {
                         if (reason != QSystemTrayIcon::Trigger)
                             return;
                         if (window->isVisible())
                             window->hide();
                         else
                             openWindow();
                     });
    auto updateTray = [&client, &tray, privacyAction, pauseWakeAction] {
        const QVariantMap voice = client.voice();
        const bool privacy = voice.value(QStringLiteral("privacy_mode")).toBool();
        const bool paused = voice.value(QStringLiteral("wake_paused")).toBool();
        privacyAction->blockSignals(true);
        pauseWakeAction->blockSignals(true);
        privacyAction->setChecked(privacy);
        pauseWakeAction->setChecked(paused);
        privacyAction->blockSignals(false);
        pauseWakeAction->blockSignals(false);
        tray.setIcon(stateIcon(client.state(), privacy));
        tray.setToolTip(QStringLiteral("Carlos // %1")
                            .arg(privacy ? QStringLiteral("PRIVATE") : client.state()));
    };
    QObject::connect(&client, &EvClient::stateChanged, &app, updateTray);
    QObject::connect(&client, &EvClient::snapshotChanged, &app, updateTray);
    client.connectToCore();
    const QString screenshotPath = qEnvironmentVariable("EV_SCREENSHOT_PATH");
    if (!screenshotPath.isEmpty()) {
        const int testWidth = qEnvironmentVariableIntValue("EV_TEST_WIDTH");
        const int testHeight = qEnvironmentVariableIntValue("EV_TEST_HEIGHT");
        const int testPage = qEnvironmentVariableIntValue("EV_TEST_PAGE");
        if (testWidth > 0)
            window->setWidth(testWidth);
        if (testHeight > 0)
            window->setHeight(testHeight);
        if (testPage >= 0)
            window->setProperty("testPage", testPage);
        if (qEnvironmentVariableIsSet("EV_TEST_SCENE_PICKER"))
            window->setProperty("testScenePicker", true);
        QTimer::singleShot(1700, &app, [&app, &client, window, screenshotPath] {
            const bool saved = window->grabWindow().save(screenshotPath);
            client.disconnectFromCore();
            window->close();
            QTimer::singleShot(600, &app, [&app, saved] { app.exit(saved ? 0 : 3); });
        });
    }
    return app.exec();
}
