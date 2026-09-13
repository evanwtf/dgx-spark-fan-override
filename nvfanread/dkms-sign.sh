#!/bin/sh
# DKMS POST_BUILD hook: sign the freshly built nvfanread.ko with the machine's
# MOK so it loads under Secure Boot. Runs after every build (install and
# kernel-update autoinstall), with cwd = the DKMS build dir.
#
# Key/cert come from /etc/nvfanread/signing.conf, defaulting to
# /var/lib/nvfanread/mok/. If no usable key is present it WARNS and leaves the
# module unsigned (which simply won't load under Secure Boot) rather than
# failing the build.
set -eu

conf=/etc/nvfanread/signing.conf
# shellcheck source=/dev/null
[ -r "$conf" ] && . "$conf"

key="${NVFANREAD_MOK_KEY:-/var/lib/nvfanread/mok/mok.priv}"
cert="${NVFANREAD_MOK_CERT:-/var/lib/nvfanread/mok/mok.der}"
kver="${kernelver:-$(uname -r)}"
sign="/lib/modules/${kver}/build/scripts/sign-file"
ko=./nvfanread.ko

if [ ! -f "$ko" ]; then
	echo "nvfanread dkms-sign: $ko not found in $(pwd); nothing to sign" >&2
	exit 0
fi
if [ ! -r "$key" ] || [ ! -r "$cert" ]; then
	echo "nvfanread dkms-sign: MOK key/cert not readable ($key / $cert);" \
	     "leaving module UNSIGNED (it will not load under Secure Boot)" >&2
	exit 0
fi
if [ ! -x "$sign" ]; then
	echo "nvfanread dkms-sign: sign-file not found at $sign; leaving module unsigned" >&2
	exit 0
fi

"$sign" sha256 "$key" "$cert" "$ko"
echo "nvfanread dkms-sign: signed $ko for ${kver} with $cert"
