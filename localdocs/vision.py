"""Describing pictures that have no readable text (photos, wallpapers, diagrams), so the assistant can answer by itself
instead of asking the user to do something. Uses a vision model: the active one if it can see, else any installed
Ollama model that can. Nothing leaves the machine unless the active model is a cloud one that was chosen on purpose."""
from __future__ import annotations

import base64
import io
import json
import urllib.request
from pathlib import Path

from . import config
from .config import NUM_CTX, OLLAMA
from .llm import api_key, base_url, installed_models, protocol, split

PROMPT = ("Describe this image for someone who cannot see it: what it shows, the main subjects, colours, style and mood, "
          "and transcribe any visible text. Be factual and concise (4-6 sentences).")
_CAPS: dict = {}


def _can_see(model: str) -> bool:
    if model not in _CAPS:
        try:
            req = urllib.request.Request(OLLAMA + "/api/show", json.dumps({"model": model}).encode(),
                                         {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as r:
                _CAPS[model] = "vision" in (json.load(r).get("capabilities") or [])
        except Exception:
            _CAPS[model] = False
    return _CAPS[model]


def local_vision_model() -> str:
    """The active local model if it can see, otherwise the first installed one that can; '' if none."""
    provider, name = split()
    candidates = ([name] if provider == "ollama" else []) + installed_models()
    return next((m for m in candidates if _can_see(m)), "")


def vision_ready() -> bool:
    """True if something can look at pictures right now (a chosen cloud model, or an installed local vision model)."""
    provider, _ = split()
    return (provider != "ollama" and protocol(provider) != "cli" and bool(api_key(provider))) or bool(local_vision_model())


def _jpeg_b64(src) -> str:
    """Base64 JPEG (max 1024 px) of an image file path or an already-open PIL image."""
    from PIL import Image
    im = src if isinstance(src, Image.Image) else Image.open(src)
    im = im.convert("RGB")
    im.thumbnail((1024, 1024))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def _post(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def describe(path) -> str:
    """A description of the picture (a file path or PIL image), or '' if no model available can look at it."""
    provider, name = split()
    b64 = _jpeg_b64(path)
    if provider != "ollama" and protocol(provider) != "cli" and api_key(provider):   # a cloud model the user chose
        try:
            if protocol(provider) == "anthropic":
                data = _post(base_url(provider) + "/v1/messages",
                             {"model": name, "max_tokens": 600, "messages": [{"role": "user", "content": [
                                 {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                                 {"type": "text", "text": PROMPT}]}]},
                             {"x-api-key": api_key(provider), "anthropic-version": "2023-06-01"})
                return data["content"][0]["text"].strip()
            data = _post(base_url(provider) + "/chat/completions",
                         {"model": name, "messages": [{"role": "user", "content": [
                             {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
                             {"type": "text", "text": PROMPT}]}]},
                         {"Authorization": f"Bearer {api_key(provider)}"})
            return data["choices"][0]["message"]["content"].strip()
        except Exception:
            pass                                    # this model can't see: fall back to a local one
    model = local_vision_model()
    if not model:
        return ""
    data = _post(OLLAMA + "/api/chat",
                 {"model": model, "stream": False, "think": False, "keep_alive": "30m", "options": {"num_ctx": NUM_CTX},
                  "messages": [{"role": "user", "content": PROMPT, "images": [b64]}]}, {})
    return data["message"]["content"].strip()
