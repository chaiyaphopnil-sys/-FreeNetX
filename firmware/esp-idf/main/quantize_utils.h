/*
 * Binary Stream Quantization helpers for FreeNet-X.
 *
 * Converts a 32-bit float into one signed byte using Min-Max scaling, and back.
 * Intended for low-bandwidth links (ESP-NOW, weak Wi-Fi) and compact buffers.
 */
#pragma once

#include <stdint.h>
#include <math.h>

/* Quantize float in [min_val, max_val] to int8 in [-128, 127]. */
static inline int8_t quantize_float_to_i8(float val, float min_val, float max_val)
{
    if (max_val <= min_val) return 0;
    if (val <= min_val) return -128;
    if (val >= max_val) return 127;

    float normal = (val - min_val) / (max_val - min_val); /* 0..1 */
    int16_t scaled = (int16_t)roundf(normal * 255.0f);    /* 0..255 */
    return (int8_t)(scaled - 128);                        /* -128..127 */
}

/* Reconstruct an approximate float from the quantized byte. */
static inline float dequantize_i8_to_float(int8_t qval, float min_val, float max_val)
{
    uint8_t u = (uint8_t)(qval + 128); /* 0..255 */
    float normal = (float)u / 255.0f;
    return normal * (max_val - min_val) + min_val;
}

/* Compact packet for a single sampled value. 6 bytes, packed. */
typedef struct __attribute__((packed)) {
    uint8_t  node_id;   /* 1 byte  */
    int8_t   quantized; /* 1 byte  */
    uint32_t timestamp; /* 4 bytes, milliseconds */
} compressed_sensor_t;
