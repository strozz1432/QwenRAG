"""API keys saved from inside the app (/connect), kept out of the project folder and out of git.

macOS: the login Keychain, through the `security` tool. Elsewhere (or if the Keychain is unavailable):
~/.config/localdocs/keys.json with owner-only permissions. Environment variables always take priority
over anything saved here (see llm.api_key).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SERVICE = "localdocs"
FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "localdocs" / "keys.json"
_cache: dict = {}


def _security(*args: str) -> subprocess.CompletedProcess | None:
    if sys.platform != "darwin":
        return None
    try:
        return subprocess.run(["security", *args], capture_output=True, text=True, timeout=10)
    except Exception:
        return None


def _read_file() -> dict:
    try:
        return json.loads(FILE.read_text())
    except Exception:
        return {}


def get(name: str) -> str:
    if name in _cache:
        return _cache[name]
    r = _security("find-generic-password", "-s", SERVICE, "-a", name, "-w")
    value = r.stdout.strip() if r is not None and r.returncode == 0 else _read_file().get(name, "")
    _cache[name] = value
    return value


def set(name: str, value: str) -> str:
    """Save a value. Returns where it went: 'keychain' or the file path."""
    _cache[name] = value
    r = _security("add-generic-password", "-U", "-s", SERVICE, "-a", name, "-w", value)
    if r is not None and r.returncode == 0:
        return "keychain"
    FILE.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(FILE.parent, 0o700)
    data = _read_file()
    data[name] = value
    fd = os.open(FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    return str(FILE)


def delete(name: str):
    _cache.pop(name, None)
    _security("delete-generic-password", "-s", SERVICE, "-a", name)
    data = _read_file()
    if name in data:
        del data[name]
        FILE.write_text(json.dumps(data))


def custom() -> list:
    """Names of providers the user added with /connect <name> <key> <url>."""
    return [n for n in get("custom_providers").split(",") if n]


def add_custom(name: str):
    names = custom()
    if name not in names:
        set("custom_providers", ",".join(names + [name]))


def remove_custom(name: str):
    set("custom_providers", ",".join(n for n in custom() if n != name))
    delete("url:" + name)


def redact(text: str) -> str:
    """'/connect groq gsk_…' -> '/connect groq ••••••••' so a key never reaches the screen, history or logs."""
    parts = text.split(None, 3)
    if len(parts) >= 3 and parts[0] == "/connect":
        return " ".join(parts[:2] + ["•" * 8] + parts[3:])
    return text
