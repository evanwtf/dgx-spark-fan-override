#!/bin/sh
# Check that every place that declares the nvfanread version agrees.
#
# Usage: sh scripts/check-versions.sh [root]   (default root: the repo)
# Exit 0 if all versions are equal, 1 if they differ or one is missing.
set -u

root="${1:-$(dirname "$0")/..}"

get() {
	# $1 = label, $2 = file, $3 = sed expression that prints the version
	v="$(sed -n "$3" "$root/$2" 2>/dev/null | head -n 1)"
	if [ -z "$v" ]; then
		echo "check-versions: no version found in $2 ($1)" >&2
		echo "MISSING"
		return
	fi
	echo "$v"
}

control="$(get control packaging/nvfanread/DEBIAN/control 's/^Version: *//p')"
dkms="$(get dkms nvfanread/dkms.conf 's/^PACKAGE_VERSION="\(.*\)"$/\1/p')"
postinst="$(get postinst packaging/nvfanread/DEBIAN/postinst 's/^version=//p')"
prerm="$(get prerm packaging/nvfanread/DEBIAN/prerm 's/^version=//p')"
build="$(get build scripts/build-nvfanread-deb.sh 's/^ver=//p')"
module="$(get module nvfanread/nvfanread.c 's/^MODULE_VERSION("\(.*\)");$/\1/p')"
readme_deb="$(get readme-deb nvfanread/README.md 's/.*nvfanread_\([0-9][0-9.]*\)_arm64\.deb.*/\1/p')"
status_deb="$(get status-deb docs/STATUS.md 's/.*nvfanread_\([0-9][0-9.]*\)_arm64\.deb.*/\1/p')"

status=0
for pair in "dkms.conf:$dkms" "postinst:$postinst" "prerm:$prerm" \
	    "build-nvfanread-deb.sh:$build" "MODULE_VERSION:$module" \
	    "nvfanread/README.md .deb:$readme_deb" "docs/STATUS.md .deb:$status_deb"; do
	name="${pair%%:*}"
	v="${pair#*:}"
	if [ "$v" != "$control" ]; then
		echo "check-versions: $name has $v, DEBIAN/control has $control" >&2
		status=1
	fi
done
if [ "$control" = "MISSING" ]; then
	status=1
fi

if [ "$status" -eq 0 ]; then
	echo "check-versions: nvfanread version $control is consistent"
fi
exit "$status"
