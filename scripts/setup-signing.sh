#!/bin/sh
# One-time signing setup so DKMS builds of nvfanread are signed with YOUR
# enrolled Machine-Owner Key (required to load the module under Secure Boot).
#
# It installs the key/cert under /var/lib/nvfanread/mok and writes a DKMS
# framework drop-in pointing DKMS's module signing at them. Run as root BEFORE
# installing the .deb (or before `dkms install`).
#
# Usage: sudo scripts/setup-signing.sh <mok.priv> <mok.der>
#
# The certificate (mok.der) must already be enrolled in the firmware
# (`mokutil --list-enrolled`); see docs/secure-boot-signing.md.
set -eu

priv="${1:?usage: setup-signing.sh <mok.priv> <mok.der>}"
cert="${2:?usage: setup-signing.sh <mok.priv> <mok.der>}"

if [ ! -r "$priv" ] || [ ! -r "$cert" ]; then
	echo "setup-signing: key/cert not readable: $priv / $cert" >&2
	exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
	echo "setup-signing: run as root (sudo)" >&2
	exit 1
fi

install -d -m 0700 /var/lib/nvfanread/mok
install -m 0600 "$priv" /var/lib/nvfanread/mok/mok.priv
install -m 0644 "$cert" /var/lib/nvfanread/mok/mok.der

install -d -m 0755 /etc/dkms/framework.conf.d
cat > /etc/dkms/framework.conf.d/nvfanread-mok.conf <<'EOF'
# Sign DKMS-built modules with the nvfanread MOK. The certificate must be
# enrolled in the firmware (mokutil) or signed modules still will not load.
mok_signing_key="/var/lib/nvfanread/mok/mok.priv"
mok_certificate="/var/lib/nvfanread/mok/mok.der"
EOF

echo "setup-signing: DKMS will sign with /var/lib/nvfanread/mok/mok.der"
if command -v mokutil >/dev/null 2>&1 &&
   ! mokutil --list-enrolled 2>/dev/null | grep -q "$(openssl x509 -inform der -in "$cert" -noout -subject 2>/dev/null | sed 's/^subject=//')"; then
	echo "setup-signing: NOTE — verify this cert is enrolled: mokutil --list-enrolled" >&2
fi
