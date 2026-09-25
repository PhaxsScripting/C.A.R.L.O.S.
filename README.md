# C.A.R.L.O.S.

> I fucking hate Carlos. He doesn't respond on time.

Phax's desktop assistant, hand controls, and phone dashboard. Built around Python,
C++20, Qt/QML, and a browser client, with Gentoo KDE/Plasma as the main development
platform.

**Early public source release.** This is working development code with automated
tests, not a claim that the entire Carlos specification is finished. Speech
latency, wake reliability, hardware integration, and portability still need work.

## What's here

| Folder | Job |
| --- | --- |
| [`carlos/core/ev`](carlos/core/ev) | Assistant, planning, tools, voice, permissions, local IPC, telemetry |
| [`carlos/ui`](carlos/ui) | Qt/QML control center and voice HUD |
| [`carlos/plasma`](carlos/plasma) | Optional Plasma voice activity widget |
| [`carlos/sentinel`](carlos/sentinel) | Separate-node authenticated wake reference |
| [`holohand/src`](holohand/src) | Camera tracking, gestures, calibration, desktop input |
| [`holohand/remote`](holohand/remote) | Phone web app, passkey pairing, desktop, terminal, files |

The phone app is an installable web app: open it in Safari and add it to the Home
Screen. It is not a native App Store binary. Remote access uses your own private
network and HTTPS setup. Model weights, credentials, personal settings, runtime
data, and compiled dependencies are not shipped.

## Get started

Read [building and setup](docs/SETUP.md) before installing. Build and test each
component independently; cloning this repository starts no services.

```sh
git clone https://github.com/PhaxsScripting/C.A.R.L.O.S..git
cd C.A.R.L.O.S.
python3 -m venv .venv
. .venv/bin/activate
pip install -r carlos/requirements.txt
PYTHONPATH=carlos/core python3 -m unittest discover -s carlos/tests
```

See [style](STYLE.md), [security](SECURITY.md), and
[third-party notices](THIRD_PARTY.md). Old `ev` commands and identifiers remain
for compatibility. Internal helpers include `PhaxEventBus`, `CarlosCore`,
`GiggleGuard`, and `BigBootyBudget`; the jokes stop at permission boundaries.

## License

Original project code is [MIT](LICENSE), copyright Phax. Bundled third-party
sources and patches retain their own licenses; see [the notices](THIRD_PARTY.md).
