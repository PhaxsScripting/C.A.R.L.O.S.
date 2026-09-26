#include "PetController.h"
#include <QApplication>
#include <QDBusConnection>
#include <QDBusInterface>
#include <QMenu>
#include <QPainter>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickWindow>
#include <QSystemTrayIcon>
#include <csignal>

namespace {
volatile std::sig_atomic_t quitting = 0;
void finish(int) { quitting = 1; }
} // namespace

int main(int argc, char **argv) {
    QApplication app(argc, argv);
    app.setOrganizationName("Carlos");
    app.setApplicationName("CarlosPet");
    app.setApplicationDisplayName("Carlos Pet");
    app.setQuitOnLastWindowClosed(false);
    const bool preview = app.arguments().contains("--preview");
    auto bus = QDBusConnection::sessionBus();
    if (app.arguments().contains("--quit")) {
        QDBusInterface existing("org.phax.CarlosPet", "/Pet", "org.phax.CarlosPet", bus);
        if (existing.isValid())
            existing.call("Quit");
        return 0;
    }
    if (!preview && !bus.registerService("org.phax.CarlosPet")) {
        QDBusInterface existing("org.phax.CarlosPet", "/Pet", "org.phax.CarlosPet", bus);
        return existing.call("Show").type() == QDBusMessage::ErrorMessage ? 1 : 0;
    }
    PetController pet(preview);
    QQmlApplicationEngine engine;
    engine.rootContext()->setContextProperty("pet", &pet);
    engine.loadFromModule("Carlos.Pet", "Pet");
    if (engine.rootObjects().isEmpty())
        return 2;
    auto *window = qobject_cast<QQuickWindow *>(engine.rootObjects().first());
    if (!window)
        return 2;
    pet.attach(window);
    QPixmap icon(48, 48);
    icon.fill(Qt::transparent);
    QPainter paint(&icon);
    paint.setRenderHint(QPainter::Antialiasing);
    paint.setPen(QPen(QColor("#463b54"), 3));
    paint.setBrush(QColor("#c9b8df"));
    paint.drawRoundedRect(5, 8, 38, 33, 9, 9);
    paint.setPen(Qt::NoPen);
    paint.setBrush(QColor("#302d3e"));
    paint.drawRoundedRect(11, 17, 26, 15, 4, 4);
    paint.setBrush(QColor("#a6f3d3"));
    paint.drawEllipse(16, 21, 4, 6);
    paint.drawEllipse(28, 21, 4, 6);
    paint.end();
    QSystemTrayIcon tray{QIcon(icon)};
    QMenu menu;
    menu.addAction("Show Carlos Pet", &pet, &PetController::Show);
    menu.addAction("Pet settings", &pet, &PetController::menu);
    menu.addSeparator();
    menu.addAction("Quit pet", &app, &QCoreApplication::quit);
    tray.setContextMenu(&menu);
    tray.setToolTip("Carlos Pet: right-click for settings");
    QObject::connect(&tray, &QSystemTrayIcon::activated, &pet, [&pet](auto reason) {
        if (reason == QSystemTrayIcon::Trigger)
            pet.Show();
    });
    if (!preview)
        tray.show();
    QObject::connect(&app, &QCoreApplication::aboutToQuit, &pet, &PetController::stop);
    std::signal(SIGTERM, finish);
    std::signal(SIGINT, finish);
    QTimer shutdown;
    QObject::connect(&shutdown, &QTimer::timeout, &app, [&app] {
        if (quitting)
            app.quit();
    });
    shutdown.start(1000);
    if (preview) {
        QTimer::singleShot(1000, &app, [&app, window] {
            const auto path = qEnvironmentVariable("CARLOS_PET_PREVIEW");
            if (!path.isEmpty())
                app.exit(window->grabWindow().save(path) ? 0 : 3);
        });
    }
    return app.exec();
}
