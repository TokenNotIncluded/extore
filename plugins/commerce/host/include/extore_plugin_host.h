#ifndef EXTORE_PLUGIN_HOST_H
#define EXTORE_PLUGIN_HOST_H
#include <stddef.h>
#include <stdint.h>

/* This is the host bridge ABI, not a replacement for zplugin's native ABI.
 * The Rust bridge uses zplugin to load and verify extore.commerce v1.
 * All handle operations run on the opening OS thread.
 * Every successful catalog result must be released exactly once.
 * The native plugin image remains pinned until process exit. */
enum {
    EXTORE_HOST_OK = 0,
    EXTORE_HOST_LOAD_FAILED = -1,
    EXTORE_HOST_INVALID_ARGUMENT = -2,
    EXTORE_HOST_BUFFER_TOO_SMALL = -3,
    EXTORE_HOST_BUSY = -4,
    EXTORE_HOST_PANICKED = -5,
    EXTORE_HOST_POISONED = -6,
    EXTORE_HOST_WRONG_THREAD = -1001,
    EXTORE_COMMERCE_DENIED = 1001,
    EXTORE_COMMERCE_UNAVAILABLE = 1002,
    EXTORE_COMMERCE_PROTOCOL = 1003,
    EXTORE_COMMERCE_NOT_CONFIGURED = 1004,
    EXTORE_COMMERCE_ALREADY_CONFIGURED = 1005
};

typedef struct {
    const uint8_t *data;
    size_t len;
    uintptr_t owner;
} ExtoreBytes;

uint32_t extore_host_abi_version(void);
int32_t extore_host_open(const uint8_t *path, size_t path_len,
    const uint8_t *origin, size_t origin_len,
    const uint8_t *certificate_pem, size_t certificate_len,
    uintptr_t *out_handle);
int32_t extore_host_catalog(uintptr_t handle, const uint8_t *token,
    size_t token_len, ExtoreBytes *output);
void extore_host_release(uintptr_t owner);
int32_t extore_host_close(uintptr_t handle);
#endif
