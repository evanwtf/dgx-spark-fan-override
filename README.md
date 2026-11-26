# NVIDIA DGX Spark fan control

Community reverse-engineering of the DGX Spark's fan control, reached through the
SoC's FF-A eSPI service and the embedded controller (EC) thermal mailbox. It
provides two independent kernel modules:

| Module | Direction | What it does |
|---|---|---|
| [`nvfancontrol`](nvfancontrol/) | **write** | Override the fans: force 100% or restore the EC's automatic thermal policy. |
| [`nvfanread`](nvfanread/) | **read-only** | Read fan telemetry from the EC without changing anything; intended to surface fan RPM through hwmon / `sensors` / node_exporter. |

Both talk to the same verified FF-A eSPI service (`arm-ffa-17`) through one
shared page. Neither module needs to bind `arm-ffa-17`. Each request takes a
mailbox lock on the shared page (the `nvfan-mailbox` iomem region), so the two
modules can stay loaded together and take turns. See
[`nvfanread/README.md`](nvfanread/README.md#one-mailbox-user-at-a-time) for how
this works on each kernel.

> **Install `nvfanread` and `nvfancontrol` from the same release.** Older
> builds do not take the mailbox lock: `nvfancontrol` 1.0.0, `nvfanread`
> 1.0.0, and the `nvfanread` build from
> [#17](https://github.com/evanwtf/dgx-spark-fan-override/pull/17). A build
> without the lock can write the shared page at the same time as the other
> module. `nvfancontrol` refuses to send (`error -11`) while it sees an
> `nvfanread` without the lock.

`nvfanread` is the read-only tool — see
[`nvfanread/README.md`](nvfanread/README.md). Everything below documents the
shared EC/FF-A protocol.

> **Kernel 7.0.0-1025 and later.** The built-in `nvidia-ffa-ec` driver now
> binds `arm-ffa-17` itself, and no other driver can take it over (see
> [#15](https://github.com/evanwtf/dgx-spark-fan-override/issues/15)).
> `nvfanread` and `nvfancontrol` work with this: they use the device without
> binding it (see
> [#16](https://github.com/evanwtf/dgx-spark-fan-override/issues/16)). The
> `nvfancontrol` build that binds as a driver (1.0.0) loads on these kernels
> but does not attach, and cannot use the mailbox.

### Motivation: fan RPM in Prometheus

`nvfanread` exists to get DGX Spark **fan speed into Prometheus via
node_exporter**. The Spark exposes no fan RPM to the OS out of the box
(`sensors` shows only temperatures), so there's nothing for monitoring to
scrape. `nvfanread` reads the EC's fan tachometer telemetry and presents it as a
standard **hwmon** device, which node_exporter's `hwmon` collector picks up
automatically — no exporter config needed:

```
node_hwmon_fan_rpm{chip="devices_arm_ffa_17",sensor="fan1"} 2700
node_hwmon_fan_rpm{chip="devices_arm_ffa_17",sensor="fan2"} 4050
```

The `chip` label comes from the FF-A device's sysfs path, so it depends on the
kernel:

| Kernel | FF-A device path | `chip` label |
|---|---|---|
| 6.17.0-1032, 7.0.0-1019 | `/sys/devices/arm-ffa-17` | `devices_arm_ffa_17` |
| 7.0.0-1025 and later | `/sys/devices/platform/arm-ffa/arm-ffa-17` | `arm_ffa_arm_ffa_17` |

To check it, run `readlink -f /sys/class/hwmon/hwmonN/device` for the
`nvfanread` hwmon device. For a query that works on every kernel, select by the
chip name instead of the label:

```promql
node_hwmon_fan_rpm * on(chip) group_left node_hwmon_chip_names{chip_name="nvfanread"}
```

`sensors` shows the same RPM as `nvfanread-virtual-0` on every kernel.

> **Unofficial and firmware-specific.** This is reverse-engineered, not an NVIDIA
> interface. The addresses and command numbers below were observed on EC firmware
> [`0x3000508`](https://fwupd.org/lvfs/devices/com.nvidia.dgx.spark.ec.firmware);
> a different EC version may behave differently. Use at your own risk.

## Safety notes (read before using `nvfancontrol`)

- **The EC override is sticky.** Writing `max` sets an EC override slot that
  **persists across module unload and even package removal** — unloading the
  module or uninstalling the package does *not* restore automatic control. You
  must explicitly write `auto` (EC `0xFFFF`) to hand control back to the EC.
  Restore `auto` *before* uninstalling, or the fans stay pinned.
- **The packaged systemd unit forces max at boot.** If you install and enable the
  `nvfancontrol` service it writes `max` on every boot. Don't enable it unless you
  want the fans pinned at full speed permanently.
- If you only want to *read* fan speed, use `nvfanread` and leave `nvfancontrol`
  uninstalled — the read path never writes an override slot.
- **The `fan` attribute moved.** It is now
  `/sys/devices/faux/nvfancontrol/fan` on every kernel. The build that bound
  the FF-A device put it at `/sys/bus/arm_ffa/devices/arm-ffa-17/fan`. Update
  any script that writes the old path.
- **Use matching versions.** Install `nvfanread` and `nvfancontrol` from the
  same release. A build without the mailbox lock can write the shared page
  while the other module uses it.
- **`error -11` means nothing was sent.** The EC keeps its previous setting,
  and a retry is safe. `sudo nvfancontrol` retries it 3 times, 1 s apart, then
  exits with status 75. Any other error means the request may have reached
  the EC, and its override state is unknown.
- **A timeout poisons the mailbox.** If the EC does not answer within 5 s, the
  module keeps the mailbox lock, leaves the shared page as it is, and refuses
  every later request with `error -131`. Unload that module to clear it. The
  EC may then need the cold power drain from
  [#10](https://github.com/evanwtf/dgx-spark-fan-override/issues/10).

## `nvfancontrol` — override

Once loaded, the module finds `arm-ffa-17` on the FF-A bus by UUID and holds a
reference to it. It does not bind the device. It creates:

```text
/sys/devices/faux/nvfancontrol/fan
```

Writing `max` or `auto` sends one request each; reading the attribute returns
`ready`, `max`, `auto`, or `error <errno>`. Use `sudo nvfancontrol max`,
`sudo nvfancontrol auto` or `sudo nvfancontrol status`; the helper loads the
module when needed.

Each request:

1. Refuses if a driver that binds `arm-ffa-17` and writes the shared page
   without the lock owns the device (the `nvfancontrol` build that binds, or
   the `ec-probe` tool). `nvidia-ffa-ec` and `nvfanread` are allowed.
2. Refuses if an `nvfanread` without the mailbox lock is loaded. Every
   `nvfanread` puts a hwmon device named `nvfanread` under `arm-ffa-17`; a
   build with the lock also exports the symbol `nvfanread_mailbox_lock_v1`.
3. Takes the `nvfan-mailbox` lock: `request_mem_region()` on the shared page
   `0x933dd000`, size `0x1000`. If another module holds it, it waits up to
   8 s. That is longer than the longest `nvfanread` request (5 s reply timeout
   plus 10 ms of doorbell retries). It then gives up with `-EAGAIN` and sends
   nothing.
4. Snapshots the page, writes the frame, rings the doorbell (and retries a busy
   doorbell for up to 10 ms), waits up to 5 s for the EC reply and restores the
   page.
5. Releases the lock.

`nvfanread` takes the same lock for each read, but waits only 50 ms for it.

The lock is held for one request only, so `nvfanread` and `nvfancontrol` can
stay loaded together on every kernel. While a request runs, `/proc/iomem`
shows `933dd000-933ddfff : nvfan-mailbox` under the `reserved` region. Each
module refuses to load if the shared page overlaps System RAM.

Error codes from a write to `fan` (the attribute then reads `error <errno>`):

| Errno | Meaning | Safe to retry? |
|---|---|---|
| `-11` (`EAGAIN`) | Nothing was sent: the lock was busy, an owner or `nvfanread` check refused, the mailbox was not idle, the FF-A partition stayed busy, or the page could not be mapped. | Yes |
| `-22` (`EINVAL`) | The input was not `max` or `auto`. Nothing was sent. | Fix the input |
| `-4` (`EINTR`) | A signal arrived while waiting for another write. Nothing was sent. | Yes |
| `-131` (`ENOTRECOVERABLE`) | An earlier request timed out. The mailbox is poisoned until the module is unloaded. | No |
| `-110` (`ETIMEDOUT`) | The EC did not answer within 5 s. The mailbox is now poisoned. | No |
| `-16` (`EBUSY`) | The EC answered "busy" (service status 10). | State unknown |
| `-5`, `-71`, `-117`, others | Transport failure, service error, reply mismatch, or failed page restore. | State unknown |

`nvfanread` uses the same codes for its reads: `-EAGAIN` means nothing was
sent.

```text
Full fan speed:   07 05 00 BC 34    # 0x34BC = 13500 RPM
Restore default:  07 05 00 FF FF    # 0xFFFF = override slot disabled
```

## Control link

```text
Linux kernel module
  -> ARM FF-A Direct Request 2
  -> arm-ffa-17 / eSPI Service
  -> OEM1 command 17 + ns_shm0 shared page
  -> eSPI Memory32 0x06000800 request region
  -> eSPI Memory32 0x06000504 doorbell/status
  -> EC outer service 0x07
  -> EC thermal mailbox inner command 5
  -> EC SRAM 0x119192 = 13500
  -> dual-fan target policy
  -> RPM-to-PWM conversion, clamped to 100%
  -> PWM0/TACH0 and PWM1/TACH1
```

FF-A raw-eSPI service:

```text
device:       /sys/bus/arm_ffa/devices/arm-ffa-17
UUID:         884a63a0-3285-4120-83aa-eec008a0a546
partition ID: 0x8003
properties:   0x109
shared page:  0x933DD000, size 0x1000
```

The partition ID is the FF-A endpoint ID (`partition_id` in sysfs). On kernel
7.0.0-1025 it reads `0x8003`, and the other EC services (thermal, fan, power and
more) share it: one secure partition serves many UUIDs. The `17` in `arm-ffa-17`
(`0x11`) is only the bus device index. Earlier versions of this README gave
`0x11` as the partition ID, which mixed up the two.

## EC fan-control firmware reference

### Hardware mapping and basic parameters

| Item | Firmware implementation |
|---|---|
| Number of fans | 2 |
| fan0 | PWM0 / TACH0 |
| fan1 | PWM1 / TACH1 |
| PWM upper limit | 100% |
| PWM frequency constant | 28000 (≈ 28 kHz) |
| Primary temperature inputs | Sensor IDs `0x4C`, `0x49`; policy uses the higher value |
| Default control mode | `0` (RPM mode) |
| fan0 RPM range | 1260–9000 RPM |
| fan1 RPM range | 1890–13500 RPM |
| Ramp-up rate | Max increase of 10% per control update |
| Ramp-down rate | Decrease of 1% per control update |
| Final PWM clamp | Max `0x64` (100%) |

### Static thermal control profiles

Profile A:

| Level | Temperature threshold | PWM | Step-down hysteresis |
|---:|---:|---:|---:|
| 0 | 30 °C | 30% | 10 °C |
| 1 | 80 °C | 40% | 30 °C |
| 2 | 90 °C | 54% | 20 °C |
| 3 | 95 °C | 75% | 10 °C |
| 4 | 101 °C | 100% | 15 °C |

Profile B:

| Level | Temperature threshold | PWM | Step-down hysteresis |
|---:|---:|---:|---:|
| 0 | 20 °C | 30% | 10 °C |
| 1 | 40 °C | 45% | 30 °C |
| 2 | 70 °C | 70% | 20 °C |
| 3 | 85 °C | 100% | 10 °C |
| 4 | 101 °C | 100% | 15 °C |

### Dynamic override slots

During EC initialization these two 16-bit slots are set to `0xFFFF` (disabled):

| Address | Commands | Function |
|---|---|---|
| `0x119190` | 2 (read), 3 (write) | Low-end override slot; caps the target (upper bound). |
| `0x119192` | 4 (read), 5 (write) | High-end override slot; raises the target floor (minimum baseline). |

`0xFFFF` means the slot is disabled. In RPM mode the slot value is converted to a
PWM percentage from each fan's RPM range, then combined with the temperature-curve
target; in percentage mode the low byte is used directly.

With the default `0x119190 = 0xFFFF`, writing `13500` to `0x119192` gives:

```text
fan0: 13500 > 9000  -> RPM conversion returns 100%
fan1: 13500 = 13500 -> RPM conversion returns 100%
final common PWM path then executes min(target, 100)
```

## EC thermal-control mailbox: inner commands

"EC command" here means the second byte (processed by EC function `0x000C3900`)
within outer service `0x07`. The generic frame header is:

```text
[0]   Sequence number / outer service ID (local unit uses 07)
[1]   Inner command
[2]   Status (00 for requests; 00 = success, FF = unsupported in replies)
[3..] Little-endian data or output
```

| Inner command | Direction | Function | Data / reply |
|---:|---|---|---|
| `0` | — | Unsupported | status = `FF` |
| `1` | read | Query capabilities, mode, and control ranges for both channels | 13-byte reply: capabilities, mode, fan0 min/max, fan1 min/max |
| `2` | read | Read low-end override slot `0x119190` | LE16 at `+3..4` |
| `3` | write | Write LE16 from `+3..4` to `0x119190` | status = `00`; input echoed |
| `4` | read | Read high-end override slot `0x119192` | LE16 at `+3..4` |
| `5` | write | Write LE16 from `+3..4` to `0x119192` | status = `00`; input echoed |
| `6` | — | Explicitly unsupported by the firmware jump table | status = `FF` |
| `7` | read | Thermal/fan telemetry snapshot | Copies 64 bytes from `0x1188E2` to reply `+3`; refreshes both channels' RPM/percentage status |
| other | — | Unsupported | status = `FF` |

Example reply to command 1:

```text
07 01 00 01 00 EC 04 28 23 62 07 BC 34
```

| Offset | Value | Meaning |
|---:|---|---|
| 0 | `07` | Sequence number |
| 1 | `01` | Command 1 |
| 2 | `00` | Success |
| 3 | `01` | capability = 1 |
| 4 | `00` | RPM mode |
| 5–6 | `EC 04` | fan0 min = 1260 |
| 7–8 | `28 23` | fan0 max = 9000 |
| 9–10 | `62 07` | fan1 min = 1890 |
| 11–12 | `BC 34` | fan1 max = 13500 |

If the mode is `1`, command 1 returns 14–100 ranges for both channels, indicating
PWM percentage mode.

## SoC eSPI service: OEM1 commands 1–18

These are the outer OEM1 debug/service commands for the FF-A secure partition,
distinct from the EC inner commands above. Only OEM1 command 17 wraps and executes
the full EC EMI mailbox flow.

| OEM1 command | Function | Use in this project |
|---:|---|---|
| `1` | Output POST code to I/O port `0x80` | Unused |
| `2` | eSPI flash sector erase | Unused; destructive |
| `3` | eSPI flash write | Unused; destructive |
| `4` | eSPI flash read | Unused |
| `5` | eSPI RPMC operation 1 | Unused; security/counter-related |
| `6` | eSPI RPMC operation 2 | Unused; security/counter-related |
| `7` | Single-byte I/O write | Recoverable marker only (dev history) |
| `8` | Single-byte I/O read | Status probing (dev history) |
| `9` | Single-byte Memory32 write | Marker; handler masks underlying failure |
| `10` | Single-byte Memory32 read | Zero value creates failure ambiguity |
| `11` | Variable-length Memory32 write | Marker; masks underlying failure |
| `12` | Variable-length Memory32 read | Zero value creates failure ambiguity |
| `13` | Variable-length Memory64 write | Unused |
| `14` | Variable-length Memory64 read | Unused |
| `15` | eSPI OOB write | Unused |
| `16` | Configure eSPI general I/O | Unused |
| `17` | Execute full generic EC EMI request via `ns_shm0` | **The only control entry point used** |
| `18` | Fixed read of two bytes from Memory32 `0x06000798`, checking bits 0/7/8/9/10/11 | Read-only diagnostics (dev history) |

OEM1 command 17 shared-page layout:

| Offset | Size | Meaning |
|---:|---:|---|
| `0x00` | 1 | Input length |
| `0x01` | 1 | Output length |
| `0x02` | 1 | EC output start offset |
| `0x03` | 1 | Accepted |
| `0x04` | 1 | Ready |
| `0x10` | N | Input data; overwritten by EC output on completion |

Service status:

| Status | Meaning |
|---:|---|
| `0` | Security service request completed |
| `5` | Underlying status read, request write, or doorbell write failed; ~54 ms usually indicates a controller completion timeout |
| `10` | EC mailbox status busy |

## Releases

Every merge to `main` publishes a
[GitHub Release](https://github.com/evanwtf/dgx-spark-fan-override/releases)
with both `.deb` packages (`nvfanread` and `nvfancontrol`) at one version.
Download the `.deb` files from the Releases page.

A label on the pull request sets the version bump:

| Label | Effect |
|---|---|
| `semver:patch` | Patch bump (the default when no label is set) |
| `semver:minor` | Minor bump |
| `semver:major` | Major bump |
| `skip-release` | The merge publishes nothing |

A merge that changes only `docs/` or top-level `*.md` files publishes nothing.

The workflow (`.github/workflows/release.yml`) owns the `v*` tags. Do not tag
by hand. The tag is the only record of the version: the tree carries the
placeholder `0.0.0`, and the build scripts stamp the release version into the
package. The first automated release is 1.1.0. It continues from 1.0.0, the
version built and deployed by hand before release automation.

## Credits

This project builds directly on prior community reverse-engineering:

- **[Z841973620/dgx-spark-fan-override](https://github.com/Z841973620/dgx-spark-fan-override)**
  — the upstream project (author `841973620`) that reverse-engineered the EC /
  FF-A fan-control protocol documented above (partition UUID, `ns_shm0` shared
  page, OEM1 command 17, the EC inner-command table, fan RPM ranges).
- **[mathieu-lacage/dgx-spark-fan-override](https://github.com/mathieu-lacage/dgx-spark-fan-override)**
  — a fork that added read-only EC telemetry and validated the live fan-RPM
  **decode offsets** (command-7 reply bytes 7 and 9) on real hardware. The
  `nvfanread` decode (`nvfr_snapshot_fan_rpm()`) uses exactly these offsets.

The kernel modules are GPL-2.0; see [`LICENSE`](LICENSE).
