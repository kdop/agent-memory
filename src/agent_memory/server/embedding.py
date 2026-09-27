"""Turn text into vectors on the server, with a small local model.

Memories are private, so the write path must not lean on an outside service.
The model runs in-process through `fastembed` (the `[embed]` extra). It is
optional: when it is not installed, or `AGENT_MEMORY_EMBED_MODEL=off`, the
server gets a `NullEmbedder` and keeps running without vectors.

Settings, read once when `make_embedder()` runs at server start:

    AGENT_MEMORY_EMBED_MODEL   model name (default BAAI/bge-small-en-v1.5);
                               the value `off` disables embedding
    AGENT_MEMORY_EMBED_CACHE   where model files land (default .cache/fastembed
                               under the repo root)

Every embedder returns unit-length vectors, so `cosine` is a plain dot product.
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_CACHE_SUBDIR = Path(".cache") / "fastembed"


class EmbeddingUnavailable(RuntimeError):
    """Raised when text is sent to an embedder that has no model."""


class Embedder:
    """What the server needs from an embedding backend.

    `model_name` names the model (None when there is none), `dim` is the vector
    length, and `embed` maps a list of texts to a list of unit-length vectors,
    one per text, in the same order.
    """

    model_name: str | None
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class NullEmbedder(Embedder):
    """The embedder the server runs with when there is no model."""

    model_name = None
    dim = 0

    def __init__(self, reason: str = "embedding is disabled"):
        self.reason = reason

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailable(self.reason)


class FastEmbedEmbedder(Embedder):
    """A local model run through fastembed. Loading the model takes a few seconds
    the first time, and downloads it into `cache_dir` if it is not there yet."""

    def __init__(self, model_name: str = DEFAULT_MODEL, cache_dir: str | os.PathLike | None = None):
        # Imported here, not at module load, so the server imports fine without
        # the [embed] extra.
        from fastembed import TextEmbedding

        cache = Path(cache_dir) if cache_dir is not None else default_cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        self.model_name = model_name
        self._model = TextEmbedding(model_name=model_name, cache_dir=str(cache))
        # One probe tells us the vector length for sure, whatever the model.
        self.dim = len(self.embed(["probe"])[0])

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [normalize([float(x) for x in vec]) for vec in self._model.embed(texts)]


def normalize(vec: list[float]) -> list[float]:
    """Scale a vector to unit length. A zero vector stays zero."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return list(vec)
    return [x / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors, in pure Python. Works for any two
    vectors of the same length; for unit vectors it is the dot product."""
    if len(a) != len(b):
        raise ValueError(f"vectors differ in length: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def repo_root() -> Path:
    """The checkout this package lives in: the nearest parent of this file that
    holds a pyproject.toml. Falls back to the current directory when the
    package is installed somewhere else."""
    here = Path(__file__).resolve().parent
    for d in (here, *here.parents):
        if (d / "pyproject.toml").is_file():
            return d
    return Path.cwd()


def default_cache_dir() -> Path:
    """Where model files go: AGENT_MEMORY_EMBED_CACHE, else .cache/fastembed
    under the repo root."""
    env = os.environ.get("AGENT_MEMORY_EMBED_CACHE")
    return Path(env) if env else repo_root() / DEFAULT_CACHE_SUBDIR


def make_embedder() -> Embedder:
    """Build the embedder from the environment. Returns a `NullEmbedder`, and
    logs one line saying why, when the model is switched off or fastembed is
    not installed."""
    model = os.environ.get("AGENT_MEMORY_EMBED_MODEL", DEFAULT_MODEL).strip()
    if model.lower() == "off":
        log.info("embedding is off (AGENT_MEMORY_EMBED_MODEL=off)")
        return NullEmbedder("embedding is off (AGENT_MEMORY_EMBED_MODEL=off)")
    try:
        import fastembed  # noqa: F401
    except ImportError:
        log.info("embedding is off: fastembed is not installed (pip install 'agent-memory[embed]')")
        return NullEmbedder("fastembed is not installed (pip install 'agent-memory[embed]')")
    cache = default_cache_dir()
    log.info("loading embedding model %s (cache %s)", model, cache)
    return FastEmbedEmbedder(model, cache)
