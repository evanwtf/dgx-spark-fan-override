obj-m += nvfanread.o

KERNELRELEASE ?= $(shell uname -r)
KDIR ?= /lib/modules/$(KERNELRELEASE)/build
CHECKPATCH := $(KDIR)/scripts/checkpatch.pl

.PHONY: all clean test lint check

# Build the kernel module against the running (or specified) kernel.
all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
	$(MAKE) -C tests clean

# Userspace unit tests for the pure protocol logic (no kernel, no hardware).
test:
	$(MAKE) -C tests test

# Advisory kernel style check. NEW_TYPEDEFS is intentionally ignored: the
# shared header defines dual-mode (kernel + userspace) fixed-width aliases.
lint:
	@if [ -x "$(CHECKPATCH)" ]; then \
		"$(CHECKPATCH)" --no-tree --terse --ignore NEW_TYPEDEFS \
			-f nvfanread.c nvfanread_proto.h; \
	else \
		echo "checkpatch.pl not found under $(KDIR); skipping lint"; \
	fi

# Full gate: the module must build and the unit tests must pass.
check: all test
	@echo "nvfanread: module build + unit tests OK"
