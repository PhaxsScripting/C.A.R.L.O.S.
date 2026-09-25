#include "ui/HandPreview.h"
#include <QApplication>
#include <QElapsedTimer>
#include <QImage>
#include <QPainter>
#include <QThread>
#include <cassert>
#include <iostream>
using namespace holohand;
int main(int argc, char **argv) {
    QApplication app(argc, argv);
    HandPreview preview;
    preview.resize(740, 480);
    QImage plain(640, 480, QImage::Format_RGB32);
    plain.fill(QColor(180, 90, 40));
    preview.setVideo(plain);
    QImage plainRender(preview.size(), QImage::Format_RGB32);
    preview.render(&plainRender);
    assert(plainRender.pixelColor(370, 240) == QColor(180, 90, 40)); // no tint/dimming/grid
    QImage image(640, 480, QImage::Format_RGB32);
    image.fill(QColor("#0a1824"));
    Hand h;
    h.valid = true;
    h.confidence = .99;
    h.p[0] = {.5, .86};
    const Point fingers[][4] = {{{.4, .72}, {.32, .63}, {.25, .54}, {.2, .45}},
                                {{.38, .57}, {.37, .4}, {.36, .26}, {.35, .15}},
                                {{.48, .54}, {.48, .35}, {.48, .2}, {.48, .1}},
                                {{.57, .56}, {.6, .4}, {.61, .26}, {.62, .18}},
                                {{.65, .61}, {.69, .5}, {.72, .4}, {.74, .32}}};
    for (int finger = 0; finger < 5; ++finger)
        for (int joint = 0; joint < 4; ++joint)
            h.p[1 + finger * 4 + joint] = fingers[finger][joint];
    const QSize hint = preview.sizeHint();
    preview.setFrame(image, h);
    QImage normal(preview.size(), QImage::Format_RGB32);
    preview.render(&normal);
    preview.setState("SCROLL", .75, true);
    preview.trigger({Action::Scroll, 2, 1}, h);
    preview.trigger({Action::LeftClick}, h);
    QThread::msleep(160); // let synthetic particles spread before rendering the fixture
    QImage render(preview.size(), QImage::Format_ARGB32_Premultiplied);
    render.fill(Qt::transparent);
    preview.render(&render);
    assert(preview.sizeHint() == hint); // incoming frames must not resize the layout
    assert(render.pixelColor(20, 20).alpha() > 0);
    int changed = 0;
    for (int y = 35; y < 115; ++y)
        for (int x = 230; x < 320; ++x)
            changed += normal.pixelColor(x, y) != render.pixelColor(x, y);
    assert(changed > 100); // action burst visibly differs from ambient hand particles
    if (argc > 1)
        assert(render.save(argv[1])); // synthetic fixture only, never webcam
    for (int i = 0; i < 500; ++i)
        preview.trigger({Action::LeftClick}, h);
    assert(preview.particleCount() <= 1200);
    QElapsedTimer timing;
    timing.start();
    for (int i = 0; i < 60; ++i)
        preview.render(&render);
    std::cout << "Dense effect rendering mean: " << timing.nsecsElapsed() / 60e6 << " ms\n";
    preview.setEffects(1);
    for (int i = 0; i < 100; ++i)
        preview.trigger({Action::LeftClick}, h);
    assert(preview.particleCount() <= 180);
    preview.setEffects(0);
    preview.trigger({Action::LeftClick}, h);
    assert(preview.particleCount() == 0);
    preview.setFrame(image, {});
    preview.render(&render);
    assert(render.pixelColor(274, 72) == image.pixelColor(224, 72)); // no stale finger on new frame
    preview.clearTracking();
    assert(preview.particleCount() == 0);
    return 0;
}
