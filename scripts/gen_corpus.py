"""Brainstorm request phrasings with the local Qwen server.

Usage: python -I scripts/gen_corpus.py OUT.json [ROUNDS]

Writes {category: [candidate request, ...]} for a human to filter and label into
tests/fixtures/nl/requests.yaml. Prompts are generic device descriptions only.
"""

import json
import sys
import urllib.request

URL = "http://100.89.28.64:8080/v1/chat/completions"
EXTRA = {"temperature": 0.9, "top_p": 0.8, "top_k": 20, "chat_template_kwargs": {"enable_thinking": False}}

STYLES = (
    "terse and abbreviated; chatty hobbyist; with typos; non-native English phrasing; "
    "pin names like D4, IO25, pin 25 or GPIO25; one long run-on sentence; bullet-like fragments"
)

BRIEFS = {
    "clear": (
        "The request states, for every pin, its GPIO number, whether it is input or output, and for inputs "
        "exactly how it is pulled (internal pull-up, external resistor, or wired to a driven output). "
        "Use only safe pins such as 4, 13, 14, 16-19, 21-23, 25-27, 32, 33."
    ),
    "ambiguous": (
        "The request leaves a key fact out: a button with no pull stated, no pin number, 'the usual I2C pins', "
        "whether Wi-Fi is used while reading an analog sensor, or whether a button is active-low or active-high."
    ),
    "rule_violation": (
        "The request asks for something electrically invalid on a classic ESP32 devkit: output on GPIO 34-39, "
        "internal pull on 35, GPIO 6-11 or 20, analog read on an ADC2 pin like 4 or 25 with Wi-Fi on, "
        "external pull-up on GPIO 12, deep-sleep wake on a non-RTC pin like 5, LED on GPIO 1 or 3 while using "
        "the serial console, I2C with 'no resistors', GPIO 37, two devices on one pin."
    ),
    "state_machine": (
        "The request describes device behaviour in modes: states, button-driven transitions, retries after N "
        "failures, periodic actions every N seconds, timeouts. It also names a button pin."
    ),
    "out_of_scope": (
        "The request asks for something a pin/state-machine language cannot express: PWM frequency or duty, "
        "drawing on a display, Wi-Fi SSID and password, MQTT topics, sensor calibration maths."
    ),
}


def chat(prompt: str) -> str:
    body = {"model": "qwen-exec", "messages": [{"role": "user", "content": prompt}], **EXTRA}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def parse_list(text: str) -> list[str]:
    start, end = text.find("["), text.rfind("]")
    try:
        return [s for s in json.loads(text[start : end + 1]) if isinstance(s, str)]
    except ValueError:
        return []


def main() -> None:
    out_path = sys.argv[1]
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    result: dict[str, list[str]] = {}
    for cat, brief in BRIEFS.items():
        cands: list[str] = []
        for _ in range(rounds):
            prompt = (
                "You write sample requests that a person might type to describe an ESP32 gadget they want "
                f"firmware for. Write 10 different requests, each 1-3 sentences. Vary the register: {STYLES}.\n"
                f"Constraint for this batch: {brief}\n"
                "Reply with only a JSON array of 10 strings."
            )
            cands += parse_list(chat(prompt))
        result[cat] = cands
        print(cat, len(cands), file=sys.stderr)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=1)


if __name__ == "__main__":
    main()
