#include "driver/i2c_master.h"
#include "driver/pulse_cnt.h"

#define PIN_ENC_A GPIO_NUM_32
#define PIN_ENC_B 13
#define OLED_ADDR 0x3C
#define I2C_HZ 400000
#define ADDR_LEN I2C_ADDR_BIT_LEN_7

void i2c_init(void) {
  i2c_master_bus_config_t bus = {
      .i2c_port = -1,
      .sda_io_num = GPIO_NUM_21,
      .scl_io_num = GPIO_NUM_22,
  };
  i2c_device_config_t dev = {
      .dev_addr_length = ADDR_LEN,
      .device_address = OLED_ADDR,
  };
  dev.scl_speed_hz = I2C_HZ;
}

void pcnt_init(void) {
  pcnt_unit_config_t unit = {.high_limit = 100, .low_limit = -100};
  pcnt_glitch_filter_config_t filt = {.max_glitch_ns = 1000};
  pcnt_chan_config_t chan_a = {.edge_gpio_num = PIN_ENC_A, .level_gpio_num = PIN_ENC_B};
  pcnt_chan_config_t chan_b = {.edge_gpio_num = PIN_ENC_B, .level_gpio_num = PIN_ENC_A};
}

void bad_pcnt(void) {
  pcnt_chan_config_t stray = {.edge_gpio_num = 5, .level_gpio_num = unknown_pin()};
}
