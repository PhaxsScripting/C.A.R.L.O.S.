#include "ui/App.h"
#include <QApplication>
#include <QDir>
#include <QLocalSocket>
#include <QLockFile>
#include <QProcess>
#include <QTextStream>
#include <sys/stat.h>
#include <unistd.h>
#ifdef __FreeBSD__
namespace holohand {
int inputGuardian();
}
#endif
int main(int argc, char **argv) {
#ifdef __FreeBSD__
    if (argc == 2 && std::string(argv[1]) == "--input-guardian")
        return holohand::inputGuardian();
#endif
    QApplication app(argc, argv);
    app.setApplicationName("holohand");
    app.setOrganizationName("Phax");
    app.setQuitOnLastWindowClosed(false);
    auto args = app.arguments();
    QString cmd = args.size() > 1 ? args[1] : "--status";
    if (cmd == "--benchmark") {
        QProcess p;
        p.setProcessChannelMode(QProcess::ForwardedChannels);
        QString models = qEnvironmentVariable("HOLOHAND_MODELS");
        if (models.isEmpty())
            models = QCoreApplication::applicationDirPath() + "/../share/holohand/models";
        p.start(QCoreApplication::applicationDirPath() + "/holohand-benchmark", {models});
        if (!p.waitForStarted(3000))
            return 3;
        if (!p.waitForFinished(60000)) {
            p.kill();
            p.waitForFinished(3000);
            return 4;
        }
        if (p.exitStatus() != QProcess::NormalExit)
            return 5;
        return p.exitCode();
    }
    QString runtime = qEnvironmentVariable("XDG_RUNTIME_DIR");
    if (runtime.isEmpty())
        runtime = QString("/tmp/holohand-%1").arg(getuid());
    QLocalSocket existing;
    existing.connectToServer(runtime + "/holohand.sock");
    if (existing.waitForConnected(250)) {
        existing.write(cmd.toUtf8());
        existing.waitForBytesWritten(1000);
        existing.waitForReadyRead(1500);
        QTextStream(stdout) << existing.readAll();
        return 0;
    }
    if (args.size() > 1 && cmd != "--calibrate" && cmd != "--background") {
        QTextStream(stdout) << "HoloHand is not running\n";
        return 2;
    }
    if (!QDir(runtime).exists()) {
        if (::mkdir(runtime.toLocal8Bit(), 0700) != 0)
            return 4;
    }
    struct stat rs{};
    if (lstat(runtime.toLocal8Bit(), &rs) || !S_ISDIR(rs.st_mode) || rs.st_uid != getuid() ||
        (rs.st_mode & 0077))
        return 4;
    QLockFile lock(runtime + "/holohand.lock");
    if (!lock.tryLock(0))
        return 4;
    try {
        holohand::App window;
        if (cmd == "--calibrate")
            window.command(cmd);
        return app.exec();
    } catch (const std::exception &e) {
        QTextStream(stderr) << e.what() << '\n';
        return 3;
    }
}
