#!/bin/sh
# Build every static Debian package tree in the repo (a top-level directory
# with DEBIAN/control; today that is nvfancontrol/) into a .deb.
#
# Usage: sh scripts/build-debs.sh [version] [outdir]
#   version  X.Y.Z to stamp into the package (default 0.0.0, the in-tree
#            placeholder). The release workflow passes the version of the
#            v* tag it is about to create.
#   outdir   where the .deb goes (default dist)
#
# The script copies each tree to a temp staging dir and stamps the version
# there: DEBIAN/control Version, module_version='...' in the maintainer
# scripts, PACKAGE_VERSION in usr/src/*/dkms.conf, and every MODULE_VERSION
# in usr/src/*/*.c. It never edits tracked
# files. `scripts/release.py verify` checks the result.
#
# The packages are DKMS source packages: dpkg-deb just archives the tree, so
# this runs on any architecture (the module is compiled on the target at
# install time), and needs only dpkg-deb.
set -eu

cd "$(dirname "$0")/.."
ver="${1:-0.0.0}"
outdir="${2:-dist}"

if ! printf '%s\n' "$ver" | grep -Eqx '(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'; then
	echo "build-debs: version must be X.Y.Z, got '$ver'" >&2
	exit 1
fi

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$outdir"
found=0

for ctrl in */DEBIAN/control; do
	[ -f "$ctrl" ] || continue
	pkgdir="${ctrl%/DEBIAN/control}"
	name="$(awk -F': ' '/^Package:/ { print $2; exit }' "$ctrl")"
	arch="$(awk -F': ' '/^Architecture:/ { print $2; exit }' "$ctrl")"

	if [ -z "$name" ] || [ -z "$arch" ]; then
		echo "build-debs: $ctrl is missing Package/Architecture" >&2
		exit 1
	fi

	tree="$stage/$name"
	cp -a "$pkgdir" "$tree"

	sed -i "s/^Version: .*/Version: $ver/" "$tree/DEBIAN/control"
	for s in postinst prerm postrm; do
		[ -f "$tree/DEBIAN/$s" ] || continue
		sed -i "s/^module_version='.*'\$/module_version='$ver'/" "$tree/DEBIAN/$s"
	done
	for c in "$tree"/usr/src/*/*.c; do
		[ -f "$c" ] || continue
		sed -i "s/MODULE_VERSION(\"[^\"]*\")/MODULE_VERSION(\"$ver\")/g" "$c"
	done
	for conf in "$tree"/usr/src/*/dkms.conf; do
		[ -f "$conf" ] || continue
		sed -i "s/^PACKAGE_VERSION=\".*\"\$/PACKAGE_VERSION=\"$ver\"/" "$conf"
	done

	out="$outdir/${name}_${ver}_${arch}.deb"
	dpkg-deb --root-owner-group --build "$tree" "$out"
	echo "build-debs: built $out"
	found=1
done

if [ "$found" -eq 0 ]; then
	echo "build-debs: no Debian package trees (*/DEBIAN/control) found" >&2
	exit 1
fi
