"""Talking to Ollama over local HTTP."""
from __future__ import annotations

import json
import urllib.request
from .config import EMBED, LLM, NUM_CTX, OLLAMA

def _post(path: str, payload: dict):
    req = urllib.request.Request(OLLAMA + path, json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=600)


def check_ollama():
    urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3).close()


def embed(texts: list) -> list:
    with _post("/api/embed", {"model": EMBED, "input": texts}) as r:
        return json.load(r)["embeddings"]


def chat(prompt: str, as_json: bool = False) -> str:
    body = {"model": LLM, "messages": [{"role": "user", "content": prompt}],
            "stream": False, "think": False, "keep_alive": "30m", "options": {"num_ctx": NUM_CTX, "repeat_penalty": 1.1}}
    if as_json:
        body["format"] = "json"
    with _post("/api/chat", body) as r:
        return json.load(r)["message"]["content"].strip()


