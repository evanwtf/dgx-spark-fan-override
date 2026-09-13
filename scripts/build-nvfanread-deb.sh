#!/bin/sh
# Assemble the nvfanread DKMS .deb from the single source tree (nvfanread/) plus
# the package files (packaging/nvfanread/). The .deb ships source only; DKMS
# compiles and signs the module on the target at install time, so this builds on
# any architecture (no arm64 cross-compile needed).
#
# Usage: sh scripts/build-nvfanread-deb.sh [outdir]   (default outdir: dist)
# If $1 (a version) is given, it must match the control Version (release gate).
set -eu

cd "$(dirname "$0")/.."
name=nvfanread
ver=1.0.0
pkgsrc="packaging/$name"
outdir="dist"

# Optional version gate (release tag, leading v already stripped by the caller).
want="${1:-}"
case "$want" in
	"" ) : ;;
	*[!0-9.]* ) outdir="$want" ;;             # non-version arg = outdir
	* ) [ "$want" = "$ver" ] || { echo "build-nvfanread-deb: version gate failed: $want != $ver" >&2; exit 1; } ;;
esac
[ "${2:-}" != "" ] && outdir="$2"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

# Control + maintainer scripts (scripts must be executable in the archive).
mkdir -p "$stage/DEBIAN"
cp "$pkgsrc/DEBIAN/control" "$stage/DEBIAN/control"
for s in postinst prerm postrm; do
	cp "$pkgsrc/DEBIAN/$s" "$stage/DEBIAN/$s"
	chmod 0755 "$stage/DEBIAN/$s"
done

# DKMS source at /usr/src/<name>-<ver> (single source of truth: nvfanread/).
src="$stage/usr/src/${name}-${ver}"
mkdir -p "$src"
cp nvfanread/nvfanread.c nvfanread/nvfanread_proto.h nvfanread/Makefile \
   nvfanread/dkms.conf nvfanread/dkms-sign.sh "$src/"
chmod 0755 "$src/dkms-sign.sh"

# Runtime config: boot auto-load + sensors labels.
mkdir -p "$stage/lib/modules-load.d" "$stage/etc/sensors.d"
cp "$pkgsrc/lib/modules-load.d/nvfanread.conf" "$stage/lib/modules-load.d/"
cp "$pkgsrc/etc/sensors.d/nvfanread.conf" "$stage/etc/sensors.d/"

mkdir -p "$outdir"
out="$outdir/${name}_${ver}_arm64.deb"
dpkg-deb --root-owner-group --build "$stage" "$out"
echo "build-nvfanread-deb: built $out"
