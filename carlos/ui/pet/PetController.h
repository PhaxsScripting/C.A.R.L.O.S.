#pragma once
#include "PetPolicy.h"
#include <QDBusContext>
#include <QElapsedTimer>
#include <QLocalSocket>
#include <QPoint>
#include <QSettings>
#include <QTimer>
#include <QWindow>

class PetController : public QObject, protected QDBusContext {
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface", "org.phax.CarlosPet")
    Q_PROPERTY(QString bubble READ bubble NOTIFY changed)
    Q_PROPERTY(QString mood READ mood NOTIFY changed)
    Q_PROPERTY(bool shown READ shown NOTIFY changed)
    Q_PROPERTY(bool still READ still NOTIFY changed)
    Q_PROPERTY(bool quiet READ quiet NOTIFY changed)
    Q_PROPERTY(bool observing READ observing NOTIFY changed)
  public:
    explicit PetController(bool preview = false, QObject *parent = nullptr);
    ~PetController() override;
    void attach(QWindow *window);
    QString bubble() const { return m_bubble; }
    QString mood() const { return m_mood; }
    bool shown() const;
    bool still() const { return m_settings.value("still", false).toBool(); }
    bool quiet() const { return m_settings.value("quiet", false).toBool(); }
    bool observing() const { return m_tracking; }
    Q_INVOKABLE void pet();
    Q_INVOKABLE void menu();
    Q_INVOKABLE void beginDrag();
    Q_INVOKABLE void drag();
    Q_INVOKABLE void endDrag();
    void stop();
  public slots:
    Q_SCRIPTABLE void Observe(const QString &app, bool fullscreen);
    Q_SCRIPTABLE void Show();
    Q_SCRIPTABLE void Quit();
    void SetLocked(bool locked);
  signals:
    void changed();

  private:
    void tick();
    void position();
    void startTracking();
    void pollCore();
    void say(const QString &line);
    void clearContext();
    QSettings m_settings;
    QElapsedTimer m_clock;
    PetPolicy m_policy;
    QTimer m_timer, m_coreTimer, m_coreTimeout;
    QLocalSocket m_core;
    QByteArray m_buffer;
    QWindow *m_window = nullptr;
    QString m_bubble, m_mood = "happy", m_kwinOwner;
    bool m_preview, m_locked = true, m_fullscreen = false, m_hidden = false;
    bool m_tracking = false, m_loaded = false, m_corePrivate = false, m_dragging = false;
    qint64 m_bubbleUntil = 0, m_snoozeUntil = 0, m_lastPet = -1000;
    QPoint m_dragStart;
    int m_right = 24, m_bottom = 64, m_dragRight = 0, m_dragBottom = 0;
};
