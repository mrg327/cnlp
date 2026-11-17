#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"

#define NOWAIT 0
#define WAIT_MS 10

static int shared_count;
static const int limit = 4;
static QueueHandle_t q;

static void render(void) {
  printf("x");
  shared_count++;
}

static void audio_task(void *arg) {
  float x = 0.5;
  for (;;) {
    render();
    xQueueReceive(q, &x, NOWAIT);
  }
}

static void ui_task(void *arg) {
  for (;;) {
    shared_count = 3;
    xQueueSend(q, &shared_count, pdMS_TO_TICKS(WAIT_MS));
    vTaskDelay(1);
  }
}

static void late_init(void) { q = xQueueCreate(4, 4); }

void app_main(void) {
  xTaskCreatePinnedToCore(audio_task, "audio", 2048, NULL, 20, NULL, 1);
  late_init();
  xTaskCreatePinnedToCore(ui_task, "ui", 2048, NULL, 5, NULL, 0);
}
