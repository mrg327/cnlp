"""Runs the Pi extension's guard unit tests with node (type stripping, node >= 22.18)."""

import shutil
import subprocess
from pathlib import Path

import pytest

PI_NODE = Path.home() / ".local/share/pi-node/node-v22.23.2-linux-x64/bin/node"
NODE = shutil.which("node") or (str(PI_NODE) if PI_NODE.exists() else None)
DIR = Path(__file__).resolve().parent.parent / "pi/extensions/cnlp"


def _supports_ts(node: str) -> bool:
    r = subprocess.run([node, "-e", "import('node:module').then(m=>process.exit(m.stripTypeScriptTypes?0:1))"],
                       check=False, capture_output=True, text=True)
    return r.returncode == 0


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_guard_node_tests():
    if not _supports_ts(NODE):
        pytest.skip("node too old for TypeScript type stripping (need >= 22.18)")
    r = subprocess.run([NODE, "--test", str(DIR / "guard.test.mjs")], check=False, capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1000:]
