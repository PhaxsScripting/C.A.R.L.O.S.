# Third-party notices

The root MIT license covers original Carlos code. Dependencies and model files
keep their own licenses.

Python packages are installed separately from
[`carlos/requirements.txt`](carlos/requirements.txt): aiohttp, psutil, Pillow and
Jeepney, plus their dependencies. Their distributions include their license
information.

The desktop UI uses Qt and, on Linux, KDE's LayerShellQt. These are installed
separately. Check their terms before distributing a build with those libraries.

Optional runtimes include llama.cpp, whisper.cpp, Piper, Sherpa-ONNX and ONNX
Runtime. Model and voice licenses can differ from the runtime's license. No
model weights or voice files are included, and this repo doesn't grant rights
to provider services or third-party models.
