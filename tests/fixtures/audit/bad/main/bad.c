#include "driver/gpio.h"
#include "driver/i2s_std.h"

#define LED_PIN GPIO_NUM_2

void led_init(void) {
  gpio_config_t cfg = {.pin_bit_mask = 1ULL << LED_PIN, .mode = GPIO_MODE_OUTPUT};
  gpio_config(&cfg);
}

void probe_init(void) {
  gpio_config_t cfg = {.pin_bit_mask = 1ULL << GPIO_NUM_2, .mode = GPIO_MODE_INPUT};
  gpio_config(&cfg);
}

void other(void) {
  /* input-only pin with an internal pull */
  gpio_config_t c = {.pin_bit_mask = 1ULL << 34, .mode = GPIO_MODE_INPUT,
                     .pull_up_en = GPIO_PULLUP_ENABLE};
  gpio_config(&c);
  /* planned input set as output */
  gpio_set_direction(GPIO_NUM_32, GPIO_MODE_OUTPUT);
  /* plan says none, code pulls up */
  gpio_set_pull_mode(GPIO_NUM_16, GPIO_PULLUP_ONLY);
  /* planned internal_up never enabled */
  gpio_config_t e = {.pin_bit_mask = 1ULL << 27, .mode = GPIO_MODE_INPUT};
  gpio_config(&e);
  /* unplanned */
  gpio_set_direction(GPIO_NUM_15, GPIO_MODE_OUTPUT);
}

void audio(void) {
  i2s_std_config_t s = {.gpio_cfg = {.bclk = GPIO_NUM_17, .ws = GPIO_NUM_25, .dout = GPIO_NUM_33}};
  (void)s;
}
