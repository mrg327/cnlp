#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define SCAN_PRIO (configMAX_PRIORITIES - 15)
#define UNKNOWN_PRIO get_prio()

static void scan_task(void *arg) {}
static void ui_task(void *arg) {}
static void display_task(void *arg) {}

void start_others(void) {
  xTaskCreatePinnedToCore(scan_task, "scan", 2048, NULL, SCAN_PRIO, NULL, 1);
  xTaskCreate(ui_task, "ui", 2048, NULL, 5, NULL);
  xTaskCreatePinnedToCore(display_task, "display", 2048, NULL, UNKNOWN_PRIO, NULL, tskNO_AFFINITY);
}
