#!/bin/sh
set -eu
remote_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build_root="$HOME/.cache/holohand-native-rebuild"
mkdir -p "$build_root"
python3 - "$remote_root" "$build_root" <<'PY'
import hashlib,json,pathlib,subprocess,sys,tarfile,urllib.request
root=pathlib.Path(sys.argv[1]);build=pathlib.Path(sys.argv[2]);manifest=json.loads((root/'vendor/sources.json').read_text())
for name in ('guacamole-server','guacamole-client'):
 item=manifest[name];archive=build/(name+'-'+item['version']+'.tar.gz')
 if not archive.exists():urllib.request.urlretrieve(item['url'],archive)
 assert hashlib.sha256(archive.read_bytes()).hexdigest()==item['sha256'],'Source checksum mismatch'
 directory=build/(name+'-'+item['version'])
 if not directory.exists():
  with tarfile.open(archive) as t:t.extractall(build,filter='data')
  if name=='guacamole-server':subprocess.run(['patch','-p1','-i',str(root/'vendor/guacamole.patch')],cwd=directory,check=True)
source=build/'krdp'
if not source.exists():
 subprocess.run(['git','clone','https://invent.kde.org/plasma/krdp.git',str(source)],check=True)
 subprocess.run(['git','checkout',manifest['krdp_commit']],cwd=source,check=True)
 subprocess.run(['git','apply',str(root/'vendor/krdp.patch')],cwd=source,check=True)
PY
cd "$build_root/guacamole-server-1.6.0"
./configure --prefix="$HOME/.local/opt/holohand-guacamole" --with-freerdp-plugin-dir="$HOME/.local/opt/holohand-guacamole/lib/freerdp3" --disable-guacenc --disable-guaclog --without-vnc --without-ssh --without-telnet --without-kubernetes CFLAGS='-O2 -Wno-error -Wno-deprecated-declarations'
make -j2 CFLAGS='-O2 -Wno-error -Wno-deprecated-declarations'
make install
cmake -S "$build_root/krdp" -B "$build_root/krdp-build" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$HOME/.local/opt/holohand-krdp" -DBUILD_EXAMPLES=OFF -DBUILD_TESTING=OFF -DCMAKE_DISABLE_FIND_PACKAGE_Systemd=ON
cmake --build "$build_root/krdp-build" -j2
cmake --install "$build_root/krdp-build"
