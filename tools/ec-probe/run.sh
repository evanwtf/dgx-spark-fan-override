#!/bin/sh
# build + sign + load nvfanprobe with the given module params, read once, unload.
# usage: sh run.sh [ec_cmd=N] [in_len=N] [out_len=N] [out_off=N] [dump=N]
set -e
cd "$(dirname "$0")"
SIGN="/lib/modules/$(uname -r)/build/scripts/sign-file"
make -s >/dev/null
"$SIGN" sha256 "$HOME/.mok/nvfanread-mok.priv" "$HOME/.mok/nvfanread-mok.der" nvfanprobe.ko
grep -qE '^nvfanprobe ' /proc/modules && sudo rmmod nvfanprobe || true
grep -qE '^nvfanread '  /proc/modules && sudo rmmod nvfanread  || true
sudo insmod nvfanprobe.ko "$@"
echo "--- /sys/.../arm-ffa-17/probe (params: $*) ---"
cat /sys/bus/arm_ffa/devices/arm-ffa-17/probe || echo "(read returned error)"
sudo rmmod nvfanprobe
