#!/bin/sh
# Build every Debian package tree in the repo (a directory containing
# DEBIAN/control) into dist/. If a version is passed as $1 (e.g. a release tag
# with the leading "v" stripped), every package's control Version must match it
# or the build fails — this is the release version gate.
#
# The packages are DKMS source packages: dpkg-deb just archives the tree, so
# this runs on any architecture (the module is compiled on the target at
# install time), and needs only dpkg-deb.
set -eu

cd "$(dirname "$0")/.."
want="${1:-}"
mkdir -p dist
found=0

for ctrl in */DEBIAN/control; do
	[ -f "$ctrl" ] || continue
	pkgdir="${ctrl%/DEBIAN/control}"
	name="$(awk -F': ' '/^Package:/ { print $2; exit }' "$ctrl")"
	ver="$(awk -F': ' '/^Version:/ { print $2; exit }' "$ctrl")"
	arch="$(awk -F': ' '/^Architecture:/ { print $2; exit }' "$ctrl")"

	if [ -z "$name" ] || [ -z "$ver" ] || [ -z "$arch" ]; then
		echo "build-debs: $ctrl is missing Package/Version/Architecture" >&2
		exit 1
	fi
	if [ -n "$want" ] && [ "$want" != "$ver" ]; then
		echo "build-debs: version gate failed: tag $want != $name control Version $ver" >&2
		exit 1
	fi

	out="dist/${name}_${ver}_${arch}.deb"
	dpkg-deb --root-owner-group --build "$pkgdir" "$out"
	echo "build-debs: built $out"
	found=1
done

if [ "$found" -eq 0 ]; then
	echo "build-debs: no Debian package trees (*/DEBIAN/control) found" >&2
	exit 1
fi
