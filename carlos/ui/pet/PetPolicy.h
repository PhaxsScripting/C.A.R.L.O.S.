#pragma once
#include <QString>
#include <QStringList>

class PetPolicy {
  public:
    static QString category(const QString &app) {
        const auto name = app.toLower().left(160);
        if (name.contains("minecraft") || name.contains("prismlauncher"))
            return "game";
        if (name.contains("codium") || name.contains("code") || name.contains("kate") ||
            name.contains("jetbrains"))
            return "code";
        if (name.contains("firefox") || name.contains("chromium") || name.contains("chrome"))
            return "browser";
        if (name.contains("konsole") || name.contains("terminal") || name.contains("kitty"))
            return "terminal";
        if (name.contains("spotify") || name.contains("vlc") || name.contains("neko-music"))
            return "media";
        if (name.contains("krita") || name.contains("gimp") || name.contains("inkscape"))
            return "art";
        if (name.contains("discord") || name.contains("signal") || name.contains("telegram"))
            return "chat";
        if (name.contains("dolphin"))
            return "files";
        return "desktop";
    }

    void observe(const QString &app, qint64 now) {
        const auto next = category(app);
        if (next != context) {
            context = next;
            since = now;
        }
    }
    void reset(qint64 now) {
        context = "desktop";
        since = now;
        lastTalk = now;
    }
    void interacted(qint64 now) { lastTalk = now; }
    QString next(qint64 now, bool allowed) {
        if (!allowed || now - since < 8000 || now - lastTalk < 90000)
            return {};
        lastTalk = now;
        const auto lines = comments(context);
        const QString result = lines.at(sequence++ % lines.size());
        return result;
    }
    static QStringList comments(const QString &kind) {
        if (kind == "game")
            return {"block supervision is my full time job", "tiny bot. big mining plans.",
                    "i brought absolutely no diamonds"};
        if (kind == "code")
            return {"you code. i'll look extremely helpful.", "tiny rubber duck reporting for duty",
                    "that semicolon has an emotional support bot now"};
        if (kind == "browser")
            return {"just one more tab, huh", "i'm coming along for the browsing",
                    "professional tab companion"};
        if (kind == "terminal")
            return {"little window. big computer energy.", "i'll stand back from the commands",
                    "terminal buddy reporting for duty"};
        if (kind == "media")
            return {"i brought my tiny headphones", "vibes department is here",
                    "quiet little listening buddy"};
        if (kind == "art")
            return {"i volunteer as a very small reference", "creative mode. i like it.",
                    "tiny art assistant, zero art supplies"};
        if (kind == "chat")
            return {"social side quest", "i'll let you talk. mostly.",
                    "tiny bot, respecting the conversation"};
        if (kind == "files")
            return {"folder expedition", "i promise not to rearrange anything",
                    "looking busy next to your files"};
        return {"just hanging out with you", "small guy. important desk duties.",
                "i live here now ig"};
    }
    QString context = "desktop";
    qint64 since = 0;
    qint64 lastTalk = -90000;
    unsigned sequence = 0;
};
