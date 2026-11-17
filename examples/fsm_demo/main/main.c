#include <stdint.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_timer.h"
#include "driver/gpio.h"
#include "board_pins.h"
#include "fsm_sensor.h"

static sensor_ctx_t sensor;
static int reads;

/* Stand-in for a real sensor: every read fails, so 'after 3 read failed' is reached. */
static void run_action(sensor_action_t action)
{
    if (action == SENSOR_ACTION_READ_SENSOR) {
        reads++;
        printf("read sensor (attempt %d): failed\n", reads);
        sensor_dispatch(&sensor, SENSOR_EVENT_READ_FAILED);
        printf("state: %s\n", sensor_state_name(sensor.state));
    }
}

void app_main(void)
{
    board_pins_init();
    sensor_init(&sensor);
    printf("state: %s\n", sensor_state_name(sensor.state));

    int last = gpio_get_level(PIN_BUTTON);
    for (;;) {
        int b = gpio_get_level(PIN_BUTTON);
        if (last == 1 && b == 0) { /* pressed: pull-up, active low */
            sensor_dispatch(&sensor, SENSOR_EVENT_BUTTON_PRESS);
            printf("button press -> state: %s\n", sensor_state_name(sensor.state));
        }
        last = b;
        sensor_tick(&sensor, (uint32_t)(esp_timer_get_time() / 1000), run_action);
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}
