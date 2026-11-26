// SPDX-License-Identifier: GPL-2.0-only
/*
 * Userspace unit tests for the pure protocol logic in nvfanread_proto.h.
 *
 * These run without a kernel or hardware: they exercise the frame builder,
 * mailbox/reply validation, and RPM-candidate classification that the module
 * relies on. The FF-A / shared-page I/O in nvfanread.c is NOT covered here
 * (it cannot be exercised without loading the module) and is validated at
 * load time instead.
 *
 * No external test framework: a couple of CHECK macros and a summary. Exit
 * status is 0 only if every check passed.
 */
#include <errno.h>
#include <stdio.h>
#include <string.h>

#include "../nvfanread_proto.h"

static int g_checks;
static int g_failures;

#define CHECK(cond)                                                            \
	do {                                                                  \
		g_checks++;                                                   \
		if (!(cond)) {                                                \
			g_failures++;                                        \
			fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__,        \
				__LINE__, #cond);                            \
		}                                                            \
	} while (0)

#define CHECK_EQ(a, b)                                                         \
	do {                                                                  \
		long _a = (long)(a);                                         \
		long _b = (long)(b);                                         \
		g_checks++;                                                  \
		if (_a != _b) {                                              \
			g_failures++;                                        \
			fprintf(stderr, "FAIL %s:%d: %s (%ld) != %s (%ld)\n", \
				__FILE__, __LINE__, #a, _a, #b, _b);         \
		}                                                            \
	} while (0)

static void test_le16(void)
{
	const nvfr_u8 bytes[] = { 0xBC, 0x34 };      /* 0x34BC = 13500 */

	CHECK_EQ(nvfr_le16(bytes), 13500);
	{
		const nvfr_u8 zero[] = { 0x00, 0x00 };
		const nvfr_u8 lo[]   = { 0xEC, 0x04 };   /* 0x04EC = 1260 */
		const nvfr_u8 ff[]   = { 0xFF, 0xFF };

		CHECK_EQ(nvfr_le16(zero), 0);
		CHECK_EQ(nvfr_le16(lo), 1260);
		CHECK_EQ(nvfr_le16(ff), 0xFFFF);
	}
}

static void test_build_request_frame(void)
{
	nvfr_u8 frame[NVFR_SNAP_LEN];
	unsigned int i;

	memset(frame, 0xAA, sizeof(frame));
	CHECK_EQ(nvfr_build_request_frame(frame, sizeof(frame)), 0);

	/* Header describes a 3-byte request and a 67-byte reply at offset 0. */
	CHECK_EQ(frame[NVFR_FRAME_INPUT_LEN], NVFR_EC_REQUEST_LEN);
	CHECK_EQ(frame[NVFR_FRAME_OUTPUT_LEN], NVFR_EC_REPLY_LEN);
	CHECK_EQ(frame[NVFR_FRAME_OUTPUT_OFFSET], 0);
	CHECK_EQ(frame[NVFR_FRAME_INPUT_ACCEPTED], 0);
	CHECK_EQ(frame[NVFR_FRAME_OUTPUT_READY], 0);

	/* The EC request is exactly [07][07][00]. */
	CHECK_EQ(frame[NVFR_FRAME_DATA_OFFSET + 0], NVFR_THERMAL_OUTER_COMMAND);
	CHECK_EQ(frame[NVFR_FRAME_DATA_OFFSET + 1], NVFR_THERMAL_TELEMETRY_READ);
	CHECK_EQ(frame[NVFR_FRAME_DATA_OFFSET + 2], 0);

	/* Safety: the inner command is the read-only telemetry command (7),
	 * never an override write (3 = write low slot, 5 = write high slot).
	 */
	CHECK(frame[NVFR_FRAME_DATA_OFFSET + 1] != 0x03);
	CHECK(frame[NVFR_FRAME_DATA_OFFSET + 1] != 0x05);

	/* Everything else is zeroed (0xAA fill fully overwritten). */
	for (i = 0; i < sizeof(frame); i++) {
		if (i == NVFR_FRAME_INPUT_LEN || i == NVFR_FRAME_OUTPUT_LEN ||
		    i == NVFR_FRAME_DATA_OFFSET ||
		    i == NVFR_FRAME_DATA_OFFSET + 1)
			continue;
		CHECK_EQ(frame[i], 0);
	}
}

static void test_build_request_frame_too_small(void)
{
	nvfr_u8 tiny[NVFR_FRAME_DATA_OFFSET + NVFR_EC_REQUEST_LEN - 1];

	CHECK_EQ(nvfr_build_request_frame(tiny, sizeof(tiny)), -1);
}

static void test_mailbox_idle(void)
{
	CHECK(nvfr_mailbox_idle(0, 0));
	CHECK(!nvfr_mailbox_idle(1, 0));
	CHECK(!nvfr_mailbox_idle(0, 1));
	CHECK(!nvfr_mailbox_idle(0xFF, 0xFF));
}

static void test_reply_header_ok(void)
{
	const nvfr_u8 good[]  = { 0x07, 0x07, 0x00 };
	const nvfr_u8 badcmd[] = { 0x07, 0x01, 0x00 };
	const nvfr_u8 badsts[] = { 0x07, 0x07, 0xFF };
	const nvfr_u8 badout[] = { 0x00, 0x07, 0x00 };

	CHECK(nvfr_reply_header_ok(good));
	CHECK(!nvfr_reply_header_ok(badcmd));
	CHECK(!nvfr_reply_header_ok(badsts));
	CHECK(!nvfr_reply_header_ok(badout));
}

static void test_rpm_classification(void)
{
	/* Below both ranges. */
	CHECK(!nvfr_is_rpm_candidate(1259));
	CHECK_EQ(nvfr_rpm_flags(1259), NVFR_CAND_NONE);

	/* fan0-only window [1260, 1889]. */
	CHECK(nvfr_is_rpm_candidate(1260));
	CHECK_EQ(nvfr_rpm_flags(1260), NVFR_CAND_FAN0);
	CHECK_EQ(nvfr_rpm_flags(1889), NVFR_CAND_FAN0);

	/* Overlap [1890, 9000]: both. */
	CHECK_EQ(nvfr_rpm_flags(1890), NVFR_CAND_FAN0 | NVFR_CAND_FAN1);
	CHECK_EQ(nvfr_rpm_flags(5000), NVFR_CAND_FAN0 | NVFR_CAND_FAN1);
	CHECK_EQ(nvfr_rpm_flags(9000), NVFR_CAND_FAN0 | NVFR_CAND_FAN1);

	/* fan1-only window [9001, 13500]. */
	CHECK_EQ(nvfr_rpm_flags(9001), NVFR_CAND_FAN1);
	CHECK_EQ(nvfr_rpm_flags(13500), NVFR_CAND_FAN1);

	/* Above both ranges. */
	CHECK(!nvfr_is_rpm_candidate(13501));
	CHECK_EQ(nvfr_rpm_flags(13501), NVFR_CAND_NONE);

	/* is_candidate is exactly "some fan matched". */
	CHECK_EQ(nvfr_is_rpm_candidate(7000) != 0, nvfr_rpm_flags(7000) != 0);
	CHECK_EQ(nvfr_is_rpm_candidate(100) != 0, nvfr_rpm_flags(100) != 0);
}

static void test_fan_rpm_decode(void)
{
	nvfr_u8 snap[NVFR_TELEMETRY_LEN];
	nvfr_u16 fan0, fan1;
	unsigned int i;

	for (i = 0; i < NVFR_TELEMETRY_LEN; i++)
		snap[i] = 0;

	/* fan0 at bytes 4-5 (LE) = 0x0813 = 2067; fan1 at 6-7 = 0x0A28 = 2600 */
	snap[4] = 0x13;
	snap[5] = 0x08;
	snap[6] = 0x28;
	snap[7] = 0x0A;
	nvfr_snapshot_fan_rpm(snap, &fan0, &fan1);
	CHECK_EQ(fan0, 2067);
	CHECK_EQ(fan1, 2600);

	/* Distinct offsets: changing only fan1's bytes leaves fan0 alone. */
	snap[6] = 0xBC;
	snap[7] = 0x34;                 /* 0x34BC = 13500 */
	nvfr_snapshot_fan_rpm(snap, &fan0, &fan1);
	CHECK_EQ(fan0, 2067);
	CHECK_EQ(fan1, 13500);

	/* Snapshot offsets line up with the documented reply offsets 7 and 9. */
	CHECK_EQ(NVFR_SNAPSHOT_FAN0_RPM_OFFSET + NVFR_EC_REQUEST_LEN, 7);
	CHECK_EQ(NVFR_SNAPSHOT_FAN1_RPM_OFFSET + NVFR_EC_REQUEST_LEN, 9);
}

/* Guard the compile-time constants the module and decode logic depend on. */
static void test_layout_constants(void)
{
	CHECK_EQ(NVFR_EC_REPLY_LEN, 67);
	CHECK_EQ(NVFR_TELEMETRY_DATA_OFFSET, 0x13);
	CHECK_EQ(NVFR_TELEMETRY_LEN, 64);
	/* Reply must fit within our 128-byte snapshot/frame window. */
	CHECK(NVFR_TELEMETRY_DATA_OFFSET + NVFR_TELEMETRY_LEN <= NVFR_SNAP_LEN);
}

/*
 * The module finds the eSPI service by UUID on the FF-A bus instead of binding
 * it (the built-in nvidia-ffa-ec driver binds it on 7.0+ kernels).
 */
static void test_uuid_match(void)
{
	/* 884a63a0-3285-4120-83aa-eec008a0a546 in uuid_t (big-endian) order. */
	const nvfr_u8 espi[16] = {
		0x88, 0x4a, 0x63, 0xa0, 0x32, 0x85, 0x41, 0x20,
		0x83, 0xaa, 0xee, 0xc0, 0x08, 0xa0, 0xa5, 0x46,
	};
	/* The same UUID in ACPI ToUUID() mixed-endian order must NOT match. */
	const nvfr_u8 mixed[16] = {
		0xa0, 0x63, 0x4a, 0x88, 0x85, 0x32, 0x20, 0x41,
		0x83, 0xaa, 0xee, 0xc0, 0x08, 0xa0, 0xa5, 0x46,
	};
	/* Fan service 7697530c-d079-4ec1-a4c4-cf0d2bdc93fa: another EC service. */
	const nvfr_u8 fan_svc[16] = {
		0x76, 0x97, 0x53, 0x0c, 0xd0, 0x79, 0x4e, 0xc1,
		0xa4, 0xc4, 0xcf, 0x0d, 0x2b, 0xdc, 0x93, 0xfa,
	};
	const nvfr_u8 null_uuid[16] = { 0 };
	nvfr_u8 last_byte_off[16];

	CHECK(nvfr_uuid_is_espi(espi));
	CHECK(!nvfr_uuid_is_espi(mixed));
	CHECK(!nvfr_uuid_is_espi(fan_svc));
	CHECK(!nvfr_uuid_is_espi(null_uuid));

	memcpy(last_byte_off, espi, sizeof(last_byte_off));
	last_byte_off[15] ^= 0x01;
	CHECK(!nvfr_uuid_is_espi(last_byte_off));
}

/*
 * Retry policy for the FF-A send. The EC secure partition is shared by many
 * services and answers FFA_RET_BUSY (-EBUSY) while another request is in
 * flight. Only -EBUSY retries, and only inside both bounds.
 */
static void test_send_retry_policy(void)
{
	/* Success and non-busy errors never retry. */
	CHECK(!nvfr_send_should_retry(0, 1, 0));
	CHECK(!nvfr_send_should_retry(-EIO, 1, 0));
	CHECK(!nvfr_send_should_retry(-EAGAIN, 1, 0));
	CHECK(!nvfr_send_should_retry(-EINVAL, 1, 0));
	CHECK(!nvfr_send_should_retry(-EOPNOTSUPP, 1, 0));
	CHECK(!nvfr_send_should_retry(-ETIMEDOUT, 1, 0));
	CHECK(!nvfr_send_should_retry(EBUSY, 1, 0));    /* wrong sign */

	/* -EBUSY retries while both bounds hold. */
	CHECK(nvfr_send_should_retry(-EBUSY, 1, 0));
	CHECK(nvfr_send_should_retry(-EBUSY, NVFR_SEND_MAX_ATTEMPTS - 1,
				     NVFR_SEND_RETRY_BUDGET_US - 1));

	/* The attempt bound stops it. */
	CHECK(!nvfr_send_should_retry(-EBUSY, NVFR_SEND_MAX_ATTEMPTS, 0));
	CHECK(!nvfr_send_should_retry(-EBUSY, NVFR_SEND_MAX_ATTEMPTS + 1, 0));

	/* The time bound stops it. */
	CHECK(!nvfr_send_should_retry(-EBUSY, 1, NVFR_SEND_RETRY_BUDGET_US));
	CHECK(!nvfr_send_should_retry(-EBUSY, 1, NVFR_SEND_RETRY_BUDGET_US + 1));

	/* Bounds match nvidia-ffa-ec's 10 ms FFH retry, and stay small. */
	CHECK_EQ(NVFR_SEND_RETRY_BUDGET_US, 10000);
	CHECK(NVFR_SEND_MAX_ATTEMPTS >= 2);
	CHECK(NVFR_SEND_MAX_ATTEMPTS <= 50);
}

/*
 * Mailbox owner check: the shared page has no lock, so nvfanread only uses it
 * while the FF-A device is unbound, bound to nvidia-ffa-ec (which never uses
 * the page), or bound to nvfanread itself.
 */
static void test_owner_allowed(void)
{
	CHECK(nvfr_owner_allowed(NULL));
	CHECK(nvfr_owner_allowed("nvidia-ffa-ec"));
	CHECK(nvfr_owner_allowed("nvfanread"));
	CHECK_EQ(NVFR_DRIVER_NAME[0], 'n');

	/* Drivers that write the shared page themselves. */
	CHECK(!nvfr_owner_allowed("nvfancontrol"));
	CHECK(!nvfr_owner_allowed("nvfanprobe"));       /* tools/ec-probe */

	/* Near misses and junk must not pass. */
	CHECK(!nvfr_owner_allowed(""));
	CHECK(!nvfr_owner_allowed("nvidia-ffa-e"));
	CHECK(!nvfr_owner_allowed("nvidia-ffa-ec2"));
	CHECK(!nvfr_owner_allowed("nvidia-ffa-control"));
	CHECK(!nvfr_owner_allowed("nvfanread "));
	CHECK(!nvfr_owner_allowed("NVFANREAD"));
}

/*
 * Cache decision. Results (success or failure) are reused while younger than
 * NVFR_CACHE_US, so a reader loop cannot hammer the shared SP.
 */
static void test_cache_fresh(void)
{
	CHECK(nvfr_cache_fresh(1, 0));
	CHECK(nvfr_cache_fresh(1, NVFR_CACHE_US - 1));

	CHECK(!nvfr_cache_fresh(0, 0));                 /* nothing cached */
	CHECK(!nvfr_cache_fresh(1, NVFR_CACHE_US));     /* at the bound */
	CHECK(!nvfr_cache_fresh(1, NVFR_CACHE_US + 1));
	CHECK(!nvfr_cache_fresh(1, -1));                /* clock went back */

	CHECK_EQ(NVFR_CACHE_US, 200000);
}

/*
 * Mailbox lock retry: a busy region is tried again only inside both bounds.
 * Neither bound may be zero, or a module that holds the lock for a normal
 * transaction would make every other request fail at once.
 */
static void test_lock_retry_policy(void)
{
	/* Retries while both bounds hold. */
	CHECK(nvfr_lock_should_retry(1, 0));
	CHECK(nvfr_lock_should_retry(NVFR_LOCK_MAX_ATTEMPTS - 1,
				     NVFR_LOCK_RETRY_BUDGET_US - 1));

	/* The attempt bound stops it. */
	CHECK(!nvfr_lock_should_retry(NVFR_LOCK_MAX_ATTEMPTS, 0));
	CHECK(!nvfr_lock_should_retry(NVFR_LOCK_MAX_ATTEMPTS + 1, 0));

	/* The time bound stops it. */
	CHECK(!nvfr_lock_should_retry(1, NVFR_LOCK_RETRY_BUDGET_US));
	CHECK(!nvfr_lock_should_retry(1, NVFR_LOCK_RETRY_BUDGET_US + 1));

	/* A clock that went back must not extend the wait. */
	CHECK(!nvfr_lock_should_retry(1, -1));

	/* Bounded at 50 ms total; at least two tries. */
	CHECK_EQ(NVFR_LOCK_RETRY_BUDGET_US, 50000);
	CHECK(NVFR_LOCK_MAX_ATTEMPTS >= 2);
	CHECK(NVFR_LOCK_MAX_ATTEMPTS <= 100);

	/* The name shows in /proc/iomem while a transaction runs. */
	CHECK_EQ(strcmp(NVFR_MAILBOX_LOCK_NAME, "nvfan-mailbox"), 0);
}

int main(void)
{
	test_le16();
	test_build_request_frame();
	test_build_request_frame_too_small();
	test_mailbox_idle();
	test_reply_header_ok();
	test_rpm_classification();
	test_fan_rpm_decode();
	test_layout_constants();
	test_uuid_match();
	test_send_retry_policy();
	test_owner_allowed();
	test_cache_fresh();
	test_lock_retry_policy();

	if (g_failures == 0) {
		printf("ok - %d checks passed\n", g_checks);
		return 0;
	}
	fprintf(stderr, "NOT OK - %d/%d checks failed\n", g_failures, g_checks);
	return 1;
}
