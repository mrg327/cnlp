"""Flash broker: flashes only an image approved at the flash gate, then verifies readback.

Safety properties:
- The image is copied to a private temp dir and re-hashed there; the copy is what gets
  flashed, so a rebuild between approval and flashing can't slip an unapproved image in.
- esptool is called directly with `write_flash`. Never `idf.py flash`, which rebuilds first.
- This module never calls espefuse or any eFuse / secure-boot / encryption command.

In the full design the broker runs outside the agent's sandbox (phase 3). In the MVP it is
the same process as the CLI.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

IDF_PATH = Path(os.environ.get("CNLP_IDF_PATH", Path.home() / "esp" / "esp-idf-v5.5.6"))
BAUD_FLASH = 115200  # 460800 fails on the reference board (phase 0a spike)
BAUD_CONSOLE = 115200
READBACK_END = "=== CNLP READBACK END ==="


def _flash_files(build: Path) -> list[tuple[str, Path]]:
    """(offset, path) pairs from build/flash_args, paths relative to build/."""
    pairs: list[tuple[str, Path]] = []
    for line in (build / "flash_args").read_text().splitlines():
        parts = shlex.split(line)
        if parts and parts[0].startswith("0x"):
            pairs += [(parts[i], Path(parts[i + 1])) for i in range(0, len(parts) - 1, 2)]
    return pairs


def _copy_image(project: Path, dest: Path) -> Path:
    """Mirror the project's build/ layout under dest for every file the image hash covers."""
    build = project / "build"
    for name in ["flash_args", "project_description.json"]:
        shutil.copy2(build / name, dest / name)
    for _, rel in _flash_files(build):
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(build / rel, dest / rel)
    return dest


def _capture_serial(port: str, seconds: float) -> str:
    import serial  # pyserial

    with serial.Serial(port, BAUD_CONSOLE, timeout=0.2) as s:
        s.dtr = False  # reset the chip so we see output from boot
        s.rts = True
        time.sleep(0.1)
        s.rts = False
        buf = b""
        end = time.time() + seconds
        while time.time() < end:
            buf += s.read(4096)
            if READBACK_END.encode() in buf:
                break
    return buf.decode(errors="replace")


def flash(project: Path, port: str = "/dev/ttyUSB0", capture_seconds: float = 8.0) -> dict[str, Any]:
    from cnlp import gates, manifest, readback

    project = Path(project)
    image = gates.image_hash(project)
    if image is None:
        return {"ok": False, "stage": "image", "message": "No build found. Run idf.py build first."}
    if not gates.approved_image(project, image):
        return {"ok": False, "stage": "gate", "image_sha256": image,
                "message": "This image hasn't been approved at the flash gate. "
                           "Run `cnlp gate next` and approve it first."}

    with tempfile.TemporaryDirectory(prefix="cnlp-flash-") as tmp:
        staged = Path(tmp) / "proj"
        (staged / "build").mkdir(parents=True)
        _copy_image(project, staged / "build")
        if gates.image_hash(staged) != image:
            return {"ok": False, "stage": "image",
                    "message": "The build changed while it was being staged. Not flashed."}
        cmd = (f"source {shlex.quote(str(IDF_PATH / 'export.sh'))} >/dev/null 2>&1 && "
               f"cd {shlex.quote(str(staged / 'build'))} && "
               f"python -m esptool --chip esp32 -p {shlex.quote(port)} -b {BAUD_FLASH} "
               "--before default_reset --after no_reset write_flash @flash_args")
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=600,
                           check=False)
        if r.returncode != 0 or "Hash of data verified" not in r.stdout:
            tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-8:])
            return {"ok": False, "stage": "flash", "image_sha256": image,
                    "message": f"esptool failed:\n{tail}"}

    log = _capture_serial(port, capture_seconds)
    log_path = project / ".cnlp" / "readback.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log)
    m = manifest.load(project / ".cnlp" / "manifest.yaml")
    try:
        mismatches = readback.diff(m, readback.parse(log))
    except readback.ReadbackError as e:
        return {"ok": False, "stage": "readback", "image_sha256": image, "flashed": True,
                "log": str(log_path), "message": str(e)}
    return {"ok": not mismatches, "stage": "done", "image_sha256": image, "flashed": True,
            "log": str(log_path), "mismatches": mismatches,
            "message": "Flashed; register readback matches the manifest." if not mismatches
            else "Flashed, but register readback does NOT match the manifest."}
