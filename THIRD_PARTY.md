# Third-party notices

The root MIT license applies to original project code only. It does not replace
upstream licenses, copyright notices, or model terms.

| Material | Origin and terms |
| --- | --- |
| `holohand/vendor/*mediapipe*` | OpenCV Zoo MediaPipe palm/hand references; Apache-2.0 license files and upstream READMEs retained beside them |
| HoloHand model downloads | OpenCV Zoo revision and SHA-256 hashes in `holohand/models/sources.json`; weights downloaded separately |
| `holohand/vendor/openvino/licenses` | OpenVINO Apache-2.0 license and third-party notices; optional runtime not bundled |
| `holohand/remote/web/guacamole.js` | Apache Guacamole JavaScript client, Apache-2.0; header retained, full license in `remote/vendor/guacamole-LICENSE` |
| `holohand/remote/vendor/guacamole.patch` | Changes to Apache Guacamole 1.6.0, under Apache-2.0; original source archives and hashes in `sources.json` |
| `holohand/remote/vendor/krdp.patch` | Changes to KDE KRDP at the pinned revision in `sources.json`; affected files use `LGPL-2.1-only OR LGPL-3.0-only OR LicenseRef-KDE-Accepted-LGPL`; license texts in `vendor/krdp-licenses` |

KRDP's affected upstream files credit Arjen Hiemstra and Aleix Pol Gonzalez;
the complete pinned upstream retains each file's attribution. Patches are source,
not redistributed KRDP binaries. Building or distributing upstream libraries
requires complying with their respective terms.

Python and npm dependencies are installed separately using the lock files and
retain their package licenses. Qt, KDE, OpenCV, FreeRDP, and optional speech/model
runtimes also retain their own licenses. This repository does not grant rights to
third-party models, voices, or provider services.

LegacyCord was a coding-style reference only. None of its source is included.
