#include "EvClient.h"

#include <QCoreApplication>
#include <QDir>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonValue>
#include <QUuid>

#include <unistd.h>

namespace {
QString valueString(const QJsonObject &object, const char *key) {
    return object.value(QLatin1String(key)).toString();
}

QVariantMap objectMap(const QJsonValue &value) {
    return value.isObject() ? value.toObject().toVariantMap() : QVariantMap{};
}
} // namespace

EvClient::EvClient(QObject *parent) : QObject(parent) {
    m_recoveryClock.start();
    m_activationTimeout.setSingleShot(true);
    m_activationTimeout.setInterval(10000);
    connect(&m_activationTimeout, &QTimer::timeout, this, [this] {
        if (m_activationProcess.state() != QProcess::NotRunning)
            m_activationProcess.kill();
        if (!connected())
            setStatus(QStringLiteral("Core activation timed out; retrying automatically"));
    });
    connect(&m_activationProcess, &QProcess::finished, this,
            [this](int code, QProcess::ExitStatus) {
                m_activationTimeout.stop();
                if (!m_reconnectEnabled || connected())
                    return;
                if (code != 0)
                    setStatus(QStringLiteral("Could not start Carlos core: %1")
                                  .arg(QString::fromUtf8(m_activationProcess.readAllStandardError())
                                           .trimmed()
                                           .left(240)));
                m_reconnectTimer.start();
            });
    connect(&m_activationProcess, &QProcess::errorOccurred, this, [this](QProcess::ProcessError) {
        m_activationTimeout.stop();
        if (m_reconnectEnabled && !connected()) {
            setStatus(QStringLiteral("Core activation failed: %1")
                          .arg(m_activationProcess.errorString()));
            m_reconnectTimer.start();
        }
    });
    m_reconnectTimer.setInterval(1500);
    m_activityTimer.setSingleShot(true);
    m_activityTimer.setInterval(1150);
    m_voiceRefreshTimer.setSingleShot(true);
    m_voiceRefreshTimer.setInterval(180);
    m_eventRefreshTimer.setSingleShot(true);
    m_eventRefreshTimer.setInterval(80);
    connect(&m_eventRefreshTimer, &QTimer::timeout, this, &EvClient::eventsChanged);

    connect(&m_socket, &QLocalSocket::connected, this, [this] {
        m_connecting = false;
        m_reconnectTimer.stop();
        m_events.clear();
        emit eventsChanged();
        emit connectedChanged();
        setStatus(QStringLiteral("Secure local core connected"));
        refreshSnapshot();
        sendRequest(QStringLiteral("conversation.list"), {{QStringLiteral("limit"), 100}});
        // Subscribe before fetching history so no events slip through.
        // processEvent drops duplicates from the overlap.
        sendRequest(QStringLiteral("subscribe"));
        sendRequest(QStringLiteral("events.history"), {{QStringLiteral("limit"), 120}});
        refreshMemories();
        refreshTools();
        refreshPhase3();
    });
    connect(&m_socket, &QLocalSocket::disconnected, this, [this] {
        m_connecting = false;
        m_state = QStringLiteral("OFFLINE");
        m_detail = QStringLiteral("Core disconnected; reconnecting");
        m_pendingRequests.clear();
        m_readBuffer.clear();
        m_voiceRefreshTimer.stop();
        m_eventRefreshTimer.stop();
        m_activityTimer.stop();
        if (!m_activeNodes.isEmpty()) {
            m_activeNodes.clear();
            emit activeNodesChanged();
        }
        if (!m_inputWaveform.isEmpty()) {
            m_inputWaveform.clear();
            emit inputWaveformChanged();
        }
        if (!m_outputWaveform.isEmpty()) {
            m_outputWaveform.clear();
            emit outputWaveformChanged();
        }
        emit connectedChanged();
        emit stateChanged();
        setStatus(QStringLiteral("Core unavailable — reconnecting"));
        if (m_reconnectEnabled)
            m_reconnectTimer.start();
    });
    connect(&m_socket, &QLocalSocket::readyRead, this, [this] {
        m_readBuffer += m_socket.readAll();
        qsizetype newline = -1;
        while ((newline = m_readBuffer.indexOf('\n')) >= 0) {
            const QByteArray line = m_readBuffer.left(newline);
            m_readBuffer.remove(0, newline + 1);
            if (!line.trimmed().isEmpty())
                processLine(line);
        }
    });
    connect(&m_socket, &QLocalSocket::errorOccurred, this,
            [this](QLocalSocket::LocalSocketError error) {
                m_connecting = false;
                if (m_reconnectEnabled && (error == QLocalSocket::ServerNotFoundError ||
                                           error == QLocalSocket::ConnectionRefusedError))
                    activateCore();
                if (error != QLocalSocket::ServerNotFoundError &&
                    error != QLocalSocket::ConnectionRefusedError)
                    setStatus(QStringLiteral("IPC error: %1").arg(m_socket.errorString()));
                if (!connected() && m_reconnectEnabled)
                    m_reconnectTimer.start();
            });
    connect(&m_reconnectTimer, &QTimer::timeout, this, &EvClient::connectToCore);
    connect(&m_voiceRefreshTimer, &QTimer::timeout, this, &EvClient::refreshSnapshot);
    connect(&m_activityTimer, &QTimer::timeout, this, [this] {
        if (!m_activeNodes.isEmpty()) {
            m_activeNodes.clear();
            emit activeNodesChanged();
        }
    });
}

void EvClient::activateCore() {
    // Test sockets shouldn't wake the real desktop core.
    const QString runtime = QString::fromLocal8Bit(qgetenv("XDG_RUNTIME_DIR"));
    if (runtime != QStringLiteral("/run/user/%1").arg(static_cast<qulonglong>(getuid())))
        return;
    if (!m_reconnectEnabled || m_activationProcess.state() != QProcess::NotRunning ||
        (m_activationCooldown.isValid() && m_activationCooldown.elapsed() < 15000))
        return;
    if (!m_recoveryBudget.take(m_recoveryClock.elapsed())) {
        setStatus(QStringLiteral(
            "Core recovery paused after three attempts. Use Settings → Reconnect to retry."));
        return;
    }
    m_activationCooldown.start();
    setStatus(QStringLiteral("Starting Carlos core; reconnecting automatically"));
    m_activationProcess.start(QStringLiteral("/usr/bin/gdbus"),
                              {QStringLiteral("call"), QStringLiteral("--session"),
                               QStringLiteral("--dest"), QStringLiteral("org.freedesktop.DBus"),
                               QStringLiteral("--object-path"),
                               QStringLiteral("/org/freedesktop/DBus"), QStringLiteral("--method"),
                               QStringLiteral("org.freedesktop.DBus.StartServiceByName"),
                               QStringLiteral("com.ev.Core"), QStringLiteral("0")});
    m_activationTimeout.start();
}

bool EvClient::connected() const { return m_socket.state() == QLocalSocket::ConnectedState; }

QString EvClient::socketPath() const {
    const QByteArray runtime = qgetenv("XDG_RUNTIME_DIR");
    const QString root =
        runtime.isEmpty()
            ? QStringLiteral("/tmp/ev-runtime-%1").arg(static_cast<qulonglong>(getuid()))
            : QString::fromLocal8Bit(runtime);
    return QDir(root).filePath(QStringLiteral("ev/ev.sock"));
}

void EvClient::rememberFailureCard(const QString &proposalId) {
    sendRequest(QStringLiteral("hud.reference.set"), {{QStringLiteral("proposal_id"), proposalId}});
}

void EvClient::retryCore() {
    m_recoveryBudget.reset();
    m_activationCooldown.invalidate();
    connectToCore();
}

void EvClient::connectToCore() {
    if (connected() || m_connecting)
        return;
    m_reconnectEnabled = true;
    m_connecting = true;
    m_socket.abort();
    m_socket.connectToServer(socketPath(), QIODevice::ReadWrite);
}

void EvClient::disconnectFromCore() {
    m_reconnectEnabled = false;
    m_activationTimeout.stop();
    if (m_activationProcess.state() != QProcess::NotRunning)
        m_activationProcess.kill();
    m_reconnectTimer.stop();
    m_voiceRefreshTimer.stop();
    m_eventRefreshTimer.stop();
    m_activityTimer.stop();
    if (!m_activeNodes.isEmpty()) {
        m_activeNodes.clear();
        emit activeNodesChanged();
    }
    if (!m_inputWaveform.isEmpty()) {
        m_inputWaveform.clear();
        emit inputWaveformChanged();
    }
    if (!m_outputWaveform.isEmpty()) {
        m_outputWaveform.clear();
        emit outputWaveformChanged();
    }
    m_pendingRequests.clear();
    m_socket.abort();
}

QString EvClient::sendRequest(const QString &type, const QJsonObject &payload) {
    if (!connected()) {
        setStatus(QStringLiteral("Cannot send request: core is offline"));
        return {};
    }
    // One snapshot at a time. Speech and tool updates can pile up fast.
    if (type == QStringLiteral("snapshot") || type == QStringLiteral("plan.list")) {
        for (auto pending = m_pendingRequests.cbegin(); pending != m_pendingRequests.cend();
             ++pending) {
            if (pending.value() == type)
                return pending.key();
        }
    }
    const QString id = QStringLiteral("ui-%1-%2")
                           .arg(++m_requestCounter)
                           .arg(QUuid::createUuid().toString(QUuid::Id128));
    const QJsonObject message{{QStringLiteral("type"), type},
                              {QStringLiteral("id"), id},
                              {QStringLiteral("payload"), payload}};
    m_pendingRequests.insert(id, type);
    m_socket.write(QJsonDocument(message).toJson(QJsonDocument::Compact) + '\n');
    m_socket.flush();
    return id;
}

void EvClient::sendCommand(const QString &text) {
    const QString clean = text.trimmed();
    if (clean.isEmpty())
        return;
    appendTimeline(QStringLiteral("USER"), QStringLiteral("ME"), clean);
    sendRequest(QStringLiteral("command.submit"), {{QStringLiteral("text"), clean}});
    setStatus(QStringLiteral("Message sent to Carlos"));
}

void EvClient::startListening() { sendRequest(QStringLiteral("voice.capture.start")); }
void EvClient::stopListening() { sendRequest(QStringLiteral("voice.capture.stop")); }
void EvClient::testMicrophone() { sendRequest(QStringLiteral("voice.microphone_test.start")); }
void EvClient::testTranscription() {
    sendRequest(QStringLiteral("voice.transcription_test.start"));
}
void EvClient::testWakeWord() {
    sendRequest(QStringLiteral("wake.test.start"), {{QStringLiteral("timeout_seconds"), 30}});
}
void EvClient::testVoice() {
    sendRequest(QStringLiteral("tts.speak"),
                {{QStringLiteral("text"), QStringLiteral("Systems ready. How can I help?")}});
}
void EvClient::testFullVoicePipeline() { sendRequest(QStringLiteral("voice.full_test.start")); }
void EvClient::setPrivacyMode(bool enabled) {
    sendRequest(QStringLiteral("voice.privacy.set"), {{QStringLiteral("enabled"), enabled}});
}
void EvClient::setPrivacyProfile(const QString &mode) {
    sendRequest(QStringLiteral("carlos.privacy.set"), {{QStringLiteral("mode"), mode}});
}
void EvClient::setWakePaused(bool paused) {
    sendRequest(QStringLiteral("wake.pause.set"), {{QStringLiteral("paused"), paused}});
}
void EvClient::stopSpeaking() { sendRequest(QStringLiteral("tts.stop")); }
void EvClient::refreshConfirmations() { sendRequest(QStringLiteral("confirmation.list")); }
void EvClient::stopCore() {
    // If the user hit Stop, dont bring Carlos right back.
    m_reconnectEnabled = false;
    m_reconnectTimer.stop();
    m_activationTimeout.stop();
    sendRequest(QStringLiteral("core.stop"));
}
void EvClient::speak(const QString &text) {
    sendRequest(QStringLiteral("tts.speak"), {{QStringLiteral("text"), text}});
}
void EvClient::refreshSnapshot() { sendRequest(QStringLiteral("snapshot")); }
void EvClient::refreshMemories() {
    sendRequest(QStringLiteral("memory.list"), {{QStringLiteral("limit"), 100}});
}
void EvClient::refreshTools() { sendRequest(QStringLiteral("tool.catalog")); }
void EvClient::refreshDaily() { sendRequest(QStringLiteral("daily.snapshot")); }
void EvClient::callTool(const QString &name, const QVariantMap &arguments) {
    sendRequest(QStringLiteral("tool.call"),
                {{QStringLiteral("name"), name},
                 {QStringLiteral("arguments"), QJsonObject::fromVariantMap(arguments)}});
}
void EvClient::refreshPhase3() {
    sendRequest(QStringLiteral("plan.list"));
    sendRequest(QStringLiteral("security.snapshot"));
    sendRequest(QStringLiteral("latency.report"), {{QStringLiteral("limit"), 12}});
    sendRequest(QStringLiteral("self.diagnostics"));
}
void EvClient::updatePersonality(const QString &key, const QVariant &value) {
    if (!key.isEmpty())
        sendRequest(QStringLiteral("personality.update"), {{key, QJsonValue::fromVariant(value)}});
}
void EvClient::cancelActivePlan() {
    sendRequest(QStringLiteral("plan.cancel"),
                {{QStringLiteral("reason"), QStringLiteral("control_center")}});
}

void EvClient::remember(const QString &text) {
    const QString clean = text.trimmed();
    if (!clean.isEmpty())
        sendRequest(QStringLiteral("memory.remember"),
                    {{QStringLiteral("content"), clean}, {QStringLiteral("tags"), QJsonArray{}}});
}

void EvClient::forget(const QString &id) {
    if (!id.isEmpty())
        sendRequest(QStringLiteral("memory.forget"), {{QStringLiteral("id"), id}});
}

void EvClient::respondToConfirmation(bool approved) {
    const QString id = m_confirmation.value(QStringLiteral("id")).toString();
    const QString token = m_confirmation.value(QStringLiteral("approval_token")).toString();
    if (id.isEmpty() || token.isEmpty())
        return;
    sendRequest(QStringLiteral("confirmation.respond"), {{QStringLiteral("id"), id},
                                                         {QStringLiteral("approval_token"), token},
                                                         {QStringLiteral("approved"), approved}});
    m_confirmation.clear();
    emit confirmationChanged();
}

void EvClient::processLine(const QByteArray &line) {
    QJsonParseError error;
    const QJsonDocument document = QJsonDocument::fromJson(line, &error);
    if (error.error != QJsonParseError::NoError || !document.isObject()) {
        setStatus(QStringLiteral("Rejected malformed core message"));
        return;
    }
    const QJsonObject message = document.object();
    const QString type = valueString(message, "type");
    if (type == QStringLiteral("event"))
        processEvent(message.value(QStringLiteral("payload")).toObject());
    else if (type == QStringLiteral("response"))
        processResponse(message);
    else if (type == QStringLiteral("error")) {
        m_pendingRequests.remove(valueString(message, "id"));
        setStatus(QStringLiteral("Core error: %1")
                      .arg(message.value(QStringLiteral("payload"))
                               .toObject()
                               .value(QStringLiteral("message"))
                               .toString(QStringLiteral("request failed"))));
    }
}

void EvClient::processResponse(const QJsonObject &message) {
    const QString id = valueString(message, "id");
    const QString requestType = m_pendingRequests.take(id);
    const QJsonObject payload = message.value(QStringLiteral("payload")).toObject();
    if (requestType == QStringLiteral("snapshot")) {
        applySnapshot(payload);
    } else if (requestType == QStringLiteral("events.history")) {
        const QJsonArray events = payload.value(QStringLiteral("events")).toArray();
        for (const QJsonValue &entry : events)
            processEvent(entry.toObject(), true);
    } else if (requestType == QStringLiteral("conversation.list")) {
        m_timeline.clear();
        const QJsonArray conversations = payload.value(QStringLiteral("conversations")).toArray();
        for (const QJsonValue &entry : conversations) {
            const QJsonObject item = entry.toObject();
            const QString role = item.value(QStringLiteral("role")).toString();
            appendTimeline(role == QStringLiteral("user") ? QStringLiteral("USER")
                                                          : QStringLiteral("ASSISTANT"),
                           role == QStringLiteral("user") ? QStringLiteral("ME")
                                                          : QStringLiteral("Carlos"),
                           item.value(QStringLiteral("content")).toString(),
                           item.value(QStringLiteral("created_at")).toString());
        }
    } else if (requestType == QStringLiteral("memory.list")) {
        m_memories = jsonArrayToList(payload.value(QStringLiteral("memories")));
        emit memoriesChanged();
    } else if (requestType == QStringLiteral("tool.catalog")) {
        m_tools = jsonArrayToList(payload.value(QStringLiteral("tools")));
        emit toolsChanged();
    } else if (requestType == QStringLiteral("daily.snapshot")) {
        m_daily = payload.toVariantMap();
        emit dailyChanged();
    } else if (requestType == QStringLiteral("plan.list")) {
        m_activePlan = objectMap(payload.value(QStringLiteral("active")));
        m_plans = jsonArrayToList(payload.value(QStringLiteral("recent")));
        emit phase3Changed();
    } else if (requestType == QStringLiteral("security.snapshot")) {
        m_security = payload.toVariantMap();
        emit phase3Changed();
    } else if (requestType == QStringLiteral("latency.report")) {
        m_latency = payload.toVariantMap();
        emit phase3Changed();
    } else if (requestType == QStringLiteral("self.diagnostics")) {
        m_diagnostics = payload.toVariantMap();
        emit phase3Changed();
    } else if (requestType == QStringLiteral("personality.update")) {
        m_personality = objectMap(payload.value(QStringLiteral("personality")));
        emit phase3Changed();
        setStatus(QStringLiteral("Personality updated"));
    } else if (requestType == QStringLiteral("plan.cancel")) {
        sendRequest(QStringLiteral("plan.list"));
    } else if (requestType == QStringLiteral("confirmation.list")) {
        const QJsonArray confirmations = payload.value(QStringLiteral("confirmations")).toArray();
        m_confirmation = confirmations.isEmpty() ? QVariantMap{}
                                                 : confirmations.first().toObject().toVariantMap();
        emit confirmationChanged();
    } else if (requestType == QStringLiteral("memory.remember") ||
               requestType == QStringLiteral("memory.forget") ||
               requestType == QStringLiteral("tool.call")) {
        if (payload.value(QStringLiteral("status")).toString() ==
            QStringLiteral("confirmation_required")) {
            m_confirmation = objectMap(payload.value(QStringLiteral("confirmation")));
            emit confirmationChanged();
        } else {
            refreshMemories();
            if (requestType == QStringLiteral("tool.call")) {
                const QJsonObject result = payload.value(QStringLiteral("result")).toObject();
                m_toolResult = result.toVariantMap();
                emit toolResultChanged();
                const QString report =
                    result.value(QStringLiteral("message"))
                        .toString(
                            payload.value(QStringLiteral("error"))
                                .toString(payload.value(QStringLiteral("message")).toString()));
                if (!report.isEmpty()) {
                    setStatus(report);
                    appendTimeline(QStringLiteral("ASSISTANT"), QStringLiteral("Carlos"), report);
                }
                refreshDaily();
            }
        }
    } else if (requestType == QStringLiteral("command.submit") ||
               requestType == QStringLiteral("agent.tasks.steer")) {
        if (payload.contains(QStringLiteral("response")))
            setStatus(payload.value(QStringLiteral("response")).toString().left(240));
        if (payload.contains(QStringLiteral("cognition"))) {
            m_cognition = objectMap(payload.value(QStringLiteral("cognition")));
            emit cognitionChanged();
        }
        if (payload.value(QStringLiteral("status")).toString() ==
            QStringLiteral("confirmation_required")) {
            m_confirmation = objectMap(payload.value(QStringLiteral("confirmation")));
            emit confirmationChanged();
            appendTimeline(QStringLiteral("CONFIRMATION"), QStringLiteral("PERMISSION REQUIRED"),
                           payload.value(QStringLiteral("response")).toString());
        } else if (payload.contains(QStringLiteral("response"))) {
            appendTimeline(QStringLiteral("ASSISTANT"), QStringLiteral("Carlos"),
                           payload.value(QStringLiteral("response")).toString());
        }
    } else if (requestType == QStringLiteral("confirmation.respond")) {
        refreshMemories();
        const QJsonObject command = payload.value(QStringLiteral("command")).toObject();
        if (!command.isEmpty() && command.contains(QStringLiteral("response"))) {
            m_cognition = objectMap(command.value(QStringLiteral("cognition")));
            emit cognitionChanged();
            appendTimeline(QStringLiteral("ASSISTANT"), QStringLiteral("Carlos"),
                           command.value(QStringLiteral("response")).toString());
        }
    } else if (requestType == QStringLiteral("voice.capture.stop")) {
        setStatus(payload.value(QStringLiteral("status")).toString());
    }
}

void EvClient::applySnapshot(const QJsonObject &snapshot) {
    m_insights = jsonArrayToList(snapshot.value(QStringLiteral("insights")));
    emit insightsChanged();
    m_activity = objectMap(snapshot.value(QStringLiteral("activity")));
    emit activityChanged();
    const QJsonObject core = snapshot.value(QStringLiteral("core")).toObject();
    m_state = core.value(QStringLiteral("state")).toString(QStringLiteral("OFFLINE"));
    m_detail = core.value(QStringLiteral("detail")).toString();
    m_telemetry = objectMap(snapshot.value(QStringLiteral("telemetry")));
    m_provider = objectMap(snapshot.value(QStringLiteral("provider")));
    m_voice = objectMap(snapshot.value(QStringLiteral("voice")));
    m_personality = objectMap(snapshot.value(QStringLiteral("personality")));
    const QJsonObject planner = snapshot.value(QStringLiteral("planner")).toObject();
    m_activePlan = objectMap(planner.value(QStringLiteral("active")));
    m_plans = jsonArrayToList(planner.value(QStringLiteral("recent")));
    emit stateChanged();
    emit telemetryChanged();
    emit snapshotChanged();
    emit voiceChanged();
    emit phase3Changed();
}

void EvClient::processEvent(const QJsonObject &event, bool historical) {
    const QString type = valueString(event, "type");
    const QString source = valueString(event, "source");
    const QJsonObject payload = event.value(QStringLiteral("payload")).toObject();
    if (type == QStringLiteral("carlos.privacy_changed")) {
        m_voice.insert(QStringLiteral("privacy_profile"),
                       payload.value(QStringLiteral("mode")).toString());
        m_timeline.clear();
        m_events.clear();
        m_memories.clear();
        m_daily.clear();
        m_plans.clear();
        m_activePlan.clear();
        m_confirmation.clear();
        m_cognition.clear();
        emit voiceChanged();
        emit timelineChanged();
        emit eventsChanged();
        emit memoriesChanged();
        emit dailyChanged();
        emit phase3Changed();
        emit confirmationChanged();
        emit cognitionChanged();
    }
    const bool audioSample =
        type == QStringLiteral("voice.audio_level") || type == QStringLiteral("tts.audio_level");
    // Keep waveform samples out of the event list. Rebuilding the whole
    // graph 10-20 times a second for mic levels was a bit much.
    if (!audioSample) {
        const QVariantMap item = event.toVariantMap();
        const qint64 sequence = item.value(QStringLiteral("sequence")).toLongLong();
        bool duplicate = false;
        int insertion = m_events.size();
        if (sequence > 0) {
            for (int index = 0; index < m_events.size(); ++index) {
                const qint64 existing =
                    m_events.at(index).toMap().value(QStringLiteral("sequence")).toLongLong();
                if (existing == sequence) {
                    duplicate = true;
                    break;
                }
                if (existing > sequence) {
                    insertion = index;
                    break;
                }
            }
        }
        if (!duplicate)
            m_events.insert(insertion, item);
        while (m_events.size() > 300)
            m_events.removeFirst();
        if (!duplicate && !m_eventRefreshTimer.isActive())
            m_eventRefreshTimer.start();
    }
    // Old events are just history. Dont replay their actions or approval dialogs.
    if (historical)
        return;
    if (!audioSample)
        setActivityForEvent(type, source, payload);

    if (type == QStringLiteral("agent.insights_changed")) {
        m_insights = jsonArrayToList(payload.value(QStringLiteral("items")));
        emit insightsChanged();
    } else if (type == QStringLiteral("agent.activity")) {
        m_activity = payload.toVariantMap();
        emit activityChanged();
    } else if (type == QStringLiteral("system.telemetry")) {
        m_telemetry = payload.toVariantMap();
        emit telemetryChanged();
    } else if (type == QStringLiteral("core.state_changed")) {
        m_state = payload.value(QStringLiteral("to")).toString();
        m_detail = payload.value(QStringLiteral("detail")).toString();
        emit stateChanged();
    } else if (type == QStringLiteral("voice.audio_level")) {
        m_inputWaveform = jsonArrayToList(payload.value(QStringLiteral("waveform")));
        QVariantMap diagnostics = m_voice.value(QStringLiteral("diagnostics")).toMap();
        diagnostics.insert(QStringLiteral("input_rms"),
                           payload.value(QStringLiteral("rms")).toDouble());
        diagnostics.insert(QStringLiteral("input_peak"),
                           payload.value(QStringLiteral("peak")).toDouble());
        const QJsonObject vad = payload.value(QStringLiteral("vad")).toObject();
        if (!vad.isEmpty()) {
            diagnostics.insert(QStringLiteral("voice_activity"),
                               vad.value(QStringLiteral("active")).toBool());
            diagnostics.insert(QStringLiteral("noise_floor"),
                               vad.value(QStringLiteral("noise_floor")).toDouble());
        }
        m_voice.insert(QStringLiteral("diagnostics"), diagnostics);
        emit inputWaveformChanged();
        emit voiceChanged();
    } else if (type == QStringLiteral("tts.audio_level")) {
        m_outputWaveform = jsonArrayToList(payload.value(QStringLiteral("waveform")));
        emit outputWaveformChanged();
    } else if (type == QStringLiteral("ai.request_complete")) {
        m_cognition = payload.toVariantMap();
        emit cognitionChanged();
    } else if (type == QStringLiteral("carlos.settings_changed")) {
        if (!historical)
            refreshDaily();
    } else if (type == QStringLiteral("presence.returned")) {
        appendTimeline(QStringLiteral("INFO"), QStringLiteral("CARLOS"),
                       payload.value(QStringLiteral("greeting")).toString(),
                       valueString(event, "timestamp"));
    } else if (type == QStringLiteral("carlos.scene_changed")) {
        if (!historical) {
            emit sceneActivated(payload.value(QStringLiteral("hud")).toString());
            refreshDaily();
        }
        appendTimeline(QStringLiteral("INFO"), QStringLiteral("SCENE"),
                       payload.value(QStringLiteral("name")).toString(),
                       valueString(event, "timestamp"));
    } else if (type == QStringLiteral("reminder.due")) {
        appendTimeline(QStringLiteral("REMINDER"), QStringLiteral("REMINDER"),
                       payload.value(QStringLiteral("label")).toString(),
                       valueString(event, "timestamp"));
        refreshDaily();
    } else if (type.startsWith(QStringLiteral("power."))) {
        appendTimeline(QStringLiteral("ACTION"), QStringLiteral("POWER"),
                       type + QStringLiteral(" ") +
                           payload.value(QStringLiteral("action")).toString(),
                       valueString(event, "timestamp"));
        refreshDaily();
    } else if (type == QStringLiteral("tool.started")) {
        appendTimeline(QStringLiteral("ACTION"), QStringLiteral("TOOL EXECUTION"),
                       payload.value(QStringLiteral("tool")).toString(),
                       valueString(event, "timestamp"));
    } else if (type == QStringLiteral("tool.permission_check") &&
               payload.value(QStringLiteral("decision")).toString() == QStringLiteral("PENDING")) {
        refreshConfirmations();
    } else if (type == QStringLiteral("tool.failed") || type == QStringLiteral("system.error") ||
               type == QStringLiteral("system.warning")) {
        const bool warning = type == QStringLiteral("system.warning");
        appendTimeline(warning ? QStringLiteral("WARNING") : QStringLiteral("ERROR"),
                       warning ? QStringLiteral("SYSTEM WARNING") : QStringLiteral("SYSTEM ERROR"),
                       payload.value(QStringLiteral("error"))
                           .toString(payload.value(QStringLiteral("message")).toString()),
                       valueString(event, "timestamp"));
    }
    if (type.startsWith(QStringLiteral("plan.")))
        sendRequest(QStringLiteral("plan.list"));
    if ((source == QStringLiteral("voice") || type.startsWith(QStringLiteral("wake.")) ||
         type.startsWith(QStringLiteral("tts."))) &&
        type != QStringLiteral("voice.audio_level") && type != QStringLiteral("tts.audio_level"))
        m_voiceRefreshTimer.start();
}

void EvClient::setActivityForEvent(const QString &type, const QString &source,
                                   const QJsonObject &payload) {
    QStringList nodes;
    if (source == QStringLiteral("voice") || type.startsWith(QStringLiteral("tts.")))
        nodes << QStringLiteral("VOICE");
    if (source == QStringLiteral("language") || type.startsWith(QStringLiteral("command.")))
        nodes << QStringLiteral("LANGUAGE");
    if (source == QStringLiteral("reasoning") || type.startsWith(QStringLiteral("ai.")))
        nodes << QStringLiteral("REASONING");
    if (source == QStringLiteral("memory") || type.startsWith(QStringLiteral("memory.")))
        nodes << QStringLiteral("MEMORY");
    if (source == QStringLiteral("security"))
        nodes << QStringLiteral("SECURITY");
    if (source == QStringLiteral("tools") || type.startsWith(QStringLiteral("tool."))) {
        nodes << QStringLiteral("TOOL ROUTER");
        const QString category = payload.value(QStringLiteral("category")).toString().toUpper();
        if (!category.isEmpty())
            nodes << category;
    }
    nodes.removeDuplicates();
    if (!nodes.isEmpty()) {
        if (m_activeNodes != nodes) {
            m_activeNodes = nodes;
            emit activeNodesChanged();
        }
        m_activityTimer.start();
    }
}

void EvClient::setStatus(const QString &message) {
    if (message == m_statusMessage)
        return;
    m_statusMessage = message;
    emit statusMessageChanged();
}

void EvClient::steerTask(const QString &taskId, const QString &text) {
    if (taskId.trimmed().isEmpty() || text.trimmed().isEmpty())
        return;
    appendTimeline(QStringLiteral("USER"), QStringLiteral("TASK REVISION"), text);
    sendRequest(QStringLiteral("agent.tasks.steer"),
                {{QStringLiteral("task_id"), taskId}, {QStringLiteral("text"), text}});
}

void EvClient::appendTimeline(const QString &kind, const QString &title, const QString &body,
                              const QString &timestamp) {
    QVariantMap item{{QStringLiteral("kind"), kind},
                     {QStringLiteral("title"), title},
                     {QStringLiteral("body"), body},
                     {QStringLiteral("timestamp"), timestamp}};
    m_timeline.append(item);
    while (m_timeline.size() > 150)
        m_timeline.removeFirst();
    emit timelineChanged();
}

QVariantList EvClient::jsonArrayToList(const QJsonValue &value) {
    return value.isArray() ? value.toArray().toVariantList() : QVariantList{};
}
