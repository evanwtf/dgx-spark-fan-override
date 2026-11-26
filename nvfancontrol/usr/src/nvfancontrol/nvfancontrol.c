// SPDX-License-Identifier: GPL-2.0-only
/*
 * DGX Spark DKMS-managed EC fan override / rollback sysfs driver
 *
 * Loading the module finds the verified FF-A eSPI service (UUID
 * 884a63a0-3285-4120-83aa-eec008a0a546, bus device arm-ffa-17) on the FF-A
 * bus, holds a reference to it, and creates:
 *
 *   /sys/devices/faux/nvfancontrol/fan
 *
 * Writing "max" or "auto" issues one OEM1 command 17 request. Requests are
 * serialized, and the interface remains available for subsequent state changes.
 *
 * The module does not bind the FF-A device. On 7.0.0-1025+ the built-in
 * nvidia-ffa-ec driver binds it, sets suppress_bind_attrs, and the FF-A bus
 * has no driver_override, so no other driver can bind it (issue #16). The
 * module sends through the device's ops without owning it, as nvfanread 1.1.0
 * does.
 *
 * Because neither module binds on those kernels, binding no longer keeps
 * nvfanread and nvfancontrol off the shared page at the same time. Each
 * request therefore takes the page as a busy iomem region (MAILBOX_LOCK_NAME)
 * for the whole transaction, and nvfanread does the same. The address, size
 * and retry bounds must match nvfanread/nvfanread_proto.h.
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/arm_ffa.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/errno.h>
#include <linux/device/bus.h>
#include <linux/device/faux.h>
#include <linux/io.h>
#include <linux/ioport.h>
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
#define ESPI_NS_SHM_PROTOCOL_SIZE  32U

#define GENERIC_FRAME_LENGTH       5U
#define GENERIC_OUTPUT_OFFSET      0U
#define GENERIC_DATA_OFFSET        0x10U
#define GENERIC_INPUT_ACCEPTED     3U
#define GENERIC_OUTPUT_READY       4U

#define THERMAL_OUTER_COMMAND      0x07U
#define THERMAL_SET_HIGH_OVERRIDE  0x05U
#define TARGET_FULL_RPM            13500U
#define TARGET_AUTOMATIC           0xffffU

#define REPLY_TIMEOUT_MS           5000U
#define REPLY_POLL_MS              10U
#define ESPI_TIMEOUT_FLOOR_US      40000LL

/*
 * FF-A doorbell retry. The EC secure partition serves many services and
 * answers -EBUSY while another request is in flight. Same bounds as
 * NVFR_SEND_RETRY_BUDGET_US / NVFR_SEND_MAX_ATTEMPTS in nvfanread_proto.h.
 */
#define SEND_RETRY_BUDGET_US       10000LL
#define SEND_MAX_ATTEMPTS          20U

/*
 * Cross-module mailbox lock. Same name and bounds as NVFR_MAILBOX_LOCK_NAME,
 * NVFR_LOCK_RETRY_BUDGET_US and NVFR_LOCK_MAX_ATTEMPTS in nvfanread_proto.h.
 */
#define MAILBOX_LOCK_NAME          "nvfan-mailbox"
#define LOCK_RETRY_BUDGET_US       50000LL
#define LOCK_MAX_ATTEMPTS          50U

/*
 * Drivers that may own the FF-A device while this module uses the shared page:
 * none, nvidia-ffa-ec (it never uses the page), or nvfanread (its claim
 * driver on 6.17 / 7.0.0-1019; 1.1.0+ takes the mailbox lock). Any other owner
 * (ec-probe's nvfanprobe, nvfancontrol 1.0.0) writes the page without the
 * lock.
 */
#define NVIDIA_EC_DRIVER_NAME      "nvidia-ffa-ec"
#define NVFANREAD_DRIVER_NAME      "nvfanread"

enum nvfancontrol_fan_state {
	NVFANCONTROL_READY = 0,
	NVFANCONTROL_MAX,
	NVFANCONTROL_AUTO,
	NVFANCONTROL_ERROR,
};

/*
 * Module-global state. The FF-A device belongs to another driver (or to none),
 * so nothing is stored in its drvdata. The fan attribute lives on a faux
 * device and reads this state directly.
 */
struct nvfancontrol_state {
	struct ffa_device *fdev;	/* reference held from bus_find_device() */
	struct faux_device *faux;
	struct mutex request_lock;
	enum nvfancontrol_fan_state fan_state;
	int last_error;
};

static struct nvfancontrol_state nvfancontrol_state = {
	.request_lock = __MUTEX_INITIALIZER(nvfancontrol_state.request_lock),
	.fan_state = NVFANCONTROL_READY,
};

static const uuid_t espi_uuid = UUID_INIT(0x884a63a0, 0x3285, 0x4120,
					  0x83, 0xaa, 0xee, 0xc0,
					  0x08, 0xa0, 0xa5, 0x46);

/*
 * Take the shared page as a busy iomem region for one transaction. nvfanread
 * requests the same region, and the kernel grants it to one caller at a time.
 * Retry a busy region for at most LOCK_RETRY_BUDGET_US, then return -EBUSY.
 */
static int mailbox_lock(struct device *dev)
{
	ktime_t start = ktime_get();
	unsigned int attempts = 0;
	s64 elapsed_us;

	for (;;) {
		if (request_mem_region(ESPI_NS_SHM_PA, ESPI_NS_SHM_SIZE,
				       MAILBOX_LOCK_NAME))
			return 0;
		attempts++;
		elapsed_us = ktime_us_delta(ktime_get(), start);
		if (attempts >= LOCK_MAX_ATTEMPTS || elapsed_us < 0 ||
		    elapsed_us >= LOCK_RETRY_BUDGET_US)
			break;
		usleep_range(1000, 2000);
	}

	dev_err(dev,
		"mailbox lock %#llx busy after %u tries (%lld us); no EC request was sent\n",
		ESPI_NS_SHM_PA, attempts, elapsed_us);
	return -EBUSY;
}

static void mailbox_unlock(void)
{
	release_mem_region(ESPI_NS_SHM_PA, ESPI_NS_SHM_SIZE);
}

/* Refuse while a driver that writes the page without the lock owns the device. */
static int check_owner(struct device *dev, struct ffa_device *fdev)
{
	const struct device_driver *owner = READ_ONCE(fdev->dev.driver);

	if (!owner || !strcmp(owner->name, NVIDIA_EC_DRIVER_NAME) ||
	    !strcmp(owner->name, NVFANREAD_DRIVER_NAME))
		return 0;

	dev_err(dev,
		"%s is bound to '%s', which also uses the shared page; no EC request was sent\n",
		dev_name(&fdev->dev), owner->name);
	return -EBUSY;
}

/*
 * Ring the doorbell (FF-A direct request 2, OEM1 command 17). Retry -EBUSY
 * inside SEND_RETRY_BUDGET_US / SEND_MAX_ATTEMPTS; any other result returns
 * at once.
 */
static int send_doorbell(struct ffa_device *fdev,
			 struct ffa_send_direct_data2 *msg,
			 s64 *elapsed_us, s64 *last_us, unsigned int *attempts)
{
	ktime_t start = ktime_get();
	ktime_t attempt_start;
	int ret;

	*attempts = 0;
	for (;;) {
		memset(msg, 0, sizeof(*msg));
		((u8 *)msg->data)[0] = ESPI_OEM_GENERIC_EMI;
		attempt_start = ktime_get();
		ret = fdev->ops->msg_ops->sync_send_receive2(fdev, msg);
		(*attempts)++;
		*last_us = ktime_us_delta(ktime_get(), attempt_start);
		*elapsed_us = ktime_us_delta(ktime_get(), start);
		if (ret != -EBUSY || *attempts >= SEND_MAX_ATTEMPTS ||
		    *elapsed_us >= SEND_RETRY_BUDGET_US)
			return ret;
		usleep_range(500, 1000);
	}
}

static void restore_shared_page(struct device *dev, u8 *shm,
				const u8 snapshot[ESPI_NS_SHM_PROTOCOL_SIZE],
				int *result)
{
	memcpy(shm, snapshot, ESPI_NS_SHM_PROTOCOL_SIZE);
	/* Ensure the restore write lands in the shared page before we verify it. */
	mb();

	if (memcmp(shm, snapshot, ESPI_NS_SHM_PROTOCOL_SIZE)) {
		dev_crit(dev,
			 "SHARED-BUFFER RESTORE VERIFY FAILED at physical address %#llx\n",
			 ESPI_NS_SHM_PA);
		*result = -EUCLEAN;
		return;
	}

	dev_info(dev, "shared-buffer restore verified (first %u bytes)\n",
		 ESPI_NS_SHM_PROTOCOL_SIZE);
}

static int submit_fan_request(struct nvfancontrol_state *state, u16 target)
{
	struct ffa_device *fdev = state->fdev;
	struct device *dev = &state->faux->dev;
	struct ffa_send_direct_data2 msg = {};
	u8 snapshot[ESPI_NS_SHM_PROTOCOL_SIZE];
	u8 frame[ESPI_NS_SHM_PROTOCOL_SIZE] = {};
	u8 request[GENERIC_FRAME_LENGTH];
	u8 response[GENERIC_FRAME_LENGTH];
	u8 *shm;
	unsigned long pfn = PHYS_PFN(ESPI_NS_SHM_PA);
	unsigned long raw_response[4];
	u32 service_status;
	bool map_memory;
	bool reserved_page = false;
	unsigned int attempts;
	unsigned int elapsed;
	s64 elapsed_us;
	s64 last_us;
	int result;
	int ret;

	ret = check_owner(dev, fdev);
	if (ret)
		return ret;

	map_memory = pfn_is_map_memory(pfn);
	if (map_memory)
		reserved_page = PageReserved(pfn_to_page(pfn));

	dev_info(dev,
		 "manifest ns_shm0: physical=%#llx size=%#x map_memory=%u page_reserved=%u\n",
		 ESPI_NS_SHM_PA, ESPI_NS_SHM_SIZE,
		 (unsigned int)map_memory, (unsigned int)reserved_page);

	if (map_memory && !reserved_page) {
		dev_err(dev,
			"refusing ns_shm0: PFN is Linux map memory but is not marked reserved\n");
		return -EPERM;
	}

	ret = mailbox_lock(dev);
	if (ret)
		return ret;

	shm = memremap(ESPI_NS_SHM_PA, ESPI_NS_SHM_SIZE, MEMREMAP_WB);
	if (!shm) {
		dev_err(dev, "memremap of manifest ns_shm0 failed\n");
		ret = -ENOMEM;
		goto out_unlock;
	}

	memcpy(snapshot, shm, sizeof(snapshot));
	dev_info(dev, "shared pre-state: %*ph\n",
		 (int)sizeof(snapshot), snapshot);

	if (snapshot[GENERIC_INPUT_ACCEPTED] != 0 ||
	    snapshot[GENERIC_OUTPUT_READY] != 0) {
		dev_err(dev,
			"refusing request: shared mailbox is not idle (accepted=%#04x ready=%#04x)\n",
			snapshot[GENERIC_INPUT_ACCEPTED],
			snapshot[GENERIC_OUTPUT_READY]);
		ret = -EBUSY;
		goto out_unmap;
	}

	request[0] = THERMAL_OUTER_COMMAND;
	request[1] = THERMAL_SET_HIGH_OVERRIDE;
	request[2] = 0;
	put_unaligned_le16(target, &request[3]);

	frame[0] = GENERIC_FRAME_LENGTH;
	frame[1] = GENERIC_FRAME_LENGTH;
	frame[2] = GENERIC_OUTPUT_OFFSET;
	memcpy(&frame[GENERIC_DATA_OFFSET], request, sizeof(request));
	memcpy(shm, frame, sizeof(frame));
	/* Publish the request frame to the shared page before the doorbell send. */
	mb();

	if (target == TARGET_FULL_RPM)
		dev_warn(dev,
			 "starting MAX request: fixed EC frame %*ph\n",
			 (int)sizeof(request), request);
	else
		dev_warn(dev,
			 "starting AUTO request: fixed EC frame %*ph\n",
			 (int)sizeof(request), request);

	ret = send_doorbell(fdev, &msg, &elapsed_us, &last_us, &attempts);
	if (ret) {
		dev_crit(dev,
			 "FF-A TRANSPORT FAILURE: ret=%d attempts=%u elapsed=%lld us; EC override state is unknown\n",
			 ret, attempts, elapsed_us);
		restore_shared_page(dev, shm, snapshot, &ret);
		goto out_unmap;
	}

	raw_response[0] = msg.data[0];
	raw_response[1] = msg.data[1];
	raw_response[2] = msg.data[2];
	raw_response[3] = msg.data[3];
	service_status = get_unaligned_le32((u8 *)msg.data);
	dev_info(dev,
		 "command-17 response: status=%u attempts=%u elapsed=%lld us raw=%#018lx %#018lx %#018lx %#018lx\n",
		 service_status, attempts, elapsed_us, raw_response[0], raw_response[1],
		 raw_response[2], raw_response[3]);

	if (service_status != 0) {
		/* Time only the final send; retries must not fake a timeout. */
		if (service_status == 5 && last_us >= ESPI_TIMEOUT_FLOOR_US)
			dev_crit(dev,
				 "eSPI TIMEOUT: internal mailbox operation timed out; EC override state is unknown\n");
		else
			dev_crit(dev,
				 "SERVICE REJECTED: status=%u; EC override state is unknown\n",
				 service_status);

		ret = service_status == 10 ? -EBUSY : -EIO;
		restore_shared_page(dev, shm, snapshot, &ret);
		goto out_unmap;
	}

	for (elapsed = 0; elapsed < REPLY_TIMEOUT_MS;
	     elapsed += REPLY_POLL_MS) {
		if (READ_ONCE(shm[GENERIC_OUTPUT_READY]) == 1)
			break;
		msleep(REPLY_POLL_MS);
	}

	if (READ_ONCE(shm[GENERIC_OUTPUT_READY]) != 1) {
		dev_crit(dev,
			 "OUTPUT TIMEOUT after %u ms: accepted=%#04x ready=%#04x; EC override state is unknown; shared frame is left intact and no retry is allowed\n",
			 REPLY_TIMEOUT_MS,
			 READ_ONCE(shm[GENERIC_INPUT_ACCEPTED]),
			 READ_ONCE(shm[GENERIC_OUTPUT_READY]));
		ret = -ETIMEDOUT;
		goto out_unmap;
	}

	/* Order the ready-flag load ahead of reading the reply payload below. */
	mb();
	memcpy(response, &shm[GENERIC_DATA_OFFSET], sizeof(response));
	dev_info(dev,
		 "EC reply after <=%u ms: accepted=%#04x ready=%#04x response=%*ph\n",
		 elapsed, READ_ONCE(shm[GENERIC_INPUT_ACCEPTED]),
		 READ_ONCE(shm[GENERIC_OUTPUT_READY]),
		 (int)sizeof(response), response);

	if (memcmp(response, request, sizeof(response)) || response[2] != 0) {
		dev_crit(dev,
			 "RESPONSE MISMATCH: expected=%*ph observed=%*ph; EC override state is unknown\n",
			 (int)sizeof(request), request,
			 (int)sizeof(response), response);
		result = -EPROTO;
		restore_shared_page(dev, shm, snapshot, &result);
		ret = result;
		goto out_unmap;
	}

	if (target == TARGET_FULL_RPM)
		dev_warn(dev,
			 "MAX ACKNOWLEDGED: EC accepted high override=13500 RPM; fan0 and fan1 policy outputs saturate at 100%%\n");
	else
		dev_warn(dev,
			 "AUTO ACKNOWLEDGED: EC accepted high override=0xffff; automatic thermal curve is restored\n");

	result = 0;
	restore_shared_page(dev, shm, snapshot, &result);
	ret = result;

out_unmap:
	memunmap(shm);
out_unlock:
	mailbox_unlock();
	return ret;
}

static ssize_t fan_show(struct device *dev, struct device_attribute *attr,
			char *buf)
{
	struct nvfancontrol_state *state = &nvfancontrol_state;
	ssize_t length;

	mutex_lock(&state->request_lock);
	switch (state->fan_state) {
	case NVFANCONTROL_MAX:
		length = sysfs_emit(buf, "max\n");
		break;
	case NVFANCONTROL_AUTO:
		length = sysfs_emit(buf, "auto\n");
		break;
	case NVFANCONTROL_ERROR:
		length = sysfs_emit(buf, "error %d\n", state->last_error);
		break;
	case NVFANCONTROL_READY:
	default:
		length = sysfs_emit(buf, "ready\n");
		break;
	}
	mutex_unlock(&state->request_lock);

	return length;
}

static ssize_t fan_store(struct device *dev, struct device_attribute *attr,
			 const char *buf, size_t count)
{
	struct nvfancontrol_state *state = &nvfancontrol_state;
	enum nvfancontrol_fan_state requested_state;
	u16 target;
	int ret;

	if (sysfs_streq(buf, "max")) {
		target = TARGET_FULL_RPM;
		requested_state = NVFANCONTROL_MAX;
	} else if (sysfs_streq(buf, "auto")) {
		target = TARGET_AUTOMATIC;
		requested_state = NVFANCONTROL_AUTO;
	} else {
		dev_err(dev, "fan accepts only 'max' or 'auto'\n");
		return -EINVAL;
	}

	ret = mutex_lock_interruptible(&state->request_lock);
	if (ret)
		return ret;

	ret = submit_fan_request(state, target);
	if (ret) {
		state->fan_state = NVFANCONTROL_ERROR;
		state->last_error = ret;
	} else {
		state->fan_state = requested_state;
		state->last_error = 0;
	}

	mutex_unlock(&state->request_lock);
	return ret ? ret : count;
}

static DEVICE_ATTR_RW(fan);

/* Lives on the faux device, never on the FF-A device. */
static struct attribute *nvfancontrol_attrs[] = {
	&dev_attr_fan.attr,
	NULL,
};
ATTRIBUTE_GROUPS(nvfancontrol);

static int nvfancontrol_match_espi(struct device *dev, const void *data)
{
	return uuid_equal(&to_ffa_dev(dev)->uuid, &espi_uuid);
}

static int __init nvfancontrol_init(void)
{
	struct nvfancontrol_state *state = &nvfancontrol_state;
	const struct device_driver *owner;
	struct ffa_device *fdev;
	struct faux_device *faux;
	struct device *dev;
	int ret;

	dev = bus_find_device(&ffa_bus_type, NULL, NULL,
			      nvfancontrol_match_espi);
	if (!dev) {
		pr_err("FF-A eSPI service %pUb not found on the arm_ffa bus\n",
		       &espi_uuid);
		return -ENODEV;
	}
	fdev = to_ffa_dev(dev);

	if (!fdev->ops || !fdev->ops->msg_ops ||
	    !fdev->ops->msg_ops->sync_send_receive2) {
		pr_err("%s: FF-A Direct Request 2 is unavailable\n",
		       dev_name(dev));
		ret = -EOPNOTSUPP;
		goto err_put;
	}

	owner = READ_ONCE(dev->driver);
	pr_info("%s: found partition=%#x uuid=%pUb properties=%#x mode_32bit=%u driver='%s' boot-age=%llu s\n",
		dev_name(dev), fdev->vm_id, &fdev->uuid, fdev->properties,
		(unsigned int)fdev->mode_32bit, owner ? owner->name : "(none)",
		(unsigned long long)ktime_get_boottime_seconds());

	if (fdev->mode_32bit) {
		pr_err("%s: refusing Direct Request 2 for a 32-bit-mode device\n",
		       dev_name(dev));
		ret = -EOPNOTSUPP;
		goto err_put;
	}

	state->fdev = fdev;

	faux = faux_device_create_with_groups(KBUILD_MODNAME, NULL, NULL,
					      nvfancontrol_groups);
	if (!faux) {
		pr_err("failed to create the faux device for the fan attribute\n");
		state->fdev = NULL;
		ret = -ENOMEM;
		goto err_put;
	}
	state->faux = faux;

	pr_info("sysfs control ready: %s/fan accepts 'max' or 'auto'; module load issued no EC request\n",
		dev_name(&faux->dev));
	return 0;

err_put:
	put_device(dev);
	return ret;
}

static void __exit nvfancontrol_exit(void)
{
	struct nvfancontrol_state *state = &nvfancontrol_state;

	/* Destroy first: after this no fan_show/fan_store can reach state->fdev. */
	faux_device_destroy(state->faux);
	state->faux = NULL;
	put_device(&state->fdev->dev);
	state->fdev = NULL;
	pr_warn("module removed; unloading does NOT change the EC override value\n");
}

module_init(nvfancontrol_init);
module_exit(nvfancontrol_exit);

MODULE_DESCRIPTION("DGX Spark persistent EC fan sysfs controller");
MODULE_AUTHOR("841973620");
MODULE_LICENSE("GPL");
