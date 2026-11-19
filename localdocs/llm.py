"""Talking to a language model: local Ollama by default, or any cloud API.

The active model is a single string in config.LLM:
    "qwen3:8b"                      -> Ollama, on this machine (default)
    "<provider>:<model>"            -> a cloud API, e.g. "anthropic:claude-sonnet-5-5", "gemini:...", "groq:..."
Providers are in PROVIDERS below: Anthropic has its own protocol, everything else speaks the OpenAI-style
chat API (Gemini, Groq, OpenRouter, Mistral, DeepSeek, xAI, Together, Perplexity, Fireworks, Cerebras, OpenAI...).
Any other OpenAI-compatible server can be added by name with /connect <name> <key> <base url>.
Keys come from /connect (saved in the Keychain, see keys.py) or the provider's environment variable
(e.g. ANTHROPIC_API_KEY). Embeddings always stay on Ollama, so the index never depends on a cloud service.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

from . import config, keys
from .config import EMBED, NUM_CTX, OLLAMA

# name -> (protocol, base URL). The key variable is <NAME>_API_KEY, the URL override <NAME>_BASE_URL.
PROVIDERS = {
    "anthropic": ("anthropic", "https://api.anthropic.com"),
    "openai": ("openai", "https://api.openai.com/v1"),
    "gemini": ("openai", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "groq": ("openai", "https://api.groq.com/openai/v1"),
    "openrouter": ("openai", "https://openrouter.ai/api/v1"),
    "mistral": ("openai", "https://api.mistral.ai/v1"),
    "deepseek": ("openai", "https://api.deepseek.com/v1"),
    "xai": ("openai", "https://api.x.ai/v1"),
    "together": ("openai", "https://api.together.xyz/v1"),
    "perplexity": ("openai", "https://api.perplexity.ai"),
    "fireworks": ("openai", "https://api.fireworks.ai/inference/v1"),
    "cerebras": ("openai", "https://api.cerebras.ai/v1"),
    "claude-cli": ("cli", ""),   # your own logged-in `claude` command (uses your Claude plan); no key, see _stream_cli
}
CLI_TIMEOUT = 240   # seconds before a stuck `claude` call is killed
CLI_MODELS = ["sonnet", "opus", "haiku"]
ANTHROPIC_MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]
NAME_OK = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")


def providers() -> list:
    """Built-in providers plus any the user added with /connect <name> <key> <url>."""
    return list(PROVIDERS) + [n for n in keys.custom() if n not in PROVIDERS]


def key_var(provider: str) -> str:
    return provider.upper().replace("-", "_") + "_API_KEY"


MAX_REPLY_TOKENS = 2000


# ---------------------------------------------------------------- which model

def split(model: str | None = None) -> tuple:
    """('ollama' | <provider>, model name)."""
    m = model or config.LLM
    prefix, sep, name = m.partition(":")
    if sep and prefix.lower() in providers():
        return prefix.lower(), name
    return "ollama", m


def is_cloud(model: str | None = None) -> bool:
    return split(model)[0] != "ollama"


def api_key(provider: str) -> str:
    """The environment variable if set, otherwise the key saved with /connect."""
    if provider == "claude-cli":     # no key: "connected" means the claude command exists and the user turned it on
        return "on" if shutil.which("claude") and keys.get("claude-cli") == "on" else ""
    return os.environ.get(key_var(provider), "").strip() or keys.get(provider)


def base_url(provider: str) -> str:
    env = os.environ.get(provider.upper().replace("-", "_") + "_BASE_URL", "").strip()
    return (env or keys.get("url:" + provider) or PROVIDERS.get(provider, ("", ""))[1]).rstrip("/")


def protocol(provider: str) -> str:
    return PROVIDERS.get(provider, ("openai", ""))[0]


def tag() -> str:
    """Short label for the status line, e.g. 'ollama.chat qwen3:8b' or 'anthropic.messages claude-…'."""
    provider, name = split()
    return f"ollama.chat {name}" if provider == "ollama" else f"{provider}.{'messages' if protocol(provider) == 'anthropic' else 'chat'} {name}"


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
        raise RuntimeError(f"No API key for {provider}. Type /connect {provider} <your key>.")
    name = split()[1]
    if protocol(provider) == "anthropic":
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        body = {"model": name, "max_tokens": MAX_REPLY_TOKENS, "stream": stream,
                "messages": [m for m in messages if m["role"] != "system"]}
        if system:
            body["system"] = system
        url = base_url(provider) + "/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    else:
        body = {"model": name, "messages": messages, "stream": stream}
        if as_json and provider == "openai":   # other servers differ on JSON mode; _json_only() copes without it
            body["response_format"] = {"type": "json_object"}
        url = base_url(provider) + "/chat/completions"
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


_NOT_CHAT = ("embed", "tts", "whisper", "dall-e", "moderation", "audio", "image", "realtime", "transcribe", "search", "davinci", "babbage")
_REMOTE: dict = {}


def fetch_models(provider: str) -> list:
    """Ask the provider which chat models this key can use. Raises on a bad key or no connection."""
    if protocol(provider) == "cli":
        _REMOTE[provider] = CLI_MODELS
        return CLI_MODELS
    anth = protocol(provider) == "anthropic"
    headers = ({"x-api-key": api_key(provider), "anthropic-version": "2023-06-01"} if anth
               else {"Authorization": f"Bearer {api_key(provider)}"})
    url = base_url(provider) + ("/v1/models?limit=100" if anth else "/models")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{provider} returned {e.code}") from None
    rows = data.get("data", []) if isinstance(data, dict) else data   # Together returns a bare list
    ids = [str(m["id"]).removeprefix("models/") for m in rows if isinstance(m, dict) and "id" in m]
    ids = [i for i in ids if not any(w in i.lower() for w in _NOT_CHAT)]
    _REMOTE[provider] = sorted(ids)
    return _REMOTE[provider]


def cloud_models() -> list:
    """Models for the /model menu: whatever a connected provider offered, else Anthropic's known names."""
    out = []
    for provider in providers():
        if api_key(provider):
            names = _REMOTE.get(provider) or (ANTHROPIC_MODELS if provider == "anthropic" else CLI_MODELS if provider == "claude-cli" else [])
            out += [f"{provider}:{n}" for n in names]
    return out


def usable(model: str) -> str:
    """'' if the model can be selected, otherwise the reason it can't."""
    provider, name = split(model)
    if provider == "ollama":
        return "" if model in installed_models() else f"'{model}' isn't installed. Installed: {', '.join(installed_models()) or 'none found'}"
    if not name:
        return f"Give a model name, e.g. {provider}:<model>."
    if not api_key(provider):
        return f"No {provider} key yet. Type /connect {provider} <your key>."
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
    if protocol(provider) == "cli":
        text = "".join(_stream_cli([{"role": "user", "content": prompt}], [])).strip()
        return _json_only(text) if as_json else text
    with _cloud_request(provider, [{"role": "user", "content": prompt}], False, as_json) as r:
        data = json.load(r)
    text = (data["content"][0]["text"] if protocol(provider) == "anthropic" else data["choices"][0]["message"]["content"]).strip()
    return _json_only(text) if as_json else text


def chat_with(model: str, prompt: str) -> str:
    """One-off call to a specific model, without changing the active one."""
    old, config.LLM = config.LLM, model
    try:
        return chat(prompt)
    finally:
        config.LLM = old


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
    if protocol(provider) == "cli":
        yield from _stream_cli(messages, holder)
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
            if protocol(provider) == "anthropic":
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


# ---------------------------------------------------------------- your own `claude` command

class _Proc:
    """Lets the caller cancel a running `claude` process the same way it closes an HTTP response."""
    def __init__(self, proc):
        self.proc = proc

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()


def _cli_prompt(messages: list) -> tuple:
    """(system prompt, text to send): earlier turns are folded into the text, the last user message ends it."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    last = turns[-1]["content"] if turns else ""
    earlier = "\n\n".join(f"{m['role'].capitalize()}: {m['content']}" for m in turns[:-1])
    return system, (f"<earlier_conversation>\n{earlier}\n</earlier_conversation>\n\n{last}" if earlier else last)


def _stream_cli(messages: list, holder: list):
    """Run the user's own logged-in `claude` command as a plain text model: no tools, no project files, no saved session.

    Local Docs never sees or stores the user's Claude login; the command uses it itself. ANTHROPIC_API_KEY is removed
    from its environment so it bills the plan, not an API key.
    """
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("The `claude` command isn't installed or isn't on your PATH (install Claude Code and log in).")
    system, text = _cli_prompt(messages)
    cmd = [exe, "-p", "--tools", "", "--no-session-persistence", "--disable-slash-commands", "--setting-sources", "",
           "--strict-mcp-config", "--model", split()[1] or "sonnet",
           "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
    if system:
        cmd += ["--system-prompt", system]
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    # start_new_session: no controlling terminal, so it can't query or wait on the TUI's terminal
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            cwd=tempfile.gettempdir(), env=env, start_new_session=True)
    holder.append(_Proc(proc))
    watchdog = threading.Timer(CLI_TIMEOUT, _Proc(proc).close)    # never hang forever
    watchdog.daemon = True
    watchdog.start()
    try:
        proc.stdin.write(text)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    try:
        yield from _read_cli(proc)
    finally:
        watchdog.cancel()
        _Proc(proc).close()


def _read_cli(proc):
    streamed, final = False, ""
    for line in proc.stdout:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "stream_event":
            d = ev.get("event", {})
            if d.get("type") == "content_block_delta" and d.get("delta", {}).get("type") == "text_delta":
                streamed = True
                yield d["delta"]["text"]
        elif ev.get("type") == "result":
            if ev.get("is_error"):
                raise RuntimeError(f"claude: {str(ev.get('result') or 'request failed')[:300]}")
            final = ev.get("result") or ""
    if not streamed and final:       # no partial events arrived: still return the answer
        yield final
    code = proc.wait()
    if not streamed and not final:
        if code == -9:
            raise RuntimeError(f"claude didn't answer within {CLI_TIMEOUT}s")
        raise RuntimeError("claude failed: " + ((proc.stderr.read() or "").strip() or f"exit {code}")[:300])
