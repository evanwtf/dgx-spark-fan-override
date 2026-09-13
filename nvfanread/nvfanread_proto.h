/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Pure, hardware-independent protocol logic for the DGX Spark read-only EC
 * fan telemetry probe.
 *
 * Everything here is free of kernel and hardware side effects so it can be
 * compiled and unit-tested in plain userspace (see tests/). The kernel module
 * includes this header and performs the actual FF-A / shared-page I/O around
 * these helpers; those I/O paths cannot be unit-tested without loading the
 * module, and are validated at load time instead.
 */
#ifndef NVFANREAD_PROTO_H
#define NVFANREAD_PROTO_H

/*
 * Fixed-width aliases so the same helpers compile in the kernel and in the
 * userspace tests. checkpatch flags these as new typedefs; that is intentional
 * for a header shared across both worlds (the lint target ignores NEW_TYPEDEFS).
 */
#ifdef __KERNEL__
#include <linux/types.h>
typedef u8  nvfr_u8;
typedef u16 nvfr_u16;
typedef size_t nvfr_size;
#else
#include <stdint.h>
#include <stddef.h>
typedef uint8_t  nvfr_u8;
typedef uint16_t nvfr_u16;
typedef size_t   nvfr_size;
#endif

/* Shared-page frame layout (OEM1 command 17). */
#define NVFR_FRAME_INPUT_LEN        0U
#define NVFR_FRAME_OUTPUT_LEN       1U
#define NVFR_FRAME_OUTPUT_OFFSET    2U
#define NVFR_FRAME_INPUT_ACCEPTED   3U
#define NVFR_FRAME_OUTPUT_READY     4U
#define NVFR_FRAME_DATA_OFFSET      0x10U

/* EC generic frame: [0]=outer service, [1]=inner command, [2]=status. */
#define NVFR_THERMAL_OUTER_COMMAND  0x07U
#define NVFR_THERMAL_TELEMETRY_READ 0x07U   /* EC inner command 7, read-only */

#define NVFR_EC_REQUEST_LEN         3U      /* header only; command 7 has no input data */
#define NVFR_TELEMETRY_LEN          64U     /* bytes copied from EC SRAM 0x1188E2 */
#define NVFR_EC_REPLY_LEN           (NVFR_EC_REQUEST_LEN + NVFR_TELEMETRY_LEN)
#define NVFR_TELEMETRY_DATA_OFFSET  (NVFR_FRAME_DATA_OFFSET + NVFR_EC_REQUEST_LEN)

/*
 * Region of the shared page the module snapshots and restores around a
 * request. Must be large enough to hold the whole reply
 * (NVFR_TELEMETRY_DATA_OFFSET + NVFR_TELEMETRY_LEN); asserted in the tests.
 */
#define NVFR_SNAP_LEN               128U

/* Documented per-channel RPM ranges, used only to flag decode candidates. */
#define NVFR_FAN0_RPM_MIN           1260U
#define NVFR_FAN0_RPM_MAX           9000U
#define NVFR_FAN1_RPM_MIN           1890U
#define NVFR_FAN1_RPM_MAX           13500U

/* Candidate flags returned by nvfr_rpm_flags(). */
#define NVFR_CAND_NONE              0U
#define NVFR_CAND_FAN0              (1U << 0)
#define NVFR_CAND_FAN1              (1U << 1)

/* Read a little-endian u16 from a byte pointer (alignment-independent). */
static inline nvfr_u16 nvfr_le16(const nvfr_u8 *p)
{
	return (nvfr_u16)((nvfr_u16)p[0] | ((nvfr_u16)p[1] << 8));
}

/*
 * Build the shared-page frame that requests EC command 7. Zeroes @frame,
 * writes the length/offset header and the 3-byte EC request. Returns 0 on
 * success, -1 if @frame is too small to hold the request.
 *
 * This is READ-ONLY by construction: the only EC command it can ever emit is
 * command 7 (telemetry). It contains no path to override commands 3 or 5.
 */
static inline int nvfr_build_request_frame(nvfr_u8 *frame, nvfr_size frame_len)
{
	nvfr_size i;

	if (frame_len < NVFR_FRAME_DATA_OFFSET + NVFR_EC_REQUEST_LEN)
		return -1;

	for (i = 0; i < frame_len; i++)
		frame[i] = 0;

	frame[NVFR_FRAME_INPUT_LEN] = NVFR_EC_REQUEST_LEN;
	frame[NVFR_FRAME_OUTPUT_LEN] = NVFR_EC_REPLY_LEN;
	frame[NVFR_FRAME_OUTPUT_OFFSET] = 0;
	frame[NVFR_FRAME_DATA_OFFSET + 0] = NVFR_THERMAL_OUTER_COMMAND;
	frame[NVFR_FRAME_DATA_OFFSET + 1] = NVFR_THERMAL_TELEMETRY_READ;
	frame[NVFR_FRAME_DATA_OFFSET + 2] = 0;
	return 0;
}

/* True if the mailbox handshake bytes indicate an idle mailbox. */
static inline int nvfr_mailbox_idle(nvfr_u8 accepted, nvfr_u8 ready)
{
	return accepted == 0 && ready == 0;
}

/*
 * Validate the EC reply header (the 3 bytes at the frame data offset). A
 * successful command-7 reply echoes [07][07][00].
 */
static inline int nvfr_reply_header_ok(const nvfr_u8 *reply)
{
	return reply[0] == NVFR_THERMAL_OUTER_COMMAND &&
	       reply[1] == NVFR_THERMAL_TELEMETRY_READ &&
	       reply[2] == 0;
}

/* True if @v falls inside the union of both fans' documented RPM ranges. */
static inline int nvfr_is_rpm_candidate(nvfr_u16 v)
{
	return v >= NVFR_FAN0_RPM_MIN && v <= NVFR_FAN1_RPM_MAX;
}

/*
 * Classify a candidate value against the per-channel ranges. Returns a mask of
 * NVFR_CAND_FAN0 / NVFR_CAND_FAN1. Given fan0 [1260,9000] and fan1
 * [1890,13500]: values in [1260,1889] set FAN0 only, values in the overlap
 * [1890,9000] set both bits, values in [9001,13500] set FAN1 only, and
 * anything outside [1260,13500] returns NVFR_CAND_NONE.
 */
static inline unsigned int nvfr_rpm_flags(nvfr_u16 v)
{
	unsigned int flags = NVFR_CAND_NONE;

	if (!nvfr_is_rpm_candidate(v))
		return NVFR_CAND_NONE;
	if (v <= NVFR_FAN0_RPM_MAX)
		flags |= NVFR_CAND_FAN0;
	if (v >= NVFR_FAN1_RPM_MIN)
		flags |= NVFR_CAND_FAN1;
	return flags;
}

#endif /* NVFANREAD_PROTO_H */
