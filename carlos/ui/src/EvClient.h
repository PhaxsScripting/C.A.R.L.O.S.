#pragma once
#include "BigBootyBudget.h"

#include <QElapsedTimer>
#include <QJsonObject>
#include <QLocalSocket>
#include <QObject>
#include <QProcess>
#include <QTimer>
#include <QVariantList>
#include <QVariantMap>

class EvClient final : public QObject {
    Q_OBJECT
    Q_PROPERTY(bool connected READ connected NOTIFY connectedChanged)
    Q_PROPERTY(QString state READ state NOTIFY stateChanged)
    Q_PROPERTY(QString detail READ detail NOTIFY stateChanged)
    Q_PROPERTY(QString statusMessage READ statusMessage NOTIFY statusMessageChanged)
    Q_PROPERTY(QVariantMap telemetry READ telemetry NOTIFY telemetryChanged)
    Q_PROPERTY(QVariantMap provider READ provider NOTIFY snapshotChanged)
    Q_PROPERTY(QVariantMap voice READ voice NOTIFY voiceChanged)
    Q_PROPERTY(QVariantMap cognition READ cognition NOTIFY cognitionChanged)
    Q_PROPERTY(QVariantMap confirmation READ confirmation NOTIFY confirmationChanged)
    Q_PROPERTY(QVariantList events READ events NOTIFY eventsChanged)
    Q_PROPERTY(QVariantList timeline READ timeline NOTIFY timelineChanged)
    Q_PROPERTY(QVariantList memories READ memories NOTIFY memoriesChanged)
    Q_PROPERTY(QVariantList tools READ tools NOTIFY toolsChanged)
    Q_PROPERTY(QVariantList plans READ plans NOTIFY phase3Changed)
    Q_PROPERTY(QVariantMap activePlan READ activePlan NOTIFY phase3Changed)
    Q_PROPERTY(QVariantMap activity READ activity NOTIFY activityChanged)
    Q_PROPERTY(QVariantList insights READ insights NOTIFY insightsChanged)
    Q_PROPERTY(QVariantMap security READ security NOTIFY phase3Changed)
    Q_PROPERTY(QVariantMap latency READ latency NOTIFY phase3Changed)
    Q_PROPERTY(QVariantMap diagnostics READ diagnostics NOTIFY phase3Changed)
    Q_PROPERTY(QVariantMap personality READ personality NOTIFY phase3Changed)
    Q_PROPERTY(QVariantMap daily READ daily NOTIFY dailyChanged)
    Q_PROPERTY(QVariantMap toolResult READ toolResult NOTIFY toolResultChanged)
    Q_PROPERTY(QVariantList inputWaveform READ inputWaveform NOTIFY inputWaveformChanged)
    Q_PROPERTY(QVariantList outputWaveform READ outputWaveform NOTIFY outputWaveformChanged)
    Q_PROPERTY(QStringList activeNodes READ activeNodes NOTIFY activeNodesChanged)

  public:
    explicit EvClient(QObject *parent = nullptr);

    bool connected() const;
    QString state() const { return m_state; }
    QString detail() const { return m_detail; }
    QString statusMessage() const { return m_statusMessage; }
    QVariantMap telemetry() const { return m_telemetry; }
    QVariantMap provider() const { return m_provider; }
    QVariantMap voice() const { return m_voice; }
    QVariantMap cognition() const { return m_cognition; }
    QVariantMap confirmation() const { return m_confirmation; }
    QVariantList events() const { return m_events; }
    QVariantList timeline() const { return m_timeline; }
    QVariantList memories() const { return m_memories; }
    QVariantList tools() const { return m_tools; }
    QVariantList plans() const { return m_plans; }
    QVariantMap activePlan() const { return m_activePlan; }
    QVariantMap activity() const { return m_activity; }
    QVariantList insights() const { return m_insights; }
    Q_INVOKABLE void steerTask(const QString &taskId, const QString &text);
    QVariantMap security() const { return m_security; }
    QVariantMap latency() const { return m_latency; }
    QVariantMap diagnostics() const { return m_diagnostics; }
    QVariantMap personality() const { return m_personality; }
    QVariantMap daily() const { return m_daily; }
    QVariantMap toolResult() const { return m_toolResult; }
    Q_INVOKABLE void refreshDaily();
    Q_INVOKABLE void callTool(const QString &name, const QVariantMap &arguments);
    QVariantList inputWaveform() const { return m_inputWaveform; }
    QVariantList outputWaveform() const { return m_outputWaveform; }
    QStringList activeNodes() const { return m_activeNodes; }

    Q_INVOKABLE void connectToCore();
    Q_INVOKABLE void retryCore();
    Q_INVOKABLE void rememberFailureCard(const QString &proposalId);
    void disconnectFromCore();
    Q_INVOKABLE void sendCommand(const QString &text);
    Q_INVOKABLE void startListening();
    Q_INVOKABLE void stopListening();
    Q_INVOKABLE void testMicrophone();
    Q_INVOKABLE void testTranscription();
    Q_INVOKABLE void testWakeWord();
    Q_INVOKABLE void testVoice();
    Q_INVOKABLE void testFullVoicePipeline();
    Q_INVOKABLE void setPrivacyMode(bool enabled);
    Q_INVOKABLE void setPrivacyProfile(const QString &mode);
    Q_INVOKABLE void setWakePaused(bool paused);
    Q_INVOKABLE void stopSpeaking();
    Q_INVOKABLE void refreshConfirmations();
    Q_INVOKABLE void stopCore();
    Q_INVOKABLE void speak(const QString &text);
    Q_INVOKABLE void respondToConfirmation(bool approved);
    Q_INVOKABLE void refreshSnapshot();
    Q_INVOKABLE void refreshMemories();
    Q_INVOKABLE void refreshTools();
    Q_INVOKABLE void refreshPhase3();
    Q_INVOKABLE void updatePersonality(const QString &key, const QVariant &value);
    Q_INVOKABLE void cancelActivePlan();
    Q_INVOKABLE void remember(const QString &text);
    Q_INVOKABLE void forget(const QString &id);

  signals:
    void sceneActivated(const QString &hud);
    void activityChanged();
    void insightsChanged();
    void dailyChanged();
    void toolResultChanged();
    void connectedChanged();
    void stateChanged();
    void statusMessageChanged();
    void telemetryChanged();
    void snapshotChanged();
    void voiceChanged();
    void cognitionChanged();
    void confirmationChanged();
    void eventsChanged();
    void timelineChanged();
    void memoriesChanged();
    void toolsChanged();
    void inputWaveformChanged();
    void outputWaveformChanged();
    void activeNodesChanged();
    void phase3Changed();

  private:
    QString socketPath() const;
    QString sendRequest(const QString &type, const QJsonObject &payload = {});
    void processLine(const QByteArray &line);
    void processResponse(const QJsonObject &message);
    void processEvent(const QJsonObject &event, bool historical = false);
    void applySnapshot(const QJsonObject &snapshot);
    void setStatus(const QString &message);
    void activateCore();
    void appendTimeline(const QString &kind, const QString &title, const QString &body,
                        const QString &timestamp = {});
    void setActivityForEvent(const QString &type, const QString &source,
                             const QJsonObject &payload);
    static QVariantList jsonArrayToList(const QJsonValue &value);

    QLocalSocket m_socket;
    QTimer m_reconnectTimer;
    QTimer m_activityTimer;
    QTimer m_voiceRefreshTimer;
    QTimer m_eventRefreshTimer;
    QByteArray m_readBuffer;
    quint64 m_requestCounter = 0;
    QHash<QString, QString> m_pendingRequests;
    bool m_connecting = false;
    bool m_reconnectEnabled = true;
    QProcess m_activationProcess;
    QElapsedTimer m_activationCooldown;
    QElapsedTimer m_recoveryClock;
    BigBootyBudget m_recoveryBudget;
    QTimer m_activationTimeout;
    QString m_state = QStringLiteral("OFFLINE");
    QString m_detail = QStringLiteral("Waiting for E.V. core");
    QString m_statusMessage = QStringLiteral("Core disconnected");
    QVariantMap m_telemetry;
    QVariantMap m_provider;
    QVariantMap m_voice;
    QVariantMap m_cognition;
    QVariantMap m_confirmation;
    QVariantList m_events;
    QVariantList m_timeline;
    QVariantList m_memories;
    QVariantList m_tools;
    QVariantList m_plans;
    QVariantMap m_activePlan;
    QVariantMap m_activity;
    QVariantList m_insights;
    QVariantMap m_security;
    QVariantMap m_latency;
    QVariantMap m_diagnostics;
    QVariantMap m_personality;
    QVariantMap m_daily;
    QVariantMap m_toolResult;
    QVariantList m_inputWaveform;
    QVariantList m_outputWaveform;
    QStringList m_activeNodes;
};
