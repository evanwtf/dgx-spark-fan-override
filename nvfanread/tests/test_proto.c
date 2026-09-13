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

/* Guard the compile-time constants the module and decode logic depend on. */
static void test_layout_constants(void)
{
	CHECK_EQ(NVFR_EC_REPLY_LEN, 67);
	CHECK_EQ(NVFR_TELEMETRY_DATA_OFFSET, 0x13);
	CHECK_EQ(NVFR_TELEMETRY_LEN, 64);
	/* Reply must fit within our 128-byte snapshot/frame window. */
	CHECK(NVFR_TELEMETRY_DATA_OFFSET + NVFR_TELEMETRY_LEN <= NVFR_SNAP_LEN);
}

int main(void)
{
	test_le16();
	test_build_request_frame();
	test_build_request_frame_too_small();
	test_mailbox_idle();
	test_reply_header_ok();
	test_rpm_classification();
	test_layout_constants();

	if (g_failures == 0) {
		printf("ok - %d checks passed\n", g_checks);
		return 0;
	}
	fprintf(stderr, "NOT OK - %d/%d checks failed\n", g_failures, g_checks);
	return 1;
}
