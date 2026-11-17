#include "driver/i2s_std.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define RATE_BASE 48000
#define AUDIO_RATE RATE_BASE
#define AUDIO_FRAMES 128
#define AUDIO_DMA 2
#define AUDIO_PRIO 20
#define AUDIO_CORE 1

static void audio_task(void *arg) { for (;;) {} }

void audio_init(void) {
  i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);
  chan_cfg.dma_desc_num = AUDIO_DMA;
  chan_cfg.dma_frame_num = AUDIO_FRAMES;
  i2s_std_config_t std_cfg = {
      .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(AUDIO_RATE),
  };
}

void audio_start(void) {
  BaseType_t ok = xTaskCreatePinnedToCore(audio_task, "audio", 2048, NULL, AUDIO_PRIO, NULL, AUDIO_CORE);
}
