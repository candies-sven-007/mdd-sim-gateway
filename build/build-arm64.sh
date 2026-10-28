#!/bin/bash
# Build the offline arm64 release for Debian 11 (bullseye) devices.
#
# Runs inside a debian:bullseye container on an arm64 machine (.github/workflows/build-arm64.yml),
# from the repository root, as root. Everything is compiled against bullseye's own libraries, so
# the result runs on the device with nothing but `apt install` of the runtime libraries it lists
# (payload/apt-packages.txt). Every download is pinned: tarballs by SHA-256, git sources by commit.
#
#   build/build-arm64.sh <version> <webui-dist-dir> <output-dir>
#
# Result in <output-dir>:
#   mdd-slim-arm64-<version>.tar.gz        install.sh + payload/ (+ SHA256SUMS of the payload)
#   mdd-slim-xray-arm64-<version>.tar.gz   bin/xray, optional (D4: Xray is shipped separately)
#   *.sha256
set -euo pipefail

VERSION=${1:?version}
WEBUI_DIST=$(readlink -f "${2:?webui dist}")
OUT=$(mkdir -p "${3:?output dir}" && readlink -f "$3")
REPO=$(pwd)
PREFIX=/opt/mdd/current          # the path every unit and compiled-in default uses
JOBS=$(nproc)
WORK=/tmp/mdd-build
mkdir -p "$WORK"

PCSC_VERSION=2.3.3
PCSC_SHA256=00b667aa71504ed1d39a48ad377de048c70dbe47229e8c48a3239ab62979c70f
CCID_VERSION=1.6.2
CCID_SHA256=6d5e6a6884090831ed155ee75cbc03aed252bd8158d94f507a94f05ebaba296c
PJPROJECT_REPOSITORY=https://github.com/MddIdd/pjproject-sysmocom-mirror.git
PJPROJECT_COMMIT=20537ab1e6beb2baf9ec15599e0217a601cf7ce5
ASTERISK_REPOSITORY=https://github.com/MddIdd/asterisk-sysmocom-mirror.git
ASTERISK_COMMIT=d231cb2c658545773fcd5eb9de787219b9ef6566
ASTERISK_CHERRY_PICK=f1b60dcd9568c4045512fd0d8b619b9fb91a7f35
LPAC_VERSION=2.3.0
LPAC_COMMIT=c2fcf5e4b21c712d54e35a11da2ad9ad134fb821
CMAKE_VERSION=3.31.12
CMAKE_SHA256=83f8fd91d2038a56556e1400390fcfe42f79602940c494f6c6f1cdae7f9e7f40
PBS_RELEASE=20260929
PBS_ASSET=cpython-3.12.14+${PBS_RELEASE}-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz
PBS_SHA256=706111f834ae1557314f48b7293ddc72cc96e952aa75c5d31f56c2b7ddeac3d3
SINGBOX_VERSION=1.13.15
SINGBOX_SHA256=f0810bbb5722ae36635687c421019defcc8b328d31a0b3c287901f331747ca93
XRAY_VERSION=26.3.27
XRAY_SHA256=4d30283ae614e3057f730f67cd088a42be6fdf91f8639d82cb69e48cde80413c
MESON_VERSION=1.6.1

log() { printf '\n==> %s\n' "$*"; }

fetch() {   # url destination sha256
  local attempt=1
  while ! curl -fsSL --retry 3 --connect-timeout 20 -o "$2" "$1"; do
    [ "$attempt" -ge 3 ] && { echo "download failed: $1" >&2; return 1; }
    attempt=$((attempt + 1)); sleep 5
  done
  echo "$3  $2" | sha256sum -c -
}

checkout() {   # repository destination commit
  rm -rf "$2"; git init -q "$2"; git -C "$2" remote add origin "$1"
  local attempt=1
  until git -C "$2" -c http.version=HTTP/1.1 fetch -q --depth=1 origin "$3"; do
    [ "$attempt" -ge 3 ] && return 1
    attempt=$((attempt + 1)); sleep 5
  done
  git -C "$2" checkout -q --detach FETCH_HEAD
  [ "$(git -C "$2" rev-parse HEAD)" = "$3" ]
}

[ "$(uname -m)" = aarch64 ] || { echo "build on aarch64" >&2; exit 1; }
grep -q 'VERSION_CODENAME=bullseye' /etc/os-release || { echo "build in debian:bullseye" >&2; exit 1; }

# ---------------------------------------------------------------------------------- toolchain
log "build dependencies"
export DEBIAN_FRONTEND=noninteractive
# bullseye-security now lives on archive.debian.org; deb.debian.org answers 404 for its pool.
for sources in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
  if [ -f "$sources" ]; then sed -i 's|deb.debian.org/debian-security|archive.debian.org/debian-security|g' "$sources"; fi
done
apt-get update -q
apt-get install -y -q --no-install-recommends \
  build-essential pkg-config autoconf automake libtool flex bison gettext patch perl \
  git curl wget ca-certificates xz-utils unzip file binutils python3 python3-pip ninja-build \
  swig libssl-dev libedit-dev uuid-dev libjansson-dev libxml2-dev libsqlite3-dev libsrtp2-dev \
  libcurl4-openssl-dev libunbound-dev libopencore-amrnb-dev libopencore-amrwb-dev \
  libvo-amrwbenc-dev libspeex-dev libspeexdsp-dev libogg-dev libvorbis-dev libgsm1-dev \
  libudev-dev libusb-1.0-0-dev zlib1g-dev subversion
python3 -m pip install -q "meson==${MESON_VERSION}"
export PATH="/usr/local/bin:$PATH"

rm -rf "${PREFIX:?}"; mkdir -p "$PREFIX"/{bin,sbin,lib}
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig"
export LD_LIBRARY_PATH="$PREFIX/lib:$PREFIX/asterisk/lib"

# --------------------------------------------------------------------------------- pcsc-lite
log "pcsc-lite $PCSC_VERSION"
cd "$WORK"
fetch "https://github.com/LudovicRousseau/PCSC/archive/refs/tags/${PCSC_VERSION}.tar.gz" pcsc.tgz "$PCSC_SHA256"
rm -rf "PCSC-$PCSC_VERSION"; tar xzf pcsc.tgz; cd "PCSC-$PCSC_VERSION"
meson setup build --prefix="$PREFIX" --libdir=lib --sbindir=sbin \
  -Dpolkit=false -Dlibsystemd=false -Dusbdropdir="$PREFIX/lib/pcsc/drivers" \
  -Dipcdir=/run/pcscd
ninja -C build && ninja -C build install

# --------------------------------------------------------------------------------------- ccid
log "ccid $CCID_VERSION + patches/ccid"
cd "$WORK"
fetch "https://github.com/LudovicRousseau/CCID/archive/refs/tags/${CCID_VERSION}.tar.gz" ccid.tgz "$CCID_SHA256"
rm -rf "CCID-$CCID_VERSION"; tar xzf ccid.tgz; cd "CCID-$CCID_VERSION"
for patch_file in "$REPO"/patches/ccid/*.patch; do echo "applying $(basename "$patch_file")"; patch -p1 < "$patch_file"; done
# The Alcor AK9563 (2ce3:9563) on the target device must be in the supported-reader table.
grep -qi '0x2ce3:0x9563' readers/supported_readers.txt || { echo "ccid lacks 2ce3:9563" >&2; exit 1; }
meson setup build --prefix="$PREFIX" --libdir=lib
ninja -C build && ninja -C build install
ls "$PREFIX/lib/pcsc/drivers/ifd-ccid.bundle/Contents/Linux/"

# ----------------------------------------------------------------------------------- asterisk
log "sysmocom pjproject + Asterisk"
cd "$WORK"
checkout "$PJPROJECT_REPOSITORY" pjproject "$PJPROJECT_COMMIT"
checkout "$ASTERISK_REPOSITORY" asterisk "$ASTERISK_COMMIT"
cd asterisk
git config user.email build@mdd.invalid; git config user.name "mdd build"
git -c http.version=HTTP/1.1 fetch -q --depth=2 origin "$ASTERISK_CHERRY_PICK"
git cherry-pick "$ASTERISK_CHERRY_PICK"
for patch_script in "$REPO"/engine/patches/asterisk/*.py; do echo "applying $(basename "$patch_script")"; python3 "$patch_script"; done
ln -s ../../../pjproject third-party/pjproject/source
sh bootstrap.sh
./configure --prefix="$PREFIX/asterisk" --sysconfdir=/etc --localstatedir=/var \
  --enable-binary-modules --with-pjproject-bundled
make menuselect/menuselect menuselect-tree menuselect.makeopts
# No prompts or music on hold are played; skipping them also keeps `make install` offline.
./menuselect/menuselect --enable codec_opus --disable BUILD_NATIVE \
  --disable-category MENUSELECT_CORE_SOUNDS --disable-category MENUSELECT_MOH \
  --disable-category MENUSELECT_EXTRA_SOUNDS
make -j"$JOBS"
rm -rf "${WORK:?}/ast-install"
make install DESTDIR="$WORK/ast-install"
cp -a "$WORK/ast-install$PREFIX/asterisk" "$PREFIX/"
# Asterisk's data directory (documentation/ is required at startup) ships read-only; the engine
# unit points astdatadir at it.
mkdir -p "$PREFIX/asterisk/var/lib"
cp -a "$WORK/ast-install/var/lib/asterisk" "$PREFIX/asterisk/var/lib/"
rm -rf "${PREFIX:?}/asterisk/var/lib/asterisk/moh" "${PREFIX:?}/asterisk/var/lib/asterisk/static-http"

log "asterisk modules: keep list"
cd "$PREFIX/asterisk/lib/asterisk/modules"
sed -e 's/#.*//' -e 's/[[:space:]]//g' -e '/^$/d' "$REPO/engine/asterisk-keep-modules.txt" | sort -u > "$WORK/keep.list"
missing=""
while IFS= read -r module; do [ -f "$module" ] || missing="$missing $module"; done < "$WORK/keep.list"
[ -z "$missing" ] || { echo "keep-list modules not built:$missing" >&2; exit 1; }
for module in *.so; do grep -qxF "$module" "$WORK/keep.list" || rm -f "$module"; done
[ "$(ls -1 *.so | wc -l)" -eq "$(wc -l < "$WORK/keep.list")" ]
strip "$PREFIX/asterisk/sbin/asterisk"
find "$PREFIX/asterisk/lib" -name '*.so*' -type f -exec strip --strip-unneeded {} +

# --------------------------------------------------------------------------------------- lpac
log "lpac $LPAC_VERSION + patches/lpac"
cd "$WORK"
fetch "https://github.com/Kitware/CMake/releases/download/v${CMAKE_VERSION}/cmake-${CMAKE_VERSION}-linux-aarch64.tar.gz" cmake.tgz "$CMAKE_SHA256"
rm -rf cmake; mkdir cmake; tar xzf cmake.tgz -C cmake --strip-components=1
checkout https://github.com/estkme-group/lpac.git lpac "$LPAC_COMMIT"
cd lpac
for patch_file in "$REPO"/patches/lpac/*.patch; do echo "applying $(basename "$patch_file")"; patch -p1 < "$patch_file"; done
"$WORK/cmake/bin/cmake" -S . -B build -DCMAKE_BUILD_TYPE=Release -DSTANDALONE_MODE=ON \
  -DLPAC_WITH_APDU_PCSC=ON -DLPAC_WITH_HTTP_CURL=ON -DLPAC_WITH_APDU_AT=OFF \
  -DLPAC_WITH_APDU_QMI=OFF -DLPAC_WITH_APDU_QMI_QRTR=OFF -DLPAC_WITH_APDU_UQMI=OFF \
  -DLPAC_WITH_APDU_MBIM=OFF -DLPAC_WITH_APDU_GBINDER=OFF \
  -DCMAKE_PREFIX_PATH="$PREFIX"
"$WORK/cmake/bin/cmake" --build build --parallel "$JOBS"
rm -rf "${WORK:?}/lpac-install"; DESTDIR="$WORK/lpac-install" "$WORK/cmake/bin/cmake" --install build
lpac_bin=$(find "$WORK/lpac-install" -type f -name lpac -perm -u+x | head -n1)
[ -n "$lpac_bin" ] || { echo "lpac binary not found" >&2; exit 1; }
mkdir -p "$PREFIX/lpac"
cp -a "$(dirname "$lpac_bin")"/. "$PREFIX/lpac/"
strip "$PREFIX/lpac/lpac" || true

# ------------------------------------------------------------------------------------- python
log "python $PBS_ASSET"
cd "$WORK"
fetch "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${PBS_ASSET//+/%2B}" python.tgz "$PBS_SHA256"
tar xzf python.tgz -C "$PREFIX"            # -> $PREFIX/python
ln -sfn python "$PREFIX/venv"              # the units name venv/bin/python
PY="$PREFIX/python/bin/python3"
# pyscard, if it has to be compiled, finds the pinned pcsc-lite here.
CFLAGS="-I$PREFIX/include/PCSC" LDFLAGS="-L$PREFIX/lib" \
  "$PY" -m pip install --no-cache-dir --no-warn-script-location -r "$REPO/packaging/requirements-runtime.txt"
"$PY" -m pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true

# ------------------------------------------------------------------------------ sing-box/xray
log "sing-box $SINGBOX_VERSION"
cd "$WORK"
fetch "https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/sing-box-${SINGBOX_VERSION}-linux-arm64.tar.gz" singbox.tgz "$SINGBOX_SHA256"
tar xzf singbox.tgz
install -m 0755 "sing-box-${SINGBOX_VERSION}-linux-arm64/sing-box" "$PREFIX/bin/sing-box"
log "xray $XRAY_VERSION (separate archive)"
fetch "https://github.com/XTLS/Xray-core/releases/download/v${XRAY_VERSION}/Xray-linux-arm64-v8a.zip" xray.zip "$XRAY_SHA256"
rm -rf xray-pkg; mkdir -p xray-pkg/bin; unzip -q -o xray.zip -d xray-unzip
install -m 0755 xray-unzip/xray xray-pkg/bin/xray
cp xray-unzip/LICENSE xray-pkg/LICENSE.xray 2>/dev/null || true

# ------------------------------------------------------------------------------ gateway files
log "gateway sources"
for item in control engine host packaging VERSION LICENSE NOTICE THIRD_PARTY_LICENSES.md; do
  cp -a "$REPO/$item" "$PREFIX/"
done
rm -rf "${PREFIX:?}/engine/patches"
echo "$VERSION" > "$PREFIX/VERSION"
mkdir -p "$PREFIX/webui"; cp -a "$WEBUI_DIST" "$PREFIX/webui/dist"
find "${PREFIX:?}" -name __pycache__ -prune -exec rm -rf {} +
chmod 0755 "$PREFIX/engine/entrypoint.sh" "$PREFIX/packaging/sbin/mdd-enginectl"
# ProtectSystem=strict keeps /opt read-only at runtime, so compile the bytecode now.
"$PY" -m compileall -q "$PREFIX/control" "$PREFIX/engine" "$PREFIX/host" >/dev/null
rm -rf "${PREFIX:?}/include" "${PREFIX:?}/lib/pkgconfig" "${PREFIX:?}/share/man" "${PREFIX:?}/share/doc"

# ---------------------------------------------------------------------------- runtime closure
log "runtime libraries -> apt-packages.txt"
{
  find "$PREFIX" -type f \( -name '*.so' -o -name '*.so.*' -o -perm -u+x \) -print0 |
    xargs -0 file | awk -F: '/ELF/ {print $1}' |
    while IFS= read -r binary; do ldd "$binary" 2>/dev/null || true; done |
    sed -n -e 's/^[[:space:]]*[^[:space:]]* => \(\/[^[:space:]]*\).*/\1/p' |
    sort -u | grep -v "^$PREFIX/" |
    while IFS= read -r library; do
      dpkg -S "$library" 2>/dev/null || dpkg -S "$(readlink -f "$library")" 2>/dev/null || true
    done | cut -d: -f1 | sort -u
  # Not visible to ldd: tools the scripts call.
  printf '%s\n' nftables iproute2 procps sudo ca-certificates kmod
} | sort -u > "$PREFIX/apt-packages.txt"
cat "$PREFIX/apt-packages.txt"

log "smoke tests"
unresolved=$(find "$PREFIX" -type f \( -name '*.so' -o -name '*.so.*' -o -perm -u+x \) -print0 |
  xargs -0 file | awk -F: '/ELF/ {print $1}' |
  while IFS= read -r binary; do ldd "$binary" 2>/dev/null | grep 'not found' | sed "s|^|$binary: |"; done)
[ -z "$unresolved" ] || { echo "$unresolved" >&2; exit 1; }
"$PREFIX/asterisk/sbin/asterisk" -V
"$PREFIX/sbin/pcscd" --version | head -n1
"$PREFIX/lpac/lpac" version || true
"$PREFIX/bin/sing-box" version | head -n1
"$PY" -c 'import Crypto, cryptography, jinja2, panoramisk, requests, serial, smartcard, socks, card, yaml, fastapi, uvicorn, websockets, multipart; print("python imports ok")'
(cd "$PREFIX/control" && MDD_DATA="$WORK/smoke-data" "$PY" -c 'import app.main; print("control imports ok")')
netns_status=0; "$PY" -I "$PREFIX/host/mdd_netns.py" up 0 2>/dev/null || netns_status=$?
[ "$netns_status" -eq 64 ]
sh -n "$PREFIX/engine/entrypoint.sh" "$PREFIX/packaging/sbin/mdd-enginectl"

cat > "$PREFIX/BUILDINFO" <<EOF
version=$VERSION
commit=$(git -C "$REPO" rev-parse HEAD)
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
pcsc-lite=$PCSC_VERSION ccid=$CCID_VERSION asterisk=$ASTERISK_COMMIT+$ASTERISK_CHERRY_PICK pjproject=$PJPROJECT_COMMIT
lpac=$LPAC_VERSION@$LPAC_COMMIT python=$PBS_ASSET sing-box=$SINGBOX_VERSION xray=$XRAY_VERSION
EOF

# ------------------------------------------------------------------------------------ package
log "package"
STAGE="$WORK/mdd-slim-$VERSION"
rm -rf "${STAGE:?}"; mkdir -p "$STAGE"
cp -a "$PREFIX" "$STAGE/payload"
install -m 0755 "$REPO/install.sh" "$STAGE/install.sh"
(cd "$STAGE" && find payload -type f -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
tar -C "$WORK" -czf "$OUT/mdd-slim-arm64-$VERSION.tar.gz" "mdd-slim-$VERSION"
tar -C "$WORK/xray-pkg" -czf "$OUT/mdd-slim-xray-arm64-$VERSION.tar.gz" .
(cd "$OUT" && for file in *.tar.gz; do sha256sum "$file" > "$file.sha256"; done; ls -la)
