#!/bin/sh
set -eu
[ "$(uname -s)" = FreeBSD ] && [ "$(id -u)" = 1000 ] || exit 1
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
export PATH=/usr/local/bin:/usr/bin:/bin
cmake -S "$root" -B "$root/build" -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build "$root/build" -j2
ctest --test-dir "$root/build" --output-on-failure
sh "$root/scripts/install-user.sh"
echo 'Native build installed. Login to Plasma X11, then run holohand --calibrate.'
echo 'Camera/input/gesture/thermal validation is still required on physical FreeBSD.'
