"""Offline checks (no Ollama, no network): provider parsing, cloud streaming, OCR. Run: uv run python tests/test_offline.py"""
import io
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from localdocs import config, llm, ocr, store   # noqa: E402


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): pass


def test_split():
    assert llm.split("qwen3:8b") == ("ollama", "qwen3:8b")
    assert llm.split("anthropic:claude-sonnet-5-5") == ("anthropic", "claude-sonnet-5-5")
    assert llm.split("openai:gpt-x") == ("openai", "gpt-x")


def test_streaming():
    os.environ.update(ANTHROPIC_API_KEY="k", OPENAI_API_KEY="k")
    anth = b'data: {"type":"content_block_delta","delta":{"text":"Hi"}}\n\ndata: {"type":"content_block_delta","delta":{"text":" there"}}\n\n'
    oai = b'data: {"choices":[{"delta":{"content":"Yo"}}]}\n\ndata: [DONE]\n\n'
    for model, data, want in [("anthropic:m", anth, "Hi there"), ("openai:m", oai, "Yo")]:
        config.LLM = model
        with mock.patch("urllib.request.urlopen", return_value=Resp(data)):
            assert "".join(llm.stream_chat([{"role": "user", "content": "u"}], [])) == want
    config.LLM = config.DEFAULT_LLM


def test_missing_key():
    os.environ.pop("OPENAI_API_KEY", None)
    assert "OPENAI_API_KEY" in llm.usable("openai:x")


def test_ocr():
    if not ocr.available():
        print("  (skipped: tesseract not installed)")
        return
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (900, 200), "white")
    ImageDraw.Draw(img).text((30, 60), "Order 5521 arrives Friday", font=ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 44), fill="black")
    with tempfile.TemporaryDirectory() as d:
        img.save(Path(d) / "a.png")
        img.save(Path(d) / "a.pdf")
        for name in ("a.png", "a.pdf"):
            assert "Order 5521 arrives Friday" in store.read_text(Path(d) / name), name


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
