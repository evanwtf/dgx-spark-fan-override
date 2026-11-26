// SPDX-License-Identifier: GPL-2.0-only
/*
 * Test tool: hold the nvfan-mailbox lock for a while, then release it.
 *
 * Loading the module takes the shared page (0x933dd000, size 0x1000) as a busy
 * iomem region, the same lock nvfanread and nvfancontrol take, and releases it
 * after hold_ms milliseconds. It never maps or touches the page and never
 * talks to the EC. scripts/live-test-nvfancontrol.sh uses it to prove that
 * both modules refuse with -EAGAIN and send nothing while the lock is busy.
 *
 *   insmod nvfanhold.ko hold_ms=2000
 *
 * Loading fails with -EBUSY if the lock is already held. Unloading early
 * releases the lock at once.
 */

#define pr_fmt(fmt) KBUILD_MODNAME ": " fmt

#include <linux/ioport.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/workqueue.h>

#define ESPI_NS_SHM_PA             0x933dd000ULL
#define ESPI_NS_SHM_SIZE           0x1000U
#define MAILBOX_LOCK_NAME          "nvfan-mailbox"
#define HOLD_MS_MAX                60000U

static unsigned int hold_ms = 2000;
module_param(hold_ms, uint, 0444);
MODULE_PARM_DESC(hold_ms, "Milliseconds to hold the mailbox lock (max 60000)");

static void nvfanhold_release(struct work_struct *work)
{
	release_mem_region(ESPI_NS_SHM_PA, ESPI_NS_SHM_SIZE);
	pr_info("released the mailbox lock after %u ms\n", hold_ms);
}

static DECLARE_DELAYED_WORK(nvfanhold_work, nvfanhold_release);

static int __init nvfanhold_init(void)
{
	if (hold_ms > HOLD_MS_MAX) {
		pr_err("hold_ms=%u is above %u\n", hold_ms, HOLD_MS_MAX);
		return -EINVAL;
	}
	if (!request_mem_region(ESPI_NS_SHM_PA, ESPI_NS_SHM_SIZE,
				MAILBOX_LOCK_NAME)) {
		pr_err("mailbox lock %#llx is already held\n", ESPI_NS_SHM_PA);
		return -EBUSY;
	}
	pr_info("holding the mailbox lock %#llx for %u ms\n",
		ESPI_NS_SHM_PA, hold_ms);
	schedule_delayed_work(&nvfanhold_work, msecs_to_jiffies(hold_ms));
	return 0;
}

static void __exit nvfanhold_exit(void)
{
	/* If the work had not run yet, the lock is still ours. */
	if (cancel_delayed_work_sync(&nvfanhold_work)) {
		release_mem_region(ESPI_NS_SHM_PA, ESPI_NS_SHM_SIZE);
		pr_info("released the mailbox lock early at unload\n");
	}
}

module_init(nvfanhold_init);
module_exit(nvfanhold_exit);

MODULE_DESCRIPTION("Test tool: hold the DGX Spark nvfan-mailbox lock");
MODULE_LICENSE("GPL");
