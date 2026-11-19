"""Talking to a language model: local Ollama by default, or a cloud API (Anthropic, OpenAI-compatible).

The active model is a single string in config.LLM:
    "qwen3:8b"                      -> Ollama, on this machine (default)
    "anthropic:claude-sonnet-5-5"   -> Anthropic API   (key: ANTHROPIC_API_KEY)
    "openai:<model>"                -> OpenAI API      (key: OPENAI_API_KEY, URL: OPENAI_BASE_URL)
Any OpenAI-compatible server (LM Studio, llama.cpp, Groq, OpenRouter, vLLM...) works through "openai:" by
setting OPENAI_BASE_URL. Embeddings always stay on Ollama, so the index never depends on a cloud service.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from . import config
from .config import EMBED, NUM_CTX, OLLAMA

CLOUD = ("anthropic", "openai")
ANTHROPIC_MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]
KEY_VARS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
MAX_REPLY_TOKENS = 2000


# ---------------------------------------------------------------- which model

def split(model: str | None = None) -> tuple:
    """('ollama' | 'anthropic' | 'openai', model name)."""
    m = model or config.LLM
    prefix, sep, name = m.partition(":")
    if sep and prefix.lower() in CLOUD:
        return prefix.lower(), name
    return "ollama", m


def is_cloud(model: str | None = None) -> bool:
    return split(model)[0] != "ollama"


def api_key(provider: str) -> str:
    return os.environ.get(KEY_VARS[provider], "").strip()


def tag() -> str:
    """Short label for the status line, e.g. 'ollama.chat qwen3:8b' or 'anthropic.messages claude-…'."""
    provider, name = split()
    return f"ollama.chat {name}" if provider == "ollama" else f"{provider}.{'messages' if provider == 'anthropic' else 'chat'} {name}"


def display() -> str:
    """The model as shown to the user; cloud models get a ☁ so it is obvious when text leaves the machine."""
    return f"☁ {config.LLM}" if is_cloud() else config.LLM


def privacy_line() -> str:
    return "text from your files is sent to the provider" if is_cloud() else "private, on-device"


# ---------------------------------------------------------------- HTTP plumbing

def _post(path: str, payload: dict):
    req = urllib.request.Request(OLLAMA + path, json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=600)


def _cloud_request(provider: str, messages: list, stream: bool, as_json: bool):
    key = api_key(provider)
    if not key:
        raise RuntimeError(f"No API key: set {KEY_VARS[provider]} in your environment, then restart.")
    name = split()[1]
    if provider == "anthropic":
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        body = {"model": name, "max_tokens": MAX_REPLY_TOKENS, "stream": stream,
                "messages": [m for m in messages if m["role"] != "system"]}
        if system:
            body["system"] = system
        url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/") + "/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    else:
        body = {"model": name, "messages": messages, "stream": stream}
        if as_json:
            body["response_format"] = {"type": "json_object"}
        url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
    req = urllib.request.Request(url, json.dumps(body).encode(), headers)
    try:
        return urllib.request.urlopen(req, timeout=600)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf8", "ignore")[:300]
        raise RuntimeError(f"{provider} returned {e.code}: {detail}") from None


def _json_only(text: str) -> str:
    """Cloud models sometimes wrap JSON in prose or code fences; keep just the {...}."""
    i, j = text.find("{"), text.rfind("}")
    return text[i:j + 1] if 0 <= i < j else text


# ---------------------------------------------------------------- Ollama discovery

def check_ollama():
    urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3).close()


_CACHE = {"t": None, "names": []}


def installed_models() -> list:
    """Chat models installed in Ollama (embedding models left out). Cached for a few seconds."""
    if _CACHE["t"] is None or time.monotonic() - _CACHE["t"] > 5:
        try:
            with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3) as r:
                names = [m["name"] for m in json.load(r)["models"]]
            _CACHE.update(t=time.monotonic(), names=sorted(n for n in names if "embed" not in n.lower()))
        except Exception:
            pass
    return _CACHE["names"]


def cloud_models() -> list:
    """Model names offered in the /model menu: Anthropic's ones when a key is set (OpenAI names are typed by hand)."""
    return [f"anthropic:{n}" for n in ANTHROPIC_MODELS] if api_key("anthropic") else []


def usable(model: str) -> str:
    """'' if the model can be selected, otherwise the reason it can't."""
    provider, name = split(model)
    if provider == "ollama":
        return "" if model in installed_models() else f"'{model}' isn't installed. Installed: {', '.join(installed_models()) or 'none found'}"
    if not name:
        return f"Give a model name, e.g. {provider}:<model>."
    if not api_key(provider):
        return f"{KEY_VARS[provider]} isn't set. Export it in your shell and restart Local Docs."
    return ""


# ---------------------------------------------------------------- the three calls

def embed(texts: list) -> list:
    with _post("/api/embed", {"model": EMBED, "input": texts}) as r:
        return json.load(r)["embeddings"]


def chat(prompt: str, as_json: bool = False) -> str:
    provider = split()[0]
    if provider == "ollama":
        body = {"model": config.LLM, "messages": [{"role": "user", "content": prompt}],
                "stream": False, "think": False, "keep_alive": "30m", "options": {"num_ctx": NUM_CTX, "repeat_penalty": 1.1}}
        if as_json:
            body["format"] = "json"
        with _post("/api/chat", body) as r:
            return json.load(r)["message"]["content"].strip()
    with _cloud_request(provider, [{"role": "user", "content": prompt}], False, as_json) as r:
        data = json.load(r)
    text = (data["content"][0]["text"] if provider == "anthropic" else data["choices"][0]["message"]["content"]).strip()
    return _json_only(text) if as_json else text


def stream_chat(messages: list, holder: list):
    """Yield reply tokens. The open HTTP response is put in `holder` so another thread can close it to cancel."""
    provider = split()[0]
    if provider == "ollama":
        body = {"model": config.LLM, "messages": messages, "stream": True, "think": False, "keep_alive": "30m",
                "options": {"num_ctx": NUM_CTX, "repeat_penalty": 1.15, "num_predict": MAX_REPLY_TOKENS}}
        with _post("/api/chat", body) as r:
            holder.append(r)
            for line in r:
                part = json.loads(line)
                tok = part.get("message", {}).get("content", "")
                if tok:
                    yield tok
                if part.get("done"):
                    return
        return
    with _cloud_request(provider, messages, True, False) as r:   # server-sent events: "data: {...}" lines
        holder.append(r)
        for raw in r:
            line = raw.decode("utf8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                ev = json.loads(data)
            except ValueError:
                continue
            if provider == "anthropic":
                if ev.get("type") == "content_block_delta":
                    tok = ev.get("delta", {}).get("text", "")
                elif ev.get("type") == "error":
                    raise RuntimeError(ev.get("error", {}).get("message", "anthropic stream error"))
                else:
                    tok = ""
            else:
                choices = ev.get("choices") or [{}]
                tok = choices[0].get("delta", {}).get("content") or ""
            if tok:
                yield tok
