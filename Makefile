# Repo-level convenience targets. Per-module kernel builds live under
# nvfanread/ and nvfancontrol/ (kbuild / DKMS).
.PHONY: lint test check

# Repo-wide lint: checkpatch (kernel C) + shellcheck (root maintainer scripts).
lint:
	sh scripts/lint.sh

# Unit tests for the read-only telemetry protocol logic.
test:
	$(MAKE) -C nvfanread test

# Build the nvfanread module and run its unit tests.
check:
	$(MAKE) -C nvfanread check
