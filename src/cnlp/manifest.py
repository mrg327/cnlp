"""Pin manifest: the shared data model every engine stage reads.

This module is a contract. Other modules import Manifest, Pin, Features and
Finding from here and must not redefine them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import jsonschema
import yaml

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class Features:
    wifi: bool = False
    bluetooth: bool = False
    deep_sleep: bool = False
    console: str = "uart0"  # uart0 | uart1 | uart2 | usb | none
    jtag: bool = False


@dataclass(frozen=True)
class Pin:
    gpio: int
    role: str  # snake_case name, e.g. "button"; becomes PIN_BUTTON
    mode: str  # input | output | open_drain
    pull: str  # none | internal_up | internal_down | external_up | external_down | external_offboard
    function: str = "gpio"  # gpio | adc | dac | interrupt | pwm | i2c | spi | uart
    bus: str | None = None  # e.g. "i2c0_sda" when function is a bus
    wake: str | None = None  # low | high: deep-sleep wake level
    reset_level: str | None = None  # low | high: declared strap level at reset
    source: str | None = None  # request sentence, or "assumption"


@dataclass(frozen=True)
class Manifest:
    chip: str
    module: str
    board: str
    idf_version: str
    features: Features
    pins: tuple[Pin, ...]
    state_machines: tuple[Any, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["pins"] = [{k: v for k, v in p.items() if v is not None} for p in d["pins"]]
        d["state_machines"] = list(d["state_machines"])
        return d

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False)


@dataclass(frozen=True)
class Finding:
    """One rule result. `rule` is a rule ID such as "C2" or "OWNER"."""

    rule: str
    severity: Severity
    message: str  # plain English, names the pin and a fix
    gpio: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


class ManifestError(ValueError):
    """Raised when a manifest fails schema validation."""


def schema() -> dict[str, Any]:
    text = resources.files("cnlp.schema").joinpath("manifest.schema.json").read_text()
    return json.loads(text)


def from_dict(data: dict[str, Any]) -> Manifest:
    try:
        jsonschema.validate(data, schema())
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "(top level)"
        raise ManifestError(f"Manifest is invalid at {where}: {e.message}") from None
    return Manifest(
        chip=data["chip"],
        module=data["module"],
        board=data["board"],
        idf_version=str(data["idf_version"]),
        features=Features(**data["features"]),
        pins=tuple(Pin(**p) for p in data["pins"]),
        state_machines=tuple(data.get("state_machines", [])),
    )


def load(path: str | Path) -> Manifest:
    return from_dict(yaml.safe_load(Path(path).read_text()))
