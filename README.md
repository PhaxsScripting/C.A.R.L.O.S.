# C.A.R.L.O.S.

> I fucking hate Carlos. He doesn't respond on time.

My desktop AI assistant. He talks, handles desktop tasks, remembers things you
ask him to, and has a Qt control center with a voice HUD. Python for the core,
C++ and QML for the UI. Built on Gentoo with KDE Plasma.

Still working on him. Voice latency and wake reliability need work, and other
desktops haven't had the same testing. This is a source release, so you'll need
to set up your own speech runtimes and models.

## What's here

| Folder | What's in it |
| --- | --- |
| [`carlos/core/ev`](carlos/core/ev) | AI providers, voice, memory, desktop tools, permissions and local IPC |
| [`carlos/ui`](carlos/ui) | Control center and voice HUD |
| [`carlos/plasma`](carlos/plasma) | Optional voice activity widget for Plasma |
| [`carlos/assets`](carlos/assets) | Desktop bridge and wake word files |
| [`carlos/scripts`](carlos/scripts) | Install, rollback, diagnostics and benchmarks |
| [`carlos/tests`](carlos/tests) | Core and UI tests |
| [`docs`](docs) | Setup, usage and test results |

This repo is just Carlos now. HoloHand, the phone app and the separate wake
server aren't included. Carlos still has optional hooks for those apps if you
install them separately. They aren't needed to build or test the assistant.

## Get started

Read [setup](docs/SETUP.md) for dependencies and the UI build.

```sh
git clone https://github.com/PhaxsScripting/C.A.R.L.O.S..git
cd C.A.R.L.O.S.
python3 -m venv .venv
. .venv/bin/activate
pip install -r carlos/requirements.txt
PYTHONPATH=carlos/core python3 -m unittest discover -s carlos/tests
```

Cloning and building won't start Carlos. The user installer does enable login
startup, so read its notes before running it. Models, keys and personal data stay
on your machine. The old `ev` names are still there so existing installs work.

[Using Carlos](docs/USAGE.md) · [Test results](docs/VALIDATION.md) ·
[Security](SECURITY.md) · [Third-party notices](THIRD_PARTY.md)

## License

Original code is [MIT](LICENSE), copyright Phax. Dependencies, models and voices
keep their own licenses.
