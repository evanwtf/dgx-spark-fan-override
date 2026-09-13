# Building, signing, and loading the module under Secure Boot (DGX Spark)

This is the runbook for getting a self-built kernel module (`nvfanread`, and by
the same steps `nvfancontrol`) to load on the DGX Spark, which ships with Secure
Boot enabled. It is a **living document** — the build/sign steps are verified,
but the one-time MOK-enrollment UI steps have **not yet been done end-to-end** on
this machine and will likely need small corrections on first attempt.

> ⚠️ Status: **not yet completed on hardware.** Loading is currently blocked
> until the MOK is enrolled (needs a supervised reboot with a monitor). Iterate
> this doc as you go. Tracked in issue #9.

> 🔌 **Run this standalone — no live assistant.** The Claude sessions on this
> machine run *on the DGX itself* and terminate the instant it reboots, so you
> get no help during MokManager. Before you start: make sure this doc is pushed
> to GitHub and keep it open on your **phone or Mac**. If you want a live
> assistant through the reboot, drive it from a Claude session on your **Mac**
> (it survives the DGX reboot and can read this doc from the repo) — not one on
> the Spark. The peer benchmark session on this box also dies on the reboot, so
> coordinate/accept that loss first.

## Why this is needed

The box enforces kernel-module signatures, so an unsigned `insmod` is rejected:

```
insmod: ERROR: could not insert module ...: Key was rejected by service
dmesg:  Loading of unsigned module is rejected
```

Under Secure Boot the kernel loads only modules signed by a trusted key. The
NVIDIA modules load because they're signed by *Canonical's* built-in key — not
something we can use. So we sign our module with our own **Machine Owner Key
(MOK)** and enroll it once.

## Verified environment (as of 2026-09-13, kernel `6.17.0-1032-nvidia` aarch64)

| Fact | Value |
|---|---|
| Secure Boot | **enabled** (`mokutil --sb-state`) |
| Module sig enforcement | `/sys/module/module/parameters/sig_enforce` = **Y** |
| Kernel lockdown | **integrity** |
| BMC / IPMI / remote KVM | **none** (no `/dev/ipmi*`, no IPMI DMI record) |
| Display / input | **HDMI** + **3× USB-C** (no USB-A); normally headless |
| shim / MokManager | `shimaa64.efi` + `mmaa64.efi` present in `/boot/efi/EFI/ubuntu/` |
| Existing user MOK | **none enrolled**; no key at `/var/lib/shim-signed/mok/` |
| Firmware-setup-on-reboot | supported (`OsIndicationsSupported` bit0 set) |

**Consequence:** enrollment cannot be done headlessly or over SSH — it requires
a **physical HDMI monitor + a wired USB-C keyboard** for one supervised reboot.
After enrollment the machine is headless again permanently.

## The MOK key

- **On the box:** `~/.mok/nvfanread-mok.priv` (PEM private key, `chmod 600`) and
  `~/.mok/nvfanread-mok.der` (DER public cert). This is where signing happens —
  **do not delete it, never commit it** (the repo `.gitignore` blocks key
  material as defense-in-depth).
- **Backup:** 1Password → "Code Secrets" vault → item *"nvfanread MOK — DGX Spark
  module signing"*.
- **Public-key fingerprint:** `4F:9E:7D:1D:9A:77:56:56:9E:CE:05:DA:EC:7E:3E:76:BD:B1:D8:16`

To regenerate from scratch (DR only — this invalidates the enrolled key and
requires re-enrollment):

```sh
mkdir -p ~/.mok && chmod 700 ~/.mok
openssl req -new -x509 -newkey rsa:2048 -nodes -days 36500 \
  -keyout ~/.mok/nvfanread-mok.priv -outform DER -out ~/.mok/nvfanread-mok.der \
  -subj "/CN=nvfanread module signing (evan MOK)/"
chmod 600 ~/.mok/nvfanread-mok.priv
```

## Step 1 — Build (over SSH, no root)

```sh
cd ~/git/dgx-spark-fan-override/nvfanread
make            # builds nvfanread.ko against the running kernel's headers
```

Verify the vermagic matches the running kernel:

```sh
modinfo -F vermagic ./nvfanread.ko   # must match `uname -r` ... aarch64
```

## Step 2 — Sign (over SSH, no root; repeat after every rebuild)

```sh
SIGN="/lib/modules/$(uname -r)/build/scripts/sign-file"
"$SIGN" sha256 ~/.mok/nvfanread-mok.priv ~/.mok/nvfanread-mok.der ./nvfanread.ko
modinfo ./nvfanread.ko | grep -E 'signer|sig_hashalgo'
# expect: signer: nvfanread module signing (evan MOK) / sig_hashalgo: sha256
```

Signing must be redone every time the `.ko` is rebuilt (e.g., after a kernel
update). Enrollment (Step 3) is one-time only. Automating this via DKMS is
issue #6.

## Step 3 — Enroll the MOK (ONE TIME, needs monitor + keyboard)

This reboots the machine. **Coordinate first:** a reboot ends any in-flight GPU
work (e.g., a running benchmark), and this box has had hard OOM lockups today, so
do it during a GPU-idle window with someone at the machine.

1. Stage the key (over SSH). It will prompt for a throwaway password (type it
   twice; you only need it for the next few minutes):
   ```sh
   sudo mokutil --import ~/.mok/nvfanread-mok.der
   sudo mokutil --list-new    # confirm "nvfanread module signing (evan MOK)" is staged
   ```
2. Physically attach an **HDMI monitor** and a **wired USB-C keyboard** (or a
   USB-C→USB-A adapter for a wired keyboard). Avoid wireless/gaming keyboards —
   their init can lag past the UEFI timeout.
3. Reboot:
   ```sh
   sudo reboot
   ```
4. On the monitor, the blue **MokManager** ("Perform MOK management") screen
   appears. **Press a key within ~10 s** or it boots normally without enrolling.
   Then: **Enroll MOK → View key 0** (verify it's ours) **→ Continue → Yes →**
   enter the password from step 1 **→ Reboot**.
5. Back headless, over SSH, confirm enrollment:
   ```sh
   mokutil --list-enrolled | grep -i nvfanread
   ```
6. Unplug the monitor/keyboard — the machine is headless again for good.

> Do **not** disable Secure Boot as an alternative: it also needs the monitor,
> and it drops `lockdown` to `none` (opens `/dev/mem`, kexec, unsigned modules)
> — strictly worse posture than trusting only our own key.

### If you forget the one-time password (or enrollment doesn't take)

The one-time password only gates the MokManager confirmation — it is **not** the
MOK private key and **not** stored in 1Password. If you forget it, or MokManager
times out / you cancel, the certificate is left **pending** (unenrolled) with the
old password, and a plain reboot will just ask for that same forgotten password.
Clear the stale pending request and re-stage with a new password:

```sh
# 1. Check state
sudo mokutil --list-enrolled | grep -i nvfanread   # empty  => not enrolled
sudo mokutil --list-new       | grep -i nvfanread   # non-empty => a stale pending request

# 2. Cancel the stale pending request — no password needed (root clears MokNew)
sudo mokutil --revoke-import

# 3. Confirm it's gone
sudo mokutil --list-new                             # should show nothing pending

# 4. Re-stage with a NEW password (WRITE IT DOWN), confirm, and reboot
sudo mokutil --import ~/.mok/nvfanread-mok.der       # type the new password twice
sudo mokutil --list-new                             # confirms "CN=nvfanread module signing (evan MOK)"
sudo reboot
# → MokManager: press a key → Enroll MOK → View key 0 → Continue → Yes → NEW password → Reboot
```

`mokutil --revoke-import` clears the pending request as root without the old
password, so a forgotten password is fully recoverable — nothing is lost, and the
signing key is untouched.

## Step 4 — Load and verify

```sh
sudo insmod ~/git/dgx-spark-fan-override/nvfanread/nvfanread.ko
cat /sys/bus/arm_ffa/devices/arm-ffa-17/telemetry   # the payoff: the fan snapshot
sudo dmesg | grep -i nvfanread | tail
sudo rmmod nvfanread                                 # when done
```

If load still fails with "Key was rejected by service" *after* enrollment, the
signing key and the enrolled key don't match — re-check `modinfo ... signer` vs
`mokutil --list-enrolled`.

## Persistence (later)

Once loading is proven, auto-load at boot and package with DKMS auto-signing so
rebuilds are signed transparently — issue #6. Until then, `insmod` manually (and
re-sign after any rebuild).

## Known gotchas / to confirm on first attempt

- MokManager rendering over HDMI and USB-C keyboard input are **expected to work
  but unconfirmed** on this box — verify on first attempt and update this doc.
- The exact MokManager menu wording/order may differ slightly from the above.
- `sudo systemctl reboot --firmware-setup` drops into UEFI setup on next boot
  (supported here) — only relevant if you ever need firmware setup; it still
  needs the monitor.
