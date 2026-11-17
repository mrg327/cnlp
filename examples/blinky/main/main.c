#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "board_pins.h"

void app_main(void)
{
    board_pins_init();
    board_pins_readback();

    int led = 0;
    int last_button = gpio_get_level(PIN_BUTTON);
    printf("button level: %d\n", last_button);

    for (int tick = 0;; tick++) {
        /* Poll the button every 50 ms; toggle the LED every 500 ms. */
        if (tick % 10 == 0) {
            led = !led;
            gpio_set_level(PIN_LED, led);
        }
        int b = gpio_get_level(PIN_BUTTON);
        if (b != last_button) {
            last_button = b;
            printf("button level: %d\n", b);
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}
