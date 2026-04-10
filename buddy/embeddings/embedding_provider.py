# buddy/embeddings/embedding_provider.py
from __future__ import annotations

import os
import threading
from typing import List, Optional, Sequence

import numpy as np
from sentence_transformers import SentenceTransformer

from buddy.logger.logger import get_logger

logger = get_logger("embedding_provider")


class EmbeddingProvider:
    """
    Single source of truth for all embeddings in Buddy.

    ── Model resolution (first wins) ──────────────────────────────────────────
      1. BUDDY_EMBED_MODEL env var
         Bootstrap sets this to the locally-downloaded model path BEFORE the
         first EmbeddingProvider() call, giving fully offline behavior after
         the initial download. Example:
             os.environ["BUDDY_EMBED_MODEL"] = "~/.buddy/data/models/st/Qwen__Qwen3-Embedding-0.6B"
             provider = EmbeddingProvider()   ← picks up local path

      2. Hardcoded HF default: "Qwen/Qwen3-Embedding-0.6B"
         Falls through to HuggingFace auto-cache if no env var is set.
         Fine for development; not recommended for fully offline deployments.

    ── Locked spec (v1+) ──────────────────────────────────────────────────────
      Model     : Qwen/Qwen3-Embedding-0.6B
      Dimension : 1024 (default full dim)
      Norm      : L2-normalized (normalize_embeddings=True)
      Query     : prompt_name="query" (Qwen3 asymmetric retrieval)
    """

    _instance: Optional["EmbeddingProvider"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "EmbeddingProvider":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._init()
                    cls._instance = inst
        return cls._instance

    # --------------------------------------------------
    # Initialization
    # --------------------------------------------------

    def _init(self) -> None:
        # Model resolution: env var (set by bootstrap) → hardcoded HF default
        env_model = (os.getenv("BUDDY_EMBED_MODEL") or "").strip()
        self.model_name: str = env_model if env_model else "Qwen/Qwen3-Embedding-0.6B"

        self.debug = False
        self.device = self._choose_device()

        logger.info(
            "EmbeddingProvider loading: %s | device=%s", self.model_name, self.device
        )

        self._model = SentenceTransformer(
            self.model_name, device=self.device, local_files_only=True
        )

        dim = self._model.get_sentence_embedding_dimension()
        if dim is None:
            raise ValueError("Could not determine embedding dimension from model")
        self._dimension = int(dim)

        # Warmup — eliminates first-call latency spike in real pipeline
        try:
            self._model.encode(
                ["warmup"],
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        except Exception as ex:
            self._debug("Warmup failed (non-fatal):", repr(ex))

        logger.info(
            "EmbeddingProvider ready | model=%s dim=%d device=%s",
            self.model_name,
            self._dimension,
            self.device,
        )

    def _choose_device(self) -> str:
        # Allow explicit override
        env = (os.getenv("BUDDY_EMBED_DEVICE") or "").strip().lower()
        if env in {"mps", "cpu", "cuda"}:
            return env
        # Best-effort MPS detection without importing torch at module load time
        try:
            import torch  # type: ignore

            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        return "cpu"

    # --------------------------------------------------
    # Debug
    # --------------------------------------------------

    def _debug(self, *args: object) -> None:
        if getattr(self, "debug", False):
            logger.debug(" ".join(str(a) for a in args))

    # --------------------------------------------------
    # Public API
    # --------------------------------------------------

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed_passage(self, text: str) -> np.ndarray:
        """
        Embed stored content / memory / anchors.
        Qwen3: documents do NOT use the query prompt.
        Returns: (D,) float32, L2-normalized.
        """
        t = (text or "").strip()
        if not t:
            raise ValueError("embed_passage: text is empty")
        return self._encode([t], prompt_name=None)[0]

    def embed_query(self, text: str) -> np.ndarray:
        """
        Embed a retrieval query / user intent.
        Qwen3: queries use prompt_name="query" for asymmetric retrieval.
        Returns: (D,) float32, L2-normalized.
        """
        t = (text or "").strip()
        if not t:
            raise ValueError("embed_query: text is empty")
        return self._encode([t], prompt_name="query")[0]

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        """Batch embed passages. Returns (N, D) float32."""
        items = [str(x or "").strip() for x in (texts or [])]
        items = [x for x in items if x]
        if not items:
            raise ValueError("embed_passages: no non-empty texts")
        return self._encode(items, prompt_name=None)

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        """Batch embed queries. Returns (N, D) float32."""
        items = [str(x or "").strip() for x in (texts or [])]
        items = [x for x in items if x]
        if not items:
            raise ValueError("embed_queries: no non-empty texts")
        return self._encode(items, prompt_name="query")

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """
        Fast cosine similarity for single vectors.
        Since embeddings are L2-normalized, dot product ≈ cosine.
        """
        av = self._as_np(a)
        bv = self._as_np(b)
        denom = float(np.linalg.norm(av) * np.linalg.norm(bv))
        if denom <= 0.0:
            return 0.0
        return float(np.dot(av, bv) / denom)

    def cosine_similarity_batch(
        self, query: np.ndarray, passages: np.ndarray
    ) -> np.ndarray:
        """
        (D,) query vs (N, D) passages → (N,) float32.
        Assumes L2-normalized embeddings.
        """
        q = self._as_np(query)
        P = np.asarray(passages, dtype=np.float32)
        if P.ndim != 2:
            raise ValueError("passages must be shape (N, D)")
        if q.ndim != 1:
            raise ValueError("query must be shape (D,)")
        return (P @ q).astype(np.float32)

    # --------------------------------------------------
    # Internal
    # --------------------------------------------------

    def _encode(self, texts: List[str], *, prompt_name: Optional[str]) -> np.ndarray:
        self._debug("encode n=", len(texts), "prompt_name=", prompt_name)
        try:
            if prompt_name:
                arr = self._model.encode(
                    texts,
                    prompt_name=prompt_name,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
            else:
                arr = self._model.encode(
                    texts,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
        except TypeError:
            # Fallback for older sentence-transformers versions (< 2.7)
            arr = self._model.encode(
                texts,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )

        out = np.asarray(arr, dtype=np.float32)
        if out.ndim == 1:
            out = out.reshape(1, -1)
        if out.shape[1] <= 0:
            raise ValueError("encode: empty embedding dimension")
        return out

    @staticmethod
    def _as_np(x: np.ndarray) -> np.ndarray:
        arr = np.asarray(x, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            raise ValueError("Embedding vector is empty")
        return arr


# --------------------------------------------------
# In-file tests
# --------------------------------------------------
if __name__ == "__main__":
    print("🧪 embedding_provider.py tests starting…")

    emb1 = EmbeddingProvider()
    emb2 = EmbeddingProvider()
    assert emb1 is emb2, "must be singleton"
    print("✅ TEST 1: singleton ok | model =", emb1.model_name)

    dim = emb1.dimension
    assert isinstance(dim, int) and dim > 0
    print("✅ TEST 2: dimension =", dim)

    v_pass = emb1.embed_passage("VS Code is my editor")
    v_q = emb1.embed_query("What editor do I use?")
    assert v_pass.shape == (dim,) and v_pass.dtype == np.float32
    assert v_q.shape == (dim,) and v_q.dtype == np.float32
    print("✅ TEST 3: shapes ok")

    for label, v in [("passage", v_pass), ("query", v_q)]:
        n = float(np.linalg.norm(v))
        assert 0.95 <= n <= 1.05, f"{label} norm unexpected: {n}"
    print("✅ TEST 4: normalization ok")

    s = emb1.cosine_similarity(v_pass, v_pass)
    assert 0.98 <= s <= 1.001, f"self similarity unexpected: {s}"
    print("✅ TEST 5: cosine self similarity ok")

    docs = emb1.embed_passages(["alpha", "beta", "alpha"])
    q = emb1.embed_query("alpha")
    assert docs.shape == (3, dim)
    sims = emb1.cosine_similarity_batch(q, docs)
    assert sims.shape == (3,)
    assert float(sims[0]) >= float(sims[1])
    assert float(sims[2]) >= float(sims[1])
    print("✅ TEST 6: batch encode + similarity ok")

    for bad in ["   ", ""]:
        try:
            emb1.embed_query(bad)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
    print("✅ TEST 7: empty input guards ok")

    print("🎉 All tests passed")
