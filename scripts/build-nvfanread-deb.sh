#!/bin/sh
# Assemble the nvfanread DKMS .deb from the single source tree (nvfanread/) plus
# the package files (packaging/nvfanread/). The .deb ships source only; DKMS
# compiles and signs the module on the target at install time, so this builds on
# any architecture (no arm64 cross-compile needed).
#
# Usage: sh scripts/build-nvfanread-deb.sh [version] [outdir]
#   version  X.Y.Z to stamp into the package (default 0.0.0, the in-tree
#            placeholder). The release workflow passes the version of the
#            v* tag it is about to create.
#   outdir   where the .deb goes (default dist)
#
# The version is stamped into the staging copy only (control Version,
# version= in postinst/prerm, PACKAGE_VERSION in dkms.conf, MODULE_VERSION in
# nvfanread.c). This script never edits tracked files.
# `scripts/release.py verify` checks the result.
set -eu

cd "$(dirname "$0")/.."
name=nvfanread
ver="${1:-0.0.0}"
outdir="${2:-dist}"
pkgsrc="packaging/$name"

if ! printf '%s\n' "$ver" | grep -Eqx '(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'; then
	echo "build-nvfanread-deb: version must be X.Y.Z, got '$ver'" >&2
	exit 1
fi

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

# Control + maintainer scripts (scripts must be executable in the archive).
mkdir -p "$stage/DEBIAN"
cp "$pkgsrc/DEBIAN/control" "$stage/DEBIAN/control"
sed -i "s/^Version: .*/Version: $ver/" "$stage/DEBIAN/control"
for s in postinst prerm postrm; do
	cp "$pkgsrc/DEBIAN/$s" "$stage/DEBIAN/$s"
	sed -i "s/^version=.*/version=$ver/" "$stage/DEBIAN/$s"
	chmod 0755 "$stage/DEBIAN/$s"
done

# DKMS source at /usr/src/<name>-<ver> (single source of truth: nvfanread/).
src="$stage/usr/src/${name}-${ver}"
mkdir -p "$src"
cp nvfanread/nvfanread.c nvfanread/nvfanread_proto.h nvfanread/Makefile \
   nvfanread/dkms.conf "$src/"
sed -i "s/^PACKAGE_VERSION=\".*\"\$/PACKAGE_VERSION=\"$ver\"/" "$src/dkms.conf"
sed -i "s/^MODULE_VERSION(\".*\");\$/MODULE_VERSION(\"$ver\");/" "$src/nvfanread.c"

# Runtime config: boot auto-load + sensors labels.
mkdir -p "$stage/lib/modules-load.d" "$stage/etc/sensors.d"
cp "$pkgsrc/lib/modules-load.d/nvfanread.conf" "$stage/lib/modules-load.d/"
cp "$pkgsrc/etc/sensors.d/nvfanread.conf" "$stage/etc/sensors.d/"

mkdir -p "$outdir"
out="$outdir/${name}_${ver}_arm64.deb"
dpkg-deb --root-owner-group --build "$stage" "$out"
echo "build-nvfanread-deb: built $out"
