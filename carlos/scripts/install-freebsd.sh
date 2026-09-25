#!/bin/sh
set -eu
[ "$(uname -s)" = FreeBSD ] && [ "$(id -u)" = 1000 ] || exit 1
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
export PATH=/usr/local/bin:/usr/bin:/bin
external=${1:?Pass verified public external-assets directory}
build="$root/build/freebsd"
mkdir -p "$build"
python3.11 -m venv --system-site-packages "$build/runtime"
py="$build/runtime/bin/python"
"$py" -c 'import aiohttp,psutil,jeepney,dbus,gi,numpy,onnxruntime,PIL'
[ -r /usr/local/include/onnxruntime/onnxruntime_cxx_api.h ]
[ -r /usr/local/lib/libonnxruntime.so ]
if ! "$py" -c 'import sherpa_onnx' >/dev/null 2>&1;then
 [ "$(sha256 -q "$external/sherpa-onnx-1.13.7.tar.gz")" = ee0c20cafb34cc1f86afb2845babd941c26e46de4a9925cbe86fd55ff3557818 ]
 [ -d "$build/sherpa-onnx-1.13.7" ] || tar -xzf "$external/sherpa-onnx-1.13.7.tar.gz" -C "$build"
 export SHERPA_ONNXRUNTIME_INCLUDE_DIR=/usr/local/include/onnxruntime
 export SHERPA_ONNXRUNTIME_LIB_DIR=/usr/local/lib
 export SHERPA_ONNX_CMAKE_ARGS='-DCMAKE_BUILD_TYPE=Release -DSHERPA_ONNX_ENABLE_BINARY=OFF -DSHERPA_ONNX_ENABLE_PORTAUDIO=OFF -DSHERPA_ONNX_ENABLE_WEBSOCKET=OFF -DSHERPA_ONNX_ENABLE_TTS=OFF -DSHERPA_ONNX_ENABLE_SPEAKER_DIARIZATION=OFF -DSHERPA_ONNX_ENABLE_GPU=OFF -DCMAKE_POLICY_VERSION_MINIMUM=3.5'
 export SHERPA_ONNX_MAKE_ARGS=-j2
 "$py" -m pip install --no-build-isolation --no-deps "$build/sherpa-onnx-1.13.7"
fi
"$py" -c 'import sherpa_onnx; assert hasattr(sherpa_onnx,"KeywordSpotter")'
"$py" -m pip install --no-index --no-deps "$external/rapidocr-3.9.2-py3-none-any.whl"
"$py" -c 'import rapidocr'
cmake -S "$root/ui" -B "$root/build/ui" -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build "$root/build/ui" -j2
ctest --test-dir "$root/build/ui" --output-on-failure
PYTHONPATH="$root/core" "$py" "$root/scripts/validate-freebsd.py" --preinstall
"$py" "$root/scripts/fetch-freebsd-model.py" "$external"
"$py" "$root/scripts/install-freebsd-user.py" "$build/runtime" "$external"
echo 'E.V. native application installed; session validation is still pending.'
