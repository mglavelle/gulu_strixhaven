"""Settings read from environment / .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

def _default_ollama_url() -> str:
    # Reuse Ollama's own OLLAMA_HOST setting if present (e.g. "192.168.1.50:11434").
    host = os.getenv("OLLAMA_HOST", "127.0.0.1:11434")
    if host.startswith("0.0.0.0"):
        host = host.replace("0.0.0.0", "127.0.0.1", 1)
    return host if host.startswith("http") else f"http://{host}"


OLLAMA_URL = os.getenv("OLLAMA_URL") or _default_ollama_url()
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen3.5:latest")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
# Context window for answers. Model + context must fit in graphics memory or Windows
# spills it to system RAM (3x slower, stalls). On an 8 GB card with desktop apps open,
# 6144 keeps qwen3.5 fully on the GPU; 8192 did not. Prompts are trimmed to fit.
CHAT_CTX = int(os.getenv("CHAT_CTX", "6144"))
ANSWER_RESERVE = int(os.getenv("ANSWER_RESERVE", "1200"))  # tokens kept free for the answer
# Force how many model layers go on the GPU (99 = all). Leave unset to let Ollama decide.
# Needed on Intel Arc + Vulkan: Ollama's "does it fit?" probe misreads free memory there and
# ends up putting 0 layers on the GPU even with 7 GB free.
CHAT_NUM_GPU = int(os.getenv("CHAT_NUM_GPU")) if os.getenv("CHAT_NUM_GPU", "").strip() else None
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "30m")         # keep model loaded between questions
# Run the small search-embedding model on the CPU (0) so the chat model keeps all the
# graphics memory. -1 = let Ollama decide (faster bulk ingestion, uses ~350 MB of VRAM).
EMBED_NUM_GPU = int(os.getenv("EMBED_NUM_GPU", "0"))
THINK = os.getenv("THINK", "false").lower() in ("1", "true", "yes")  # reason before answering

DM_PASSWORD = os.getenv("DM_PASSWORD", "")
PLAYER_PASSWORD = os.getenv("PLAYER_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
DEFAULT_PASSWORDS = {"", "change-me", "change-me-too"}
DATA_DIR = (ROOT / os.getenv("DATA_DIR", "data")).resolve()
DB_PATH = DATA_DIR / "companion.db"
SOURCES_DIR = DATA_DIR / "sources"
