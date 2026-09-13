# NVIDIA DGX Spark fan control

Community reverse-engineering of the DGX Spark's fan control, reached through the
SoC's FF-A eSPI service and the embedded controller (EC) thermal mailbox. It
provides two independent kernel modules:

| Module | Direction | What it does |
|---|---|---|
| [`nvfancontrol`](nvfancontrol/) | **write** | Override the fans: force 100% or restore the EC's automatic thermal policy. |
| [`nvfanread`](nvfanread/) | **read-only** | Read fan telemetry from the EC without changing anything; intended to surface fan RPM through hwmon / `sensors` / node_exporter. |

Both bind the same verified FF-A partition (`arm-ffa-17`) but only one can be
bound at a time. `nvfanread` is the read-only tool — see
[`nvfanread/README.md`](nvfanread/README.md). Everything below documents the
shared EC/FF-A protocol.

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

That works today. `sensors` shows the same via `nvfanread-virtual-0`.

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

## `nvfancontrol` — override

Once loaded, the module binds `arm-ffa-17` and creates:

```text
/sys/bus/arm_ffa/devices/arm-ffa-17/fan
```

Writing `max` or `auto` sends one request each; reading the attribute returns
`ready`, `max`, `auto`, or `error <errno>`.

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

FF-A raw-eSPI partition:

```text
device:       /sys/bus/arm_ffa/devices/arm-ffa-17
UUID:         884a63a0-3285-4120-83aa-eec008a0a546
partition ID: 0x11
properties:   0x109
shared page:  0x933DD000, size 0x1000
```

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
