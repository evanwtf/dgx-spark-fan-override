# Userspace unit tests for the pure protocol logic (no kernel, no hardware).
CC ?= cc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O2

.PHONY: test clean
test: test_proto
	./test_proto

test_proto: test_proto.c ../nvfanread_proto.h
	$(CC) $(CFLAGS) -o $@ test_proto.c

clean:
	rm -f test_proto
