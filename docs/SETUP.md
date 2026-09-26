# Setup

Gentoo KDE Plasma is where I work on Carlos. FreeBSD scripts are included, but
that port hasn't been validated for this release. Run these commands from the
repo root as your normal user.

## What you need

- Python 3.11+; tested with 3.14.
- CMake 3.21+, a C++20 compiler and Qt 6.6+ with Core, Gui, Qml, Quick,
  QuickControls2, Network, Widgets, DBus and Test.
- LayerShellQt 6.6+ on Linux for the voice HUD.
- Qt's `qmltestrunner` for the QML tests.
- Bubblewrap (`bwrap`) for sandboxed project execution and its Linux tests.

Install the native packages through your distro, then set up Python and build:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r carlos/requirements.txt
PYTHONPATH=carlos/core python3 -m unittest discover -s carlos/tests
cmake -S carlos/ui -B carlos/build/ui -DBUILD_TESTING=ON
cmake --build carlos/build/ui -j2
ctest --test-dir carlos/build/ui --output-on-failure
```

CTest should list both `ev-client-tests` and `ev-interface-tests`. If the second
one is missing, install `qmltestrunner` and configure CMake again.

## Run from source

With the venv active:

```sh
PYTHONPATH=carlos/core python3 -m ev
```

In another terminal at the repo root:

```sh
. .venv/bin/activate
carlos/build/ui/ev-ui
```

Don't run a second core beside an installed one. Both use the same local socket
and user configuration. See [usage](USAGE.md) for health checks and controls.

## Models and voice

Settings live in `~/.config/ev/config.json` by default. Data and state follow the
XDG directories. [`DEFAULT_CONFIG`](../carlos/core/ev/config.py) has the full list
of settings, including runtime paths you need to change for your machine.

The default `offline` provider handles a limited set of commands. It isn't a
chat model. For local conversation, install llama.cpp and a compatible GGUF model,
then configure `providers.local_llama` and set `providers.active` to
`local_agent`. Check the model's license before downloading it.

Voice needs its own setup: whisper.cpp for transcription, Piper for speech,
and the configured wake/VAD runtimes and models. The Python requirements here
only cover the core. They don't install model weights or those runtimes.
Set their paths in the `voice` config and check availability with `health`.
Wake listening is off by default.

Optional desktop tools need their own applications or system utilities. Missing
HoloHand or mobile services don't prevent Carlos from running.

## Install for your user

Read `carlos/scripts/install-user.sh` before running it. It installs into your
home directory, backs up replaced files, registers launchers, **enables login
autostart**, and starts the core. `--no-start` skips starting it right now; it
still enables autostart.

The installed launchers use system Python. Install the core dependencies through
your distro too; activating this repo's venv won't provide them to the launchers.
`rollback-user.py` and `uninstall-user.sh` are in the same scripts folder.

The optional Plasma widget has its own installer. After installing it, add it
through Plasma's Add Widgets menu.

## Desktop pet

For the little desktop buddy, see [Carlos Pet](PET.md). You can install it on its
own after the UI build, without starting the core or enabling autostart.
