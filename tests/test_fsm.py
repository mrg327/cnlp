import shutil
import subprocess
from pathlib import Path

import pytest

from cnlp import cnl, codegen, fsm

HW = "chip is esp32.\nmodule is esp32-wroom-32.\nboard is b1.\n"
GOOD = """
machine sensor starts in idle.
state idle: on button press go to measuring.
state measuring: every 10 s do read sensor; on read ok go to idle; after 3 read failed go to error.
state error: on button press go to idle.
"""


def check(body):
    return fsm.check(cnl.parse(HW + body))


def rules(body):
    return {f.rule for f in check(body)}


def test_good_machine_only_unhandled_warnings():
    fs = check(GOOD)
    assert {f.rule for f in fs} == {"FSM_UNHANDLED"}
    assert all(f.severity == "warning" and f.gpio is None for f in fs)


def test_unreachable_state_known_bad():
    fs = check(GOOD + "state orphan: on button press go to idle.\n")
    (f,) = [f for f in fs if f.rule == "FSM_UNREACHABLE"]
    assert f.severity == "error" and "'orphan'" in f.message and "'sensor'" in f.message


def test_unknown_state_target_and_start():
    fs = check("machine a starts in s.\nstate s: on x go to nowhere.\n")
    assert [f.rule for f in fs if f.severity == "error"] == ["FSM_UNKNOWN_STATE"]
    assert "nowhere" in fs[0].message
    assert "FSM_UNKNOWN_STATE" in rules("machine a starts in ghost.\nstate s: on x go to s.\n")


def test_dead_end_warns_unless_terminal():
    fs = check("machine a starts in s.\nstate s: on x go to stuck; on y go to done.\nstate stuck.\nstate done.\n")
    dead = [f for f in fs if f.rule == "FSM_DEAD_END"]
    assert len(dead) == 1 and dead[0].severity == "warning" and "'stuck'" in dead[0].message


def test_duplicates():
    assert "FSM_DUPLICATE" in rules("machine a starts in s.\nstate s: on x go to s; on x go to s.\n")
    assert "FSM_DUPLICATE" in rules("machine a starts in s.\nstate s: on x go to s.\nstate s: on y go to s.\n")
    assert "FSM_DUPLICATE" in rules("machine a starts in s.\nstate s: on x go to s.\n"
                                    "machine a starts in s.\nstate s: on x go to s.\n")


def test_nondeterministic():
    assert "FSM_NONDETERMINISTIC" in rules(
        "machine a starts in s.\nstate s: on x go to s; after 2 x go to s.\n")


def test_unhandled_names_states():
    (f,) = [f for f in check("machine a starts in s.\nstate s: on x go to t.\nstate t: on y go to s.\n")
            if f.rule == "FSM_UNHANDLED" and "'x'" in f.message]
    assert "'s'" in f.message and "'t'" in f.message


GCC = shutil.which("gcc")

DRIVER = r"""
#include <assert.h>
#include <stdio.h>
#include "fsm_sensor.h"

static int runs;
static void run(sensor_action_t a) { assert(a == SENSOR_ACTION_READ_SENSOR); runs++; }

int main(void)
{
    sensor_ctx_t c;
    sensor_init(&c);
    assert(c.state == SENSOR_STATE_IDLE);
    /* Unhandled events are ignored. */
    assert(sensor_dispatch(&c, SENSOR_EVENT_READ_OK) == SENSOR_STATE_IDLE);
    assert(sensor_dispatch(&c, SENSOR_EVENT_BUTTON_PRESS) == SENSOR_STATE_MEASURING);

    /* Periodic action: first run 10 s after entering, then every 10 s. */
    sensor_tick(&c, 1000, run);
    sensor_tick(&c, 10999, run);
    assert(runs == 0);
    sensor_tick(&c, 11000, run);
    assert(runs == 1);
    sensor_tick(&c, 20999, run);
    assert(runs == 1);
    sensor_tick(&c, 21000, run);
    assert(runs == 2);
    /* Idle runs nothing. */
    assert(sensor_dispatch(&c, SENSOR_EVENT_READ_OK) == SENSOR_STATE_IDLE);
    sensor_tick(&c, 99999, run);
    assert(runs == 2);

    /* Exactly three failures, no more, no fewer. */
    sensor_dispatch(&c, SENSOR_EVENT_BUTTON_PRESS);
    assert(sensor_dispatch(&c, SENSOR_EVENT_READ_FAILED) == SENSOR_STATE_MEASURING);
    assert(sensor_dispatch(&c, SENSOR_EVENT_READ_FAILED) == SENSOR_STATE_MEASURING);
    assert(sensor_dispatch(&c, SENSOR_EVENT_READ_FAILED) == SENSOR_STATE_ERROR);
    assert(sensor_dispatch(&c, SENSOR_EVENT_READ_FAILED) == SENSOR_STATE_ERROR);

    /* The count resets on leaving the state: two failures, leave, return, two more. */
    sensor_dispatch(&c, SENSOR_EVENT_BUTTON_PRESS);
    assert(c.state == SENSOR_STATE_IDLE);
    sensor_dispatch(&c, SENSOR_EVENT_BUTTON_PRESS);
    sensor_dispatch(&c, SENSOR_EVENT_READ_FAILED);
    sensor_dispatch(&c, SENSOR_EVENT_READ_FAILED);
    assert(sensor_dispatch(&c, SENSOR_EVENT_READ_OK) == SENSOR_STATE_IDLE);
    sensor_dispatch(&c, SENSOR_EVENT_BUTTON_PRESS);
    assert(sensor_dispatch(&c, SENSOR_EVENT_READ_FAILED) == SENSOR_STATE_MEASURING);
    assert(sensor_dispatch(&c, SENSOR_EVENT_READ_FAILED) == SENSOR_STATE_MEASURING);
    assert(sensor_dispatch(&c, SENSOR_EVENT_READ_FAILED) == SENSOR_STATE_ERROR);
    return 0;
}
"""


@pytest.mark.skipif(GCC is None, reason="gcc not installed")
def test_generated_c_on_host(tmp_path):
    spec = (Path(__file__).parent.parent / "examples" / "fsm_demo" / "spec.cnl").read_text()
    out = tmp_path / "board"
    codegen.generate(cnl.parse(spec), out)
    (tmp_path / "driver.c").write_text(DRIVER)
    exe = tmp_path / "t"
    cc = subprocess.run(
        [GCC, "-std=c11", "-Wall", "-Wextra", "-Werror", "-fsanitize=address,undefined",
         "-fno-sanitize-recover=all", "-I", str(out / "include"), str(out / "fsm_sensor.c"),
         str(tmp_path / "driver.c"), "-o", str(exe)],
        check=False, capture_output=True, text=True)
    assert cc.returncode == 0, cc.stderr
    run = subprocess.run([str(exe)], check=False, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
