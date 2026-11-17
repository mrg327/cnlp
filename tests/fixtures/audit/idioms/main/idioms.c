#include "driver/gpio.h"
#include "driver/i2s_std.h"
#include "driver/i2c_master.h"

#define LED_PIN GPIO_NUM_2
#define BTN 5
#define OUT_A GPIO_NUM_12
#define OUT_B GPIO_NUM_13
#define OUT_MASK ((1ULL << OUT_A) | BIT64(OUT_B))
#define WS_PIN GPIO_NUM_25

static const gpio_num_t row_pins[3] = {GPIO_NUM_16, GPIO_NUM_17, GPIO_NUM_18};
static const gpio_num_t col_pins[] = {GPIO_NUM_34, GPIO_NUM_35, GPIO_NUM_23};
static const int NCOLS = 3;

void setup_led(void) {
  gpio_config_t cfg = {
      .pin_bit_mask = 1ULL << LED_PIN,
      .mode = GPIO_MODE_INPUT_OUTPUT,
      .pull_up_en = GPIO_PULLUP_DISABLE,
      .pull_down_en = GPIO_PULLDOWN_DISABLE,
      .intr_type = GPIO_INTR_DISABLE,
  };
  ESP_ERROR_CHECK(gpio_config(&cfg));
}

void setup_defines(void) {
  gpio_config_t o = {.pin_bit_mask = OUT_MASK, .mode = GPIO_MODE_OUTPUT};
  gpio_config(&o);
  gpio_set_direction(BTN, GPIO_MODE_INPUT);
  gpio_pullup_en(BTN);
}

void setup_matrix(void) {
  uint64_t row_mask = 0;
  for (int r = 0; r < 3; r++) {
    row_mask |= 1ULL << row_pins[r];
  }
  uint64_t col_mask = 0;
  for (int c = 0; c < NCOLS; c++) {
    col_mask |= 1ULL << col_pins[c];
  }
  gpio_config_t rows = {.pin_bit_mask = row_mask, .mode = GPIO_MODE_OUTPUT};
  gpio_config(&rows);
  gpio_config_t cols;
  cols.pin_bit_mask = col_mask;
  cols.mode = GPIO_MODE_INPUT;
  cols.pull_up_en = GPIO_PULLUP_DISABLE;
  gpio_config(&cols);
}

void setup_i2s(void) {
  i2s_std_config_t std_cfg = {
      .gpio_cfg =
          {
              .mclk = I2S_GPIO_UNUSED,
              .bclk = GPIO_NUM_26,
              .ws = WS_PIN,
              .dout = GPIO_NUM_33,
              .din = I2S_GPIO_UNUSED,
          },
  };
  (void)std_cfg;
}

void setup_i2c(void) {
  i2c_master_bus_config_t bus = {
      .sda_io_num = GPIO_NUM_21,
      .scl_io_num = GPIO_NUM_22,
      .flags.enable_internal_pullup = true,
  };
  (void)bus;
}

void setup_param(uint64_t mask) {
  gpio_config_t p = {.pin_bit_mask = mask, .mode = GPIO_MODE_OUTPUT};
  gpio_config(&p);
}
