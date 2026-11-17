/* Phase 0a spike: static asserts on GPIO validity macros, and
 * gpio_dump_io_configuration() output stability. */
#include <stdio.h>
#include "driver/gpio.h"
#include "soc/soc_caps.h"

#define PIN_LED    GPIO_NUM_25
#define PIN_BUTTON GPIO_NUM_4
#define PIN_SENSE  GPIO_NUM_34

_Static_assert(GPIO_IS_VALID_OUTPUT_GPIO(PIN_LED), "PIN_LED must be output-capable");
_Static_assert(GPIO_IS_VALID_GPIO(PIN_BUTTON), "PIN_BUTTON must be a valid GPIO");
_Static_assert(!GPIO_IS_VALID_OUTPUT_GPIO(PIN_SENSE), "GPIO34 is input-only");
#ifdef SPIKE_EXPECT_FAIL
_Static_assert(GPIO_IS_VALID_OUTPUT_GPIO(GPIO_NUM_34), "expected failure: GPIO34 output");
#endif

void app_main(void)
{
    gpio_config_t out = {
        .pin_bit_mask = 1ULL << PIN_LED, .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE, .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config_t in = {
        .pin_bit_mask = 1ULL << PIN_BUTTON, .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE, .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&out);
    gpio_config(&in);
    uint64_t mask = (1ULL << PIN_LED) | (1ULL << PIN_BUTTON) | (1ULL << PIN_SENSE)
                  | (1ULL << 21) | (1ULL << 22);
    for (int i = 0; i < 2; i++) {
        printf("=== DUMP %d BEGIN ===\n", i);
        gpio_dump_io_configuration(stdout, mask);
        printf("=== DUMP %d END ===\n", i);
    }
    printf("=== FULL BEGIN ===\n");
    gpio_dump_io_configuration(stdout, SOC_GPIO_VALID_GPIO_MASK);
    printf("=== FULL END ===\n");
}
