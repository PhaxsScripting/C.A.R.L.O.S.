#include "core/Gesture.h"
#include "core/CaptureTiming.h"
#include "core/CursorMotion.h"
#include "core/DwellClick.h"
#include "core/HandFilter.h"
#include "core/PointerMapping.h"
#include "core/PointerStep.h"
#include <cassert>
#include <iostream>
#include <limits>
using namespace holohand;
Hand point() {
    Hand h;
    h.valid = true;
    h.confidence = .99;
    h.p[0] = {.5, .9};
    h.p[5] = {.35, .65};
    h.p[9] = {.5, .65};
    h.p[17] = {.65, .65};
    h.p[6] = {.4, .55};
    h.p[8] = {.4, .2};
    h.p[10] = {.5, .55};
    h.p[12] = {.55, .8};
    h.p[14] = {.6, .55};
    h.p[16] = {.6, .8};
    h.p[18] = {.7, .55};
    h.p[20] = {.7, .8};
    h.p[4] = {.15, .65};
    return h;
}
int count(const std::vector<Event> &e, Action a) {
    int n = 0;
    for (auto x : e)
        n += x.action == a;
    return n;
}
int main() {
    {
        assert(freshCapture(10, 10.1));
        assert(!freshCapture(10, 10.2));
        assert(!freshCapture(10.1, 10));
        assert(!freshCapture(0, 0));
        assert(!freshCapture(std::numeric_limits<double>::quiet_NaN(), 10));
        assert(!freshCapture(10, std::numeric_limits<double>::infinity()));
        // A delayed decode must preserve the old hardware timestamp.
        assert(captureTimestamp(9, true, 10) == 9);
        assert(!freshCapture(captureTimestamp(9, true, 10), 10.01));
        assert(captureTimestamp(123456789, false, 10) == 10);
    }
    auto p = point();
    auto pinch = p;
    pinch.p[4] = pinch.p[8];
    {
        GestureEngine g;
        auto palm = p;
        palm.p[12] = {.5, .18};
        palm.p[16] = {.6, .2};
        palm.p[20] = {.7, .25};
        g.update(p, 0);
        g.update(pinch, .1);
        auto cancelled = g.update(palm, .2);
        assert(g.state() == "PALM CANCEL");
        assert(count(cancelled, Action::LeftClick) == 0);
        assert(count(cancelled, Action::Move) == 0);
        assert(count(g.update(p, .25), Action::LeftClick) == 0);
    }
    {
        GestureEngine g;
        g.update(p, 0);
        g.update(pinch, .1);
        assert(count(g.update(p, .2), Action::LeftClick) == 0);
        auto e = g.update(p, .26);
        assert(count(e, Action::LeftClick) == 1);
        assert(count(g.update(p, .28), Action::LeftClick) == 0);
    }
    {
        GestureEngine g;
        g.update(p, 0);
        int l = 0, r = 0;
        for (int i = 1; i < 100; i++) {
            auto e = g.update(pinch, i * .02);
            l += count(e, Action::LeftClick);
            r += count(e, Action::RightClick);
        }
        assert(l == 0 && r == 1);
        assert(count(g.update(p, 2.01), Action::LeftClick) == 0);
    }
    {
        GestureEngine g;
        auto drag = p;
        drag.p[4] = drag.p[12];
        g.update(p, 0);
        int n = 0;
        for (int i = 1; i < 40; i++)
            n += count(g.update(drag, i * .02), Action::Down);
        assert(n == 1);
        assert(count(g.update(Hand{}, .81), Action::Up) == 1);
        assert(count(g.reset(), Action::Up) == 0);
    }
    {
        GestureEngine g;
        auto bad = p;
        bad.confidence = .1;
        assert(g.update(bad, 0).empty());
        g.update(p, .1);
        g.update(pinch, .2);
        assert(g.update(Hand{}, .25).empty());
        assert(count(g.update(p, .3), Action::LeftClick) == 0);
    }
    {
        GestureEngine g;
        auto natural = p;
        natural.p[16] = natural.p[20];
        g.update(p, 0);
        int n = 0;
        for (int i = 1; i < 100; i++)
            n += count(g.update(natural, i * .02), Action::Maximize);
        assert(n == 0);
    }
    {
        OneEuro f;
        assert(f.filter(.5, 0) == .5);
        double v = f.filter(.51, .02);
        assert(v > .5 && v < .51);
        f.reset();
        assert(f.filter(.1, 1) == .1);
    }
    {
        GestureEngine g;
        auto max = p;
        max.p[8] = {.4, .8};
        max.p[16] = {.6, .2};
        max.p[20] = {.64, .2};
        g.update(p, 0);
        int n = 0;
        for (int i = 1; i < 100; i++)
            n += count(g.update(max, i * .02), Action::Maximize);
        assert(n == 1);
    }
    {
        GestureEngine g;
        auto scroll = p;
        scroll.p[12] = {.43, .2};
        g.update(p, 0);
        int wheels = 0, moves = 0;
        for (int i = 1; i < 40; i++) {
            scroll.p[8].x += .004;
            scroll.p[12].x += .004;
            scroll.p[8].y += .004;
            scroll.p[12].y += .004;
            auto e = g.update(scroll, i * .02);
            wheels += count(e, Action::Scroll);
            moves += count(e, Action::Move);
        }
        assert(wheels > 0 && moves == 0);
    }
    {
        GestureEngine g;
        auto drag = p;
        drag.p[4] = drag.p[12];
        g.update(p, 0);
        for (int i = 1; i < 30; i++)
            g.update(drag, i * .02);
        assert(count(g.update(drag, 2), Action::Up) == 1);
        assert(count(g.update(drag, 2.02), Action::Down) == 0);
    }
    {
        GestureEngine g;
        auto bad = p;
        bad.confidence = std::numeric_limits<double>::quiet_NaN();
        assert(g.update(bad, 0).empty());
        assert(g.state() == "IDLE");
    }
    {
        for (double scale : {.5, 1., 1.5}) {
            GestureEngine g;
            auto a = p, b = pinch;
            for (auto &v : a.p) {
                v.x *= scale;
                v.y *= scale;
            }
            for (auto &v : b.p) {
                v.x *= scale;
                v.y *= scale;
            }
            g.update(a, 0);
            g.update(b, .1);
            assert(count(g.update(a, .2), Action::LeftClick) == 0);
            assert(count(g.update(a, .26), Action::LeftClick) == 1);
        }
    }
    {
        GestureEngine g;
        g.update(pinch, 0);
        int clicks = 0;
        for (int i = 1; i < 50; i++)
            clicks += count(g.update(pinch, i * .02), Action::RightClick);
        assert(clicks == 0);
    }
    {
        CursorMotion c;
        c.submit({.3, .3}, 1);
        assert(!c.step(1));
        c.submit({.3, .3}, 1.067);
        auto p = c.step(1.067);
        assert(p && std::abs(p->x - .3) < .001);
        c.submit({.95, .05}, 1.134);
        p = c.step(1.14);
        assert(p && std::abs(p->x - .3) < .001); // reject one-frame spike
        c.submit({.3, .3}, 1.201);
        p = c.step(1.21);
        assert(p && std::abs(p->x - .3) < .001);
        c.freeze();
        assert(!c.step(1.22));
        c.submit({.8, .8}, 1.27);
        assert(!c.step(1.27));
        c.submit({.8, .8}, 1.337);
        p = c.step(1.34);
        assert(p && p->x < .35); // reacquire without teleport
        auto prior = *p;
        for (int i = 1; i < 8; i++) {
            p = c.step(1.34 + i * .016);
            if (p) {
                assert(distance(*p, prior) <= 1.3 * .016 + .000001);
                prior = *p;
            }
        }
        assert(!c.step(2)); // stale input stops
    }
    {
        CursorMotion c;
        double t = 1;
        c.submit({.5, .5}, t);
        double low = 1, high = 0;
        for (int i = 1; i < 60; i++) {
            t += .067;
            c.submit({.5 + (i % 2 ? .001 : -.001), .5}, t);
            auto p = c.step(t);
            if (p) {
                low = std::min(low, p->x);
                high = std::max(high, p->x);
            }
        }
        assert(high - low < .002);
    }
    {
        GestureEngine g;
        g.update(p, 0);
        auto prep = p;
        prep.p[4] = {.55, .2};
        g.update(prep, .1);
        assert(g.state() == "PINCH PREP");
        assert(count(g.update(prep, .2), Action::Move) == 0);
    }
    {
        GestureEngine g;
        g.update(p, 0);
        g.update(pinch, .1);
        g.update(p, .18);
        auto back = g.update(pinch, .21);
        assert(count(back, Action::LeftClick) == 0);
        assert(g.state() == "PINCH");
    }
    {
        HandFilter f;
        auto first = f.update(p);
        auto moved = p;
        for (auto &v : moved.p) {
            v.x += .03;
            v.y += .02;
        }
        auto smooth = f.update(moved);
        assert(std::abs(smooth.p[8].x - moved.p[8].x) < .000001);
        auto noisy = moved;
        noisy.p[8].x += .006;
        auto filtered = f.update(noisy);
        assert(filtered.p[8].x < noisy.p[8].x);
        assert(distance(filtered.p[8], noisy.p[8]) <= .002001);
        assert(!f.update(Hand{}).valid);
    }
    {
        GestureEngine g;
        g.update(p, 1);
        g.update(pinch, 1.1);
        auto weak = pinch;
        weak.confidence = .7;
        assert(g.update(weak, 1.17).empty());
        assert(g.update(weak, 1.23).empty());
        assert(count(g.update(pinch, 1.25), Action::RightClick) == 0);
    }
    {
        GestureEngine g;
        auto drag = p;
        drag.p[4] = drag.p[12];
        g.update(p, 1);
        for (int i = 1; i < 30; i++)
            g.update(drag, 1 + i * .02);
        drag.confidence = .7;
        assert(count(g.update(drag, 1.6), Action::Up) == 1);
        assert(count(g.update(drag, 1.65), Action::Down) == 0);
    }
    {
        GestureEngine g;
        g.update(p, 1);
        g.update(pinch, 1.1);
        auto bent = p;
        bent.p[8] = {.4, .8};
        assert(count(g.update(bent, 1.2), Action::LeftClick) == 0);
        assert(count(g.update(bent, 1.26), Action::LeftClick) == 1);
    }
    {
        GestureEngine g;
        auto wide = p;
        wide.yScale = .75;
        auto pin = pinch;
        pin.yScale = .75;
        g.update(wide, 1);
        g.update(pin, 1.1);
        g.update(wide, 1.2);
        assert(count(g.update(wide, 1.26), Action::LeftClick) == 1);
    }
    {
        // Precision remains subpixel after settling, even on a wide desktop.
        for (int width : {1280, 2560, 4480, 7680}) {
            CursorMotion c;
            c.setViewport(width, 1440);
            c.submit({.5, .5}, 1);
            c.submit({.5, .5}, 1.067);
            c.step(1.067);
            const double target = .5 + 4. / width;
            std::optional<Point> result;
            for (int i = 1; i <= 60; ++i) {
                const double time = 1.067 + i * .02;
                c.submit({target, .5}, time);
                result = c.step(time);
            }
            assert(result && std::abs(result->x - target) * width < 1.0);
        }
    }
    {
        HandFilter filter;
        filter.update(p);
        auto noisyWrist = p;
        noisyWrist.p[0].x += .05;
        auto result = filter.update(noisyWrist);
        assert(result.valid && distance(result.p[8], p.p[8]) < .00001);
    }
    {
        auto cropped = p;
        cropped.imageBoundsKnown = true;
        cropped.p[20].x = 1.1;
        GestureEngine g;
        auto events = g.update(cropped, 1);
        assert(count(events, Action::Move) == 1);
        assert(g.state() == "PARTIAL POINT");
        cropped.p[4] = cropped.p[8]; // missing outer finger cannot authorize a pinch
        for (int i = 1; i <= 50; ++i) {
            events = g.update(cropped, 1 + i * .02);
            assert(count(events, Action::LeftClick) == 0);
            assert(count(events, Action::RightClick) == 0);
            assert(count(events, Action::Down) == 0);
        }
        cropped.observedMask &= ~(1u << 8);
        assert(g.update(cropped, 2.02).empty()); // invisible fingertip cannot point
    }
    {
        GestureEngine g;
        auto drag = p;
        drag.p[4] = drag.p[12];
        g.update(p, 1);
        for (int i = 1; i < 30; ++i)
            g.update(drag, 1 + i * .02);
        drag.imageBoundsKnown = true;
        drag.observedMask &= ~(1u << 20);
        assert(count(g.update(drag, 1.6), Action::Up) == 1);
    }
    {
        for (double speed : {300., 600., 900., 1500., 2400.})
            for (double gain : {.65, 1.}) {
                Point actual{};
                const Point target{1200, 450};
                for (int i = 0; i < 1500; ++i) {
                    const double dt = i % 7 == 0 ? .12 : .016;
                    auto step =
                        pointerStep({target.x - actual.x, target.y - actual.y}, speed, dt, gain);
                    assert(std::hypot(step.x, step.y) <= speed * std::min(dt, .032) + 1e-8);
                    const double before = distance(actual, target);
                    actual.x += step.x;
                    actual.y += step.y;
                    assert(distance(actual, target) <= before + 1e-8); // no overshoot/reversal
                }
                assert(distance(actual, target) < .1);
            }
    }
    {
        CursorMotion c;
        c.submit({.4, .4}, 1);
        c.submit({.4, .4}, 1.04);
        assert(c.step(1.04));
        c.hold();
        assert(!c.step(1.08));
        c.submit({.401, .4}, 1.12);
        auto result = c.step(1.12);
        assert(result && std::abs(result->x - .4) < .002);
        c.hold();
        c.submit({.8, .8}, 2);
        assert(!c.step(2)); // long loss still needs stable reacquisition
    }
    {
        CursorMotion c;
        c.setDirect(true);
        c.submit({.2, .3}, 1);
        assert(!c.step(1));
        c.submit({.21, .3}, 1.034);
        auto result = c.step(1.034);
        assert(result && distance(*result, {.21, .3}) < 1e-9);
        c.freeze();
        c.submit({.8, .7}, 2);
        c.submit({.8, .7}, 2.034);
        result = c.step(2.034);
        assert(result && distance(*result, {.21, .3}) <= 1.8 * .032 + 1e-9);
        const auto held = *result;
        c.submit({.05, .05}, 2.068);
        result = c.step(2.068);
        assert(result && distance(*result, held) < 1e-9); // isolated outlier rejected
        assert(!c.step(3));
    }
    {
        // Rejected spikes must not keep an old target alive indefinitely.
        CursorMotion c;
        c.setDirect(true);
        c.submit({.2, .2}, 1);
        c.submit({.2, .2}, 1.03);
        assert(c.step(1.03));
        for (int i = 1; i <= 4; ++i)
            c.submit({i % 2 ? .95 : .6, .9}, 1.03 + i * .04);
        assert(!c.step(1.2));
    }
    {
        CursorMotion c;
        c.setDirect(true);
        c.submit({.2, .2}, 1);
        c.submit({.2, .2}, 1.03);
        c.submit({.4, .4}, 1.03);
        c.submit({.3, .3}, 1.01);
        assert(distance(*c.step(1.04), {.2, .2}) < 1e-9);
        assert(!c.step(std::numeric_limits<double>::quiet_NaN()));
    }
    {
        HandFilter filter;
        filter.update(p);
        filter.update(Hand{});
        auto returned = p;
        for (auto &v : returned.p)
            v.x += .4;
        const auto reacquired = filter.update(returned);
        assert(reacquired.valid && distance(reacquired.p[8], returned.p[8]) < 1e-9);
    }
    {
        GestureEngine g;
        auto scroll = p;
        scroll.p[12] = {.43, .2};
        g.update(p, 1);
        for (int i = 1; i <= 20; ++i)
            g.update(scroll, 1 + i * .02);
        assert(g.state() == "SCROLL");
        auto wobble = scroll;
        wobble.p[12] = {.69, .2}; // slightly outside normal release distance
        assert(g.update(wobble, 1.42).empty());
        assert(g.state() == "SCROLL");
        scroll.p[8].y += .01;
        scroll.p[12].y += .01;
        assert(count(g.update(scroll, 1.46), Action::Scroll) == 1); // no repeated dwell
        auto weak = scroll;
        weak.confidence = .7;
        assert(g.update(weak, 1.5).empty());
        for (auto &v : scroll.p)
            v.x += .08;
        assert(count(g.update(scroll, 1.54), Action::Scroll) == 0); // no catch-up
        auto separated = scroll;
        separated.p[12].x += .4;
        assert(count(g.update(separated, 1.58), Action::Scroll) == 0);
        assert(g.state() != "SCROLL");
    }
    {
        // Every corner is reachable without placing the hand at a camera edge.
        PointerMapping mapping;
        const Point corners[] = {{.16, .18}, {.84, .18}, {.16, .68}, {.84, .68}};
        const Point expected[] = {{0, 0}, {1, 0}, {0, 1}, {1, 1}};
        for (int i = 0; i < 4; ++i) {
            auto p = mapping.map(corners[i]);
            assert(distance(p, expected[i]) < 1e-9);
            CursorMotion c;
            c.setDirect(true);
            c.setPrecision(true);
            c.setViewport(3926, 1562);
            c.submit(p, 1);
            c.submit(p, 1.034);
            assert(c.step(1.034) && distance(*c.step(1.034), expected[i]) < 1e-9);
        }
        assert(distance(mapping.map({-.1, 1.1}), {0, 1}) < 1e-9);
        double previous = -1;
        for (int i = 0; i <= 100; ++i) {
            const double x = mapping.map({i / 100., .5}).x;
            assert(x >= previous);
            previous = x;
        }
    }
    {
        CursorMotion c;
        c.setDirect(true);
        c.setPrecision(true);
        c.setViewport(2560, 1440);
        c.submit({.5, .5}, 1);
        c.submit({.5, .5}, 1.034);
        for (int i = 2; i < 20; ++i) {
            c.submit({.5 + (i % 2 ? .0003 : -.0003), .5}, 1 + i * .034);
            assert(distance(*c.step(1 + i * .034), {.5, .5}) < 1e-9);
        }
        c.submit({.56, .52}, 1.7);
        assert(distance(*c.step(1.7), {.56, .52}) < 1e-9); // travel has no added easing
    }
    {
        // Slow wheel movement must accumulate at both 30 and 60 observations/sec.
        double amounts[2]{};
        for (int rateIndex = 0; rateIndex < 2; ++rateIndex) {
            const int fps = rateIndex ? 60 : 30;
            GestureEngine g;
            auto h = p;
            h.p[12] = {.54, .2}; // natural two-finger gap
            g.update(p, 1);
            for (int i = 1; i <= fps; ++i)
                g.update(h, 1. + double(i) / fps);
            assert(g.state() == "SCROLL");
            for (int i = 1; i <= fps; ++i) {
                h.p[8].y += .03 / fps;
                h.p[12].y += .03 / fps;
                for (auto event : g.update(h, 2. + double(i) / fps))
                    if (event.action == Action::Scroll)
                        amounts[rateIndex] += event.y;
            }
            assert(amounts[rateIndex] < -1.1 && amounts[rateIndex] > -1.5);
        }
        assert(std::abs(amounts[0] - amounts[1]) < .16);
        GestureEngine g;
        auto palm = p;
        palm.p[12] = {.54, .2};
        palm.p[16] = {.6, .2};
        palm.p[20] = {.7, .2};
        g.update(p, 1);
        g.update(palm, 1.1);
        assert(g.state() != "SCROLL");
    }
    {
        DwellClick d;
        int clicks = 0;
        for (int i = 0; i < 100; ++i)
            clicks += d.update(Point{500, 300}, 1 + i * .034);
        assert(clicks == 1); // never repeat on a held target
        d.update({}, 4.5);
        for (int i = 0; i < 40; ++i)
            clicks += d.update(Point{500, 300}, 4.6 + i * .034);
        assert(clicks == 1); // loss does not release the same-target latch
        for (int i = 0; i < 40; ++i)
            clicks += d.update(Point{540, 300}, 6 + i * .034);
        assert(clicks == 2);
        d.reset();
        for (int i = 0; i < 20; ++i)
            assert(!d.update(Point{500, 300}, 8 + i * .034));
        d.update({}, 8.7);
        for (int i = 0; i < 20; ++i)
            assert(!d.update(Point{500, 300}, 8.8 + i * .034));
        assert(!d.update(Point{500, 300}, 10)); // stale samples cannot complete a dwell
    }
    {
        CursorMotion c;
        c.setDirect(true);
        c.setPrecision(true);
        c.submit({.15, .5}, 1);
        c.submit({.15, .5}, 1.034);
        for (int i = 1; i <= 3; ++i) {
            Point target{.15 + i * .24, .5};
            c.submit(target, 1.034 + i * .034);
            assert(distance(*c.step(1.034 + i * .034), target) < 1e-9); // fast sweep is not a spike
        }
    }
    {
        // A comfortable half-second pinch must not fall between tap and right-click.
        GestureEngine g;
        g.update(p, 1);
        for (int i = 0; i < 13; ++i)
            assert(count(g.update(pinch, 1.1 + i * .04), Action::RightClick) == 0);
        assert(count(g.update(p, 1.61), Action::LeftClick) == 0);
        assert(count(g.update(p, 1.67), Action::LeftClick) == 1);
    }
    {
        // Two deliberate release-confirmed pinches fit the desktop double-click interval.
        GestureEngine g;
        g.update(p, 1);
        g.update(pinch, 1.05);
        g.update(p, 1.13);
        assert(count(g.update(p, 1.19), Action::LeftClick) == 1);
        g.update(pinch, 1.27);
        g.update(p, 1.39);
        assert(count(g.update(p, 1.45), Action::LeftClick) == 1);
        assert(count(g.update(p, 1.5), Action::LeftClick) == 0);
    }
    std::cout << "Gesture, landmark and motion scenarios passed: "
                 "tap/hold/drag/loss/confidence/neutral/filter/maximize/scroll/gap/scale/rearm\n";
}
