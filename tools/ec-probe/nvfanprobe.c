// SPDX-License-Identifier: GPL-2.0-only
/*
 * Diagnostic probe for the DGX Spark EC mailbox (OEM1 command 17 over FF-A).
 * NOT for the repo — a debugging tool. Reading /sys/.../arm-ffa-17/probe issues
 * one EC request built from module params and dumps the raw mailbox response,
 * whether it succeeds or fails, so we can sweep EC commands and frame shapes.
 *
 * Read-only intent: default command is 7 (telemetry). It CAN be pointed at a
 * write command via params, so don't do that unless you mean to.
 */
#include <linux/arm_ffa.h>
#include <linux/delay.h>
#include <linux/device.h>
#include <linux/io.h>
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

#define ESPI_OEM_GENERIC_EMI 17U
#define SHM_PA   0x933dd000ULL
#define SHM_SIZE 0x1000U
#define DATA_OFF 0x10U
#define SNAP     128U

static u8 ec_cmd = 7;   /* EC inner command */
static u8 in_len = 3;   /* frame[0] input length */
static u8 out_len = 67; /* frame[1] output length */
static u8 out_off = 0;  /* frame[2] EC output start offset */
static u8 dump = 96;    /* bytes of the data region to dump */
static u8 d0, d1, d2;   /* EC request data bytes at 0x13/0x14/0x15 */
static u8 mapmode;      /* 0=memremap WB, 1=WT (write-through), 2=WC (write-combine) */
module_param(ec_cmd, byte, 0644);
module_param(in_len, byte, 0644);
module_param(out_len, byte, 0644);
module_param(out_off, byte, 0644);
module_param(dump, byte, 0644);
module_param(d0, byte, 0644);
module_param(d1, byte, 0644);
module_param(d2, byte, 0644);
module_param(mapmode, byte, 0644);

struct pst { struct ffa_device *fdev; struct mutex lock; };

static ssize_t probe_show(struct device *dev, struct device_attribute *a, char *buf)
{
	struct pst *s = dev_get_drvdata(dev);
	struct ffa_device *fdev = s->fdev;
	struct ffa_send_direct_data2 msg = {};
	u8 *payload = (u8 *)msg.data;
	u8 snap[SNAP], frame[SNAP] = {};
	u8 *shm;
	u32 status;
	s64 el;
	ktime_t t0;
	int ret;
	ssize_t len = 0;
	unsigned int i, e = 0;

	mutex_lock(&s->lock);
	{
		unsigned long mm = (mapmode == 1) ? MEMREMAP_WT :
				   (mapmode == 2) ? MEMREMAP_WC : MEMREMAP_WB;
		shm = memremap(SHM_PA, SHM_SIZE, mm);
	}
	if (!shm) {
		mutex_unlock(&s->lock);
		return sysfs_emit(buf, "memremap failed\n");
	}
	memcpy(snap, shm, SNAP);

	len += scnprintf(buf + len, PAGE_SIZE - len,
			 "params: cmd=%u in_len=%u out_len=%u out_off=%u\n",
			 ec_cmd, in_len, out_len, out_off);
	len += scnprintf(buf + len, PAGE_SIZE - len,
			 "pre:  accepted=%#04x ready=%#04x  hdr=%*ph\n",
			 snap[3], snap[4], 16, &snap[0]);

	frame[0] = in_len;
	frame[1] = out_len;
	frame[2] = out_off;
	frame[DATA_OFF + 0] = 0x07;
	frame[DATA_OFF + 1] = ec_cmd;
	frame[DATA_OFF + 2] = 0x00;
	frame[DATA_OFF + 3] = d0;
	frame[DATA_OFF + 4] = d1;
	frame[DATA_OFF + 5] = d2;
	memcpy(shm, frame, SNAP);
	mb();

	payload[0] = ESPI_OEM_GENERIC_EMI;
	t0 = ktime_get();
	ret = fdev->ops->msg_ops->sync_send_receive2(fdev, &msg);
	el = ktime_us_delta(ktime_get(), t0);
	status = get_unaligned_le32((u8 *)msg.data);

	len += scnprintf(buf + len, PAGE_SIZE - len,
			 "send: ret=%d status=%u elapsed=%lldus raw=%016llx %016llx %016llx %016llx\n",
			 ret, status, el,
			 (unsigned long long)msg.data[0], (unsigned long long)msg.data[1],
			 (unsigned long long)msg.data[2], (unsigned long long)msg.data[3]);

	if (ret == 0 && status == 0) {
		for (e = 0; e < 2000; e += 10) {
			if (READ_ONCE(shm[4]) == 1)
				break;
			msleep(10);
		}
		len += scnprintf(buf + len, PAGE_SIZE - len,
				 "poll: accepted=%#04x ready=%#04x waited~%ums\n",
				 READ_ONCE(shm[3]), READ_ONCE(shm[4]), e);
	}

	mb();
	for (i = 0; i < dump && (DATA_OFF + i) < SHM_SIZE; i += 16)
		len += scnprintf(buf + len, PAGE_SIZE - len,
				 "data[%#04x]: %*ph\n", DATA_OFF + i, 16, &shm[DATA_OFF + i]);

	memcpy(shm, snap, SNAP);
	mb();
	memunmap(shm);
	mutex_unlock(&s->lock);
	return len;
}
static DEVICE_ATTR_RO(probe);

static int pb(struct ffa_device *fdev)
{
	struct pst *s;

	if (!fdev->ops || !fdev->ops->msg_ops || !fdev->ops->msg_ops->sync_send_receive2)
		return -EOPNOTSUPP;
	s = devm_kzalloc(&fdev->dev, sizeof(*s), GFP_KERNEL);
	if (!s)
		return -ENOMEM;
	s->fdev = fdev;
	mutex_init(&s->lock);
	dev_set_drvdata(&fdev->dev, s);
	return device_create_file(&fdev->dev, &dev_attr_probe);
}
static void rm(struct ffa_device *fdev) { device_remove_file(&fdev->dev, &dev_attr_probe); }

static const struct ffa_device_id ids[] = {
	{ .uuid = UUID_INIT(0x884a63a0, 0x3285, 0x4120, 0x83, 0xaa,
			    0xee, 0xc0, 0x08, 0xa0, 0xa5, 0x46) },
	{},
};
static struct ffa_driver drv = { .name = "nvfanprobe", .probe = pb, .remove = rm, .id_table = ids };
module_ffa_driver(drv);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("DGX Spark EC mailbox diagnostic probe");
MODULE_AUTHOR("evan");
