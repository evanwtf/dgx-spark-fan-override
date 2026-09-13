obj-m += nvfanread.o

KERNELRELEASE ?= $(shell uname -r)
KDIR ?= /lib/modules/$(KERNELRELEASE)/build

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

# Repo-wide lint (checkpatch + shellcheck) lives at the top level; delegate so
# there is a single source of truth.
lint:
	$(MAKE) -C .. lint

# Full gate: the module must build and the unit tests must pass.
check: all test
	@echo "nvfanread: module build + unit tests OK"
