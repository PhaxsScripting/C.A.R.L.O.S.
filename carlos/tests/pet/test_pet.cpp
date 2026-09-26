#include "PetPolicy.h"
#include <QtTest>
class PetTests : public QObject {
    Q_OBJECT
  private slots:
    void noRawAppNamesInComments() {
        PetPolicy policy;
        policy.observe("secret-document-name", 0);
        QCOMPARE(policy.context, QString("desktop"));
        QVERIFY(!policy.next(10000, true).contains("secret"));
    }
    void waitsForStableContextAndRateLimits() {
        PetPolicy policy;
        policy.observe("firefox", 1000);
        QVERIFY(policy.next(8000, true).isEmpty());
        policy.observe("codium", 8001);
        QVERIFY(policy.next(16000, true).isEmpty());
        QVERIFY(!policy.next(17000, true).isEmpty());
        QVERIFY(policy.next(106000, true).isEmpty());
        QVERIFY(!policy.next(107000, true).isEmpty());
    }
    void hiddenAndQuietNeverSpeak() {
        PetPolicy policy;
        QVERIFY(policy.next(200000, false).isEmpty());
        QVERIFY(!policy.next(200000, true).isEmpty());
        policy.reset(210000);
        QVERIFY(policy.next(250000, true).isEmpty());
    }
    void pettingDelaysAutomaticComments() {
        PetPolicy policy;
        policy.interacted(120000);
        QVERIFY(policy.next(200000, true).isEmpty());
        QVERIFY(!policy.next(210000, true).isEmpty());
    }
    void knownAppsUseExpectedContext() {
        QCOMPARE(PetPolicy::category("com.mojang.minecraft"), QString("game"));
        QCOMPARE(PetPolicy::category("org.kde.konsole"), QString("terminal"));
        QCOMPARE(PetPolicy::category("org.kde.krita"), QString("art"));
        QCOMPARE(PetPolicy::category("org.kde.plasmashell"), QString("desktop"));
    }
};
QTEST_APPLESS_MAIN(PetTests)
#include "test_pet.moc"
