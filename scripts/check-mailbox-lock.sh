#!/bin/sh
# nvfanread and nvfancontrol lock the shared page with request_mem_region. The
# lock works only if both modules request the same address and size, so pin
# those equal. Also check that both modules take and release the region, and
# that nvfancontrol waits long enough to outlast nvfanread's longest hold.
#
# Usage: sh scripts/check-mailbox-lock.sh [root]   (default root: the repo)
set -u

root="${1:-$(dirname "$0")/..}"
read_c="$root/nvfanread/nvfanread.c"
proto="$root/nvfanread/nvfanread_proto.h"
ctl_c="$root/nvfancontrol/usr/src/nvfancontrol/nvfancontrol.c"
hold_c="$root/tools/mailbox-hold/nvfanhold.c"
status=0

# Margin, in microseconds, on top of nvfanread's longest lock hold.
margin_us=2000000

# Print the value of "#define NAME VALUE" in a file, or nothing.
define_value() {
	awk -v name="$2" '$1 == "#define" && $2 == name { print $3; exit }' "$1" 2>/dev/null
}

# Print a C integer literal as plain decimal digits (drop U/L suffixes), or
# nothing if it is not a decimal literal.
decimal() {
	v="$(printf '%s' "$1" | sed 's/[uUlL]*$//')"
	case "$v" in
	'' | *[!0-9]*) ;;
	*) printf '%s\n' "$v" ;;
	esac
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

# The kernel matches the region by address and size: these must be equal.
same "shared-page address" "$read_c" ESPI_NS_SHM_PA "$ctl_c" ESPI_NS_SHM_PA
same "shared-page size" "$read_c" ESPI_NS_SHM_SIZE "$ctl_c" ESPI_NS_SHM_SIZE
same "test-tool address" "$read_c" ESPI_NS_SHM_PA "$hold_c" ESPI_NS_SHM_PA
same "test-tool size" "$read_c" ESPI_NS_SHM_SIZE "$hold_c" ESPI_NS_SHM_SIZE
# The kernel ignores the name; it only shows in /proc/iomem. Pinned so the
# entry reads the same whichever module holds the lock.
same "lock name" "$proto" NVFR_MAILBOX_LOCK_NAME "$ctl_c" MAILBOX_LOCK_NAME

# nvfancontrol's lock wait must outlast nvfanread's longest hold: its reply
# timeout plus its doorbell retry budget, plus a margin.
ctl_budget="$(decimal "$(define_value "$ctl_c" LOCK_RETRY_BUDGET_US)")"
read_reply_ms="$(decimal "$(define_value "$read_c" REPLY_TIMEOUT_MS)")"
read_send_us="$(decimal "$(define_value "$proto" NVFR_SEND_RETRY_BUDGET_US)")"
if [ -z "$ctl_budget" ] || [ -z "$read_reply_ms" ] || [ -z "$read_send_us" ]; then
	echo "check-mailbox-lock: cannot read LOCK_RETRY_BUDGET_US ('$ctl_budget'), REPLY_TIMEOUT_MS ('$read_reply_ms') or NVFR_SEND_RETRY_BUDGET_US ('$read_send_us')" >&2
	status=1
else
	need=$((read_reply_ms * 1000 + read_send_us + margin_us))
	if [ "$ctl_budget" -lt "$need" ]; then
		echo "check-mailbox-lock: nvfancontrol LOCK_RETRY_BUDGET_US=$ctl_budget is below nvfanread's longest hold plus margin ($need us)" >&2
		status=1
	fi
fi

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
