# Building and setup

Developer source release. Gentoo KDE/Plasma is the primary environment; FreeBSD
scripts are experimental and were not validated in this release. Commands below
start at the repository root unless stated otherwise. Build as a normal user.

## Carlos

Requires Python 3.11+ (tested with 3.14), CMake 3.21+, C++20, Qt 6.6+
Core/Gui/Qml/Quick/QuickControls2/Network/Widgets/Test, and LayerShellQt 6.6+ on
Linux. Qt's QML test runner enables the interface tests.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r carlos/requirements.txt
PYTHONPATH=carlos/core python3 -m unittest discover -s carlos/tests
cmake -S carlos/ui -B carlos/build/ui -DBUILD_TESTING=ON
cmake --build carlos/build/ui -j2
ctest --test-dir carlos/build/ui --output-on-failure
```

Explicit development run:

```sh
PYTHONPATH=carlos/core python3 -m ev
# In another shell:
carlos/build/ui/ev-ui
PYTHONPATH=carlos/core python3 -m ev.cli health
```

Configuration is `~/.config/ev/config.json`; data/state follow XDG directories.
Review [`DEFAULT_CONFIG`](../carlos/core/ev/config.py) and set your own paths.
Local speech and reasoning require separately installed whisper.cpp, Piper,
wake/VAD runtimes, and model files. No weights or credentials are supplied. Check
the license of each model and voice you choose. Missing runtimes are unavailable;
a source build does not install them. Optional integrations need their own tools.

`carlos/scripts/install-user.sh` installs into your home, backs up replaced files,
registers launchers, **enables login autostart**, and starts the core. `--no-start`
prevents the immediate start but still registers autostart. Run it only when you
want that behavior. The installed launchers use system Python, which must also
have the core dependencies installed through your distribution. `rollback-user.py` and `uninstall-user.sh` are beside it. The
optional widget installer registers the widget; place it through Add Widgets.

## HoloHand

Requires CMake 3.21+, C++20, Qt6 Core/Gui/Widgets/Network/DBus, OpenCV 4.8+ with DNN
and video, X11/XTest development libraries, and a camera. Linux input uses uinput;
FreeBSD uses X11. OpenVINO is optional and disabled by default.

```sh
cmake -S holohand -B holohand/build
cmake --build holohand/build -j2
ctest --test-dir holohand/build --output-on-failure
python3 holohand/scripts/fetch-models.py
```

The downloader verifies hashes in `holohand/models/sources.json`; licenses are in
`holohand/vendor`. An administrator can run `holohand/scripts/setup-linux-input.sh
USER` with the actual desktop username to grant uinput access. It loads the module
and installs a persistent udev rule, refusing to replace a different rule. Do not
run the tracker as root.

`holohand/scripts/install-user.sh` installs binaries/models into `~/.local` and
**enables login autostart**. Then launch `~/.local/bin/holohand --calibrate`.
The uninstaller is beside the installer. Real-hand quality needs a physical test.

## Phone web app

Requires Python 3.11+, Node.js 20.19+ or 22.12+, and locked dependencies. This is
an installable web app, not a signed iOS binary.

```sh
cd holohand/remote
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
npm ci --ignore-scripts
npm run build
.venv/bin/python -m pytest tests -q
npx playwright install chromium
npm run test:browser -- browser.spec.js
```

The browser test uses a local server on port 8766 and fixture state under
`~/.cache/holohand-browser-tests`. It checks pairing, a real terminal, file editing,
and revocation. Other browser specs are opt-in live desktop tests and can send
real input; they require configured native desktop dependencies.

Review `scripts/install-user.sh` before deployment: it installs dependencies,
builds the client, creates a launcher, **enables autostart**, and starts the
backend. It listens on loopback port 8765. Set your own private HTTPS origin in
`~/.local/state/holohand-remote/config.json`:

```json
{"origin":"https://YOUR-COMPUTER.YOUR-TAILNET.ts.net"}
```

Configure private Tailscale Serve for that loopback endpoint and sign in on both
devices. Do not use public Funnel. The network helper prints guidance without
changing firewall/DNS. `install-system.sh` is Gentoo/OpenRC-specific: review its
package and boot-service changes before using it.

Run `~/.local/bin/holohand-remote pair` locally for a five-minute code. Open your
HTTPS URL in Safari, Add to Home Screen, then pair from that icon and approve the
passkey prompt. Keep the private network connected for cellular access.

Desktop streaming additionally needs KRDP, FreeRDP and Guacamole. The pinned
manifest and patches are in `vendor`; `scripts/build-native.sh` builds them under
`~/.local/opt`. It needs KDE/PipeWire/FreeRDP development libraries, autotools and
native build dependencies. Provision `rdp.crt` and `rdp.key` in the private remote
state directory; the desktop bridge pins the certificate fingerprint. Those
files and native binaries are not shipped. Terminal/files do not require desktop
streaming. The Codex integration needs a separately installed, authenticated CLI.

## Sentinel and acceptance

See [the node reference](../carlos/sentinel/README.md). It requires a separate
always-on node. A computer cannot run its own wake service while powered off.
Real iPhone, camera, microphone, audio, and physical wake acceptance are separate
from automated release tests. This release does not assert the full specification
is complete or universally portable.
