#include "PetController.h"
#include <QTemporaryDir>
#include <QtTest>
class PetControllerTests : public QObject {
    Q_OBJECT
    QTemporaryDir settings;
  private slots:
    void initTestCase() {
        QVERIFY(settings.isValid());
        QSettings::setPath(QSettings::NativeFormat, QSettings::UserScope, settings.path());
    }
    void init() { QSettings("Carlos", "DesktopPet").clear(); }
    void lockOverridesShowAndClearsBubble() {
        PetController pet(true);
        QVERIFY(pet.shown());
        QVERIFY(!pet.bubble().isEmpty());
        pet.SetLocked(true);
        QVERIFY(!pet.shown());
        QVERIFY(pet.bubble().isEmpty());
        pet.Show();
        QVERIFY(!pet.shown());
        pet.SetLocked(false);
        QVERIFY(pet.shown());
    }
    void observationsCannotBeInjectedDirectly() {
        PetController pet(true);
        pet.Observe("minecraft", true);
        QVERIFY(pet.shown());
        QVERIFY(!pet.observing());
    }
    void quietDoesNotRespondToPats() {
        QSettings("Carlos", "DesktopPet").setValue("quiet", true);
        PetController pet(true);
        pet.SetLocked(true);
        pet.SetLocked(false);
        pet.pet();
        QVERIFY(pet.bubble().isEmpty());
    }
};
QTEST_MAIN(PetControllerTests)
#include "test_controller.moc"
