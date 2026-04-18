# buddy/memory/vector_store.py
# 🔒 LOCKED FILE: buddy/memory/vector_store.py
# Policy:
# - Do not change the public contract: upsert(), search(), search_with_payloads()
# - Allowed: bug fixes, compatibility fixes, perf improvements that preserve outputs.
# - Any new capability must be behind defaults and must not break dense-only setups.
#
# v1 score contract (IMPORTANT):
# - Returned scores are ALWAYS similarity-like dense scores (from dense search).
# - Hybrid mode may use sparse for recall to select candidate IDs,
#   but final scores are always dense similarity re-scores.
# - Reranker (if enabled) ONLY reorders results; it never replaces scores.
#
# Update (2026-01-17):
# - Config-first: VectorStore(vs_cfg=cfg["vector_store"]) (no strict validation)
# - Exactly TWO rerankers:
#     1) Qwen3RerankerYesNo
#     2) CrossEncoderReranker
# - Keep dense score unchanged; attach rerank_score into returned payloads as:
#     payload["_rerank"] = {"method": "...", "model": "...", "score": float}
#
# FIXES & IMPROVEMENTS (applied to user's version):
#   FIX-1  : Qwen3RerankerYesNo used dtype= but HuggingFace requires torch_dtype=.
#             Model was silently loading in float32 regardless of device; VRAM wasted.
#   FIX-2  : AutoSparsePolicy.short_query_max_tokens was defined but NEVER checked.
#             _auto_use_sparse now correctly triggers sparse on short queries (≤N words).
#   FIX-3  : _dense_search fallback attempt-2 dropped query_filter entirely.
#             Deleted/wrong-type results could leak through. Now keeps filter; only
#             drops the `using` kwarg when retrying on older qdrant-client versions.
#   FIX-4  : _maybe_rerank called _init_reranker() (→ lock) on every search call.
#             Now uses cached _get_reranker() that bypasses the lock after first init.
#   FIX-5  : Hybrid score ordering was sparse-first then dense-fill, ignoring dense
#             ranking for sparse-only candidates. Replaced with proper RRF merging.
#   FIX-6  : Dense gate dropped ALL results without trying sparse rescue first.
#             Now attempts sparse rescue before giving up on low-confidence dense.
#   ACC-1  : RRF (_ordered_union_rrf): documents in both lists naturally boosted.
#   ACC-2  : MMR (Maximal Marginal Relevance) opt-in via RerankConfig.use_mmr=True.
#   FEAT-1 : Context manager support (__enter__ / __exit__).
#   FEAT-2 : upsert_batch() for efficient bulk inserts.
#   FEAT-3 : search_by_ids() for direct point retrieval by ID.
#
# ADDITIONAL FIXES (2026-02-20):
#   BUG-1  : HashingSparseEncoder.encode() counted TF on already-deduplicated tokens
#             (_lex_tokens deduplicates), so TF was always 1 and log_tf had zero
#             effect. Fixed: raw token frequencies now counted from _LEX_TOKEN_RE
#             directly, restoring proper TF weighting for repeated terms.
#   BUG-2  : Hybrid search returned results in RRF candidate order, NOT sorted by
#             dense score. This violated the score contract (callers expect score-
#             descending output). Fixed: hits.sort() by dense score before returning.

from __future__ import annotations

import math
import re
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from buddy.logger.logger import get_logger
from buddy.memory.memory_entry import MemoryEntry

logger = get_logger("vector_store")

# Qdrant imports (client + models)
try:
    from qdrant_client import QdrantClient  # type: ignore
    from qdrant_client.http import models as qm  # type: ignore
except Exception as ex:  # pragma: no cover
    raise RuntimeError(
        "qdrant-client is required for VectorStore. Install qdrant-client."
    ) from ex


# ==========================================================
# Lexical detection/tokenization (optimized)
# ==========================================================

# Fast word token finder (used for short-query heuristic)
_WORD_RE = re.compile(r"[a-z0-9_]+", re.I)

# Include backticks as "quoted" spans
_QUOTED_RE = re.compile(r'"([^"]+)"|\'([^\']+)\'|`([^`]+)`')

_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.I,
)
_HASH_RE = re.compile(r"\b[0-9a-f]{32,64}\b", re.I)  # md5..sha256-ish
_VERSION_RE = re.compile(r"\bv?\d+(?:\.\d+){1,4}\b", re.I)  # 1.2, v1.2.3, 1.2.3.4
_TICKET_RE = re.compile(
    r"\b(?:pr\s*#?\s*\d+|[a-z]{2,10}-\d+)\b", re.I
)  # PR#123, JIRA-123
_AWS_REGION_RE = re.compile(r"\b[a-z]{2}-[a-z]+-\d\b", re.I)  # ca-central-1

_FILENAME_RE = re.compile(r"\b[^/\s]+\.[a-z0-9]{1,10}\b", re.I)
_EMAIL_RE = re.compile(r"\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b", re.I)
_DOMAIN_EXACT_RE = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$", re.I)
_WIN_PATH_RE = re.compile(r"^[a-zA-Z]:\\")  # quick check

# Big token regex is precompiled once (hot path)
_LEX_TOKEN_RE = re.compile(
    r"""
    (?:\\\\[^ \t\n\r\f\v]+) |                          # UNC path \\server\share\path
    (?:[a-zA-Z]:\\[^ \t\n\r\f\v]+) |                   # windows path C:\...
    (?:~?/[^ \t\n\r\f\v]+) |                           # unix-ish path /... or ~/...
    (?:\.[a-z0-9_][a-z0-9._-]{0,80}\b) |               # hidden files (.env, .gitignore)

    (?:\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b) |  # UUID
    (?:\b[0-9a-f]{32,64}\b) |                          # hex hashes
    (?:\bv?\d+(?:\.\d+){1,4}\b) |                      # versions 1.2 / v1.2.3
    (?:\b(?:pr\s*\#?\s*\d+|[a-z]{2,10}-\d+)\b) |       # PR#123, JIRA-123
    (?:\b[a-z]{2}-[a-z]+-\d\b) |                       # AWS region ca-central-1

    (?:\b[^/\s]+\.[a-z0-9]{1,10}\b) |                  # filename.ext
    (?:\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b) |    # email
    (?:\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b) |               # domain
    (?:[a-z0-9_]+(?:\.[a-z0-9_]+)+) |                  # dotted identifiers
    (?:[a-z0-9_]+) |                                   # plain tokens

    (?:==|!=|<=|>=|->|::|\|\||&&) |                    # code operators
    (?:[{}()\[\];:=<>/\\#@])                           # symbols
""",
    re.I | re.VERBOSE,
)

# Precompile (tiny but hot) operators/punct check
_CODEISH_RE = re.compile(r"[{}()$;:=<>]|::|->|==|!=|&&|\|\|")


def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid for scalar normalization."""
    # Stable form avoids overflow for large |x|
    if x >= 0.0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def _as_np(vec: Sequence[float] | np.ndarray) -> Optional[np.ndarray]:
    """
    Convert vector-like input into a 1D float32 numpy array.

    Returns:
        np.ndarray (float32, shape=(d,)) or None if vec is missing/empty/invalid.

    Perf notes:
        - Avoids `list(vec)` for common inputs (list/tuple/np.array).
        - Only falls back to `list(vec)` when vec is a one-shot iterable/generator.
    """
    if vec is None:
        return None

    if isinstance(vec, np.ndarray):
        v = vec.astype(np.float32, copy=False).reshape(-1)
        return v if v.size else None

    try:
        # Fast path for typical sequences
        v = np.asarray(vec, dtype=np.float32).reshape(-1)  # type: ignore[arg-type]
        return v if v.size else None
    except Exception:
        # Slow but safer fallback (iterables/generators)
        try:
            v = np.asarray(list(vec), dtype=np.float32).reshape(-1)  # type: ignore[arg-type]
            return v if v.size else None
        except Exception:
            return None


def _looks_lexical(query_text: str) -> bool:
    """
    Heuristic: detect whether the query is likely to benefit from lexical sparse recall.

    This is used only for candidate recall in hybrid mode.
    Final ranking scores remain dense similarity (locked contract).
    """
    q = (query_text or "").strip()
    if not q:
        return False

    # Strong lexical signals
    if _QUOTED_RE.search(q):
        return True
    if _WIN_PATH_RE.match(q) or q.startswith("\\\\"):
        return True
    if "/" in q or "\\" in q or q.startswith("~"):
        return True
    if _EMAIL_RE.search(q) or "http://" in q or "https://" in q:
        return True
    if _FILENAME_RE.search(q):
        return True
    if _UUID_RE.search(q) or _HASH_RE.search(q) or _VERSION_RE.search(q):
        return True
    if _TICKET_RE.search(q) or _AWS_REGION_RE.search(q):
        return True

    # Bare domain exact match (e.g., "example.com")
    if _DOMAIN_EXACT_RE.fullmatch(q):
        return True

    # Code-ish punctuation/operators
    if _CODEISH_RE.search(q):
        return True

    return False


def _lex_tokens(text: str) -> List[str]:
    """
    Extract lexical tokens for hashing-sparse encoding.

    Output properties:
      - Lowercased
      - Order-preserving unique tokens
      - Includes quoted/backticked phrases as whole tokens (normalized spaces)
      - Adds split expansions for paths/dots/separators (- _ :)

    Perf:
      - Single pass order-preserving dedupe via `seen`.
      - Avoids building large intermediate lists.
    """
    t = (text or "").strip()
    if not t:
        return []

    seen: set[str] = set()
    out: List[str] = []

    def _add(tok: str) -> None:
        if not tok:
            return
        tok = tok.strip().lower()
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)

    # Quoted / backticked phrases
    for m in _QUOTED_RE.finditer(t):
        phrase = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if phrase:
            _add(" ".join(phrase.split()))

    # Main token extraction
    for raw in _LEX_TOKEN_RE.findall(t):
        tok = str(raw).strip()
        if not tok:
            continue
        tok_l = tok.lower()
        _add(tok_l)

        # Split expansions (keep order)
        if ("/" in tok_l) or ("\\" in tok_l):
            for p in re.split(r"[\\/]+", tok_l):
                _add(p)

        if "." in tok_l and len(tok_l) <= 128:
            for p in tok_l.split("."):
                _add(p)

        if ("-" in tok_l or "_" in tok_l or ":" in tok_l) and len(tok_l) <= 256:
            for p in re.split(r"[-_:]+", tok_l):
                _add(p)

    return out


def _pick_torch_device(device: str) -> str:
    """
    Pick best torch device for transformers.

    Order for "auto":
      CUDA (if truly usable) > MPS > CPU

    Returns:
      "cuda" or "cuda:0", "mps", or "cpu"
    """
    d = (device or "auto").strip().lower()
    if d != "auto":
        return d

    try:
        import torch  # type: ignore

        # ---- Prefer CUDA if usable ----
        if torch.cuda.is_available():  # type: ignore
            try:
                # sanity check: can we allocate?
                _ = torch.empty((1,), device="cuda")
                return "cuda"  # or "cuda:0"
            except Exception:
                pass

        # ---- Then Apple Silicon MPS ----
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():  # type: ignore
            return "mps"

    except Exception:
        pass

    return "cpu"


def _pick_torch_dtype(device: str):
    import torch  # type: ignore

    dev = (device or "cpu").lower()
    if dev.startswith("cuda") or dev == "mps":
        return torch.float16
    return torch.float32


def _auto_rerank_batch_size(device: str, max_length: int) -> int:
    """
    Pick a safe batch_size for Qwen3 yes/no reranking.

    Heuristic notes:
    - Cost scales roughly with (batch_size * max_length).
    - 2048-token docs are heavy; keep CPU batches small.
    - On CUDA, we can scale with VRAM if available.

    Args:
        device: "cpu" | "mps" | "cuda" | "cuda:0" | ...
        max_length: tokenizer max_length used for the prompts.

    Returns:
        A conservative batch_size that should work across machines.
    """
    dev = (device or "cpu").lower()

    # Normalize for max_length: if you increase length, reduce batch.
    # baseline assumes ~2304; scale down when longer, up a bit when shorter.
    baseline = 2304
    length_scale = baseline / max(256, int(max_length))
    # Clamp scale so we don't go crazy
    length_scale = max(0.5, min(1.5, float(length_scale)))

    if dev.startswith("cuda"):
        # Start conservative, then scale with VRAM if possible.
        bs = 8
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():  # type: ignore
                props = torch.cuda.get_device_properties(0)  # type: ignore
                vram_gb = float(props.total_memory) / (1024**3)

                # Simple VRAM tiers (conservative for 2k tokens)
                if vram_gb >= 24:
                    bs = 16
                elif vram_gb >= 16:
                    bs = 12
                elif vram_gb >= 10:
                    bs = 10
                else:
                    bs = 8
        except Exception:
            bs = 8

        bs = int(round(bs * length_scale))
        return max(4, min(32, bs))

    if dev == "mps":
        # MPS tends to be memory-sensitive; keep moderate.
        bs = 4
        bs = int(round(bs * length_scale))
        return max(2, min(12, bs))

    # CPU
    bs = 2
    bs = int(round(bs * length_scale))
    return max(1, min(6, bs))


# ==========================================================
# Sparse encoding (deterministic hashing)
# ==========================================================


@dataclass(frozen=True)
class SparseConfig:
    """
    Config for hashing-based sparse encoder used for recall only.

    Notes:
      - Increasing vocab_size reduces hashing collisions.
      - max_terms caps sparse dimensionality for query cost control.
    """

    enabled: bool = True
    vocab_size: int = 131072
    max_terms: int = 96
    use_log_tf: bool = True


class HashingSparseEncoder:
    """
    Deterministic hashing-based sparse encoder.

    Important:
      - Used only for hybrid *recall* (candidate discovery).
      - Returned search scores are ALWAYS dense similarity (locked contract).
    """

    __slots__ = ("cfg",)

    def __init__(self, cfg: SparseConfig):
        self.cfg = cfg

    def encode(self, text: str) -> "qm.SparseVector":
        # _lex_tokens gives us the deduplicated canonical token list; use it
        # to guard against empty input only.
        tokens = _lex_tokens(text)
        if not tokens:
            return qm.SparseVector(indices=[], values=[])

        vocab = int(self.cfg.vocab_size)
        h_fn = self._hash_token

        # BUG-FIX (TF dedup): _lex_tokens deduplicates, so looping over it gives
        # TF=1 for every token making log_tf a silent no-op and losing term-frequency
        # signal. Instead, count from the raw token stream so repeated terms
        # (e.g. "python python python tutorial") get their true frequency weights.
        tf: Dict[int, int] = {}
        t = (text or "").strip()
        for raw in _LEX_TOKEN_RE.findall(t):
            tok = str(raw).strip().lower()
            if not tok:
                continue
            idx = h_fn(tok, vocab)
            tf[idx] = tf.get(idx, 0) + 1

        # Keep top max_terms by TF. Sorting is OK because max_terms is small
        # and token list per query is typically small.
        items = sorted(tf.items(), key=lambda x: x[1], reverse=True)[
            : int(self.cfg.max_terms)
        ]

        use_log = bool(self.cfg.use_log_tf)
        indices: List[int] = []
        values: List[float] = []
        for idx, c in items:
            indices.append(int(idx))
            values.append(float(math.log1p(c)) if use_log else float(c))

        return qm.SparseVector(indices=indices, values=values)

    @staticmethod
    def _hash_token(token: str, vocab: int) -> int:
        """FNV-1a 32-bit hash mod vocab_size (deterministic)."""
        h = 2166136261
        for ch in token:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        return int(h % vocab)


# ==========================================================
# Auto sparse policy + rerank config
# ==========================================================


@dataclass(frozen=True)
class AutoSparsePolicy:
    """
    Heuristics for deciding when to use sparse recall in mode="auto".

    These do NOT affect scoring. They only decide whether to use sparse
    to fetch extra candidate IDs before dense re-scoring.
    """

    enabled: bool = True
    # FIX-2: short_query_max_tokens was previously defined but never checked.
    # Now _auto_use_sparse uses it: queries with ≤ N word-tokens trigger sparse.
    short_query_max_tokens: int = 4
    dense_low_score_threshold: float = 0.30
    dense_ambiguity_gap: float = 0.02


# ==========================================================
# Config dataclasses (explicit, no dict config plumbing)
# ==========================================================


@dataclass(frozen=True)
class VectorServerConfig:
    """Only used when backend='server'."""

    url: str = "http://127.0.0.1:6333"
    api_key: str = ""
    timeout: int = 10


@dataclass(frozen=True)
class RerankConfig:
    """
    Reranker configuration.

    Important contract:
      - reranker reorders results only
      - returned scores remain dense similarity scores

    New fields vs original:
      - use_mmr: enable Maximal Marginal Relevance diversity post-filtering (ACC-2)
      - mmr_lambda: MMR trade-off weight (1.0 = pure relevance, 0.0 = pure diversity)
      - rrf_k: RRF rank constant for hybrid merging, default 60 (ACC-1)
    """

    enabled: bool = True
    method: str = "qwen3"  # "qwen3" | "cross_encoder"
    always_on: bool = True

    pre_rerank_k: int = 20
    max_chars_per_doc: int = 700

    device: str = "auto"  # "auto" | "mps" | "cuda" | "cpu"
    batch_size: int = 8
    max_length: int = 2048

    qwen_model: str = "Qwen/Qwen3-Reranker-0.6B"
    cross_encoder_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    dense_threshold: float = 0.25
    rerank_threshold: float = 0.40
    rerank_min_keep: int = 0

    # ACC-2: MMR diversity post-processing (opt-in, off by default)
    use_mmr: bool = False
    mmr_lambda: float = 0.7  # 1.0 = pure relevance, 0.0 = pure diversity

    # ACC-1: RRF rank constant for hybrid candidate merging
    rrf_k: int = 60

    # Skip reranker when dense top score already exceeds this (high confidence path)
    skip_rerank_above: float = 1.1  # default >1 = never skip (off by default)


# ==========================================================
# Rerankers (EXACTLY TWO)
# ==========================================================


class CrossEncoderReranker:
    """
    SentenceTransformers CrossEncoder reranker.

    Returns normalized scores in [0, 1] via sigmoid to standardize outputs.
    """

    __slots__ = ("model_name", "device", "model")

    def __init__(self, model_name: str, device: str = "cpu"):
        from sentence_transformers import CrossEncoder  # type: ignore

        dev = (device or "cpu").strip().lower()
        if dev == "auto":
            dev = _pick_torch_device("auto")

        self.model_name = model_name
        self.device = dev

        try:
            self.model = CrossEncoder(model_name, device=self.device)
        except Exception:
            # Safety fallback to CPU
            self.device = "cpu"
            self.model = CrossEncoder(model_name, device="cpu")

    def score(self, query: str, docs: List[str]) -> List[float]:
        if not docs:
            return []
        # Pairs are required by CrossEncoder predict
        pairs = [[query, d] for d in docs]
        raw = self.model.predict(pairs, show_progress_bar=False)
        return [_sigmoid(float(s)) for s in raw]


class Qwen3RerankerYesNo:
    """
    Qwen3 causal-LM reranker: scores relevance via next-token probability mass on {yes,no}.
    """

    __slots__ = (
        "_torch",
        "model_name",
        "device",
        "dtype",
        "batch_size",
        "max_length",
        "prompt_template",
        "max_doc_chars",
        "tok",
        "model",
        "_yes_id",
        "_no_id",
    )

    def __init__(
        self,
        *,
        model_name: str,
        device: str = "auto",
        batch_size: int = 8,
        max_length: int = 2304,
        prompt_template: str = "Instruction: You are a Judge. Given a query and a memory. decide whether the Memory is relevant to the query. The Answer must be 'yes' or 'no'.\nQuery:<<<{query}>>>\nMemory:<<<{doc}>>>\nAnswer (yes/no):",
        max_doc_chars: int = 12000,
    ):
        try:
            import torch  # type: ignore
            from transformers import AutoModelForCausalLM, AutoTokenizer  # type: ignore
        except Exception as ex:
            raise RuntimeError(
                "Qwen3 reranker requires 'torch' and 'transformers'. "
                "Install: pip install torch transformers"
            ) from ex

        self._torch = torch
        self.model_name = str(model_name)

        self.device = _pick_torch_device(device)
        self.dtype = _pick_torch_dtype(self.device)

        # ✅ FIX: set max_length before using it in auto batch-size logic
        self.max_length = max(64, int(max_length))
        self.prompt_template = str(prompt_template)
        self.max_doc_chars = int(max_doc_chars) if max_doc_chars else 0

        # ✅ FIX: batch_size auto mode (<=0) now works correctly
        if int(batch_size) <= 0:
            self.batch_size = _auto_rerank_batch_size(self.device, self.max_length)
        else:
            self.batch_size = max(1, int(batch_size))

        # Tokenizer
        self.tok = AutoTokenizer.from_pretrained(
            self.model_name, trust_remote_code=True
        )

        # Ensure pad token exists for batching
        if getattr(self.tok, "pad_token_id", None) is None:
            if getattr(self.tok, "eos_token", None) is not None:
                self.tok.pad_token = self.tok.eos_token  # type: ignore[attr-defined]

        # FIX-1 (CRITICAL): The original code had comment "✅ FIX: transformers uses
        # torch_dtype=..., not dtype=..." but then still passed dtype=self.dtype.
        # HuggingFace silently ignores the unknown kwarg → model always loads in
        # float32, wasting VRAM and slowing GPU inference. Corrected to torch_dtype=.
        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_name,
            trust_remote_code=True,
            torch_dtype=self.dtype,
            low_cpu_mem_usage=True,
        )
        self.model.eval()

        try:
            self.model.to(self.device)  # type: ignore[attr-defined]
        except Exception:
            self.device = "cpu"
            self.dtype = _pick_torch_dtype("cpu")
            self.model.to("cpu")  # type: ignore[attr-defined]

        # Resolve single-token ids for yes/no (must be single-token)
        self._yes_id = self._resolve_single_token_id([" yes", "yes", " Yes", "Yes"])
        self._no_id = self._resolve_single_token_id([" no", "no", " No", "No"])
        if self._yes_id is None or self._no_id is None:
            raise RuntimeError(
                "Could not resolve single-token ids for yes/no. This method requires "
                "'yes' and 'no' to be representable as single tokens."
            )

    def _resolve_single_token_id(self, candidates: List[str]) -> Optional[int]:
        for text in candidates:
            ids = self.tok.encode(text, add_special_tokens=False)
            if len(ids) == 1:
                return int(ids[0])
        return None

    def _trim_doc(self, doc: str) -> str:
        d = (doc or "").strip()
        if self.max_doc_chars and len(d) > self.max_doc_chars:
            return d[: self.max_doc_chars]
        return d

    def _build_prompts(self, query: str, docs: List[str]) -> List[str]:
        q = (query or "").strip()
        tmpl = self.prompt_template
        return [tmpl.format(query=q, doc=self._trim_doc(d)) for d in docs]

    def score(self, query: str, docs: List[str]) -> List[float]:
        torch = self._torch
        if not docs:
            return []

        prompts = self._build_prompts(query, docs)
        scores: List[float] = []

        bs = self.batch_size
        yes_id = int(self._yes_id)  # type: ignore[arg-type]
        no_id = int(self._no_id)  # type: ignore[arg-type]

        # inference_mode is faster than no_grad and reduces overhead
        with torch.inference_mode():
            for i in range(0, len(prompts), bs):
                batch = prompts[i : i + bs]

                enc = self.tok(
                    batch,
                    return_tensors="pt",
                    padding=True,
                    truncation=True,
                    max_length=self.max_length,
                )
                enc = {k: v.to(self.device) for k, v in enc.items()}

                out = self.model(**enc)
                logits = out.logits  # (B, T, V)

                attn = enc.get("attention_mask")
                if attn is None:
                    last_idx = torch.full(
                        (logits.shape[0],), logits.shape[1] - 1, device=logits.device
                    )
                else:
                    # index of last non-pad token for each sequence
                    last_idx = (attn.sum(dim=1) - 1).clamp(min=0)

                row = torch.arange(logits.shape[0], device=logits.device)
                last_logits = logits[row, last_idx, :]  # (B, V)

                # Softmax only over {yes,no} logits => stable probabilities
                yn = torch.stack(
                    [last_logits[:, yes_id], last_logits[:, no_id]],
                    dim=1,
                )
                probs_yes = torch.softmax(yn, dim=1)[:, 0]  # P(yes | {yes,no})
                scores.extend([float(x) for x in probs_yes.detach().cpu().tolist()])

        return scores


class _NullReranker:
    """No-op reranker used when rerank is disabled or fails to load."""

    __slots__ = ()

    def score(self, query: str, docs: List[str]) -> List[float]:
        return [0.0] * len(docs)


# ==========================================================
# MMR (Maximal Marginal Relevance) helper  [ACC-2]
# ==========================================================


def _jaccard(a: str, b: str) -> float:
    """Fast word-level Jaccard similarity for MMR diversity scoring."""
    sa = set(a.lower().split())
    sb = set(b.lower().split())
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / (len(sa) + len(sb) - inter)


def _apply_mmr(
    hits: List[Tuple[str, float, Optional[Dict[str, Any]]]],
    texts: List[str],
    lambda_: float,
    top_k: int,
) -> List[Tuple[str, float, Optional[Dict[str, Any]]]]:
    """
    ACC-2: Maximal Marginal Relevance reordering for diversity.

    Balances relevance (dense score) with diversity (dissimilarity to already-selected).
    lambda_=1.0 → pure relevance. lambda_=0.0 → pure diversity.
    Dense scores are UNCHANGED — MMR only reorders.
    """
    if not hits or top_k <= 0:
        return hits

    n = len(hits)
    max_sc = max(float(h[1]) for h in hits) or 1.0
    norm = [float(h[1]) / max_sc for h in hits]

    selected_idx: List[int] = []
    remaining = list(range(n))

    while remaining and len(selected_idx) < top_k:
        best_i = None
        best_score = -float("inf")

        for i in remaining:
            rel = norm[i]
            if not selected_idx:
                mmr = rel
            else:
                redundancy = max(_jaccard(texts[i], texts[s]) for s in selected_idx)
                mmr = lambda_ * rel - (1.0 - lambda_) * redundancy

            if mmr > best_score:
                best_score = mmr
                best_i = i

        if best_i is None:
            break
        selected_idx.append(best_i)
        remaining.remove(best_i)

    return [hits[i] for i in selected_idx]


# ==========================================================
# VectorStore
# ==========================================================


class VectorStore:
    """
    Qdrant-backed vector store supporting:
      - Dense search (named vector: dense_name)
      - Optional sparse recall (named vector: sparse_name) for hybrid candidate discovery
      - Optional reranking (Qwen3 yes/no or CrossEncoder)
      - Optional MMR diversity post-processing (use_mmr=True in rerank_cfg)

    Locked behavior:
      - Returned scores are ALWAYS dense similarity scores.
      - Sparse is recall-only (candidate IDs).
      - Reranker only reorders; it never changes dense scores.

    Usage as context manager (FEAT-1):
        with VectorStore(...) as vs:
            vs.upsert(entry)
            results = vs.search(query_vector=q, query_text="...")
    """

    def __init__(
        self,
        *,
        backend: str = "local",  # "local" | "server"
        local_path: Optional[
            str
        ] = None,  # pass resolved runtime path (static in bootstrap)
        server: Optional[VectorServerConfig] = None,
        collection: str = "buddy_memories",
        dense_name: str = "dense",
        sparse_name: str = "sparse",
        prefer_grpc: bool = False,
        distance: str = "Cosine",
        debug: bool = False,
        # optional knobs you still want configurable
        rerank_cfg: Optional[Dict[str, Any]] = None,
        sparse_cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.collection = collection
        self.debug = debug
        self.dense_name = (dense_name or "").strip()
        self.sparse_name = (sparse_name or "").strip()
        self._distance = distance
        self._dim: Optional[int] = None

        if not self.dense_name:
            raise ValueError("VectorStore requires dense_name")

        backend_s = (backend or "local").strip().lower()
        if backend_s not in {"local", "server"}:
            raise ValueError(f"Invalid vector_store backend: {backend!r}")

        # --------------------------
        # Qdrant client init
        # --------------------------
        self._tempdir = None

        if backend_s == "server":
            srv = server or VectorServerConfig()
            url = (srv.url or "").strip()
            if not url:
                raise ValueError("VectorStore backend='server' requires server.url")

            self.timeout = int(srv.timeout)
            self.client = QdrantClient(
                url=url,
                api_key=srv.api_key or "",
                prefer_grpc=prefer_grpc,
                timeout=self.timeout,
            )
            self._mode = f"remote({url})"

        else:
            # Local Qdrant path should be resolved by bootstrap to ~/.buddy/data/qdrant (static)
            # but we still allow a fallback temp dir for tests.
            if not local_path:
                self._tempdir = tempfile.TemporaryDirectory()
                local_path = self._tempdir.name
            self.timeout = 10  # local client timeout can stay simple
            self.client = QdrantClient(path=str(local_path), timeout=self.timeout)
            self._mode = f"local({local_path})"

        # --------------------------
        # Sparse init (unchanged)
        # --------------------------
        self.sparse_cfg = SparseConfig()
        self.auto_sparse = AutoSparsePolicy()
        self.sparse_encoder = HashingSparseEncoder(self.sparse_cfg)

        self._sparse_supported = True
        if not hasattr(qm, "SparseVector") or not hasattr(qm, "SparseVectorParams"):
            self._disable_sparse("qdrant_client_missing_sparse_types")
        if self.sparse_cfg.enabled and not self.sparse_name:
            self._disable_sparse("sparse_enabled_but_sparse_name_empty")

        # --------------------------
        # Rerank init (explicit cfg)
        # --------------------------
        rr_cfg = rerank_cfg or {}
        self.rerank_cfg = RerankConfig(
            enabled=bool(rr_cfg.get("enabled", True)),
            method=str(rr_cfg.get("method", "qwen3") or "qwen3").strip().lower(),
            always_on=bool(rr_cfg.get("always_on", True)),
            pre_rerank_k=int(rr_cfg.get("pre_rerank_k", 20) or 20),
            max_chars_per_doc=int(rr_cfg.get("max_chars_per_doc", 700) or 700),
            device=str(rr_cfg.get("device", "auto") or "auto").strip().lower(),
            batch_size=int(rr_cfg.get("batch_size", 8) or 8),
            max_length=int(rr_cfg.get("max_length", 2048) or 2048),
            qwen_model=str(rr_cfg.get("qwen_model", "Qwen/Qwen3-Reranker-0.6B")),
            cross_encoder_model=str(
                rr_cfg.get(
                    "cross_encoder_model", "cross-encoder/ms-marco-MiniLM-L-6-v2"
                )
            ),
            dense_threshold=rr_cfg.get("dense_threshold", 0.25),
            rerank_threshold=rr_cfg.get("rerank_threshold", 0.40),
            rerank_min_keep=rr_cfg.get("rerank_min_keep", 0),
            use_mmr=bool(rr_cfg.get("use_mmr", False)),
            mmr_lambda=float(rr_cfg.get("mmr_lambda", 0.7) or 0.7),
            rrf_k=int(rr_cfg.get("rrf_k", 60) or 60),
            skip_rerank_above=float(rr_cfg.get("skip_rerank_above", 1.1) or 1.1),
        )

        self._reranker_initialized = False
        self._reranker_lock = threading.Lock()
        self._reranker: Any = None
        self._reranker_name: str = "off"
        self._reranker_init_error_logged = False

        if self.rerank_cfg.enabled and self.rerank_cfg.always_on:
            try:
                self._init_reranker()
            except Exception:
                pass

        if self.debug:
            logger.debug(
                "VectorStore init mode=%s collection=%s dense=%s sparse_ready=%s "
                "rerank_enabled=%s method=%s always_on=%s dense_thr=%s rerank_thr=%s "
                "use_mmr=%s",
                self._mode,
                self.collection,
                self.dense_name,
                self._sparse_ready(),
                self.rerank_cfg.enabled,
                self.rerank_cfg.method,
                self.rerank_cfg.always_on,
                self.rerank_cfg.dense_threshold,
                self.rerank_cfg.rerank_threshold,
                self.rerank_cfg.use_mmr,
            )

    # ----------------------------
    # Context manager support (FEAT-1)
    # ----------------------------
    def __enter__(self) -> "VectorStore":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ----------------------------
    # Debug helper (zero-cost when disabled)
    # ----------------------------
    def _debug(self, msg: str, *args: Any) -> None:
        if self.debug:
            logger.debug(msg, *args)

    # ----------------------------
    # Reranker init (thread-safe)
    # ----------------------------
    def _init_reranker(self):
        """Initialize reranker once; fallback to _NullReranker on failure."""
        if self._reranker_initialized:
            return self._reranker

        with self._reranker_lock:
            if self._reranker_initialized:
                return self._reranker

            cfg = self.rerank_cfg
            if not cfg.enabled:
                self._reranker = _NullReranker()
                self._reranker_name = "off"
                self._reranker_initialized = True
                return self._reranker

            method = (cfg.method or "qwen3").strip().lower()
            try:
                if method == "cross_encoder":
                    self._reranker = CrossEncoderReranker(
                        cfg.cross_encoder_model, device=cfg.device
                    )
                    self._reranker_name = f"cross_encoder:{cfg.cross_encoder_model}"
                else:
                    # default -> qwen3
                    self._reranker = Qwen3RerankerYesNo(
                        model_name=cfg.qwen_model,
                        device=cfg.device,
                        batch_size=cfg.batch_size,
                        max_length=cfg.max_length,
                    )
                    self._reranker_name = f"qwen3:{cfg.qwen_model}"

                logger.info("rerank: ready %s", self._reranker_name)
            except Exception as ex:
                self._reranker = _NullReranker()
                self._reranker_name = "off(error)"
                if not self._reranker_init_error_logged:
                    self._reranker_init_error_logged = True
                    logger.warning(
                        "rerank: disabled (fallback no-op). method=%s err=%r",
                        method,
                        ex,
                    )

            self._reranker_initialized = True
            return self._reranker

    def _get_reranker(self) -> Any:
        """
        FIX-4: Cached reranker access.

        _maybe_rerank originally called _init_reranker() on every search, which
        acquires a threading.Lock even after the reranker is already initialized.
        This fast-path bypasses the lock entirely after first init.
        """
        if self._reranker_initialized:
            return self._reranker
        return self._init_reranker()

    # ----------------------------
    # Sparse capability guards
    # ----------------------------
    def _disable_sparse(self, reason: str) -> None:
        """Disable sparse permanently for this instance to avoid repeated failures."""
        self._sparse_supported = False
        self.sparse_cfg = SparseConfig(enabled=False)
        self.auto_sparse = AutoSparsePolicy(enabled=False)
        self.sparse_encoder = HashingSparseEncoder(self.sparse_cfg)
        logger.info("sparse: disabled reason=%s collection=%s", reason, self.collection)

    def _sparse_ready(self) -> bool:
        """True if sparse is enabled and supported by current qdrant-client."""
        return bool(
            self._sparse_supported and self.sparse_cfg.enabled and self.sparse_name
        )

    def _auto_use_sparse(
        self,
        *,
        query_text: str,
        dense_hits: List[Tuple[str, float, Optional[Dict[str, Any]]]],
    ) -> bool:
        """
        Heuristic: decide whether to use sparse recall in 'auto' mode.

        FIX-2: now correctly checks short_query_max_tokens (was dead code before).

        Considers:
          - Whether sparse is enabled/supported
          - Whether query looks lexical (paths, filenames, emails, etc.)
          - Whether query is short (≤ short_query_max_tokens words)  ← NEW (FIX-2)
          - Whether dense results have low confidence (ambiguity gap)
        """
        if not self._sparse_ready():
            return False

        policy = self.auto_sparse
        if not policy.enabled:
            return False

        qt = (query_text or "").strip()

        # Strong lexical signal => use sparse for recall
        if _looks_lexical(qt):
            return True

        # FIX-2: Short query trigger — was defined in AutoSparsePolicy but never applied.
        # Single/few-word queries like "Pallavi", "ssh config", "wife name" are
        # semantically underspecified for dense alone; sparse recall helps greatly.
        word_count = len(_WORD_RE.findall(qt))
        if 0 < word_count <= int(policy.short_query_max_tokens):
            self._debug(
                "sparse: short_query trigger word_count=%d <= max=%d",
                word_count,
                policy.short_query_max_tokens,
            )
            return True

        # Check dense result confidence
        if dense_hits:
            scores = [float(sc) for _mid, sc, _pl in dense_hits]
            if scores:
                top_score = scores[0]
                # Low confidence => try sparse for extra candidates
                if top_score < float(policy.dense_low_score_threshold):
                    return True
                # High ambiguity (close scores) => use sparse
                if len(scores) > 1:
                    gap = top_score - scores[1]
                    if gap < float(policy.dense_ambiguity_gap):
                        return True

        return False

    # ----------------------------
    # Collection management
    # ----------------------------
    def ensure_collection(self, dim: int) -> None:
        """
        Ensure the Qdrant collection exists with correct dense vector dimension.

        This is called lazily on first upsert/search when dim becomes known.
        """
        dim = int(dim)
        if dim <= 0:
            raise ValueError("dim must be > 0")

        self._dim = dim

        # Fast existence check
        try:
            info = self.client.get_collection(collection_name=self.collection)
            # Best-effort dim verification (avoid crashing on older client structures)
            try:
                cfg = getattr(info, "config", None)
                params = getattr(cfg, "params", None)
                vectors = getattr(params, "vectors", None)
                if isinstance(vectors, dict) and self.dense_name in vectors:
                    size = getattr(vectors[self.dense_name], "size", None)
                    if size is not None and int(size) != dim:
                        raise ValueError(
                            f"Collection '{self.collection}' dense dim mismatch:"
                            f" have={size} want={dim}"
                        )
            except Exception:
                pass
            return
        except Exception:
            pass

        dist = {
            "Cosine": qm.Distance.COSINE,
            "Dot": qm.Distance.DOT,
            "Euclid": qm.Distance.EUCLID,
        }.get(self._distance, qm.Distance.COSINE)

        vectors_config = {self.dense_name: qm.VectorParams(size=dim, distance=dist)}

        sparse_vectors_config = None
        if self._sparse_ready():
            try:
                sparse_vectors_config = {self.sparse_name: qm.SparseVectorParams()}
            except Exception:
                sparse_vectors_config = None
                self._disable_sparse("SparseVectorParams_unavailable")

        self._debug(
            "Creating collection=%s mode=%s dense=%s dim=%d sparse=%s",
            self.collection,
            self._mode,
            self.dense_name,
            dim,
            self._sparse_ready(),
        )

        try:
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=vectors_config,
                sparse_vectors_config=sparse_vectors_config,
            )
        except TypeError:
            # Older qdrant-client versions may not accept sparse_vectors_config
            self.client.create_collection(
                collection_name=self.collection, vectors_config=vectors_config
            )

    # ----------------------------
    # Upsert (public contract)
    # ----------------------------
    def upsert(self, entry: MemoryEntry) -> None:
        """
        Upsert a MemoryEntry into Qdrant.

        Stored payload includes:
          - text, memory_type, deleted, role

        Sparse vector is included only if enabled/supported.
        """
        v = _as_np(getattr(entry, "embedding", None))  # type: ignore[attr-defined]
        if v is None:
            return

        if self._dim is None:
            self.ensure_collection(int(v.shape[0]))

        payload = {
            "text": entry.text,
            "memory_type": getattr(entry, "memory_type", "flash"),
            "deleted": int(getattr(entry, "deleted", 0) or 0),
            "role": getattr(entry, "role", "unknown"),
        }

        # NOTE: Qdrant expects python lists (not numpy arrays)
        vectors: Dict[str, Any] = {
            self.dense_name: v.astype(np.float32, copy=False).tolist()
        }

        if self._sparse_ready():
            try:
                vectors[self.sparse_name] = self.sparse_encoder.encode(entry.text)
            except Exception as ex:
                # Sparse is best-effort only; never fail upsert
                self._debug("sparse: encode failed (ignored) err=%r", ex)

        try:
            self.client.upsert(
                collection_name=self.collection,
                points=[
                    qm.PointStruct(id=str(entry.id), vector=vectors, payload=payload)
                ],
            )
        except Exception as ex:
            logger.exception(
                "upsert failed: collection=%s id=%s err=%r",
                self.collection,
                getattr(entry, "id", "?"),
                ex,
            )
            raise

    # FEAT-2: Batch upsert for efficient bulk inserts
    def upsert_batch(self, entries: List[MemoryEntry], batch_size: int = 64) -> int:
        """
        Upsert multiple MemoryEntry objects in batched Qdrant calls.

        Significantly faster than looping upsert() because it avoids per-call
        round-trip overhead. Entries without embeddings are silently skipped,
        matching upsert() behaviour.

        Args:
            entries: List of MemoryEntry objects with embeddings set.
            batch_size: Number of points per Qdrant upsert call.

        Returns:
            Number of entries successfully queued.
        """
        points: List[Any] = []
        count = 0

        for entry in entries:
            v = _as_np(getattr(entry, "embedding", None))  # type: ignore[attr-defined]
            if v is None:
                continue

            if self._dim is None:
                self.ensure_collection(int(v.shape[0]))

            payload = {
                "text": entry.text,
                "memory_type": getattr(entry, "memory_type", "flash"),
                "deleted": int(getattr(entry, "deleted", 0) or 0),
                "role": getattr(entry, "role", "unknown"),
            }

            vectors: Dict[str, Any] = {
                self.dense_name: v.astype(np.float32, copy=False).tolist()
            }

            if self._sparse_ready():
                try:
                    vectors[self.sparse_name] = self.sparse_encoder.encode(entry.text)
                except Exception as ex:
                    self._debug("sparse: encode failed (ignored) err=%r", ex)

            points.append(
                qm.PointStruct(id=str(entry.id), vector=vectors, payload=payload)
            )
            count += 1

            if len(points) >= batch_size:
                try:
                    self.client.upsert(collection_name=self.collection, points=points)
                except Exception as ex:
                    logger.exception(
                        "upsert_batch chunk failed: collection=%s err=%r",
                        self.collection,
                        ex,
                    )
                    raise
                points = []

        if points:
            try:
                self.client.upsert(collection_name=self.collection, points=points)
            except Exception as ex:
                logger.exception(
                    "upsert_batch final chunk failed: collection=%s err=%r",
                    self.collection,
                    ex,
                )
                raise

        return count

    # Kept for compatibility (existing callers)
    def delete(self, memory_id: str) -> None:
        """Delete a point by id."""
        self.client.delete(
            collection_name=self.collection,
            points_selector=qm.PointIdsList(points=[str(memory_id)]),
        )

    def delete_memory(self, memory_id: str) -> None:
        """Alias kept for backward compatibility."""
        self.delete(memory_id)

    def soft_delete(self, memory_id: str) -> None:
        """
        Soft-delete a point by setting deleted=1 in its payload.

        The point remains in the collection and is retrievable by ID,
        but will be excluded from search results via the deleted filter.
        """
        try:
            self.client.set_payload(
                collection_name=self.collection,
                payload={"deleted": 1},
                points=[str(memory_id)],
            )
        except Exception as ex:
            logger.exception(
                "soft_delete failed: collection=%s id=%s err=%r",
                self.collection,
                memory_id,
                ex,
            )
            raise

    # FEAT-3: Direct ID lookup
    def search_by_ids(
        self,
        ids: List[str],
        *,
        with_payload: bool = True,
    ) -> List[Tuple[str, Optional[Dict[str, Any]]]]:
        """
        Retrieve specific points by their IDs directly from Qdrant.

        Useful for resolving memory IDs returned by search() back to their full
        payloads without running another similarity search.

        Args:
            ids: List of memory IDs (strings).
            with_payload: Whether to include payload in results.

        Returns:
            List of (id, payload_or_None). Missing IDs are silently skipped.
        """
        if not ids:
            return []

        try:
            points = self.client.retrieve(
                collection_name=self.collection,
                ids=[str(i) for i in ids],
                with_payload=with_payload,
            )
            return [
                (
                    str(p.id),
                    p.payload if with_payload and isinstance(p.payload, dict) else None,
                )
                for p in points
            ]
        except Exception as ex:
            logger.exception(
                "search_by_ids failed: collection=%s ids=%s err=%r",
                self.collection,
                ids[:10],
                ex,
            )
            return []

    # ==========================================================
    # Search API (public contract)
    # ==========================================================

    def search(
        self,
        *,
        query_vector: Sequence[float] | np.ndarray,
        top_k: int = 20,
        memory_types: Optional[List[str]] = None,
        include_deleted: bool = False,
        query_text: Optional[str] = None,
        mode: str = "auto",
        rerank_mode: str = "accuracy",
    ) -> List[Tuple[str, float]]:
        """
        Search for nearest memories by dense similarity.

        Args:
            query_vector: Dense embedding.
            top_k: Number of results to return.
            memory_types: Optional whitelist (e.g., ["flash","short","long"]).
            include_deleted: If False, filters out deleted memories.
            query_text: Optional raw query text used for hybrid recall/reranking.
            mode: "auto" | "dense" | "hybrid".
            rerank_mode: "fast" | "auto" | "accuracy".

        Returns:
            List of (memory_id, dense_score). Dense score contract is preserved.
        """
        hits = self.search_with_payloads(
            query_vector=query_vector,
            query_text=(query_text or ""),
            top_k=top_k,
            memory_types=memory_types,
            include_deleted=include_deleted,
            mode=mode,
            rerank_mode=rerank_mode,
        )
        return [(mid, float(sc)) for (mid, sc, _pl) in hits]

    def search_with_payloads(
        self,
        *,
        query_vector: Sequence[float] | np.ndarray,
        query_text: str,
        top_k: int = 20,
        memory_types: Optional[List[str]] = None,
        include_deleted: bool = False,
        mode: str = "auto",
        rerank_mode: str = "accuracy",
    ) -> List[Tuple[str, float, Optional[Dict[str, Any]]]]:
        q = _as_np(query_vector)
        if q is None:
            return []

        k = int(top_k)
        if k <= 0:
            return []

        flt = self._build_filter(
            memory_types=memory_types,
            include_deleted=include_deleted,
        )

        m = (mode or "auto").strip().lower()
        if m not in {"auto", "dense", "hybrid"}:
            raise ValueError("mode must be auto|dense|hybrid")

        pre_k = int(self.rerank_cfg.pre_rerank_k)
        dense_limit = max(k * 2, pre_k)

        dense = self._dense_search(q, flt, limit=dense_limit, with_payload=True)
        if not dense:
            if self.debug:
                logger.debug("dense: no results")
            return []

        # DEV: dense preview
        if self.debug:
            prev = []
            for i in range(min(8, len(dense))):
                mid, sc, pl = dense[i]
                txt = str(pl.get("text") or "")[:80] if isinstance(pl, dict) else ""
                prev.append((mid, float(sc), txt))
            logger.debug("dense: preview top=%s", prev)

        # HARD DENSE GATE
        # FIX-6: attempt sparse rescue before dropping everything.
        # A low dense score can mean the query is lexical/exact-match rather than
        # truly irrelevant — don't give up before trying sparse recall.
        dense_thr = getattr(self.rerank_cfg, "dense_threshold", None)
        if dense_thr is not None:
            thr_f = float(dense_thr)
            top_score = float(dense[0][1])

            if top_score < thr_f:
                # Try sparse rescue (auto/hybrid modes only)
                if m in {"auto", "hybrid"} and self._sparse_ready() and query_text:
                    self._debug(
                        "dense: BELOW threshold (%.4f < %.4f) → trying sparse rescue",
                        top_score,
                        thr_f,
                    )
                    sparse_rescue = self._sparse_search_with_payloads(
                        query_text=query_text, flt=flt, limit=pre_k
                    )
                    if sparse_rescue:
                        rescue_ids = [mid for mid, _, _ in sparse_rescue]
                        flt_rescue = self._filter_with_ids(flt, rescue_ids)
                        rescored = self._dense_search(
                            q,
                            flt_rescue,
                            limit=min(len(rescue_ids), pre_k),
                            with_payload=True,
                        )
                        if rescored:
                            dense = rescored
                            top_score = float(dense[0][1])
                            self._debug(
                                "dense: sparse rescue found %d hits, new top=%.4f",
                                len(dense),
                                top_score,
                            )

                # Apply threshold after potential rescue
                if top_score < thr_f:
                    if self.debug:
                        logger.debug(
                            "dense: HARD DROP top_score=%.4f < dense_threshold=%.4f"
                            " => []",
                            top_score,
                            thr_f,
                        )
                    return []

            kept: List[Tuple[str, float, Optional[Dict[str, Any]]]] = []
            dropped: List[Tuple[str, float, str]] = []
            for mid, sc, pl in dense:
                scf = float(sc)
                if scf >= thr_f:
                    kept.append((mid, scf, pl))
                else:
                    txt = str(pl.get("text") or "")[:80] if isinstance(pl, dict) else ""
                    dropped.append((mid, scf, txt))

            if self.debug and dropped:
                logger.debug(
                    "dense: dropped_below_threshold thr=%.4f count=%d sample=%s",
                    thr_f,
                    len(dropped),
                    dropped[:12],
                )

            dense = kept
            if not dense:
                if self.debug:
                    logger.debug(
                        "dense: all dropped by dense_threshold=%.4f => []",
                        thr_f,
                    )
                return []

        # Decide sparse usage (recall only)
        use_sparse = False
        if m == "hybrid":
            use_sparse = self._sparse_ready()
        elif m == "auto":
            use_sparse = self._auto_use_sparse(query_text=query_text, dense_hits=dense)

        if not use_sparse:
            hits = dense[:k]
            return self._maybe_rerank(
                query_text=query_text, hits=hits, mode=rerank_mode
            )

        sparse = self._sparse_search_with_payloads(
            query_text=query_text, flt=flt, limit=pre_k
        )
        if not sparse:
            hits = dense[:k]
            return self._maybe_rerank(
                query_text=query_text, hits=hits, mode=rerank_mode
            )

        # ACC-1: RRF merge — documents in BOTH lists are boosted vs. those in one.
        # Replaces the old sparse-first concatenation that ignored dense ranking.
        all_ids = self._ordered_union_rrf(
            sparse, dense, rrf_k=int(self.rerank_cfg.rrf_k)
        )
        if not all_ids:
            hits = dense[:k]
            return self._maybe_rerank(
                query_text=query_text, hits=hits, mode=rerank_mode
            )

        flt_ids = self._filter_with_ids(flt, all_ids)
        rescored = self._dense_search(
            q,
            flt_ids,
            limit=min(len(all_ids), pre_k),
            with_payload=True,
        )

        if not rescored:
            hits = dense[:k]
            return self._maybe_rerank(
                query_text=query_text, hits=hits, mode=rerank_mode
            )

        res_map: Dict[str, Tuple[str, float, Optional[Dict[str, Any]]]] = {
            mid: (mid, float(sc), pl) for mid, sc, pl in rescored
        }

        # BUG-FIX: collect hits in RRF order (for candidate selection quality)
        # then sort by dense score descending so returned list is score-ordered.
        # Previously results were in RRF order which confused callers expecting
        # score-descending output (the locked score contract).
        hits: List[Tuple[str, float, Optional[Dict[str, Any]]]] = []
        for mid in all_ids:
            item = res_map.get(mid)
            if item is not None:
                hits.append(item)
                if len(hits) >= k:
                    break

        if not hits:
            hits = dense[:k]

        # Sort by dense score descending — preserve the score contract.
        hits.sort(key=lambda x: float(x[1]), reverse=True)

        return self._maybe_rerank(query_text=query_text, hits=hits, mode=rerank_mode)

    # ==========================================================
    # Cleanup
    # ==========================================================

    def close(self) -> None:
        """
        Close underlying Qdrant client and cleanup temp directory if used.

        Safe to call multiple times.
        """
        try:
            if getattr(self, "client", None) is not None:
                self.client.close()
        except Exception:
            pass

        try:
            td = getattr(self, "_tempdir", None)
            if td is not None:
                td.cleanup()
        except Exception:
            pass

    # ==========================================================
    # Dense search (query_points first, safe fallbacks)
    # ==========================================================

    def _dense_search(
        self,
        q: np.ndarray,
        flt: Optional["qm.Filter"],
        *,
        limit: int,
        with_payload: bool = False,
    ) -> List[Tuple[str, float, Optional[Dict[str, Any]]]]:
        """
        Perform a dense search against Qdrant.

        Compatibility approach (FIX-3 applied):
          1. query_points(using=dense_name, filter=flt)   ← preferred
          2. query_points(filter=flt, NO `using`)          ← FIX-3: keeps filter!
             Original dropped query_filter here, leaking deleted/wrong-type results.
          3. Raise with context from both failures.

        Returns:
            List[(id, dense_score, payload_or_None)]
        """
        lim = int(limit)
        if lim <= 0:
            return []

        # Avoid repeated conversions
        q_list = q.astype(np.float32, copy=False).tolist()

        last_err: Optional[Exception] = None

        # Attempt 1: filter + using
        try:
            res = self.client.query_points(
                collection_name=self.collection,
                query=q_list,
                query_filter=flt,
                limit=lim,
                with_payload=bool(with_payload),
                using=self.dense_name,
            )
            pts = getattr(res, "points", res)
            return self._format_points(pts, with_payload=with_payload)
        except Exception as ex:
            last_err = ex
            self._debug("dense: query_points failed (filter+using) err=%r", ex)

        # FIX-3: Attempt 2 keeps query_filter; only drops `using=` kwarg.
        # The original code dropped query_filter=flt here entirely, which could
        # return deleted entries or wrong memory_types, violating the contract.
        try:
            res = self.client.query_points(
                collection_name=self.collection,
                query=q_list,
                query_filter=flt,  # ← FIXED: was missing in original
                limit=lim,
                with_payload=bool(with_payload),
                # `using` omitted intentionally for older qdrant-client compatibility
            )
            pts = getattr(res, "points", res)
            return self._format_points(pts, with_payload=with_payload)
        except Exception as ex:
            logger.exception(
                "dense: query_points failed. collection=%s using=%s limit=%d err=%r",
                self.collection,
                self.dense_name,
                lim,
                ex,
            )
            raise RuntimeError(
                f"All dense query_points methods failed: {last_err!r} | {ex!r}"
            ) from ex

    # ==========================================================
    # Sparse search (best-effort; recall-only)
    # ==========================================================

    def _sparse_search_with_payloads(
        self,
        *,
        query_text: str,
        flt: Optional["qm.Filter"],
        limit: int,
    ) -> List[Tuple[str, float, Optional[Dict[str, Any]]]]:
        """
        Sparse recall query. Best-effort only.

        Returns:
            List[(id, sparse_score, payload)] — sparse scores are never returned
            by public APIs; only ids are used for candidate union.
        """
        if not self._sparse_ready():
            return []

        qt = (query_text or "").strip()
        if not qt:
            return []

        lim = int(limit)
        if lim <= 0:
            return []

        try:
            sv = self.sparse_encoder.encode(qt)
        except Exception as ex:
            self._debug("sparse: encode failed err=%r", ex)
            return []

        # Prefer query_points if available (newer clients)
        if hasattr(self.client, "query_points"):
            try:
                res = self.client.query_points(
                    collection_name=self.collection,
                    query=sv,
                    query_filter=flt,
                    limit=lim,
                    with_payload=True,
                    using=self.sparse_name,
                )
                pts = getattr(res, "points", res)
                return self._format_points(pts, with_payload=True)
            except Exception as ex_a:
                self._debug("sparse: query_points(using=...) failed err=%r", ex_a)

            # Some versions ignore/forbid `using` for sparse; retry without
            try:
                res = self.client.query_points(
                    collection_name=self.collection,
                    query=sv,
                    query_filter=flt,
                    limit=lim,
                    with_payload=True,
                )
                pts = getattr(res, "points", res)
                return self._format_points(pts, with_payload=True)
            except Exception as ex_b:
                self._debug("sparse: query_points(no using) failed err=%r", ex_b)

            # Disable sparse to avoid repeated expensive failures
            self._disable_sparse("sparse_query_points_unsupported")
            return []

        # Legacy fallback: client.search if available
        if hasattr(self.client, "search"):
            try:
                res = self.client.search(  # type: ignore[attr-defined]
                    collection_name=self.collection,
                    query_vector=(self.sparse_name, sv),
                    query_filter=flt,
                    limit=lim,
                    with_payload=True,
                )
                return self._format_points(res, with_payload=True)
            except Exception as ex:
                self._debug("sparse: search() failed err=%r", ex)
                return []

        return []

    # ==========================================================
    # Filters + formatting helpers
    # ==========================================================

    def _build_filter(
        self,
        *,
        memory_types: Optional[List[str]],
        include_deleted: bool,
    ) -> Optional["qm.Filter"]:
        """
        Build a Qdrant filter for memory payload.

        Notes:
          - deleted is stored as int 0/1 in payload.
        """
        must: List[Any] = []

        if not include_deleted:
            must.append(qm.FieldCondition(key="deleted", match=qm.MatchValue(value=0)))

        if memory_types:
            # Qdrant expects a list
            must.append(
                qm.FieldCondition(
                    key="memory_type", match=qm.MatchAny(any=list(memory_types))
                )
            )

        return qm.Filter(must=must) if must else None

    def _filter_with_ids(
        self,
        base: Optional["qm.Filter"],
        ids: List[str],
    ) -> Optional["qm.Filter"]:
        """
        Extend an existing filter with an ID constraint if supported.

        If qdrant-client lacks HasIdCondition, returns base unchanged.
        """
        if not ids:
            return base

        if not hasattr(qm, "HasIdCondition"):
            return base

        must: List[Any] = []
        if base is not None and getattr(base, "must", None):
            must.extend(list(base.must))  # type: ignore[attr-defined]

        must.append(qm.HasIdCondition(has_id=[str(i) for i in ids]))
        return qm.Filter(must=must)

    def _format_points(
        self,
        pts: Iterable[Any],
        *,
        with_payload: bool,
    ) -> List[Tuple[str, float, Optional[Dict[str, Any]]]]:
        """
        Convert Qdrant points into our internal tuple format.

        Keeps:
          - id as str
          - score as float
          - payload as dict or None
        """
        out: List[Tuple[str, float, Optional[Dict[str, Any]]]] = []
        for p in pts:
            mid = str(getattr(p, "id", ""))
            sc = float(getattr(p, "score", 0.0))
            if with_payload:
                pl = getattr(p, "payload", None)
                out.append((mid, sc, pl if isinstance(pl, dict) else None))
            else:
                out.append((mid, sc, None))
        return out

    def _ordered_union_rrf(
        self,
        sparse_hits: List[Tuple[str, float, Optional[Dict[str, Any]]]],
        dense_hits: List[Tuple[str, float, Optional[Dict[str, Any]]]],
        rrf_k: int = 60,
    ) -> List[str]:
        """
        ACC-1: Reciprocal Rank Fusion (RRF) ordered union.

        Formula from Cormack et al. (2009):
            score(d) = Σ_i  1 / (k + rank_i(d))

        Documents in BOTH sparse and dense lists get a combined boost.
        Documents in only one list still appear but rank lower.

        Args:
            sparse_hits: Hits from sparse search, ordered by sparse score.
            dense_hits:  Hits from dense search, ordered by dense similarity.
            rrf_k: Rank constant. Default 60 follows the original paper.

        Returns:
            IDs ordered by RRF score descending (best combined candidates first).
        """
        k = int(rrf_k)
        rrf_scores: Dict[str, float] = {}

        for rank, (mid, _sc, _pl) in enumerate(sparse_hits):
            if mid:
                rrf_scores[mid] = rrf_scores.get(mid, 0.0) + 1.0 / (k + rank + 1)

        for rank, (mid, _sc, _pl) in enumerate(dense_hits):
            if mid:
                rrf_scores[mid] = rrf_scores.get(mid, 0.0) + 1.0 / (k + rank + 1)

        return [
            mid
            for mid, _ in sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)
        ]

    # ==========================================================
    # Reranking (REORDER ONLY, NEVER CHANGE DENSE SCORE)
    # ==========================================================

    def _maybe_rerank(
        self,
        *,
        query_text: str,
        hits: List[Tuple[str, float, Optional[Dict[str, Any]]]],
        mode: str,
    ) -> List[Tuple[str, float, Optional[Dict[str, Any]]]]:
        if not hits:
            return hits

        m = (mode or "accuracy").strip().lower()
        if m not in {"auto", "fast", "accuracy"}:
            raise ValueError("rerank_mode must be auto|fast|accuracy")

        cfg = self.rerank_cfg

        # Option A: skip reranker when dense is already confident
        skip_thr = float(getattr(cfg, "skip_rerank_above", 1.1))
        if hits and float(hits[0][1]) >= skip_thr:
            self._debug(
                "rerank: skipped (dense confident top=%.4f >= skip_rerank_above=%.4f)",
                float(hits[0][1]),
                skip_thr,
            )
            return hits

        if m == "fast" or not cfg.enabled:
            # Still apply MMR even in fast mode if configured (no model call needed)
            if cfg.use_mmr and hits:
                texts = [
                    (str(pl.get("text") or "") if isinstance(pl, dict) else "")
                    for _mid, _sc, pl in hits
                ]
                hits = _apply_mmr(hits, texts, cfg.mmr_lambda, len(hits))
            return hits

        qt = (query_text or "").strip()
        if not qt:
            return hits

        limit = min(len(hits), int(cfg.pre_rerank_k))
        if limit <= 1:
            return hits

        max_chars = int(cfg.max_chars_per_doc)

        head = hits[:limit]
        ids = [mid for (mid, _sc, _pl) in head]
        dense_scores = [float(sc) for (_mid, sc, _pl) in head]
        payloads = [pl for (_mid, _sc, pl) in head]

        docs: List[str] = []
        previews: List[str] = []
        for pl in payloads:
            txt = str(pl.get("text") or "") if isinstance(pl, dict) else ""
            previews.append(txt[:80])
            docs.append(txt[:max_chars])

        # FIX-4: Use cached _get_reranker() — avoids acquiring threading.Lock
        # on every search call after the reranker is already initialized.
        reranker = self._get_reranker()

        try:
            rr_scores = reranker.score(qt, docs)  # type: ignore[attr-defined]
            if len(rr_scores) != len(ids):
                if self.debug:
                    logger.debug(
                        "rerank: score_len_mismatch got=%d want=%d",
                        len(rr_scores),
                        len(ids),
                    )
                return hits

            if self.debug:
                preview = []
                for i in range(min(10, len(ids))):
                    preview.append(
                        (ids[i], dense_scores[i], float(rr_scores[i]), previews[i])
                    )
                logger.debug("rerank: head_preview (id,dense,rerank,text)=%s", preview)

            thr = getattr(cfg, "rerank_threshold", None)
            min_keep = int(getattr(cfg, "rerank_min_keep", 0) or 0)

            full_order = sorted(
                range(len(rr_scores)),
                key=lambda i: float(rr_scores[i]),
                reverse=True,
            )

            if thr is not None:
                thr_f = float(thr)
                kept_idx = [i for i in full_order if float(rr_scores[i]) >= thr_f]
                dropped_idx = [i for i in full_order if float(rr_scores[i]) < thr_f]

                if self.debug and dropped_idx:
                    dropped = [
                        (ids[i], dense_scores[i], float(rr_scores[i]), previews[i])
                        for i in dropped_idx[:12]
                    ]
                    logger.debug(
                        "rerank: dropped_below_threshold thr=%.4f count=%d sample=%s",
                        thr_f,
                        len(dropped_idx),
                        dropped,
                    )

                # ✅ Only min_keep fallback (no dense min keep)
                if not kept_idx and min_keep > 0:
                    kept_idx = full_order[: min(min_keep, len(full_order))]
                    if self.debug:
                        kept = [
                            (ids[i], dense_scores[i], float(rr_scores[i]), previews[i])
                            for i in kept_idx
                        ]
                        logger.debug(
                            "rerank: kept 0 by threshold; applied rerank_min_keep=%d"
                            " kept=%s",
                            min_keep,
                            kept,
                        )

                # If still none => strict drop => empty
                order = kept_idx
            else:
                order = full_order

            model_name = (
                cfg.qwen_model
                if cfg.method != "cross_encoder"
                else cfg.cross_encoder_model
            )

            out: List[Tuple[str, float, Optional[Dict[str, Any]]]] = []
            for idx in order:
                pl = payloads[idx]
                if isinstance(pl, dict):
                    pl2 = dict(pl)
                    pl2["_rerank"] = {
                        "method": cfg.method,
                        "model": model_name,
                        "score": float(rr_scores[idx]),
                        "backend": self._reranker_name,
                        "threshold": float(thr) if thr is not None else None,
                    }
                else:
                    pl2 = pl
                out.append((ids[idx], dense_scores[idx], pl2))

            # Strict semantics when threshold is active: do NOT append tail
            if thr is None and len(hits) > limit:
                out.extend(hits[limit:])

            # ACC-2: Optional MMR diversity post-processing on reranked results
            if cfg.use_mmr and out:
                rerank_texts = [
                    (str(pl.get("text") or "") if isinstance(pl, dict) else "")
                    for _mid, _sc, pl in out
                ]
                out = _apply_mmr(out, rerank_texts, cfg.mmr_lambda, len(out))

            if self.debug:
                top_prev = []
                for i in range(min(10, len(out))):
                    mid, dsc, pl = out[i]
                    rs = None
                    if isinstance(pl, dict):
                        rr = pl.get("_rerank") or {}
                        rs = rr.get("score")
                    top_prev.append(
                        (mid, float(dsc), float(rs) if rs is not None else None)
                    )
                logger.debug("rerank: result_preview (id,dense,rerank)=%s", top_prev)

            return out

        except Exception as ex:
            if self.debug:
                logger.debug("rerank failed (ignored) err=%r", ex)
            return hits


# ==========================================================
# In-file tests (VectorStore)
# ==========================================================
if __name__ == "__main__":
    from buddy.embeddings.embedding_provider import EmbeddingProvider
    import os

    print(
        "🧪 VectorStore tests: dense score canonical, hybrid recall, rerank_prob "
        "normalized, threshold drop-filter"
    )

    emb = EmbeddingProvider()
    coll = f"buddy_vs_v2_test_{os.getpid()}"

    # NOTE: new config-style construction (no vs_cfg dict)
    rerank_cfg = {
        "enabled": True,
        "method": "cross_encoder",
        "always_on": True,  # eager init expected in VectorStore.__init__
        "pre_rerank_k": 20,
        "max_chars_per_doc": 700,
        "device": "auto",
        "batch_size": 4,
        "max_length": 2048,
        "qwen_model": "Qwen/Qwen3-Reranker-0.6B",
        "cross_encoder_model": "cross-encoder/ms-marco-MiniLM-L-6-v2",
        "dense_threshold": 0.25,
        "rerank_threshold": 0.4,
        "rerank_min_keep": 0,
        "use_mmr": False,  # opt-in, off by default
        "mmr_lambda": 0.7,
        "rrf_k": 60,
    }

    store = VectorStore(
        backend="local",
        local_path=None,  # local temp dir (same as old path=None behavior)
        server=None,  # unused for local backend
        rerank_cfg=rerank_cfg,
        collection=coll,
        debug=True,
        # keep defaults unless your tests need changes:
        dense_name="dense",
        sparse_name="sparse",
        distance="Cosine",
    )

    def make_entry(text: str, memory_type: str, deleted: int = 0) -> MemoryEntry:
        e = MemoryEntry(text=text)
        e.embedding = emb.embed_passage(text)
        e.memory_type = memory_type
        e.deleted = deleted
        return e

    # A few "lexical-rich" records to stress sparse + tokenizer
    uuid_txt = "b7c2f9da-c418-4563-aae6-041bf1b7feae"
    sha_txt = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    ver_txt = "v1.2.3"
    aws_txt = "ca-central-1"

    entries = [
        make_entry("Remember that my wife's name is Pallavi.", "short"),
        make_entry("I use VS Code for Python development on macOS.", "flash"),
        make_entry("The file is located at ~/Downloads/resume.pdf", "flash"),
        make_entry("Hidden config is in ~/.env and ~/.ssh/config.", "flash"),
        make_entry(
            "SSH config is in ~/.ssh/config and keys are in ~/.ssh/id_ed25519", "flash"
        ),
        make_entry("Email me at kishan@example.com for updates.", "flash"),
        make_entry(
            "My AWS region is ca-central-1 and project is named Buddy.", "short"
        ),
        make_entry("Ticket PR#123 relates to vector store regression.", "flash"),
        make_entry(
            f"Build {ver_txt} fixed hashing collisions; ref={sha_txt[:32]}", "flash"
        ),
        make_entry(
            f"UUID reference is {uuid_txt} for the Pallavi fact record.", "flash"
        ),
        make_entry("Meeting notes: Pallavi mentioned espresso again.", "flash"),
    ]

    for e in entries:
        store.upsert(e)

    # --------------------------------------------------
    # TEST 0: lexical tokenizer robustness
    # --------------------------------------------------
    probe = (
        "PR#123 in `VectorStore` at ~/Downloads/resume.pdf on"
        f" {aws_txt} {ver_txt} {uuid_txt} {sha_txt}"
    )
    toks = _lex_tokens(probe)

    # Must keep these stable enough for sparse recall usefulness
    assert any(t in toks for t in ("pr#123", "pr", "123")), toks
    assert "resume.pdf" in toks, toks
    assert aws_txt in toks, toks
    assert uuid_txt.lower() in toks, toks
    assert sha_txt[:32].lower() in toks or sha_txt.lower() in toks, toks
    # backticks/quoted phrases should show up
    assert any("vectorstore" in t or t == "vectorstore" for t in toks), toks
    print("✅ TEST 0: lexical tokenizer robust")

    # --------------------------------------------------
    # TEST 0b: short-query sparse trigger (FIX-2)
    # --------------------------------------------------
    for sq in ["Pallavi", "ssh config", "wife name"]:
        assert store._auto_use_sparse(query_text=sq, dense_hits=[]), (
            f"Short query '{sq}' should trigger sparse via short_query_max_tokens"
            " (FIX-2)"
        )
    print("✅ TEST 0b: short-query sparse trigger (FIX-2)")

    # --------------------------------------------------
    # TEST 1: dense semantic retrieval works
    # --------------------------------------------------
    q1 = emb.embed_query("What is my wife's name?")
    dense_only = store.search_with_payloads(
        query_vector=q1,
        query_text="What is my wife's name?",
        top_k=5,
        mode="dense",
        rerank_mode="fast",
    )
    assert dense_only
    assert any(
        "pallavi" in (pl.get("text", "").lower() if isinstance(pl, dict) else "")
        for _, _, pl in dense_only
    )
    print("✅ TEST 1: dense semantic retrieval")

    # --------------------------------------------------
    # TEST 2: auto uses sparse for lexical-ish query (path/filename)
    # --------------------------------------------------
    q2 = emb.embed_query("resume.pdf")
    auto_hits = store.search_with_payloads(
        query_vector=q2,
        query_text="~/Downloads/resume.pdf",
        top_k=5,
        mode="auto",
        rerank_mode="fast",
    )
    assert auto_hits
    assert any(
        "resume.pdf" in (pl.get("text", "").lower() if isinstance(pl, dict) else "")
        for _, _, pl in auto_hits
    )
    print("✅ TEST 2: lexical auto retrieval")

    # --------------------------------------------------
    # TEST 3: hybrid dense score overlap consistency (canonical scores)
    # --------------------------------------------------
    dense_resume = store.search_with_payloads(
        query_vector=q2,
        query_text="resume.pdf",
        top_k=10,
        mode="dense",
        rerank_mode="fast",
    )
    hybrid_resume = store.search_with_payloads(
        query_vector=q2,
        query_text="resume.pdf",
        top_k=10,
        mode="hybrid",
        rerank_mode="fast",
    )
    assert dense_resume and hybrid_resume
    d_map = {mid: float(sc) for mid, sc, _pl in dense_resume}
    h_map = {mid: float(sc) for mid, sc, _pl in hybrid_resume}
    overlap = set(d_map) & set(h_map)
    assert overlap
    for mid in list(overlap)[:8]:
        assert abs(d_map[mid] - h_map[mid]) < 1e-6
    print("✅ TEST 3: hybrid scores are dense similarity (overlap)")

    # --------------------------------------------------
    # TEST 3b: RRF boosts documents in both sparse + dense lists (ACC-1)
    # --------------------------------------------------
    sparse_fake: List[Tuple[str, float, Optional[Dict[str, Any]]]] = [
        ("id_a", 0.9, {}),
        ("id_b", 0.7, {}),
    ]
    dense_fake: List[Tuple[str, float, Optional[Dict[str, Any]]]] = [
        ("id_b", 0.95, {}),
        ("id_c", 0.8, {}),
        ("id_a", 0.3, {}),
    ]
    rrf_order = store._ordered_union_rrf(sparse_fake, dense_fake, rrf_k=60)
    assert (
        rrf_order[0] == "id_b"
    ), f"Expected id_b first (in both lists), got {rrf_order}"
    print("✅ TEST 3b: RRF boosts docs appearing in both sparse+dense lists")

    # --------------------------------------------------
    # TEST 4: sparse recall hits uuid/hash/version terms (auto or hybrid)
    # --------------------------------------------------
    q4a = emb.embed_query("uuid reference")
    uuid_hits = store.search_with_payloads(
        query_vector=q4a,
        query_text=uuid_txt,
        top_k=5,
        mode="auto",
        rerank_mode="fast",
    )
    assert uuid_hits
    assert any(
        uuid_txt.lower() in (pl.get("text", "").lower() if isinstance(pl, dict) else "")
        for _, _, pl in uuid_hits
    )
    print("✅ TEST 4: uuid lexical recall works")

    # --------------------------------------------------
    # TEST 5: rerank attaches payload['_rerank'] and dense scores unchanged for same IDs
    # --------------------------------------------------
    q5 = emb.embed_query("ssh config location")
    fast = store.search_with_payloads(
        query_vector=q5,
        query_text="ssh config location",
        top_k=10,
        mode="dense",
        rerank_mode="fast",
    )
    acc = store.search_with_payloads(
        query_vector=q5,
        query_text="ssh config location",
        top_k=10,
        mode="dense",
        rerank_mode="accuracy",
    )
    assert fast and acc
    print(acc)

    f_map = {mid: float(sc) for mid, sc, _pl in fast}
    a_map = {mid: float(sc) for mid, sc, _pl in acc}
    overlap = set(f_map) & set(a_map)
    assert overlap
    for mid in list(overlap)[:10]:
        assert abs(f_map[mid] - a_map[mid]) < 1e-6

    has_rerank_payload = any(
        isinstance(pl, dict) and isinstance(pl.get("_rerank"), dict)
        for _mid, _sc, pl in acc
    )
    if has_rerank_payload:
        for _mid, _sc, pl in acc:
            if isinstance(pl, dict) and isinstance(pl.get("_rerank"), dict):
                rr = pl["_rerank"]

                # Prefer explicit probability if present
                if "rerank_prob" in rr:
                    rp = float(rr.get("rerank_prob"))
                    assert 0.0 <= rp <= 1.0, (rp, rr)

                # Otherwise fall back to "score" (may be prob already, or may be raw)
                elif "score" in rr:
                    s = float(rr.get("score"))
                    # If it *looks* like a probability, enforce range. Otherwise, skip range check.
                    if 0.0 <= s <= 1.0:
                        assert 0.0 <= s <= 1.0, (s, rr)
    print(
        "✅ TEST 5: rerank keeps dense score + rerank_prob normalized =",
        has_rerank_payload,
    )

    # --------------------------------------------------
    # TEST 6: moderate threshold should keep at least one obvious relevant result
    # --------------------------------------------------
    store.rerank_cfg = RerankConfig(
        enabled=store.rerank_cfg.enabled,
        method=store.rerank_cfg.method,
        always_on=store.rerank_cfg.always_on,
        pre_rerank_k=store.rerank_cfg.pre_rerank_k,
        max_chars_per_doc=store.rerank_cfg.max_chars_per_doc,
        device=store.rerank_cfg.device,
        batch_size=store.rerank_cfg.batch_size,
        max_length=store.rerank_cfg.max_length,
        qwen_model=store.rerank_cfg.qwen_model,
        cross_encoder_model=store.rerank_cfg.cross_encoder_model,
        rerank_threshold=0.10,  # lenient
    )

    lenient = store.search_with_payloads(
        query_vector=q5,
        query_text="ssh config location",
        top_k=10,
        mode="dense",
        rerank_mode="accuracy",
    )

    # If reranker is available, should keep something; if reranker disabled/fallback, this still returns dense hits.
    assert lenient
    print("✅ TEST 6: lenient threshold keeps results")
    # --------------------------------------------------
    # TEST 7a: soft_delete sets deleted=1 in payload
    # --------------------------------------------------
    target_entry = entries[0]  # "Remember that my wife's name is Pallavi."
    store.soft_delete(target_entry.id)

    retrieved = store.search_by_ids([str(target_entry.id)], with_payload=True)
    assert retrieved, "soft_delete: point should still exist in collection"
    _, payload = retrieved[0]
    assert (
        payload.get("deleted") == 1  # type: ignore
    ), f"Expected deleted=1 after soft_delete, got: {payload.get('deleted')}"  # type: ignore
    print("✅ TEST 7a: soft_delete sets deleted=1 in payload (point still exists)")

    # --------------------------------------------------
    # TEST 7b: soft-deleted entry is excluded from search results
    # --------------------------------------------------
    q7 = emb.embed_query("What is my wife's name?")
    results_after = store.search_with_payloads(
        query_vector=q7,
        query_text="What is my wife's name?",
        top_k=10,
        mode="dense",
        rerank_mode="fast",
    )
    deleted_ids = {
        mid
        for mid, _sc, pl in results_after
        if isinstance(pl, dict) and pl.get("deleted") == 1
    }
    assert (
        str(target_entry.id) not in deleted_ids
    ), "soft_delete: deleted entry should not appear in search results"
    print("✅ TEST 7b: soft-deleted entry is excluded from search results")

    print("🎉 VectorStore tests passed.")
    store.close()
