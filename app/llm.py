"""Thin Ollama client: embeddings and chat."""
from typing import Iterator
import json

import httpx
import numpy as np

from . import config

# nomic-embed-text expects task prefixes; other models ignore them harmlessly
# only if we leave them off, so prefixes are applied per model.
_PREFIXES = {
    "nomic-embed-text": ("search_document: ", "search_query: "),
    "qwen3-embedding": ("", "Instruct: Given a question about Dungeons & Dragons, "
                            "retrieve the rules or book passage that answers it\nQuery: "),
}


def _prefix(model: str, kind: str) -> str:
    base = model.split(":")[0]
    doc, query = _PREFIXES.get(base, ("", ""))
    return doc if kind == "doc" else query


def embed(texts: list[str], kind: str = "doc", model: str | None = None,
          batch: int = 32) -> np.ndarray:
    """Return L2-normalised float32 embeddings, one row per text."""
    model = model or config.EMBED_MODEL
    pre = _prefix(model, kind)
    out = []
    with httpx.Client(base_url=config.OLLAMA_URL, timeout=300) as c:
        for i in range(0, len(texts), batch):
            chunk = [pre + t for t in texts[i:i + batch]]
            body = {"model": model, "input": chunk}
            if config.EMBED_NUM_GPU >= 0:
                body["options"] = {"num_gpu": config.EMBED_NUM_GPU}
            r = c.post("/api/embed", json=body)
            r.raise_for_status()
            out.extend(r.json()["embeddings"])
    arr = np.asarray(out, dtype=np.float32)
    arr /= np.linalg.norm(arr, axis=1, keepdims=True) + 1e-9
    return arr


def chat_stream(messages: list[dict], model: str | None = None,
                think: bool | None = None) -> Iterator[str]:
    """Yield response text pieces as Ollama generates them (thinking text is dropped)."""
    for kind, piece in chat_events(messages, model, think):
        if kind == "text":
            yield piece


def chat_events(messages: list[dict], model: str | None = None,
                think: bool | None = None) -> Iterator[tuple[str, str]]:
    """Yield ("thinking", piece) and ("text", piece) as Ollama generates them."""
    model = model or config.CHAT_MODEL
    think = config.THINK if think is None else think
    with httpx.Client(base_url=config.OLLAMA_URL, timeout=None) as c:
        with c.stream("POST", "/api/chat", json={
            "model": model, "messages": messages, "stream": True, "think": think,
            "keep_alive": config.KEEP_ALIVE,
            # temperature 0: the same question gets the same answer (repeatable, testable)
            "options": {"num_ctx": config.CHAT_CTX, "temperature": 0},
        }) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                if msg.get("error"):
                    raise RuntimeError(msg["error"])
                m = msg.get("message", {})
                if m.get("thinking"):
                    yield "thinking", m["thinking"]
                if m.get("content"):
                    yield "text", m["content"]
                if msg.get("done"):
                    break


def chat(messages: list[dict], model: str | None = None) -> str:
    return "".join(chat_stream(messages, model))
