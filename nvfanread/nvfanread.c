// SPDX-License-Identifier: GPL-2.0-only
/*
 * DGX Spark read-only EC fan telemetry probe.
 *
 * This module is deliberately READ-ONLY. It binds the verified FF-A partition
 * and creates:
 *
 *   /sys/bus/arm_ffa/devices/arm-ffa-17/telemetry
 *
 * Reading that attribute issues a single OEM1 command 17 carrying EC inner
 * command 7 ("thermal control / fan operation telemetry snapshot"), which the
 * EC firmware documents as a pure read: it copies 64 bytes from EC SRAM
 * 0x1188E2 into the reply and touches NEITHER fan-override slot (0x119190 /
 * 0x119192 are written only by EC commands 3 and 5, which this module never
 * issues). The attribute returns the raw 64 bytes as hex, plus a list of
 * little-endian u16 values that fall inside the known fan RPM ranges, so the
 * current-RPM offsets can be decoded and later surfaced through hwmon.
 *
 * The pure protocol logic lives in nvfanread_proto.h and is unit-tested in
 * userspace; this file adds the FF-A / shared-page I/O around it. The mailbox
 * handshake mirrors the override driver: it refuses a non-idle mailbox,
 * snapshots the shared page, and restores it afterwards. Requests are
 * serialized under a mutex.
 */

#include <linux/arm_ffa.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/errno.h>
#include <linux/hwmon.h>
#include <linux/io.h>
#include <linux/kernel.h>
#include <linux/ktime.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/pfn.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/sysfs.h>
#include <linux/types.h>
#include <linux/unaligned.h>
#include <linux/uuid.h>

#include "nvfanread_proto.h"

#define ESPI_OEM_GENERIC_EMI       17U

#define ESPI_NS_SHM_PA             0x933dd000ULL
#define ESPI_NS_SHM_SIZE           0x1000U


#define REPLY_TIMEOUT_MS           5000U
#define REPLY_POLL_MS              10U
#define ESPI_TIMEOUT_FLOOR_US      40000LL

/*
 * Reuse a telemetry snapshot this fresh so reading fan1 then fan2 (as sensors
 * and node_exporter do) costs one EC transaction, not two.
 */
#define NVFANREAD_CACHE_MS         200U

struct nvfanread_state {
	struct ffa_device *fdev;
	struct mutex request_lock;
	u8 cache[NVFR_TELEMETRY_LEN];
	ktime_t cache_time;
	bool cache_valid;
};

static void restore_shared_page(struct device *dev, u8 *shm,
				const u8 snapshot[NVFR_SNAP_LEN])
{
	memcpy(shm, snapshot, NVFR_SNAP_LEN);
	/* Ensure the restore write lands in the shared page before we verify it. */
	mb();

	if (memcmp(shm, snapshot, NVFR_SNAP_LEN))
		dev_crit(dev,
			 "SHARED-BUFFER RESTORE VERIFY FAILED at physical address %#llx\n",
			 ESPI_NS_SHM_PA);
}

/*
 * Issue EC command 7 and copy the 64-byte telemetry snapshot into @out.
 * Returns 0 on success. Never writes an override slot.
 */
static int read_telemetry(struct nvfanread_state *state,
			  u8 out[NVFR_TELEMETRY_LEN])
{
	struct ffa_device *fdev = state->fdev;
	struct ffa_send_direct_data2 msg = {};
	u8 snapshot[NVFR_SNAP_LEN];
	u8 frame[NVFR_SNAP_LEN];
	u8 *payload = (u8 *)msg.data;
	u8 *shm;
	unsigned long pfn = PHYS_PFN(ESPI_NS_SHM_PA);
	u32 service_status;
	bool map_memory;
	bool reserved_page = false;
	unsigned int elapsed;
	ktime_t start;
	s64 elapsed_us;
	int ret;

	if (nvfr_build_request_frame(frame, sizeof(frame))) {
		dev_err(&fdev->dev, "internal error: request frame too small\n");
		return -EINVAL;
	}

	map_memory = pfn_is_map_memory(pfn);
	if (map_memory)
		reserved_page = PageReserved(pfn_to_page(pfn));

	if (map_memory && !reserved_page) {
		dev_err(&fdev->dev,
			"refusing ns_shm0: PFN is Linux map memory but is not marked reserved\n");
		return -EPERM;
	}

	shm = memremap(ESPI_NS_SHM_PA, ESPI_NS_SHM_SIZE, MEMREMAP_WB);
	if (!shm) {
		dev_err(&fdev->dev, "memremap of manifest ns_shm0 failed\n");
		return -ENOMEM;
	}

	memcpy(snapshot, shm, sizeof(snapshot));

	if (!nvfr_mailbox_idle(snapshot[NVFR_FRAME_INPUT_ACCEPTED],
			       snapshot[NVFR_FRAME_OUTPUT_READY])) {
		dev_err(&fdev->dev,
			"refusing request: shared mailbox is not idle (accepted=%#04x ready=%#04x)\n",
			snapshot[NVFR_FRAME_INPUT_ACCEPTED],
			snapshot[NVFR_FRAME_OUTPUT_READY]);
		ret = -EBUSY;
		goto out_unmap;
	}

	memcpy(shm, frame, sizeof(frame));
	/* Publish the request frame to the shared page before the doorbell send. */
	mb();

	payload[0] = ESPI_OEM_GENERIC_EMI;
	start = ktime_get();
	ret = fdev->ops->msg_ops->sync_send_receive2(fdev, &msg);
	elapsed_us = ktime_us_delta(ktime_get(), start);
	if (ret) {
		dev_crit(&fdev->dev,
			 "FF-A TRANSPORT FAILURE: ret=%d elapsed=%lld us\n",
			 ret, elapsed_us);
		restore_shared_page(&fdev->dev, shm, snapshot);
		goto out_unmap;
	}

	service_status = get_unaligned_le32((u8 *)msg.data);
	if (service_status != 0) {
		if (service_status == 5 && elapsed_us >= ESPI_TIMEOUT_FLOOR_US)
			dev_crit(&fdev->dev,
				 "eSPI TIMEOUT: internal mailbox operation timed out (status=5)\n");
		else
			dev_crit(&fdev->dev,
				 "SERVICE REJECTED: status=%u\n", service_status);

		ret = service_status == 10 ? -EBUSY : -EIO;
		restore_shared_page(&fdev->dev, shm, snapshot);
		goto out_unmap;
	}

	for (elapsed = 0; elapsed < REPLY_TIMEOUT_MS; elapsed += REPLY_POLL_MS) {
		if (READ_ONCE(shm[NVFR_FRAME_OUTPUT_READY]) == 1)
			break;
		msleep(REPLY_POLL_MS);
	}

	if (READ_ONCE(shm[NVFR_FRAME_OUTPUT_READY]) != 1) {
		dev_crit(&fdev->dev,
			 "OUTPUT TIMEOUT after %u ms: accepted=%#04x ready=%#04x\n",
			 REPLY_TIMEOUT_MS,
			 READ_ONCE(shm[NVFR_FRAME_INPUT_ACCEPTED]),
			 READ_ONCE(shm[NVFR_FRAME_OUTPUT_READY]));
		ret = -ETIMEDOUT;
		restore_shared_page(&fdev->dev, shm, snapshot);
		goto out_unmap;
	}

	/* Order the ready-flag load ahead of reading the reply payload below. */
	mb();

	if (!nvfr_reply_header_ok(&shm[NVFR_FRAME_DATA_OFFSET])) {
		dev_crit(&fdev->dev,
			 "UNEXPECTED REPLY HEADER: %#04x %#04x %#04x\n",
			 shm[NVFR_FRAME_DATA_OFFSET],
			 shm[NVFR_FRAME_DATA_OFFSET + 1],
			 shm[NVFR_FRAME_DATA_OFFSET + 2]);
		ret = -EPROTO;
		restore_shared_page(&fdev->dev, shm, snapshot);
		goto out_unmap;
	}

	memcpy(out, &shm[NVFR_TELEMETRY_DATA_OFFSET], NVFR_TELEMETRY_LEN);
	dev_info(&fdev->dev,
		 "telemetry read OK in <=%u ms (decode via sysfs)\n", elapsed);

	ret = 0;
	restore_shared_page(&fdev->dev, shm, snapshot);

out_unmap:
	memunmap(shm);
	return ret;
}

/*
 * Return a telemetry snapshot, serialized and lightly cached: a fresh cached
 * copy (< NVFANREAD_CACHE_MS old) is reused; otherwise one EC read is issued and
 * cached. All callers go through here so requests are serialized.
 */
static int nvfanread_get_snapshot(struct nvfanread_state *state,
				  u8 snap[NVFR_TELEMETRY_LEN])
{
	int ret;

	ret = mutex_lock_interruptible(&state->request_lock);
	if (ret)
		return ret;

	if (state->cache_valid &&
	    ktime_before(ktime_get(),
			 ktime_add_ms(state->cache_time, NVFANREAD_CACHE_MS))) {
		memcpy(snap, state->cache, NVFR_TELEMETRY_LEN);
		mutex_unlock(&state->request_lock);
		return 0;
	}

	ret = read_telemetry(state, snap);
	if (!ret) {
		memcpy(state->cache, snap, NVFR_TELEMETRY_LEN);
		state->cache_time = ktime_get();
		state->cache_valid = true;
	}
	mutex_unlock(&state->request_lock);
	return ret;
}

static ssize_t telemetry_show(struct device *dev, struct device_attribute *attr,
			      char *buf)
{
	struct nvfanread_state *state = dev_get_drvdata(dev);
	u8 snap[NVFR_TELEMETRY_LEN];
	ssize_t len = 0;
	unsigned int i;
	int ret;

	ret = nvfanread_get_snapshot(state, snap);
	if (ret)
		return ret;

	len += scnprintf(buf + len, PAGE_SIZE - len,
			 "# EC command 7 telemetry snapshot (64 bytes @ 0x1188E2)\n");
	for (i = 0; i < NVFR_TELEMETRY_LEN; i += 16)
		len += scnprintf(buf + len, PAGE_SIZE - len,
				 "%02x: %16ph\n", i, &snap[i]);

	{
		u16 fan0_rpm, fan1_rpm;

		nvfr_snapshot_fan_rpm(snap, &fan0_rpm, &fan1_rpm);
		len += scnprintf(buf + len, PAGE_SIZE - len,
				 "# decoded: fan0_rpm=%u fan1_rpm=%u (snapshot offsets 4, 6)\n",
				 fan0_rpm, fan1_rpm);
	}

	len += scnprintf(buf + len, PAGE_SIZE - len,
			 "# le16 candidates in fan RPM ranges (fan0 %u-%u, fan1 %u-%u):\n",
			 NVFR_FAN0_RPM_MIN, NVFR_FAN0_RPM_MAX,
			 NVFR_FAN1_RPM_MIN, NVFR_FAN1_RPM_MAX);
	for (i = 0; i + 1 < NVFR_TELEMETRY_LEN; i++) {
		u16 v = nvfr_le16(&snap[i]);
		unsigned int flags;

		if (!nvfr_is_rpm_candidate(v))
			continue;
		flags = nvfr_rpm_flags(v);
		len += scnprintf(buf + len, PAGE_SIZE - len,
				 "  off 0x%02x = %u%s%s\n", i, v,
				 (flags & NVFR_CAND_FAN0) ? " [fan0?]" : "",
				 (flags & NVFR_CAND_FAN1) ? " [fan1?]" : "");
	}

	return len;
}

static DEVICE_ATTR_RO(telemetry);

/* ---- hwmon: fan1_input / fan2_input in RPM (channels 0 and 1) ---- */

static const char * const nvfanread_fan_labels[] = { "fan0", "fan1" };

static umode_t nvfanread_hwmon_is_visible(const void *drvdata,
					  enum hwmon_sensor_types type,
					  u32 attr, int channel)
{
	if (type == hwmon_fan &&
	    (attr == hwmon_fan_input || attr == hwmon_fan_label))
		return 0444;
	return 0;
}

static int nvfanread_hwmon_read(struct device *dev,
				enum hwmon_sensor_types type, u32 attr,
				int channel, long *val)
{
	struct nvfanread_state *state = dev_get_drvdata(dev);
	u8 snap[NVFR_TELEMETRY_LEN];
	u16 fan0_rpm, fan1_rpm;
	int ret;

	if (type != hwmon_fan || attr != hwmon_fan_input)
		return -EOPNOTSUPP;

	ret = nvfanread_get_snapshot(state, snap);
	if (ret)
		return ret;

	nvfr_snapshot_fan_rpm(snap, &fan0_rpm, &fan1_rpm);
	*val = (channel == 0) ? fan0_rpm : fan1_rpm;
	return 0;
}

static int nvfanread_hwmon_read_string(struct device *dev,
				       enum hwmon_sensor_types type, u32 attr,
				       int channel, const char **str)
{
	if (type != hwmon_fan || attr != hwmon_fan_label ||
	    channel >= (int)ARRAY_SIZE(nvfanread_fan_labels))
		return -EOPNOTSUPP;

	*str = nvfanread_fan_labels[channel];
	return 0;
}

static const struct hwmon_ops nvfanread_hwmon_ops = {
	.is_visible = nvfanread_hwmon_is_visible,
	.read = nvfanread_hwmon_read,
	.read_string = nvfanread_hwmon_read_string,
};

static const struct hwmon_channel_info * const nvfanread_hwmon_info[] = {
	HWMON_CHANNEL_INFO(fan,
			   HWMON_F_INPUT | HWMON_F_LABEL,
			   HWMON_F_INPUT | HWMON_F_LABEL),
	NULL,
};

static const struct hwmon_chip_info nvfanread_hwmon_chip = {
	.ops = &nvfanread_hwmon_ops,
	.info = nvfanread_hwmon_info,
};

static int fan_read_probe(struct ffa_device *fdev)
{
	struct nvfanread_state *state;
	struct device *hwmon;
	int ret;

	if (!fdev->ops || !fdev->ops->msg_ops ||
	    !fdev->ops->msg_ops->sync_send_receive2) {
		dev_err(&fdev->dev, "FF-A Direct Request 2 is unavailable\n");
		return -EOPNOTSUPP;
	}

	if (fdev->mode_32bit) {
		dev_err(&fdev->dev,
			"refusing Direct Request 2 for a 32-bit-mode device\n");
		return -EOPNOTSUPP;
	}

	state = devm_kzalloc(&fdev->dev, sizeof(*state), GFP_KERNEL);
	if (!state)
		return -ENOMEM;

	state->fdev = fdev;
	mutex_init(&state->request_lock);
	dev_set_drvdata(&fdev->dev, state);

	ret = device_create_file(&fdev->dev, &dev_attr_telemetry);
	if (ret) {
		dev_err(&fdev->dev, "failed to create sysfs telemetry attribute: %d\n",
			ret);
		return ret;
	}

	hwmon = devm_hwmon_device_register_with_info(&fdev->dev, "nvfanread",
						     state, &nvfanread_hwmon_chip,
						     NULL);
	if (IS_ERR(hwmon)) {
		device_remove_file(&fdev->dev, &dev_attr_telemetry);
		return PTR_ERR(hwmon);
	}

	dev_info(&fdev->dev,
		 "read-only fan telemetry ready: hwmon '%s' (fan1_input/fan2_input) + %s/telemetry; module load issued no EC request\n",
		 dev_name(hwmon), dev_name(&fdev->dev));
	return 0;
}

static void fan_read_remove(struct ffa_device *fdev)
{
	device_remove_file(&fdev->dev, &dev_attr_telemetry);
}

static const struct ffa_device_id fan_read_ids[] = {
	{
		.uuid = UUID_INIT(0x884a63a0, 0x3285, 0x4120,
				  0x83, 0xaa, 0xee, 0xc0,
				  0x08, 0xa0, 0xa5, 0x46),
	},
	{},
};

static struct ffa_driver fan_read_driver = {
	.name = "nvfanread",
	.probe = fan_read_probe,
	.remove = fan_read_remove,
	.id_table = fan_read_ids,
};

module_ffa_driver(fan_read_driver);

MODULE_DESCRIPTION("DGX Spark read-only EC fan telemetry probe");
MODULE_AUTHOR("Evan Hoffman");
MODULE_LICENSE("GPL");
