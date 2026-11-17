from __future__ import annotations

from pathlib import Path

from cnlp import lint, rtlint

FIX = Path(__file__).resolve().parent / "fixtures" / "rtlint"

BASE = {
    "tasks": {"audio": {"entry": "audio_task", "realtime": True}},
    "allowed_blocking_in_realtime": ["i2s_channel_write"],
}


def mk(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def chk(tmp_path: Path, files: dict[str, str], cfg=None, only: str | None = None):
    mk(tmp_path, files)
    srcs = sorted(tmp_path.rglob("*.c"))
    fs = rtlint.check(tmp_path, srcs, BASE if cfg is None else cfg)
    return [f for f in fs if only is None or f.rule == only]


AUDIO = """
static void render(void) { printf("x"); }
void audio_task(void *a) {
  for (;;) {
    render();
    i2s_channel_write(h, b, 4, NULL, 1000);
  }
}
"""


def test_rt1_chain(tmp_path):
    fs = chk(tmp_path, {"main/a.c": AUDIO}, only="RT1_AUDIO_BLOCKING")
    assert len(fs) == 1
    assert fs[0].message.startswith("main/a.c:2: ")
    assert "audio_task → render → printf" in fs[0].message
    assert "real-time rule 1" in fs[0].message


def test_rt1_allowed_and_nonrealtime_ok(tmp_path):
    src = """
void audio_task(void *a) { for (;;) { i2s_channel_write(h, b, 4, NULL, 5); } }
void other_task(void *a) { for (;;) { printf("hi"); vTaskDelay(1); } }
"""
    assert chk(tmp_path, {"main/a.c": src}, only="RT1_AUDIO_BLOCKING") == []


def test_rt1_many_kinds_and_zero_timeout(tmp_path):
    src = """
#define NOWAIT 0
void audio_task(void *a) {
  for (;;) {
    ESP_LOGI(TAG, "x");
    void *p = malloc(4);
    xSemaphoreTake(m, 0);
    vTaskDelay(1);
    xQueueReceive(q, &v, NOWAIT);
    xQueueReceive(q, &v, 5);
    i2s_channel_write(h, b, 4, NULL, 5);
  }
}
"""
    fs = chk(tmp_path, {"main/a.c": src}, only="RT1_AUDIO_BLOCKING")
    names = sorted(f.message.split("()")[0].split(": ")[1] for f in fs)
    assert names == ["ESP_LOGI", "malloc", "vTaskDelay", "xQueueReceive", "xSemaphoreTake"]


def test_rt2_timeouts(tmp_path):
    src = """
#define NOWAIT ((TickType_t)0)
#define MS 5
static const int zero = 0;
void f(void) {
  xQueueSend(q, &v, 0);
  xQueueSend(q, &v, NOWAIT);
  xQueueReceive(q, &v, zero);
  xQueueSendToBack(q, &v, pdMS_TO_TICKS(MS));
  xQueueReceive(q, &v, portMAX_DELAY);
  xQueuePeek(q, &v, 10);
  xQueueSendFromISR(q, &v, &w);
}
"""
    fs = chk(tmp_path, {"main/a.c": src}, only="RT2_QUEUE_TIMEOUT")
    assert [f.message.split(": ")[0] for f in fs] == ["main/a.c:9", "main/a.c:10", "main/a.c:11"]
    assert "rule 2" in fs[0].message


def test_rt3_shared_writer(tmp_path):
    src = """
static int counter;
static const int k = 1;
static volatile int ok;
static void bump(void) { counter++; }
static void a_task(void *p) { for (;;) { bump(); vTaskDelay(1); } }
static void b_task(void *p) { int counter; for (;;) { counter = 1; vTaskDelay(1); } }
static void c_task(void *p) { for (;;) { counter = 2; vTaskDelay(1); } }
void app_main(void) {
  xTaskCreate(a_task, "a", 1, 0, 1, 0);
  xTaskCreate(b_task, "b", 1, 0, 1, 0);
  xTaskCreate(c_task, "c", 1, 0, 1, 0);
}
"""
    fs = chk(tmp_path, {"main/a.c": src}, cfg={}, only="RT3_SHARED_WRITER")
    assert len(fs) == 1 and fs[0].severity == "warning"
    assert "'counter'" in fs[0].message and "a_task" in fs[0].message and "c_task" in fs[0].message
    assert "b_task" not in fs[0].message  # shadowed by a local


def test_rt3_single_writer_clean(tmp_path):
    src = """
static int n;
static void a_task(void *p) { for (;;) { n = 1; vTaskDelay(1); } }
static void b_task(void *p) { for (;;) { int y = n; (void)y; vTaskDelay(1); } }
void app_main(void) { xTaskCreate(a_task,"a",1,0,1,0); xTaskCreate(b_task,"b",1,0,1,0); }
"""
    assert chk(tmp_path, {"main/a.c": src}, cfg={}, only="RT3_SHARED_WRITER") == []


def test_rt4(tmp_path):
    src = """
static void pace(void) { vTaskDelay(1); }
static void cond(int x) { if (x) vTaskDelay(1); }
static void good1(void *p) { for (;;) { vTaskDelayUntil(&t, 1); } }
static void good2(void *p) { while (1) { work(); pace(); } }
static void good3(void *p) { while (true) { xQueueReceive(q, &v, portMAX_DELAY); } }
static void bad1(void *p) { for (;;) { work(); } }
static void bad2(void *p) { for (;;) { if (x) { vTaskDelay(1); } cond(1); } }
static void bad3(void *p) { for (;;) { xQueueReceive(q, &v, 0); } }
static void fine_not_task(void) { for (;;) { work(); } }
void app_main(void) {
  xTaskCreate(good1,"a",1,0,1,0); xTaskCreate(good2,"a",1,0,1,0); xTaskCreate(good3,"a",1,0,1,0);
  xTaskCreate(bad1,"a",1,0,1,0); xTaskCreate(bad2,"a",1,0,1,0); xTaskCreate(bad3,"a",1,0,1,0);
}
"""
    fs = chk(tmp_path, {"main/a.c": src}, cfg={}, only="RT4_TASK_NO_BLOCK")
    assert sorted(f.message.split("task loop in ")[1].split("()")[0] for f in fs) == [
        "bad1",
        "bad2",
        "bad3",
    ]
    assert "approximat" in fs[0].message and "rule 4" in fs[0].message


def test_rt5_double(tmp_path):
    src = """
#define K 0.5
#define OK 0.5f
#define S(x) sin(x)
double d;
float a = 0.5f, b = 0.5, c = 1e-3, e = 2., g = 1.0F, h = 3, i = 0x1E;
float f(float x) { return sinf(x) + sin(x) + (float)M_PI; }
"""
    fs = chk(tmp_path, {"main/a.c": src}, cfg={}, only="RT5_DOUBLE")
    msgs = [f.message for f in fs]
    lines = sorted(int(m.split(":")[1]) for m in msgs)
    assert lines == [2, 4, 5, 6, 6, 6, 7, 7, 7] or lines == [2, 4, 5, 6, 6, 6, 7, 7]
    assert any("'0.5'" in m for m in msgs) and any("1e-3" in m for m in msgs)
    assert any("'2.'" in m for m in msgs)
    assert any("sin()" in m and "sinf()" in m for m in msgs)
    assert not any("'0.5f'" in m or "1.0F" in m or "0x1E" in m for m in msgs)


def test_rt5_isr_float(tmp_path):
    src = """
static float scale;
void IRAM_ATTR gpio_handler(void *arg) { int x = 3 * scale; (void)x; float y = 1.5f; }
static void helper(void) { int z = (int)sinf(1); }
void IRAM_ATTR timer_isr(void *arg) { helper(); }
void IRAM_ATTR clean_handler(void *arg) { int q = 1; }
void plain_func(void) { float ok = 1.0f; }
void setup(void) {
  gpio_isr_handler_add(4, gpio_handler, NULL);
  gpio_isr_handler_add(5, clean_handler, NULL);
}
"""
    fs = chk(tmp_path, {"main/a.c": src}, cfg={}, only="RT5_ISR_FLOAT")
    texts = " | ".join(f.message for f in fs)
    assert "gpio_handler" in texts and "timer_isr → helper" in texts
    assert "clean_handler" not in texts and "plain_func" not in texts
    assert "rule 5" in fs[0].message


def test_rt5_isr_struct_callback(tmp_path):
    src = """
static bool IRAM_ATTR on_alarm(void *t) { float f = 2.0f; return f > 1; }
void setup(void) {
  gptimer_event_callbacks_t cbs = { .on_alarm = on_alarm };
  gptimer_register_event_callbacks(t, &cbs, NULL);
}
"""
    fs = chk(tmp_path, {"main/a.c": src}, cfg={}, only="RT5_ISR_FLOAT")
    assert fs and "on_alarm" in fs[0].message


STARTUP_CFG = {
    "tasks": {"audio": {"entry": "audio_task", "realtime": True}},
    "startup_order": [
        {"stage": "gpio", "calls": ["gpio_config", "*_gpio_init"]},
        {"stage": "tables", "calls": ["*_tables_init", "synth_init"]},
        {"stage": "queue", "calls": ["xQueueCreate*"]},
        {"stage": "i2s", "calls": ["i2s_new_channel", "audio_init"]},
        {"stage": "tasks", "calls": ["xTaskCreate*"]},
    ],
}


def test_rt6_ok_with_helpers_and_missing_stages(tmp_path):
    src = """
static void scan_init(void) { gpio_config(&c); }
static void audio_init(void) { i2s_new_channel(0, 0, 0); }
static void start_audio(void) { xTaskCreatePinnedToCore(audio_task, "a", 1, 0, 1, 0, 1); }
void app_main(void) {
  scan_init();
  synth_init();
  audio_init();
  xTaskCreate(blink_task, "b", 1, 0, 1, 0);
  start_audio();
}
"""
    assert chk(tmp_path, {"main/a.c": src}, STARTUP_CFG, "RT6_STARTUP_ORDER") == []


def test_rt6_out_of_order_via_helper(tmp_path):
    src = """
static void scan_init(void) { gpio_config(&c); }
void app_main(void) {
  synth_init();
  scan_init();
  xQueueCreate(4, 4);
}
"""
    fs = chk(tmp_path, {"main/a.c": src}, STARTUP_CFG, "RT6_STARTUP_ORDER")
    assert len(fs) == 1
    assert "'gpio'" in fs[0].message and "via scan_init()" in fs[0].message
    assert "rule 6" in fs[0].message


def test_rt6_audio_not_last(tmp_path):
    src = """
void app_main(void) {
  xTaskCreatePinnedToCore(audio_task, "a", 1, 0, 1, 0, 1);
  xTaskCreate(ui_task, "u", 1, 0, 1, 0);
}
"""
    fs = chk(tmp_path, {"main/a.c": src}, STARTUP_CFG, "RT6_STARTUP_ORDER")
    assert (
        len(fs) == 1
        and "created last" in fs[0].message
        and fs[0].message.startswith("main/a.c:4: ")
    )


def test_fixture_project_via_lint(tmp_path):
    prof = tmp_path / "outside.yaml"  # profile outside the project
    prof.write_text(
        "disable: [LINT_INCLUDE]\nrealtime:\n  tasks: {audio: {entry: audio_task, realtime: true}}\n"
        "  startup_order:\n    - {stage: queue, calls: ['xQueueCreate*']}\n"
        "    - {stage: tasks, calls: ['xTaskCreate*']}\n"
    )
    fs = lint.run(FIX / "bad", profile_path=prof)
    rules = {f.rule for f in fs}
    assert {
        "RT1_AUDIO_BLOCKING",
        "RT2_QUEUE_TIMEOUT",
        "RT3_SHARED_WRITER",
        "RT4_TASK_NO_BLOCK",
        "RT5_DOUBLE",
        "RT6_STARTUP_ORDER",
    } <= rules
    lines = [int(f.message.split(":")[1]) for f in fs]
    assert lines == sorted(lines)


def test_lint_disable_and_sources(tmp_path):
    mk(
        tmp_path,
        {
            "main/a.c": "void f(void){gpio_config(0);}\n",
            "other/b.c": "void g(void){gpio_config(0);}\n",
        },
    )
    assert len(lint.run(tmp_path)) == 1
    prof = tmp_path.parent / (tmp_path.name + "-prof.yaml")
    prof.write_text("disable: [LINT_DIRECT_GPIO]\n")
    assert lint.run(tmp_path, profile_path=prof) == []
    prof.write_text("sources: [other]\n")
    fs = lint.run(tmp_path, profile_path=prof)
    assert len(fs) == 1 and fs[0].message.startswith("other/b.c:")
    prof.write_text("sources: [main]\nrealtime: {}\n")
    assert lint.run(tmp_path, profile_path=prof)[0].message.startswith("main/a.c:")


def test_disable_rt_rule(tmp_path):
    mk(tmp_path, {"main/a.c": "double d;\n"})
    prof = tmp_path.parent / (tmp_path.name + "-p.yaml")
    prof.write_text("realtime: {}\n")
    assert [f.rule for f in lint.run(tmp_path, profile_path=prof)] == ["RT5_DOUBLE"]
    prof.write_text("realtime: {}\ndisable: [RT5_DOUBLE]\n")
    assert lint.run(tmp_path, profile_path=prof) == []
