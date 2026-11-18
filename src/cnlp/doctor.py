"""`cnlp doctor`: a fast, read-only health check of the setup for using cnlp from Pi.

Every external probe (PATH lookup, subprocess, HTTP, home dir, env) lives on `Probes`, so
tests can replace them. The model-server check is one GET on the models list (3 s timeout);
it never sends a completion.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NODE_MIN = (22, 18)
DEFAULT_PORT = "/dev/ttyUSB0"
REPO = Path(__file__).resolve().parents[2]


def _http_get(url: str, timeout: float) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
    return (r.stdout or r.stderr).strip()


def _has_module(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


@dataclass
class Probes:
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    home: Path = field(default_factory=Path.home)
    which: Callable[[str], str | None] = shutil.which
    run: Callable[[list[str]], str] = _run
    http_get: Callable[[str, float], str] = _http_get
    has_module: Callable[[str], bool] = _has_module
    repo: Path = REPO


def _c(name: str, status: str, detail: str, fix: str = "") -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def _ver(text: str) -> tuple[int, ...] | None:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(x) for x in m.groups() if x is not None) if m else None


def check_install(p: Probes) -> list[dict]:
    from importlib import metadata

    try:
        version = metadata.version("cnlp")
    except metadata.PackageNotFoundError:
        version = "unknown"
    here = Path(__file__).resolve().parents[2]
    out = [_c("cnlp install", "ok" if (p.repo / "pyproject.toml").exists() else "warn",
              f"v{version}, running from {p.repo}"
              + ("" if (p.repo / "pyproject.toml").exists() else " (not an editable checkout)"),
              "uv tool install --editable <repo>" if not (p.repo / "pyproject.toml").exists() else "")]
    exe = p.which("cnlp")
    if not exe:
        out.append(_c("cnlp on PATH", "fail", "`cnlp` not found on PATH",
                      f"cd {here} && uv tool install --editable ."))
        return out
    try:
        resolved = Path(exe).resolve()
        text = resolved.read_text("utf-8", "replace") if resolved.stat().st_size < 4096 else ""
    except OSError:
        text = ""
    # uv tool shims/venv scripts embed the venv path, not the repo; check the editable link instead.
    pth = ""
    try:
        for d in metadata.distribution("cnlp").files or []:
            if d.name.endswith(".pth") or d.name == "direct_url.json":
                pth += Path(str(d.locate())).read_text("utf-8", "replace")
    except (metadata.PackageNotFoundError, OSError):
        pass
    if str(p.repo) in pth or str(p.repo) in text:
        out.append(_c("cnlp on PATH", "ok", f"{exe} -> editable install of {p.repo}"))
    else:
        out.append(_c("cnlp on PATH", "warn", f"{exe}; could not confirm it points at {p.repo}",
                      f"cd {p.repo} && uv tool install --editable . --force"))
    return out


def check_project(project: str, p: Probes) -> tuple[list[dict], Any]:
    import yaml

    from cnlp import audit, cnl

    try:
        cfg = audit.find_config(Path(project))
    except audit.AuditError:
        return [_c("project config", "warn", f"no cnlp config covers {Path(project).resolve()}",
                   f"add {audit.projects_root()}/<name>/audit.yaml with `target: <project dir>`")], None
    out = [_c("project config", "ok", str(cfg))]
    try:
        data = yaml.safe_load(cfg.read_text("utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        return out + [_c("audit.yaml", "fail", f"unreadable: {e}")], None
    base = cfg.parent
    target = Path(str(data.get("target", ""))).expanduser()
    out.append(_c("target", "ok" if target.is_dir() else "fail", str(target),
                  "" if target.is_dir() else f"fix `target:` in {cfg}"))
    manifest = None
    spec = base / str(data.get("spec", "spec.cnl"))
    if not spec.is_file():
        out.append(_c("spec", "fail", f"{spec.name} missing", f"create {spec}"))
    else:
        try:
            manifest = cnl.parse(spec.read_text("utf-8"))
            out.append(_c("spec", "ok", f"{spec.name} parses"))
        except cnl.CnlError as e:
            out.append(_c("spec", "fail", f"{spec.name} does not parse: {str(e).splitlines()[0][:100]}",
                          f"cnlp translate {spec}"))
    for label, key, default in (("requirements", "requirements", "requirements.yaml"),
                                ("parameters", "parameters", None)):
        name = data.get(key, default)
        if not name:
            out.append(_c(label, "skip", "not configured"))
            continue
        f = base / str(name)
        out.append(_c(label, "ok" if f.is_file() else "warn", str(name) + ("" if f.is_file() else " missing"),
                      "" if f.is_file() else f"create {f}"))
    return out, manifest


def _idf_version(root: Path) -> str | None:
    vt = root / "version.txt"
    if vt.is_file():
        return vt.read_text("utf-8").strip().lstrip("v")
    vc = root / "tools" / "cmake" / "version.cmake"
    if vc.is_file():
        t = vc.read_text("utf-8", "replace")
        parts = [re.search(rf"set\(IDF_VERSION_{k}\s+(\d+)\)", t) for k in ("MAJOR", "MINOR", "PATCH")]
        if all(parts):
            return ".".join(m.group(1) for m in parts if m)
    return None


def check_idf(manifest: Any, p: Probes) -> list[dict]:
    path = Path(p.env.get("CNLP_IDF_PATH") or p.home / "esp" / "esp-idf-v5.5.6").expanduser()
    if not path.is_dir():
        return [_c("ESP-IDF", "fail", f"{path} not found",
                   "install ESP-IDF or set CNLP_IDF_PATH to its folder")]
    ver = _idf_version(path)
    pinned = getattr(manifest, "idf_version", None)
    if ver is None:
        return [_c("ESP-IDF", "warn", f"{path} (version unreadable)", "check the IDF checkout is complete")]
    if pinned and not ver.startswith(str(pinned)):
        return [_c("ESP-IDF", "warn", f"{path} is v{ver}, project pins v{pinned}",
                   f"point CNLP_IDF_PATH at an ESP-IDF v{pinned} checkout")]
    return [_c("ESP-IDF", "ok", f"{path} v{ver}" + (f" (matches pin v{pinned})" if pinned else ""))]


def check_model(p: Probes) -> list[dict]:
    from cnlp import llm

    url = p.env.get("CNLP_LLM_URL", llm.DEFAULT_URL).rstrip("/")
    want = p.env.get("CNLP_LLM_MODEL", llm.DEFAULT_MODEL)
    fix = ("only `cnlp spec` and `cnlp ask` need it; set CNLP_LLM_URL or start the server")
    try:
        body = p.http_get(url + "/models", 3.0)
        data = json.loads(body)
        ids = [m.get("id", "") for m in (data.get("data") if isinstance(data, dict) else data) or []]
    except Exception as e:  # noqa: BLE001 - any failure is just "unreachable"
        return [_c("model server", "warn", f"{url} unreachable ({type(e).__name__})", fix)]
    if want in ids:
        return [_c("model server", "ok", f"{url}, model {want} listed")]
    return [_c("model server", "warn", f"{url} up, but model {want} not listed ({', '.join(ids[:4]) or 'none'})",
               "set CNLP_LLM_MODEL to one of the listed models")]


def check_pi(p: Probes) -> list[dict]:
    out = []
    pi = p.which("pi")
    if pi:
        try:
            v = p.run([pi, "--version"]).splitlines()[0]
        except Exception:  # noqa: BLE001
            v = "version unknown"
        out.append(_c("pi", "ok", f"{pi} ({v})"))
    else:
        out.append(_c("pi", "fail", "`pi` not on PATH", "install Pi and put it on PATH"))
    for kind, sub in (("extension", "extensions"), ("skill", "skills")):
        link = p.home / ".pi" / "agent" / sub / "cnlp"
        want = p.repo / "pi" / sub / "cnlp"
        fix = f"ln -s {want} {link}"
        if not link.is_symlink():
            out.append(_c(f"pi {kind}", "fail" if not link.exists() else "warn",
                          f"{link} " + ("is not a symlink" if link.exists() else "missing"), fix))
        elif link.resolve() != want.resolve():
            out.append(_c(f"pi {kind}", "fail", f"{link} -> {link.resolve()}, not this repo", f"ln -sfn {want} {link}"))
        elif not link.exists():
            out.append(_c(f"pi {kind}", "fail", f"{link} is a dangling symlink", f"ln -sfn {want} {link}"))
        else:
            out.append(_c(f"pi {kind}", "ok", f"{link} -> {want}"))
    node = p.which("node")
    if not node:
        out.append(_c("node", "fail", "`node` not on PATH", f"install node >= {NODE_MIN[0]}.{NODE_MIN[1]}"))
    else:
        try:
            v = p.run([node, "--version"])
        except Exception:  # noqa: BLE001
            v = ""
        t = _ver(v)
        need = ".".join(map(str, NODE_MIN))
        if t and t[:2] >= NODE_MIN:
            out.append(_c("node", "ok", f"{v} (>= {need})"))
        else:
            out.append(_c("node", "fail", f"{v or 'unknown version'}; need >= {need} for the guard tests",
                          f"install node >= {need}"))
    return out


def check_extras(p: Probes) -> list[dict]:
    if p.has_module("pyvisa"):
        return [_c("pyvisa (scope)", "ok", "importable")]
    return [_c("pyvisa (scope)", "skip", "not installed; only `cnlp measure` needs it",
               'uv tool install --editable "$HOME/projects/cnlp[scope]" --force')]


def check_serial(p: Probes) -> list[dict]:
    port = Path(DEFAULT_PORT)
    if port.exists():
        return [_c("serial port", "ok", f"{port} present")]
    return [_c("serial port", "skip", f"{port} not present (board unplugged?)", "plug in the board; pass --port to flash")]


def run(project: str = ".", probes: Probes | None = None) -> dict[str, Any]:
    p = probes or Probes()
    checks = check_install(p)
    proj, manifest = check_project(project, p)
    checks += proj + check_idf(manifest, p) + check_model(p) + check_pi(p) + check_extras(p) + check_serial(p)
    return {"ok": not any(c["status"] == "fail" for c in checks), "checks": checks}
