#!/bin/sh
# nvfanread and nvfancontrol lock the shared page with request_mem_region. The
# lock works only if both modules request the same address and size. Check
# that the two sources agree on the address, the size, the lock name and the
# retry bounds.
#
# Usage: sh scripts/check-mailbox-lock.sh [root]   (default root: the repo)
set -u

root="${1:-$(dirname "$0")/..}"
read_c="$root/nvfanread/nvfanread.c"
proto="$root/nvfanread/nvfanread_proto.h"
ctl_c="$root/nvfancontrol/usr/src/nvfancontrol/nvfancontrol.c"
status=0

# Print the value of "#define NAME VALUE" in a file, or nothing.
define_value() {
	awk -v name="$2" '$1 == "#define" && $2 == name { print $3; exit }' "$1" 2>/dev/null
}

# $1 = label, $2 = file A, $3 = name in A, $4 = file B, $5 = name in B
same() {
	a="$(define_value "$2" "$3")"
	b="$(define_value "$4" "$5")"
	if [ -z "$a" ] || [ -z "$b" ]; then
		echo "check-mailbox-lock: $1 is missing ($3='$a', $5='$b')" >&2
		status=1
	elif [ "$a" != "$b" ]; then
		echo "check-mailbox-lock: $1 differs: $3=$a, $5=$b" >&2
		status=1
	fi
}

same "shared-page address" "$read_c" ESPI_NS_SHM_PA "$ctl_c" ESPI_NS_SHM_PA
same "shared-page size" "$read_c" ESPI_NS_SHM_SIZE "$ctl_c" ESPI_NS_SHM_SIZE
same "lock name" "$proto" NVFR_MAILBOX_LOCK_NAME "$ctl_c" MAILBOX_LOCK_NAME
same "lock retry budget" "$proto" NVFR_LOCK_RETRY_BUDGET_US \
	"$ctl_c" LOCK_RETRY_BUDGET_US
same "lock attempt cap" "$proto" NVFR_LOCK_MAX_ATTEMPTS \
	"$ctl_c" LOCK_MAX_ATTEMPTS

# Both modules must actually take and release the region.
for f in "$read_c" "$ctl_c"; do
	for call in request_mem_region release_mem_region; do
		if ! grep -q "$call(ESPI_NS_SHM_PA, ESPI_NS_SHM_SIZE" "$f" 2>/dev/null; then
			echo "check-mailbox-lock: $f does not call $call on the shared page" >&2
			status=1
		fi
	done
done

if [ "$status" -eq 0 ]; then
	echo "check-mailbox-lock: both modules lock the same shared page"
fi
exit "$status"
