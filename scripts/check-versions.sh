#!/bin/sh
# Check that every in-tree version declaration is the placeholder 0.0.0.
#
# The released version lives only in the v* git tag. The build scripts stamp
# it into a staging copy at build time (see README "Releases"), so the tree
# itself must carry the placeholder everywhere. A hand bump in one file would
# otherwise ship a .deb whose parts disagree.
#
# Built artifacts are checked by `scripts/release.py verify`, not here.
#
# Usage: sh scripts/check-versions.sh [root]   (default root: the repo)
# Exit 0 if all declarations are 0.0.0, 1 if one differs or is missing.
set -u

root="${1:-$(dirname "$0")/..}"
want=0.0.0
status=0

check() {
	# $1 = file, $2 = sed expression that prints the version
	v="$(sed -n "$2" "$root/$1" 2>/dev/null | head -n 1)"
	if [ -z "$v" ]; then
		echo "check-versions: no version found in $1" >&2
		status=1
	elif [ "$v" != "$want" ]; then
		echo "check-versions: $1 has $v, want the placeholder $want" >&2
		status=1
	fi
}

# nvfanread
check packaging/nvfanread/DEBIAN/control 's/^Version: *//p'
check packaging/nvfanread/DEBIAN/postinst 's/^version=//p'
check packaging/nvfanread/DEBIAN/prerm 's/^version=//p'
check nvfanread/dkms.conf 's/^PACKAGE_VERSION="\(.*\)"$/\1/p'
check nvfanread/nvfanread.c 's/^MODULE_VERSION("\(.*\)");$/\1/p'
check scripts/build-nvfanread-deb.sh 's/^ver="\${1:-\(.*\)}"$/\1/p'

# nvfancontrol
check nvfancontrol/DEBIAN/control 's/^Version: *//p'
check nvfancontrol/DEBIAN/postinst "s/^module_version='\\(.*\\)'\$/\\1/p"
check nvfancontrol/DEBIAN/prerm "s/^module_version='\\(.*\\)'\$/\\1/p"
check nvfancontrol/DEBIAN/postrm "s/^module_version='\\(.*\\)'\$/\\1/p"
check nvfancontrol/usr/src/nvfancontrol/dkms.conf 's/^PACKAGE_VERSION="\(.*\)"$/\1/p'
check scripts/build-debs.sh 's/^ver="\${1:-\(.*\)}"$/\1/p'

if [ "$status" -eq 0 ]; then
	echo "check-versions: every in-tree version is the placeholder $want"
fi
exit "$status"
