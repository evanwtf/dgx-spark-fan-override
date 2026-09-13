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
 * The mailbox handshake mirrors the override driver: it refuses a non-idle
 * mailbox, snapshots the shared page, and restores it afterwards. Requests are
 * serialized under a mutex.
 */

#include <linux/arm_ffa.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/errno.h>
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

#define ESPI_OEM_GENERIC_EMI       17U

#define ESPI_NS_SHM_PA             0x933dd000ULL
#define ESPI_NS_SHM_SIZE           0x1000U

/* Shared-page frame layout (OEM1 command 17). */
#define FRAME_INPUT_LEN            0U
#define FRAME_OUTPUT_LEN           1U
#define FRAME_OUTPUT_OFFSET        2U
#define FRAME_INPUT_ACCEPTED       3U
#define FRAME_OUTPUT_READY         4U
#define FRAME_DATA_OFFSET          0x10U

/* EC generic frame: [0]=outer service, [1]=inner command, [2]=status. */
#define THERMAL_OUTER_COMMAND      0x07U
#define THERMAL_TELEMETRY_READ     0x07U   /* EC inner command 7, read-only */

#define EC_REQUEST_LEN             3U      /* header only; command 7 has no input data */
#define TELEMETRY_LEN              64U     /* bytes copied from EC SRAM 0x1188E2 */
#define EC_REPLY_LEN               (EC_REQUEST_LEN + TELEMETRY_LEN)
#define TELEMETRY_DATA_OFFSET      (FRAME_DATA_OFFSET + EC_REQUEST_LEN)

/* Region of the shared page we snapshot and restore around a request. */
#define SNAP_LEN                   128U

#define REPLY_TIMEOUT_MS           5000U
#define REPLY_POLL_MS              10U
#define ESPI_TIMEOUT_FLOOR_US      40000LL

/* Documented per-channel RPM ranges, used only to flag decode candidates. */
#define FAN0_RPM_MIN               1260U
#define FAN0_RPM_MAX               9000U
#define FAN1_RPM_MIN               1890U
#define FAN1_RPM_MAX               13500U

struct nvfanread_state {
	struct ffa_device *fdev;
	struct mutex request_lock;
};

static void restore_shared_page(struct device *dev, u8 *shm,
				const u8 snapshot[SNAP_LEN])
{
	memcpy(shm, snapshot, SNAP_LEN);
	mb();

	if (memcmp(shm, snapshot, SNAP_LEN))
		dev_crit(dev,
			 "SHARED-BUFFER RESTORE VERIFY FAILED at physical address %#llx\n",
			 ESPI_NS_SHM_PA);
}

/*
 * Issue EC command 7 and copy the 64-byte telemetry snapshot into @out.
 * Returns 0 on success. Never writes an override slot.
 */
static int read_telemetry(struct nvfanread_state *state, u8 out[TELEMETRY_LEN])
{
	struct ffa_device *fdev = state->fdev;
	struct ffa_send_direct_data2 msg = {};
	u8 snapshot[SNAP_LEN];
	u8 frame[SNAP_LEN] = {};
	u8 request[EC_REQUEST_LEN];
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

	if (snapshot[FRAME_INPUT_ACCEPTED] != 0 ||
	    snapshot[FRAME_OUTPUT_READY] != 0) {
		dev_err(&fdev->dev,
			"refusing request: shared mailbox is not idle (accepted=%#04x ready=%#04x)\n",
			snapshot[FRAME_INPUT_ACCEPTED],
			snapshot[FRAME_OUTPUT_READY]);
		ret = -EBUSY;
		goto out_unmap;
	}

	request[0] = THERMAL_OUTER_COMMAND;
	request[1] = THERMAL_TELEMETRY_READ;
	request[2] = 0;

	frame[FRAME_INPUT_LEN] = EC_REQUEST_LEN;
	frame[FRAME_OUTPUT_LEN] = EC_REPLY_LEN;
	frame[FRAME_OUTPUT_OFFSET] = 0;
	memcpy(&frame[FRAME_DATA_OFFSET], request, sizeof(request));
	memcpy(shm, frame, sizeof(frame));
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
		if (READ_ONCE(shm[FRAME_OUTPUT_READY]) == 1)
			break;
		msleep(REPLY_POLL_MS);
	}

	if (READ_ONCE(shm[FRAME_OUTPUT_READY]) != 1) {
		dev_crit(&fdev->dev,
			 "OUTPUT TIMEOUT after %u ms: accepted=%#04x ready=%#04x\n",
			 REPLY_TIMEOUT_MS,
			 READ_ONCE(shm[FRAME_INPUT_ACCEPTED]),
			 READ_ONCE(shm[FRAME_OUTPUT_READY]));
		ret = -ETIMEDOUT;
		restore_shared_page(&fdev->dev, shm, snapshot);
		goto out_unmap;
	}

	mb();

	/* Echoed header first: [07][07][00] on success. */
	if (shm[FRAME_DATA_OFFSET] != THERMAL_OUTER_COMMAND ||
	    shm[FRAME_DATA_OFFSET + 1] != THERMAL_TELEMETRY_READ ||
	    shm[FRAME_DATA_OFFSET + 2] != 0) {
		dev_crit(&fdev->dev,
			 "UNEXPECTED REPLY HEADER: %#04x %#04x %#04x\n",
			 shm[FRAME_DATA_OFFSET], shm[FRAME_DATA_OFFSET + 1],
			 shm[FRAME_DATA_OFFSET + 2]);
		ret = -EPROTO;
		restore_shared_page(&fdev->dev, shm, snapshot);
		goto out_unmap;
	}

	memcpy(out, &shm[TELEMETRY_DATA_OFFSET], TELEMETRY_LEN);
	dev_info(&fdev->dev,
		 "telemetry read OK in <=%u ms (status field, decode via sysfs)\n",
		 elapsed);

	ret = 0;
	restore_shared_page(&fdev->dev, shm, snapshot);

out_unmap:
	memunmap(shm);
	return ret;
}

static ssize_t telemetry_show(struct device *dev, struct device_attribute *attr,
			      char *buf)
{
	struct nvfanread_state *state = dev_get_drvdata(dev);
	u8 snap[TELEMETRY_LEN];
	ssize_t len = 0;
	unsigned int i;
	int ret;

	ret = mutex_lock_interruptible(&state->request_lock);
	if (ret)
		return ret;
	ret = read_telemetry(state, snap);
	mutex_unlock(&state->request_lock);
	if (ret)
		return ret;

	len += scnprintf(buf + len, PAGE_SIZE - len,
			 "# EC command 7 telemetry snapshot (64 bytes @ 0x1188E2)\n");
	for (i = 0; i < TELEMETRY_LEN; i += 16)
		len += scnprintf(buf + len, PAGE_SIZE - len,
				 "%02x: %16ph\n", i, &snap[i]);

	len += scnprintf(buf + len, PAGE_SIZE - len,
			 "# le16 candidates in fan RPM ranges (fan0 %u-%u, fan1 %u-%u):\n",
			 FAN0_RPM_MIN, FAN0_RPM_MAX, FAN1_RPM_MIN, FAN1_RPM_MAX);
	for (i = 0; i + 1 < TELEMETRY_LEN; i++) {
		u16 v = get_unaligned_le16(&snap[i]);

		if (v >= FAN0_RPM_MIN && v <= FAN1_RPM_MAX)
			len += scnprintf(buf + len, PAGE_SIZE - len,
					 "  off 0x%02x = %u%s%s\n", i, v,
					 (v <= FAN0_RPM_MAX) ? " [fan0?]" : "",
					 (v >= FAN1_RPM_MIN) ? " [fan1?]" : "");
	}

	return len;
}

static DEVICE_ATTR_RO(telemetry);

static int fan_read_probe(struct ffa_device *fdev)
{
	struct nvfanread_state *state;
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

	dev_info(&fdev->dev,
		 "read-only telemetry ready: %s/telemetry; module load issued no EC request\n",
		 dev_name(&fdev->dev));
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
