# buddy/buddy_core/boot.py
# ═══════════════════════════════════════════════════════════
# BUDDY BOOTSTRAP  —  v2
# ═══════════════════════════════════════════════════════════
#
# Boot sequence:
#   1.  Load runtime config from ~/.buddy/config/buddy.toml
#   2.  Matrix reveal animation  (2.8 s neural activation)
#   3.  First-boot preferred name prompt  ← ONLY on first boot, stored forever
#   4.  Python dependency checks + optional auto-install
#   5.  SQLite DB prep
#   6.  Full OS profile refresh  (every boot — name preserved)
#   7.  LLM model selection  (first-boot interactive, subsequent silent)
#   8.  LLM GGUF download if missing  (no Spinner — tqdm owns terminal)
#   9.  ST model download  (embedder + reranker, no Spinner)
#  10.  Env vars: BUDDY_EMBED_MODEL, BUDDY_RERANKER_MODEL  ← set before first use
#  11.  Prompt integrity check
#  12.  Render base system prompt
#  13.  Start / wait for llama-server
#  14.  Create core objects
#  15.  Strict enforcement  ← boot report only written AFTER this passes
#  16.  Write boot report
#  17.  Show online banner with system info + user name
#
# Public API:
#   bootstrap(options: Optional[BootstrapOptions] = None) -> BootstrapState
#
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import requests

from buddy.logger.logger import get_logger
from buddy.ui.boot_ui import (
    Spinner,
    _banner_centered,
    _birth_animation,
    _c,
    _center_visible,
    _color_frame,
    _fail,
    _frame,
    _info,
    _logo_row_code,
    _matrix_stream_reveal,
    _ok,
    _raw_c,
    _term_clear,
    _term_size,
    _warn,
    print_banner_centered,
)
from buddy.buddy_core.model_selector import LLMOption, get_or_select_llm_model
from buddy.buddy_core.llama_installer import ensure_llama_binary
from buddy.buddy_core.vision_selector import VisionChoice, get_or_select_vision
from buddy.buddy_core.searxng_setup import (
    is_installed as _searxng_installed,
    setup_searxng,
    start_searxng,
    stop_searxng,
)

logger = get_logger("bootstrap")

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
_HASH_CHUNK = 65536
_HTTP = requests.Session()


# ═══════════════════════════════════════════════════════════
# Options / State types
# ═══════════════════════════════════════════════════════════


@dataclass(frozen=True)
class BootstrapOptions:
    """Frozen — no mutation during boot."""

    show_boot_ui: bool = True
    strict_integrity: bool = True
    auto_install: bool = False
    verify_prompts_lock: bool = True
    verify_os_profile_lock: bool = True
    write_boot_report: bool = True
    download_models: bool = True
    force_model_reselect: bool = False
    force_vision_reselect: bool = False
    # Passed by run_textual() when first-boot wizard ran pre-Textual.
    # bootstrap() uses this to skip re-running the wizard inside the thread.
    pre_wizard_result: Optional[Dict[str, Any]] = None


@dataclass
class BootstrapIntegrity:
    prompts_lock_ok: bool = True
    os_profile_ok: bool = True
    violations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def tainted(self) -> bool:
        return bool(self.violations)


@dataclass
class BootstrapArtifacts:
    sqlite_store: Any = None
    vector_store: Any = None
    embedder: Any = None
    llama_client: Any = None
    brain: Any = None
    memory_manager: Any = None
    conversations: Any = None

    def validate(self) -> List[str]:
        missing = []
        if self.sqlite_store is None:
            missing.append("sqlite_store")
        if self.brain is None:
            missing.append("brain")
        if self.conversations is None:
            missing.append("conversations")
        return missing


@dataclass
class BootstrapState:
    project_root: str
    package_root: str
    integrity: BootstrapIntegrity
    artifacts: BootstrapArtifacts = field(default_factory=BootstrapArtifacts)
    config: Dict[str, Any] = field(default_factory=dict)
    shutdown: Optional[Callable[[], None]] = None
    context_budget: Optional[Any] = None  # ContextBudget — set during boot


# ═══════════════════════════════════════════════════════════
# UI helpers
# ═══════════════════════════════════════════════════════════


def _ui_step(show_ui: bool, label: str) -> Spinner:
    sp = Spinner(show_ui, label)
    sp.start()
    return sp


def _ui_ok(msg: str) -> None:
    _ok(msg)


def _ui_warn(msg: str) -> None:
    _warn(msg)


def _ui_fail(msg: str) -> None:
    _fail(msg)


def _ui_info(msg: str) -> None:
    _info(msg)


# ═══════════════════════════════════════════════════════════
# Small helpers
# ═══════════════════════════════════════════════════════════


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _as_bool(x: Any, default: bool) -> bool:
    if x is None:
        return default
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return bool(x)
    return str(x).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(x: Any, default: int) -> int:
    if x is None or isinstance(x, bool):
        return default
    if isinstance(x, int):
        return x
    try:
        return int(str(x).strip())
    except Exception:
        return default


def _as_float(x: Any, default: float) -> float:
    if x is None or isinstance(x, bool):
        return default
    if isinstance(x, (int, float)):
        return float(x)
    try:
        return float(str(x).strip())
    except Exception:
        return default


def _as_str(x: Any, default: str) -> str:
    if x is None:
        return default
    s = (x if isinstance(x, str) else str(x)).strip()
    return s if s else default


def _inject_ctx_size(server_args: List[str], n_ctx: int) -> List[str]:
    """
    Replace an existing --ctx-size value in server_args with n_ctx,
    or append --ctx-size n_ctx if not present.
    """
    args = list(server_args)
    ctx_flags = {"--ctx-size", "-c"}
    i = 0
    replaced = False
    while i < len(args):
        if args[i] in ctx_flags and i + 1 < len(args):
            args[i + 1] = str(n_ctx)
            replaced = True
            i += 2
        elif "=" in args[i] and args[i].split("=")[0] in ctx_flags:
            args[i] = f"--ctx-size={n_ctx}"
            replaced = True
            i += 1
        else:
            i += 1
    if not replaced:
        args.extend(["--ctx-size", str(n_ctx)])
    return args


def _atomic_write(path: Path, text: str, *, enc: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding=enc, newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as ex:
        logger.debug("json read failed: %s err=%r", path, ex)
        return {}


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    _atomic_write(
        path, json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _sec(d: Dict[str, Any], key: str) -> Dict[str, Any]:
    v = d.get(key)
    return v if isinstance(v, dict) else {}


@lru_cache(maxsize=64)
def _which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)


def _run(cmd: List[str], *, timeout: float = 3.0) -> Optional[str]:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════
# Full OS profile  — collected on EVERY boot
# ═══════════════════════════════════════════════════════════


def _cpu() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "physical_cores": None,
        "logical_cores": None,
        "frequency_mhz": None,
        "model": None,
        "architecture": platform.machine(),
    }
    try:
        import psutil  # type: ignore

        info["physical_cores"] = psutil.cpu_count(logical=False)
        info["logical_cores"] = psutil.cpu_count(logical=True)
        freq = psutil.cpu_freq()
        if freq:
            info["frequency_mhz"] = round(freq.current, 1)
    except ImportError:
        pass
    if platform.system() == "Darwin":
        info["model"] = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or _run(
            ["sysctl", "-n", "hw.model"]
        )
        if info["logical_cores"] is None:
            n = _run(["sysctl", "-n", "hw.logicalcpu"])
            info["logical_cores"] = int(n) if n and n.isdigit() else None
        if info["physical_cores"] is None:
            n = _run(["sysctl", "-n", "hw.physicalcpu"])
            info["physical_cores"] = int(n) if n and n.isdigit() else None
    elif platform.system() == "Linux":
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        info["model"] = line.split(":", 1)[1].strip()
                        break
        except Exception:
            pass
    return info


def _ram() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "total_bytes": None,
        "available_bytes": None,
        "used_bytes": None,
        "total_gb": None,
    }
    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        info.update({
            "total_bytes": vm.total,
            "available_bytes": vm.available,
            "used_bytes": vm.used,
            "total_gb": round(vm.total / (1024**3), 2),
        })
        return info
    except ImportError:
        pass
    if platform.system() == "Darwin":
        out = _run(["sysctl", "-n", "hw.memsize"])
        if out and out.isdigit():
            total = int(out)
            info.update({"total_bytes": total, "total_gb": round(total / (1024**3), 2)})
    return info


def _disk(data_dir: Path) -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "total_bytes": None,
        "free_bytes": None,
        "total_gb": None,
        "free_gb": None,
    }
    try:
        stat = shutil.disk_usage(data_dir)
        info.update({
            "total_bytes": stat.total,
            "free_bytes": stat.free,
            "total_gb": round(stat.total / (1024**3), 2),
            "free_gb": round(stat.free / (1024**3), 2),
        })
    except Exception:
        pass
    return info


def _gpu() -> Dict[str, Any]:
    info: Dict[str, Any] = {
        "backend": None,
        "name": None,
        "metal_supported": False,
        "cuda_available": False,
        "total_vram_bytes": None,
        "total_vram_gb": None,
    }
    sys_name = platform.system()
    machine = platform.machine()

    if sys_name == "Darwin" and machine == "arm64":
        info.update({"backend": "metal", "metal_supported": True})
        info["name"] = (
            _run(["sysctl", "-n", "machdep.cpu.brand_string"])
            or _run(["sysctl", "-n", "hw.model"])
            or "Apple Silicon"
        )
        out = _run(["sysctl", "-n", "hw.memsize"])
        if out and out.isdigit():
            total = int(out)
            info.update({
                "total_vram_bytes": total,
                "total_vram_gb": round(total / (1024**3), 2),
            })
        return info

    if sys_name == "Darwin":
        info.update(
            {"backend": "metal", "metal_supported": True, "name": "Intel Mac GPU"}
        )
        return info

    if _which("nvidia-smi"):
        info.update({"backend": "cuda", "cuda_available": True})
        info["name"] = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
        vram_raw = _run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"]
        )
        if vram_raw and vram_raw.strip().isdigit():
            mib = int(vram_raw.strip())
            info.update({
                "total_vram_bytes": mib * 1024 * 1024,
                "total_vram_gb": round(mib / 1024, 2),
            })
        return info

    if _which("rocm-smi"):
        info.update({
            "backend": "rocm",
            "name": _run(["rocm-smi", "--showproductname"]) or "AMD GPU",
        })
        return info

    info["backend"] = "cpu_only"
    return info


def _build_os_profile(
    *,
    assets_dir: Path,
    data_dir: Path,
    user_preferred_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Full OS profile. Decoupled from UI — no Spinner argument.
    Called on EVERY boot. user_preferred_name preserved across boots.
    """
    sysname = platform.system().lower()
    is_windows = sysname.startswith("win")
    username = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"

    if is_windows:
        shells = ["powershell", "cmd"]
    else:
        shells = [s for s in ("zsh", "bash", "fish", "sh") if _which(s)] or ["sh"]

    return {
        "version": 2,
        "generated_at": _utc_now_iso(),
        "username": username,
        "user_preferred_name": user_preferred_name or username,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "node": platform.node(),
        },
        "os_hints": {
            "is_windows": is_windows,
            "is_macos": sysname == "darwin",
            "is_linux": sysname == "linux",
            "shell_candidates": shells,
            "preferred_shell": shells[0] if shells else "sh",
        },
        "cpu": _cpu(),
        "ram": _ram(),
        "disk": _disk(data_dir),
        "gpu": _gpu(),
        "macos": _macos_info(),
        "environment": {
            k: os.environ.get(k)
            for k in (
                "SHELL",
                "TERM",
                "LANG",
                "HOME",
                "USER",
                "VIRTUAL_ENV",
                "CONDA_DEFAULT_ENV",
            )
        },
        "tools": _installed_tools(),
        "paths": {
            "project_root": str(PROJECT_ROOT),
            "package_root": str(PACKAGE_ROOT),
            "data_dir": str(data_dir),
            "assets_dir": str(assets_dir),
            "home": str(Path.home()),
        },
    }


def _macos_info() -> Dict[str, Any]:
    if platform.system() != "Darwin":
        return {}
    info: Dict[str, Any] = {
        "product_name": None,
        "product_version": None,
        "build_version": None,
        "is_apple_silicon": platform.machine() == "arm64",
    }
    sw = _run(["sw_vers"])
    if sw:
        for line in sw.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip().lower().replace(" ", "_")
                if key in info:
                    info[key] = val.strip()
    return info


def _installed_tools() -> Dict[str, Optional[str]]:
    tools = [
        "bash",
        "zsh",
        "fish",
        "git",
        "curl",
        "wget",
        "python3",
        "pip3",
        "uv",
        "node",
        "npm",
        "vim",
        "nano",
        "code",
        "brew",
        "open",
        "llama-server",
        "llama-cli",
        "ffmpeg",
        "jq",
        "sqlite3",
        "tmux",
    ]
    result: Dict[str, Optional[str]] = {}
    for t in tools:
        path = _which(t)
        if not path:
            result[t] = None
            continue
        version = None
        if t in ("git", "python3", "node", "ffmpeg", "jq", "brew"):
            out = _run([t, "--version"], timeout=2.0)
            if out:
                version = out.splitlines()[0][:80]
        result[t] = version or path
    return result


def _is_first_boot(os_profile_file: Path) -> bool:
    """
    True if no preferred name has ever been stored.
    A profile may exist from a partial previous run but have only the raw username.
    """
    existing = _read_json(os_profile_file)
    if not existing:
        return True
    name = existing.get("user_preferred_name", "")
    username = existing.get("username", "")
    # First boot if name is missing or was never personalised
    return not name or name == username


def _ensure_os_profile(
    integrity: BootstrapIntegrity,
    *,
    os_profile_file: Path,
    assets_dir: Path,
    data_dir: Path,
    user_preferred_name: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Build and persist a fresh OS profile every boot.
    user_preferred_name: newly entered (first boot) or None (subsequent → read from file).
    """
    existing = _read_json(os_profile_file)
    stored_name = existing.get("user_preferred_name") if existing else None
    resolved = user_preferred_name or stored_name

    try:
        prof = _build_os_profile(
            assets_dir=assets_dir,
            data_dir=data_dir,
            user_preferred_name=resolved,
        )
        _write_json(os_profile_file, prof)
        logger.info("OS profile refreshed")
        return prof
    except Exception as ex:
        integrity.warnings.append(f"os_profile_collection_failed:{ex!r}")
        logger.warning("OS profile failed; using fallback. err=%r", ex)
        return existing if existing else {"version": 2, "generated_at": _utc_now_iso()}


def _refresh_profile_lock(*, os_profile_file: Path, lock_file: Path) -> None:
    """Regenerate lock hash to match the freshly written profile."""
    if not os_profile_file.exists():
        return
    lock = {
        "version": 1,
        "generated_at": _utc_now_iso(),
        "file": str(os_profile_file),
        "sha256": _sha256(os_profile_file),
        "bytes": os_profile_file.stat().st_size,
    }
    _write_json(lock_file, lock)


# ═══════════════════════════════════════════════════════════
# Dependencies
# ═══════════════════════════════════════════════════════════

_REQUIRED_IMPORTS = [
    "numpy",
    "pydantic",
    "requests",
    "tomllib" if sys.version_info >= (3, 11) else "tomli",
]
_OPTIONAL_DEPS = [
    ("psutil", "psutil"),
    ("huggingface_hub", "huggingface_hub"),
    ("qdrant_client", "qdrant-client"),
    ("sentence_transformers", "sentence-transformers"),
]


def _check_imports(required: Sequence[str]) -> Tuple[List[str], List[str]]:
    missing, present = [], []
    for mod in required:
        try:
            __import__(mod)
            present.append(mod)
        except Exception:
            missing.append(mod)
    return missing, present


def _pip_install(packages: Sequence[str]) -> Tuple[bool, str]:
    if not packages:
        return True, ""
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pip", "install", *packages],
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as ex:
        return False, repr(ex)


def _ensure_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        db_path.touch()


# ═══════════════════════════════════════════════════════════
# ST model download  (embedder + reranker)
# ═══════════════════════════════════════════════════════════


def _st_dest(hf_model: str, data_dir: Path) -> Path:
    """~/.buddy/data/models/st/Qwen__Qwen3-Embedding-0.6B/"""
    return data_dir / "models" / "st" / hf_model.replace("/", "__")


def _st_present(hf_model: str, data_dir: Path) -> bool:
    return (_st_dest(hf_model, data_dir) / "config.json").exists()


def _ensure_st_model(
    hf_model: str,
    *,
    data_dir: Path,
    integrity: BootstrapIntegrity,
    label: str,
    download_enabled: bool,
) -> Optional[Path]:
    """
    Ensure a sentence-transformers model is available locally.

    ⚠ NO Spinner here — snapshot_download uses tqdm which writes to stdout.
    A Spinner background thread + tqdm = corrupted terminal output.
    We print plain static lines and let tqdm own the terminal.
    """
    dest = _st_dest(hf_model, data_dir)

    if _st_present(hf_model, data_dir):
        _ui_ok(f"{label}: {hf_model}")
        return dest

    if not download_enabled:
        _ui_warn(f"{label} missing (download disabled): {hf_model}")
        integrity.warnings.append(f"st_{label}_missing_download_disabled:{hf_model}")
        return None

    print(f"\n  ↓  Downloading {label}: {hf_model}")
    print(f"     Saving to: {dest}")
    print(f"     (first run only — fully offline after this)\n")

    try:
        from huggingface_hub import snapshot_download  # type: ignore

        dest.parent.mkdir(parents=True, exist_ok=True)
        downloaded = snapshot_download(
            repo_id=hf_model,
            local_dir=str(dest),
            local_dir_use_symlinks=False,
            ignore_patterns=[
                "*.msgpack",
                "*.h5",
                "flax_model*",
                "tf_model*",
                "rust_model*",
                "onnx/*",
            ],
        )
        print(f"\n  ✓  {label} ready: {hf_model}\n")
        return Path(downloaded)
    except ImportError:
        print(
            f"\n  ✗  huggingface_hub not installed — run: pip install huggingface_hub\n"
        )
        integrity.warnings.append(f"st_{label}_huggingface_hub_missing")
        return None
    except Exception as ex:
        print(f"\n  ✗  Download failed for {label}: {ex}\n")
        integrity.warnings.append(f"st_{label}_download_failed:{ex!r}")
        logger.error("ST download failed %s err=%r", hf_model, ex)
        return None


# ═══════════════════════════════════════════════════════════
# LLM GGUF download
# ═══════════════════════════════════════════════════════════


def _gguf_present(models_dir: Path, filename: str) -> bool:
    p = models_dir / filename
    return p.exists() and p.stat().st_size > 0


def _ensure_llm_gguf(
    integrity: BootstrapIntegrity,
    *,
    chosen: LLMOption,
    models_dir: Path,
    download_enabled: bool,
) -> Dict[str, Any]:
    dest = models_dir / chosen.filename

    if _gguf_present(models_dir, chosen.filename):
        _ui_ok(f"LLM GGUF present: {chosen.filename}")
        return {"path": str(dest), "downloaded": False, "ok": True}

    if not download_enabled:
        _ui_warn(f"LLM GGUF missing (download disabled): {chosen.filename}")
        integrity.violations.append(f"llm_gguf_missing:{chosen.filename}")
        return {"path": None, "downloaded": False, "ok": False}

    print(f"\n  ↓  Downloading LLM: {chosen.label}")
    print(f"     Repo : {chosen.hf_repo} / {chosen.hf_filename}")
    print(f"     To   : {dest}\n")

    try:
        from huggingface_hub import hf_hub_download  # type: ignore

        models_dir.mkdir(parents=True, exist_ok=True)
        cached = hf_hub_download(
            repo_id=chosen.hf_repo,
            filename=chosen.hf_filename,
            local_dir=str(models_dir),
            local_dir_use_symlinks=False,
        )
        cached_path = Path(cached)
        if cached_path != dest and cached_path.exists() and not dest.exists():
            shutil.move(str(cached_path), str(dest))
        print(f"\n  ✓  Downloaded: {chosen.filename}\n")
        return {"path": str(dest), "downloaded": True, "ok": True}
    except ImportError:
        print(
            f"\n  ✗  huggingface_hub not installed — run: pip install huggingface_hub\n"
        )
        integrity.violations.append("llm_gguf_huggingface_hub_missing")
        return {"path": None, "downloaded": False, "ok": False}
    except Exception as ex:
        print(f"\n  ✗  LLM download failed: {ex}\n")
        integrity.violations.append(f"llm_gguf_download_failed:{ex!r}")
        return {"path": None, "downloaded": False, "ok": False}


# ═══════════════════════════════════════════════════════════
# Prompt integrity
# ═══════════════════════════════════════════════════════════


def _verify_prompts_lock(
    integrity: BootstrapIntegrity,
    *,
    strict: bool,
    prompts_dir: Path,
    prompts_lock_file: Path,
) -> None:
    if not prompts_dir.is_dir():
        integrity.prompts_lock_ok = False
        integrity.violations.append("prompts_dir_missing")
        return

    prompt_files = sorted(
        p
        for p in prompts_dir.iterdir()
        if p.is_file() and p.suffix == ".py" and not p.name.startswith("_")
    )
    if not prompt_files:
        integrity.prompts_lock_ok = False
        integrity.violations.append("prompts_dir_empty")
        return

    if not prompts_lock_file.exists():
        integrity.prompts_lock_ok = False
        (integrity.violations if strict else integrity.warnings).append(
            "prompts_lock_missing"
        )
        return

    lock = _read_json(prompts_lock_file)
    locked_files = lock.get("files") or {}
    if not isinstance(locked_files, dict):
        locked_files = {}

    actual = {p.name for p in prompt_files}
    locked = set(locked_files.keys())

    for name in sorted(actual - locked):
        integrity.prompts_lock_ok = False
        integrity.violations.append(f"prompt_not_in_lock:{name}")
    for name in sorted(locked - actual):
        integrity.prompts_lock_ok = False
        integrity.violations.append(f"lock_references_missing_prompt:{name}")

    for p in prompt_files:
        entry = locked_files.get(p.name)
        if not isinstance(entry, dict):
            integrity.prompts_lock_ok = False
            integrity.violations.append(f"lock_entry_invalid:{p.name}")
            continue
        expected = entry.get("sha256", "")
        if not expected:
            integrity.prompts_lock_ok = False
            integrity.violations.append(f"lock_entry_no_sha256:{p.name}")
            continue
        if expected != _sha256(p):
            integrity.prompts_lock_ok = False
            integrity.violations.append(f"prompt_mismatch:{p.name}")

    if integrity.prompts_lock_ok:
        logger.info("Prompts lock OK (%d files)", len(prompt_files))


# ═══════════════════════════════════════════════════════════
# Runtime config
# ═══════════════════════════════════════════════════════════


def _runtime_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) / "Buddy") if base else (Path.home() / "Buddy")
    return Path.home() / ".buddy"


def _layout() -> Dict[str, Path]:
    root = _runtime_root()
    return {
        "root": root,
        "config_dir": root / "config",
        "data_dir": root / "data",
        "cache_dir": root / "cache",
        "logs_dir": root / "logs",
        "state_dir": root / "state",
        "assets_dir": root / "data" / "assets",
        "sqlite_db": root / "data" / "mem.sqlite3",
        "qdrant_dir": root / "data" / "qdrant",
        "models_dir": root / "data" / "models",
        "conversations_snapshot": root / "state" / "conversations.json",
        "boot_report": root / "data" / "assets" / "boot_report.json",
        "os_profile": root / "data" / "assets" / "os_profile.json",
        "os_profile_lock": root / "data" / "assets" / "os_profile.lock.json",
        "llama_server_log": root / "logs" / "llama_server.log",
    }


def _ensure_dirs(paths: Dict[str, Path]) -> None:
    for k in (
        "config_dir",
        "data_dir",
        "cache_dir",
        "logs_dir",
        "assets_dir",
        "state_dir",
        "models_dir",
    ):
        paths[k].mkdir(parents=True, exist_ok=True)
    (paths["models_dir"] / "st").mkdir(parents=True, exist_ok=True)


def _copy_defaults_once(config_dir: Path) -> List[str]:
    copied: List[str] = []
    config_dir.mkdir(parents=True, exist_ok=True)
    for name, src in {
        "buddy.toml": PACKAGE_ROOT / "config" / "buddy.toml",
        "tools.toml": PACKAGE_ROOT / "config" / "tools.toml",
    }.items():
        dst = config_dir / name
        if not dst.exists() and src.exists():
            try:
                shutil.copyfile(src, dst)
                copied.append(name)
            except Exception as ex:
                logger.warning("config copy failed %s: %r", name, ex)
    return copied


def _read_toml(path: Path) -> Dict[str, Any]:
    try:
        if sys.version_info >= (3, 11):
            import tomllib as _t  # type: ignore
        else:
            import tomli as _t  # type: ignore
        with path.open("rb") as f:
            obj = _t.load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception as ex:
        logger.warning("toml read failed %s: %r", path, ex)
        return {}


def _load_runtime_config() -> Dict[str, Any]:
    paths = _layout()
    _ensure_dirs(paths)
    copied = _copy_defaults_once(paths["config_dir"])

    buddy_raw = _read_toml(paths["config_dir"] / "buddy.toml")
    tools_raw = _read_toml(paths["config_dir"] / "tools.toml")
    buddy_cfg = buddy_raw.get("buddy", buddy_raw) if isinstance(buddy_raw, dict) else {}
    tools_cfg = tools_raw.get("tools", tools_raw) if isinstance(tools_raw, dict) else {}

    boot_cfg = _sec(buddy_cfg, "bootstrap")
    llama_cfg = _sec(buddy_cfg, "llama")
    mem_cfg = _sec(buddy_cfg, "memory")
    rerank_cfg = _sec(_sec(buddy_cfg, "vector_store"), "rerank")
    prompts_dir = Path(
        _as_str(boot_cfg.get("prompts_dir"), str(PACKAGE_ROOT / "prompts"))
    )

    # Model resolution from existing buddy.toml config keys
    # Embedder lives in [memory] embedding_model  (already in your toml)
    # Reranker lives in [vector_store.rerank] qwen_model  (already in your toml)
    embedder_model = _as_str(
        mem_cfg.get("embedding_model"), "Qwen/Qwen3-Embedding-0.6B"
    )
    reranker_model = _as_str(rerank_cfg.get("qwen_model"), "Qwen/Qwen3-Reranker-0.6B")

    return {
        "buddy": buddy_cfg,
        "tools": tools_cfg,
        "embedder_model": embedder_model,
        "reranker_model": reranker_model,
        "bootstrap": {
            "show_boot_ui": _as_bool(boot_cfg.get("show_boot_ui"), True),
            "strict_integrity": _as_bool(boot_cfg.get("strict_integrity"), True),
            "auto_install": _as_bool(boot_cfg.get("auto_install"), False),
            "verify_prompts_lock": _as_bool(boot_cfg.get("verify_prompts_lock"), True),
            "verify_os_profile_lock": _as_bool(
                boot_cfg.get("verify_os_profile_lock"), True
            ),
            "write_boot_report": _as_bool(boot_cfg.get("write_boot_report"), True),
            "download_models": _as_bool(boot_cfg.get("download_models"), True),
            "force_model_reselect": _as_bool(
                boot_cfg.get("force_model_reselect"), False
            ),
            "force_vision_reselect": _as_bool(
                boot_cfg.get("force_vision_reselect"), False
            ),
        },
        "llama": {
            "base_url": _as_str(llama_cfg.get("base_url"), "http://127.0.0.1:8080"),
            "host": _as_str(llama_cfg.get("host"), "127.0.0.1"),
            "port": int(llama_cfg.get("port") or 8080),
            "model_gguf": _as_str(llama_cfg.get("model_gguf"), ""),
            "model_name": _as_str(llama_cfg.get("model_name"), "local-model"),
            "ready_timeout_s": float(llama_cfg.get("ready_timeout_s") or 180.0),
            "scan_ports": llama_cfg.get("scan_ports") or [8080, 8081, 8082, 8088, 8888, 11434],
            "server_args": llama_cfg.get("server_args") or [],
        },
        "fs": {
            "config_dir": paths["config_dir"],
            "data_dir": paths["data_dir"],
            "assets_dir": paths["assets_dir"],
            "db_path": paths["sqlite_db"],
            "qdrant_dir": paths["qdrant_dir"],
            "models_dir": paths["models_dir"],
            "state_dir": paths["state_dir"],
            "conversations_snapshot": paths["conversations_snapshot"],
            "os_profile_file": paths["os_profile"],
            "os_profile_lock_file": paths["os_profile_lock"],
            "boot_report_file": paths["boot_report"],
            "llama_server_log": paths["llama_server_log"],
            "prompts_dir": prompts_dir,
            "prompts_lock_file": prompts_dir / "lock" / "prompts.lock.json",
        },
        "copied_defaults": copied,
    }


# ═══════════════════════════════════════════════════════════
# Port scanning helpers — detect existing servers
# ═══════════════════════════════════════════════════════════


def _scan_llama_ports(host: str, scan_ports: List[int]) -> Optional[int]:
    """
    Scan a list of ports for a running llama-server.
    Returns the first port where a ready llama-server is found, or None.
    """
    for p in scan_ports:
        try:
            with socket.create_connection((host, p), timeout=0.3):
                pass
        except Exception:
            continue
        bu = f"http://{host}:{p}"
        for path in ("/health", "/v1/models"):
            try:
                r = _HTTP.get(bu + path, timeout=(0.3, 1.0))
                if r.status_code == 200:
                    logger.info("Found existing llama-server on port %d", p)
                    return p
            except Exception:
                continue
    return None


def _scan_searxng_ports(host: str, scan_ports: List[int]) -> Optional[int]:
    """
    Scan a list of ports for a running SearXNG instance.
    Returns the first port where SearXNG is responding, or None.
    """
    for p in scan_ports:
        try:
            with socket.create_connection((host, p), timeout=0.3):
                pass
        except Exception:
            continue
        bu = f"http://{host}:{p}"
        for path in ("/search", "/", "/healthz"):
            try:
                r = _HTTP.get(bu + path, timeout=(0.3, 1.5))
                if r.status_code in (200, 302, 405):
                    logger.info("Found existing SearXNG on port %d", p)
                    return p
            except Exception:
                continue
    return None


# ═══════════════════════════════════════════════════════════
# mmproj (vision) download helper
# ═══════════════════════════════════════════════════════════


def _ensure_mmproj(
    vision: "VisionChoice",
    *,
    models_dir: Path,
    download_enabled: bool,
) -> Optional[Path]:
    """
    Ensure the mmproj GGUF is present in models_dir.
    Downloads from HuggingFace if missing and download_enabled=True.
    Returns path to mmproj or None on failure.
    """
    if not vision.enabled or not vision.mmproj_hf_filename:
        return None

    dest = models_dir / vision.mmproj_hf_filename
    if dest.exists() and dest.stat().st_size > 0:
        logger.info("mmproj already present: %s", dest)
        return dest

    if not download_enabled:
        logger.warning("mmproj missing and download_models=false: %s", dest)
        return None

    repo = vision.mmproj_hf_repo
    filename = vision.mmproj_hf_filename
    if not repo or not filename:
        logger.warning("mmproj hf_repo/hf_filename not set in vision choice")
        return None

    logger.info("Downloading mmproj %s from %s", filename, repo)
    _ui_info(f"Downloading mmproj: {filename}  (~{vision.mmproj_size_gb:.2f} GB)")
    try:
        from huggingface_hub import hf_hub_download  # type: ignore

        path = hf_hub_download(
            repo_id=repo,
            filename=filename,
            local_dir=str(models_dir),
            local_dir_use_symlinks=False,
        )
        dest = Path(path)
        logger.info("mmproj downloaded → %s", dest)
        _ui_ok(f"mmproj downloaded → {dest.name}")
        return dest
    except Exception as ex:
        logger.error("mmproj download failed: %r", ex)
        _ui_warn(f"mmproj download failed: {ex}")
        return None


# ═══════════════════════════════════════════════════════════
# llama-server lifecycle
# ═══════════════════════════════════════════════════════════


def _flatten(x: Any) -> List[str]:
    if not isinstance(x, (list, tuple)):
        return []
    return [str(i).strip() for i in x if i is not None and str(i).strip()]


def _sanitize_args(args: List[str]) -> Tuple[List[str], List[str]]:
    banned = {"--host", "--port", "-m", "--model", "--chat-template-file", "--jinja"}
    out, removed = [], []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in banned:
            removed.append(tok)
            if tok in {"--host", "--port", "-m", "--model", "--chat-template-file"}:
                if i + 1 < len(args):
                    removed.append(args[i + 1])
                    i += 2
                else:
                    i += 1
            else:
                i += 1
        else:
            out.append(tok)
            i += 1
    return out, removed


def _find_gguf(filename: str, *, data_dir: Path) -> Optional[Path]:
    p = Path(filename)
    if p.is_absolute() and p.exists():
        return p
    for c in (
        data_dir / "models" / filename,
        PROJECT_ROOT / filename,
        Path.cwd() / filename,
    ):
        if c.exists():
            return c
    return None


def _probe(base_url: str, *, host: str, port: int) -> Dict[str, bool]:
    st = {"listening": False, "http_online": False, "ready": False}
    try:
        with socket.create_connection((host, port), timeout=0.2):
            st["listening"] = True
    except Exception:
        return st
    bu = base_url.rstrip("/")
    for path in ("/v1/models", "/health", "/"):
        try:
            _HTTP.get(bu + path, timeout=(0.5, 1.0))
            st["http_online"] = True
            break
        except Exception:
            pass
    for path in ("/health", "/v1/models"):
        try:
            if _HTTP.get(bu + path, timeout=(0.5, 1.5)).status_code == 200:
                st["ready"] = True
                break
        except Exception:
            pass
    return st


def _wait_external(
    *,
    base_url: str,
    host: str,
    port: int,
    timeout_s: float,
    show_wait: Callable[[float, str], None],
) -> Tuple[bool, str]:
    t0 = time.perf_counter()
    while True:
        elapsed = time.perf_counter() - t0
        s = _probe(base_url, host=host, port=port)
        if not s["listening"]:
            return False, "lost_listening"
        if s["ready"]:
            return True, "ready"
        show_wait(elapsed, "listening (not ready)")
        if elapsed >= timeout_s:
            return False, f"timeout>{timeout_s:.0f}s"
        time.sleep(0.25)


def _wait_spawned(
    *,
    proc: subprocess.Popen,
    base_url: str,
    host: str,
    port: int,
    timeout_s: float,
    show_wait: Callable[[float, str], None],
) -> Tuple[bool, str]:
    t0 = time.perf_counter()
    while True:
        if proc.poll() is not None:
            return False, f"exited rc={proc.poll()}"
        elapsed = time.perf_counter() - t0
        s = _probe(base_url, host=host, port=port)
        if s["ready"]:
            return True, "ready"
        phase = "not listening" if not s["listening"] else "listening (not ready)"
        show_wait(elapsed, phase)
        if elapsed >= timeout_s:
            return False, f"timeout>{timeout_s:.0f}s"
        time.sleep(0.25)


def _spawn(
    *, cmd: List[str], log: Path
) -> Tuple[Optional[subprocess.Popen], List[str]]:
    log.parent.mkdir(parents=True, exist_ok=True)
    log_f = None
    try:
        log_f = log.open("ab", buffering=0)
    except Exception:
        pass
    try:
        p = subprocess.Popen(
            cmd,
            stdout=log_f or subprocess.DEVNULL,
            stderr=log_f or subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        logger.info("Spawned llama-server pid=%s", p.pid)
        return p, cmd
    except Exception as ex:
        logger.warning("Failed to spawn llama-server: %r", ex)
        return None, cmd


def _terminate(proc: subprocess.Popen, *, grace: float = 2.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except Exception:
        pass
    t0 = time.time()
    while (time.time() - t0) < grace:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        pass


def _pretty_cmd(cmd: List[str]) -> List[str]:
    if not cmd:
        return []
    out = ["llama-server \\"]
    single = {"--mlock", "--mmap", "--no-webui"}
    i = 1
    while i < len(cmd):
        tok = cmd[i]
        if (
            tok.startswith("-")
            and i + 1 < len(cmd)
            and not cmd[i + 1].startswith("-")
            and tok not in single
        ):
            out.append(f"  {tok} {cmd[i+1]} \\")
            i += 2
        else:
            out.append(f"  {tok} \\")
            i += 1
    out[-1] = out[-1].rstrip(" \\")
    return out


# ═══════════════════════════════════════════════════════════
# Core objects
# ═══════════════════════════════════════════════════════════


@lru_cache(maxsize=1)
def _SQLiteStore():
    from buddy.memory.sqlite_store import SQLiteStore

    return SQLiteStore


@lru_cache(maxsize=1)
def _EmbeddingProvider():
    from buddy.embeddings.embedding_provider import EmbeddingProvider

    return EmbeddingProvider


@lru_cache(maxsize=1)
def _VectorStore():
    from buddy.memory.vector_store import VectorStore

    return VectorStore


@lru_cache(maxsize=1)
def _MemoryManager():
    from buddy.memory.memory_manager import MemoryManager

    return MemoryManager


@lru_cache(maxsize=1)
def _Conversations():
    from buddy.context.conversations import Conversations

    return Conversations


@lru_cache(maxsize=1)
def _PromptBuilder():
    from buddy.brain.prompt_builder import PromptBuilder

    return PromptBuilder


@lru_cache(maxsize=1)
def _OutputParser():
    from buddy.brain.output_parser import OutputParser

    return OutputParser


@lru_cache(maxsize=1)
def _LlamaClient():
    from buddy.llm.llama_client import LlamaClient

    return LlamaClient


@lru_cache(maxsize=1)
def _Brain():
    from buddy.brain.brain import Brain

    return Brain


def _create_artifacts(
    db_path: Path,
    integrity: BootstrapIntegrity,
    *,
    llama_model: str,
    llama_base_url: str,
    os_profile: Dict[str, Any],
    config: Dict[str, Any],
) -> BootstrapArtifacts:
    """
    Instantiate all core objects.

    BUDDY_EMBED_MODEL and BUDDY_RERANKER_MODEL env vars have already been set
    by bootstrap() at this point, so EmbeddingProvider and VectorStore
    (reranker) will load local paths without any extra arguments.
    """
    arts = BootstrapArtifacts()
    buddy_cfg = config.get("buddy", {}) or {}
    vs_cfg = _sec(buddy_cfg, "vector_store")
    ctx_cfg = _sec(buddy_cfg, "context")
    fs = config.get("runtime", {}).get("fs", {}) or {}
    qdrant_dir = Path(
        _as_str(
            fs.get("qdrant_dir") if isinstance(fs, dict) else None,
            str(_layout()["qdrant_dir"]),
        )
    )
    debug = _as_bool(_sec(buddy_cfg, "general").get("debug"), True)

    # SQLite (required)
    try:
        arts.sqlite_store = _SQLiteStore()(db_path=str(db_path), debug=debug)
    except Exception as ex:
        integrity.violations.append(f"sqlite_store_failed:{ex!r}")

    # EmbeddingProvider — env var already set, singleton uses local path
    try:
        arts.embedder = _EmbeddingProvider()()
    except Exception as ex:
        integrity.warnings.append(f"embedder_failed:{ex!r}")

    # VectorStore — BUDDY_RERANKER_MODEL env var already set
    try:
        backend = _as_str(vs_cfg.get("backend"), "local").lower()
        server_obj = None
        if backend == "server":
            try:
                from buddy.memory.vector_store import VectorServerConfig  # type: ignore

                sc = _sec(vs_cfg, "server")
                server_obj = VectorServerConfig(
                    url=_as_str(sc.get("url"), "http://127.0.0.1:6333"),
                    api_key=_as_str(sc.get("api_key"), ""),
                    timeout=_as_int(sc.get("timeout"), 10),
                )
            except Exception:
                integrity.warnings.append("vector_server_config_failed")

        arts.vector_store = _VectorStore()(
            backend="server" if backend == "server" else "local",
            local_path=str(qdrant_dir) if backend != "server" else None,
            server=server_obj,
            collection="buddy_memories",
            dense_name="dense",
            sparse_name="sparse",
            distance="Cosine",
            prefer_grpc=False,
            rerank_cfg=_sec(vs_cfg, "rerank"),
            sparse_cfg=_sec(vs_cfg, "sparse_cfg"),
            debug=debug,
        )
        if arts.vector_store and arts.embedder:
            try:
                dim = int(arts.embedder.dimension)
                if dim:
                    arts.vector_store.ensure_collection(dim=dim)
            except Exception as ex:
                integrity.warnings.append(f"vector_collection_failed:{ex!r}")
    except Exception as ex:
        integrity.warnings.append(f"vector_store_failed:{ex!r}")

    # Conversations (required)
    try:
        snapshot = (
            fs.get("conversations_snapshot") if isinstance(fs, dict) else None
        ) or str(_layout()["conversations_snapshot"])
        max_turns = _as_int(ctx_cfg.get("max_conversation_turn", 12), 12)
        arts.conversations = _Conversations()(
            max_turns=max_turns, snapshot_path=str(snapshot)
        )
    except Exception as ex:
        integrity.violations.append(f"conversations_failed:{ex!r}")

    # LlamaClient
    try:
        arts.llama_client = _LlamaClient()(
            model=llama_model, base_url=llama_base_url, debug=debug
        )
        if not arts.llama_client.warmup():
            integrity.warnings.append("llama_warmup_failed")
    except Exception as ex:
        integrity.warnings.append(f"llama_client_failed:{ex!r}")

    # Brain (required)
    try:
        if arts.llama_client:
            arts.brain = _Brain()(
                llm=arts.llama_client,
                os_profile=os_profile,
                debug=debug,
            )
        else:
            integrity.violations.append("brain_skipped_missing_deps")
    except Exception as ex:
        integrity.violations.append(f"brain_failed:{ex!r}")

    # MemoryManager (optional)
    try:
        if arts.sqlite_store:
            arts.memory_manager = _MemoryManager()(
                sqlite_store=arts.sqlite_store,
                vector_store=arts.vector_store,
                embedder=arts.embedder,
                brain=arts.brain,
                debug=debug,
            )
    except Exception as ex:
        integrity.warnings.append(f"memory_manager_failed:{ex!r}")

    return arts


def _fetch_llama_server_props(base_url: str) -> Dict[str, Any]:
    """
    Query the live llama-server for its actual runtime properties.

    Hits two endpoints that every llama.cpp server exposes:

      GET /props       native endpoint — model_path, n_ctx, n_gpu_layers,
                       n_threads, n_batch, kv cache type, flash attention,
                       rope_freq_base, slot count, chat template, build info.

      GET /v1/models   OpenAI-compat — the model identifier the server
                       was started with (may differ from the GGUF filename).

    Returns a flat dict of display-ready strings.  Every missing field is
    the string "—".  Never raises — any failure produces a degraded dict
    so the banner renders correctly rather than crashing boot.
    """
    bu = base_url.rstrip("/")
    out: Dict[str, Any] = {}

    # ── /props ───────────────────────────────────────────────────────────
    try:
        r = _HTTP.get(bu + "/props", timeout=(1.0, 4.0))
        if r.status_code == 200:
            props: Dict[str, Any] = r.json()

            # Model file — full server path → basename for display
            raw_path = props.get("model_path") or props.get("model") or ""
            out["model_file"] = Path(raw_path).name if raw_path else "—"
            out["model_path"] = raw_path or "—"

            # Context window size in tokens
            n_ctx = props.get("n_ctx") or props.get("total_slots")
            out["n_ctx"] = str(n_ctx) if n_ctx is not None else "—"

            # GPU offload layers (0 = CPU only)
            n_gpu = props.get("n_gpu_layers")
            out["n_gpu_layers"] = str(n_gpu) if n_gpu is not None else "—"

            # CPU threads
            n_thr = props.get("n_threads") or props.get("n_threads_batch")
            out["n_threads"] = str(n_thr) if n_thr is not None else "—"

            # Logical batch size
            n_bat = props.get("n_batch")
            out["n_batch"] = str(n_bat) if n_bat is not None else "—"

            # KV cache quantisation — newer llama.cpp exposes cache_type_k/v
            kv_k = props.get("cache_type_k") or props.get("kv_cache_type")
            kv_v = props.get("cache_type_v")
            if kv_k and kv_v and kv_k != kv_v:
                out["kv_cache"] = f"{kv_k}/{kv_v}"
            elif kv_k:
                out["kv_cache"] = str(kv_k)
            else:
                out["kv_cache"] = "—"

            # Flash attention flag
            fa = props.get("flash_attn")
            out["flash_attn"] = "on" if fa else "off" if fa is not None else "—"

            # RoPE base frequency — indicator of model variant / fine-tune
            rope = props.get("rope_freq_base")
            out["rope_freq_base"] = f"{rope:.0f}" if rope else "—"

            # Parallel slot count (how many concurrent inference requests)
            slots = props.get("n_slots") or props.get("slots_idle")
            out["n_slots"] = str(slots) if slots is not None else "1"

            # Chat template present (affects whether /v1/chat/completions works)
            has_tmpl = bool(props.get("chat_template") or props.get("system_prompt"))
            out["chat_template"] = "yes" if has_tmpl else "none"

            # Backend / build string — newer builds expose this
            build = props.get("build_info") or props.get("build_number") or ""
            backend = props.get("backend") or ""
            if build and backend:
                out["build"] = f"{backend} · build {build}"
            elif build:
                out["build"] = f"build {build}"
            elif backend:
                out["build"] = backend
            else:
                out["build"] = "—"

    except Exception as exc:
        logger.debug("_fetch_llama_server_props /props failed: %r", exc)

    # ── /v1/models ───────────────────────────────────────────────────────
    try:
        r = _HTTP.get(bu + "/v1/models", timeout=(1.0, 3.0))
        if r.status_code == 200:
            data = r.json().get("data") or []
            if data:
                model_id = data[0].get("id") or ""
                out["model_id"] = model_id or "—"
                # Use as fallback if /props did not return a model filename
                if out.get("model_file", "—") == "—" and model_id:
                    out["model_file"] = model_id
    except Exception as exc:
        logger.debug("_fetch_llama_server_props /v1/models failed: %r", exc)

    return out


# ═══════════════════════════════════════════════════════════
# First-boot wizard
# ═══════════════════════════════════════════════════════════


def _wizard_line(msg: str = "") -> None:
    print(_c("  │  ", "border") + msg)


def _wizard_ask(prompt: str, default: str) -> str:
    """Single-line input with AURORA prompt. Returns stripped value or default."""
    label = _c("  ▸ ", "accent") + prompt
    if default:
        label += _c(f"  [{default}]", "dim")
    label += _c("  › ", "border")
    try:
        val = input(label).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return val or default


def _wizard_yn(question: str, *, default: bool = False) -> bool:
    """Yes/No prompt. Returns bool."""
    hint = _c("Y/n" if default else "y/N", "dim")
    label = (
        _c("  ▸ ", "accent")
        + question
        + _c("  ", "reset")
        + hint
        + _c("  › ", "border")
    )
    try:
        raw = input(label).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if raw in ("y", "yes"):
        return True
    if raw in ("n", "no"):
        return False
    return default


def _wizard_header(title: str) -> None:
    cols, _ = _term_size()
    inner = min(70, max(50, cols - 6))
    print()
    print(_c("  ╔" + "═" * (inner + 2) + "╗", "border"))
    print(
        _c("  ║  ", "border") + _c(f"{title:^{inner}}", "accent") + _c("  ║", "border")
    )
    print(_c("  ╚" + "═" * (inner + 2) + "╝", "border"))
    print()


def _show_wizard_hardware(os_profile: Dict[str, Any]) -> None:
    """Print a compact hardware summary for the first-boot wizard."""
    gpu = os_profile.get("gpu", {}) or {}
    ram = os_profile.get("ram", {}) or {}
    cpu = os_profile.get("cpu", {}) or {}
    macos = os_profile.get("macos", {}) or {}

    ram_gb = ram.get("total_gb", "?")
    cores = cpu.get("logical_cores", "?")
    gpu_name = gpu.get("name") or "unknown"
    backend = gpu.get("backend") or "cpu_only"
    mac_ver = macos.get("product_version", "")

    print()
    _wizard_line(_c("Hardware detected:", "dim"))
    _wizard_line(
        f"  GPU     {_c(str(gpu_name), 'warn')}  {_c('(' + backend + ')', 'dim')}"
    )
    _wizard_line(
        f"  RAM     {_c(str(ram_gb) + ' GB', 'warn')}  · "
        f" {_c(str(cores) + ' cores', 'dim')}"
    )
    if mac_ver:
        _wizard_line(f"  macOS   {_c(mac_ver, 'dim')}")
    print()


def _run_first_boot_wizard(
    *,
    os_profile: Dict[str, Any],
    show_ui: bool,
    default_name: str,
) -> Dict[str, Any]:
    """
    Interactive first-boot wizard. Runs once, stores results to buddy.toml.
    Returns dict: {user_name, language, web_engine, stt, tts}
    """
    defaults = {
        "user_name": default_name,
        "language": "en",
        "web_engine": "duckduckgo",
        "stt": False,
        "tts": False,
    }

    if not show_ui:
        return defaults

    _wizard_header("BUDDY — FIRST AWAKENING")

    # Hardware summary
    _show_wizard_hardware(os_profile)

    # Q1: Name
    _wizard_line(_c("What should I call you?", "tagline"))
    user_name = _wizard_ask("Your name", default=default_name)
    print()

    # Q2: Language
    _wizard_line(_c("Primary language?", "tagline"))
    _wizard_line(
        f"  {_c('[1]', 'key')} English    {_c('[2]', 'key')} Hindi   "
        f" {_c('[3]', 'key')} Hinglish"
    )
    lang_raw = _wizard_ask("Choice", default="1")
    language = {"1": "en", "2": "hi", "3": "hinglish"}.get(lang_raw.strip(), "en")
    print()

    # Q3: Web search
    _wizard_line(_c("Web search engine?", "tagline"))
    _wizard_line(
        f"  {_c('[S]', 'key')} SearXNG  "
        + _c("(local, private, no rate limits — requires one-time setup)", "dim")
    )
    _wizard_line(
        f"  {_c('[D]', 'key')} DuckDuckGo  "
        + _c("(no setup required, works immediately)", "dim")
    )
    web_raw = _wizard_ask("Choice", default="D").upper()
    web_engine = "searxng" if web_raw.startswith("S") else "duckduckgo"
    print()

    # Q4: STT
    stt = _wizard_yn("Voice input — speak to Buddy via mic?", default=False)
    print()

    # Q5: TTS
    tts = _wizard_yn("Voice output — Buddy speaks responses?", default=False)
    print()

    return {
        "user_name": user_name,
        "language": language,
        "web_engine": web_engine,
        "stt": stt,
        "tts": tts,
    }


def _write_first_boot_config(
    config_dir: Path,
    *,
    user_name: str,
    language: str,
    web_engine: str,
    stt: bool,
    tts: bool,
) -> None:
    """
    Persist first-boot wizard choices to buddy.toml.
    - Patches enable_audio_stt / enable_audio_tts in [features] via regex.
    - Appends [user] and [web_search] sections if not present.
    """
    import re as _re

    toml_path = config_dir / "buddy.toml"
    try:
        text = toml_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""

    # Patch existing [features] voice flags
    stt_val = "true" if stt else "false"
    tts_val = "true" if tts else "false"
    text = _re.sub(r"enable_audio_stt\s*=\s*\w+", f"enable_audio_stt = {stt_val}", text)
    text = _re.sub(r"enable_audio_tts\s*=\s*\w+", f"enable_audio_tts = {tts_val}", text)

    # Append new [user] section
    if "[user]" not in text:
        text += f"""
# ─────────────────────────────────────────────────────────────
# User preferences — set by first-boot wizard
# ─────────────────────────────────────────────────────────────
[user]
name = "{user_name}"
language = "{language}"
"""

    # Append new [web_search] section
    if "[web_search]" not in text:
        text += f"""
[web_search]
engine = "{web_engine}"
searxng_url = "http://127.0.0.1:8888"
"""

    try:
        _atomic_write(toml_path, text)
        logger.info("First-boot config saved: %s", toml_path)
    except Exception as ex:
        logger.warning("Failed to write first-boot config: %r", ex)


# ═══════════════════════════════════════════════════════════
# run_pre_textual_setup()  — called from run_textual() BEFORE BuddyApp
# ═══════════════════════════════════════════════════════════


def run_pre_textual_setup() -> Optional[Dict[str, Any]]:
    """
    Run ALL interactive terminal I/O that cannot happen inside Textual.

    Textual takes ownership of the terminal after BuddyApp.run(), so any
    input() calls must happen here, before that point.

    Handles:
      • First-boot birth animation, matrix reveal
      • First-boot wizard (name / language / web engine / STT / TTS)
      • LLM model selection  (if no saved choice exists in buddy.toml)
      • Vision selection     (if no saved choice exists; runs after model selection)

    Returns the wizard_result dict on first boot (pass it to
    BootstrapOptions(pre_wizard_result=...)), or None if everything was
    already configured and no interaction was needed.
    """
    from buddy.buddy_core.model_selector import (
        _load_saved_choice as _ms_load,
        _run_interactive_selection,
    )
    from buddy.buddy_core.vision_selector import (
        _load_saved_choice as _vs_load,
        get_or_select_vision,
    )

    runtime = _load_runtime_config()
    fs = runtime["fs"]
    config_dir: Path = fs["config_dir"]
    assets_dir: Path = fs["assets_dir"]
    data_dir: Path = fs["data_dir"]
    os_profile_file: Path = fs["os_profile_file"]

    needs_first_boot = _is_first_boot(os_profile_file)
    needs_model = _ms_load(config_dir) is None
    needs_vision = _vs_load(config_dir) is None

    if not needs_first_boot and not needs_model and not needs_vision:
        return None  # fully configured — Textual can boot silently

    if needs_first_boot:
        # Birth animation + matrix reveal in clean terminal (pre-Textual)
        _birth_animation()
        _matrix_stream_reveal(duration=4)
        _term_clear()
        # Name is collected once inside the wizard (Q1) — no separate prompt here.

    default_name = (
        os.environ.get("USER")
        or os.environ.get("USERNAME")
        or "boss"
    )

    # Build a lightweight OS profile so wizard/model-selector shows accurate hardware info.
    os_profile = _build_os_profile(
        assets_dir=assets_dir,
        data_dir=data_dir,
        user_preferred_name=default_name,
    )

    wizard_result: Optional[Dict[str, Any]] = None

    if needs_first_boot:
        _term_clear()
        wizard_result = _run_first_boot_wizard(
            os_profile=os_profile,
            show_ui=True,
            default_name=default_name,
        )
        # Write config now so bootstrap finds it — bootstrap skips this when
        # pre_wizard_result is set.
        _write_first_boot_config(
            config_dir,
            user_name=wizard_result["user_name"],
            language=wizard_result["language"],
            web_engine=wizard_result["web_engine"],
            stt=wizard_result["stt"],
            tts=wizard_result["tts"],
        )
        _term_clear()

    # ── Model selection ────────────────────────────────────────────────────────
    # Run interactively if no saved choice; otherwise just load it.
    # Vision selection needs the LLMOption so we always resolve chosen_model.
    if needs_model:
        chosen_model = _run_interactive_selection(os_profile, config_dir)
        _term_clear()
    else:
        chosen_model = _ms_load(config_dir)

    # ── Vision selection ───────────────────────────────────────────────────────
    # Must run after model selection (depends on model's vision_capable flag).
    # Runs interactively here; bootstrap's Step 7.5 finds the saved choice and
    # skips prompting.
    if needs_vision and chosen_model is not None:
        get_or_select_vision(
            chosen_model,
            os_profile,
            config_dir=config_dir,
            show_ui=True,
            force_reselect=False,
        )
        _term_clear()

    return wizard_result  # None if only model/vision selection was needed


# ═══════════════════════════════════════════════════════════
# bootstrap()  — PUBLIC ENTRY POINT
# ═══════════════════════════════════════════════════════════


def bootstrap(
    options: Optional[BootstrapOptions] = None,
    progress_cb: Optional[Callable[[str, str], None]] = None,
) -> BootstrapState:
    now_iso = _utc_now_iso()
    runtime = _load_runtime_config()

    fs = runtime["fs"]
    buddy_cfg = runtime["buddy"]
    tools_cfg = runtime["tools"]
    boot_cfg = runtime["bootstrap"]
    llama_cfg = runtime["llama"]
    embedder_model: str = runtime["embedder_model"]
    reranker_model: str = runtime["reranker_model"]

    # Typed path references
    config_dir: Path = fs["config_dir"]
    data_dir: Path = fs["data_dir"]
    assets_dir: Path = fs["assets_dir"]
    db_path: Path = fs["db_path"]
    models_dir: Path = fs["models_dir"]
    prompts_dir: Path = fs["prompts_dir"]
    prompts_lock_file: Path = fs["prompts_lock_file"]
    os_profile_file: Path = fs["os_profile_file"]
    os_profile_lock_file: Path = fs["os_profile_lock_file"]
    boot_report_file: Path = fs["boot_report_file"]
    llama_server_log: Path = fs["llama_server_log"]

    # Only map fields that live in boot_cfg (toml-derived).
    # Fields like pre_wizard_result have no toml key and must default to None.
    opts = options or BootstrapOptions(
        **{k: boot_cfg[k] for k in BootstrapOptions.__dataclass_fields__ if k in boot_cfg}
    )

    # ── Progress callback helper (Textual BootScreen integration) ────────────
    def _pcb(msg: str, status: str = "running") -> None:
        if progress_cb:
            try:
                progress_cb(msg, status)
            except Exception:
                pass

    # When running under Textual (progress_cb provided):
    #  · shadow _ui_* so messages reach the BootLog
    #  · disable tqdm / HF progress bars (they write to stdout Textual owns)
    if progress_cb:
        os.environ.setdefault("TQDM_DISABLE", "1")
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

        def _ui_ok(msg: str) -> None:  # noqa: F811
            _pcb(msg, "ok")

        def _ui_warn(msg: str) -> None:  # noqa: F811
            _pcb(msg, "warn")

        def _ui_fail(msg: str) -> None:  # noqa: F811
            _pcb(msg, "fail")

        def _ui_info(msg: str) -> None:  # noqa: F811
            _pcb(msg, "running")

    integrity = BootstrapIntegrity()
    installed: List[str] = []

    # ── STEP 1 · Animation ───────────────────────────────────────────────────
    first_boot = _is_first_boot(os_profile_file)
    if opts.show_boot_ui:
        if first_boot:
            _birth_animation()
        _matrix_stream_reveal(duration=4)
        _term_clear()

    # ── STEP 2 · First-boot name prompt (legacy — full wizard runs at Step 6.6) ──
    # Fires ONCE, after matrix reveal so the terminal is clean.
    # On every subsequent boot this block is completely skipped.
    # When pre_wizard_result is provided the prompt already ran pre-Textual.
    user_preferred_name: Optional[str] = None

    if _is_first_boot(os_profile_file):
        if opts.pre_wizard_result is not None:
            # run_pre_textual_setup() already collected the name — no input() here.
            user_preferred_name = opts.pre_wizard_result.get("user_name") or (
                os.environ.get("USER") or os.environ.get("USERNAME") or "boss"
            )
        elif opts.show_boot_ui:
            # Name is collected once in the wizard (Step 6.6, Q1) — skip here.
            user_preferred_name = (
                os.environ.get("USER") or os.environ.get("USERNAME") or "boss"
            )
        else:
            # Headless first boot — use OS username as the default
            user_preferred_name = (
                os.environ.get("USER") or os.environ.get("USERNAME") or "boss"
            )

    # ── STEP 3 · Boot header ──────────────────────────────────────────────────
    if opts.show_boot_ui:
        print(_c("\n  ◈  Buddy Cognitive System  ·  Initializing…\n", "bright_cyan"))

    # ── STEP 4 · Python dependencies ──────────────────────────────────────────
    _pcb("Checking Python dependencies")
    sp = _ui_step(opts.show_boot_ui, "Checking Python dependencies")
    missing, present = _check_imports(_REQUIRED_IMPORTS)
    sp.stop()

    if missing:
        _ui_warn(f"Missing required: {missing}")
        if opts.auto_install:
            sp = _ui_step(opts.show_boot_ui, "Installing missing dependencies")
            ok, _ = _pip_install(missing)
            sp.stop()
            if ok:
                installed.extend(missing)
                _ui_ok(f"Installed: {missing}")
            else:
                _ui_fail("Auto-install failed")
                integrity.violations += [
                    "dep_auto_install_failed",
                    f"missing_required:{missing}",
                ]
        else:
            integrity.violations.append(f"missing_required:{missing}")
            _ui_fail("Missing required libs (auto-install disabled)")
    else:
        _ui_ok("Required imports OK")

    for import_name, pip_name in _OPTIONAL_DEPS:
        try:
            __import__(import_name)
        except ImportError:
            if opts.auto_install:
                ok, _ = _pip_install([pip_name])
                (
                    installed.append(pip_name)
                    if ok
                    else integrity.warnings.append(
                        f"optional_install_failed:{pip_name}"
                    )
                )
            else:
                integrity.warnings.append(f"optional_missing:{import_name}")

    # ── STEP 5 · SQLite DB ────────────────────────────────────────────────────
    _pcb("Preparing SQLite database")
    sp = _ui_step(opts.show_boot_ui, "Preparing SQLite database")
    try:
        _ensure_db(db_path)
        sp.stop()
        _ui_ok(f"DB ready: {db_path.name}")
    except Exception as ex:
        sp.stop()
        _ui_fail(f"DB failed: {ex}")
        integrity.violations.append(f"db_failed:{ex!r}")

    # ── STEP 6 · OS profile refresh (every boot) ──────────────────────────────
    _pcb("Collecting system profile")
    sp = _ui_step(opts.show_boot_ui, "Collecting system profile")
    os_profile = _ensure_os_profile(
        integrity,
        os_profile_file=os_profile_file,
        assets_dir=assets_dir,
        data_dir=data_dir,
        user_preferred_name=user_preferred_name,
    )
    if opts.verify_os_profile_lock:
        _refresh_profile_lock(
            os_profile_file=os_profile_file, lock_file=os_profile_lock_file
        )
    sp.stop()

    gpu_info = os_profile.get("gpu", {}) or {}
    ram_info = os_profile.get("ram", {}) or {}
    cpu_info = os_profile.get("cpu", {}) or {}
    ram_gb = ram_info.get("total_gb", "?")
    cores = cpu_info.get("logical_cores", "?")
    gpu_lbl = f"{gpu_info.get('backend', '?')} / {gpu_info.get('name', '?')}"
    pref_name = str(os_profile.get("user_preferred_name") or "").strip() or "friend"

    _ui_ok(f"System: {cores} cores · {ram_gb} GB RAM · {gpu_lbl}")

    # ── STEP 6.5 · llama-server binary ───────────────────────────────────────
    _root_dir = _runtime_root()
    _bin_dir = _root_dir / "bin"
    _state_dir = _root_dir / "state"
    _llama_bin = ensure_llama_binary(
        _bin_dir,
        on_progress=lambda m, _d: _ui_info(m),
    )
    if _llama_bin:
        _new_path = str(_llama_bin.parent) + os.pathsep + os.environ.get("PATH", "")
        os.environ["PATH"] = _new_path

    # ── STEP 6.6 · First-boot wizard ─────────────────────────────────────────
    _wizard_result: Optional[Dict[str, Any]] = None
    if first_boot:
        if opts.pre_wizard_result is not None:
            # run_pre_textual_setup() already ran the wizard and wrote the config.
            # Re-use the collected results without prompting again.
            _wizard_result = opts.pre_wizard_result
            user_preferred_name = _wizard_result.get("user_name") or user_preferred_name
            os_profile["user_preferred_name"] = user_preferred_name
            pref_name = user_preferred_name or pref_name
            _pcb(f"First-boot setup: welcome, {pref_name}!", "ok")
        else:
            if opts.show_boot_ui:
                _term_clear()
            _wizard_result = _run_first_boot_wizard(
                os_profile=os_profile,
                show_ui=opts.show_boot_ui,
                default_name=user_preferred_name
                or (os.environ.get("USER") or os.environ.get("USERNAME") or "boss"),
            )
            user_preferred_name = _wizard_result["user_name"]
            os_profile["user_preferred_name"] = user_preferred_name
            pref_name = user_preferred_name
            _write_first_boot_config(
                config_dir,
                user_name=_wizard_result["user_name"],
                language=_wizard_result["language"],
                web_engine=_wizard_result["web_engine"],
                stt=_wizard_result["stt"],
                tts=_wizard_result["tts"],
            )
            if opts.show_boot_ui:
                _term_clear()
        # Re-run OS profile write with confirmed name (always — captures name from wizard)
        _ensure_os_profile(
            integrity,
            os_profile_file=os_profile_file,
            assets_dir=assets_dir,
            data_dir=data_dir,
            user_preferred_name=user_preferred_name,
        )

    # Resolve web / voice settings from wizard or config
    _feat_cfg = _sec(buddy_cfg, "features")
    _web_cfg = _sec(buddy_cfg, "web_search")
    if _wizard_result:
        _web_engine = _wizard_result["web_engine"]
        _stt = _wizard_result["stt"]
        _tts = _wizard_result["tts"]
    else:
        _web_engine = _as_str(_web_cfg.get("engine"), "duckduckgo")
        _stt = _as_bool(_feat_cfg.get("enable_audio_stt"), False)
        _tts = _as_bool(_feat_cfg.get("enable_audio_tts"), False)

    # ── STEP 7 · LLM model selection ─────────────────────────────────────────
    _pcb("Selecting LLM model")
    # Spinner stopped before this — interactive prompt may print to terminal.
    chosen = get_or_select_llm_model(
        os_profile,
        config_dir=config_dir,
        show_ui=opts.show_boot_ui,
        force_reselect=opts.force_model_reselect,
    )
    # [model_choice] is the single source of truth for the LLM filename.
    # [llama].model_gguf is a manual escape hatch only — leave it empty in normal use.
    # Priority: model_choice (chosen) > manual [llama].model_gguf override > hardware auto-pick
    _manual_override = (llama_cfg.get("model_gguf") or "").strip()
    if chosen.filename:
        # Selection succeeded (interactive or auto-recommended) — use it
        if _manual_override and _manual_override != chosen.filename:
            _pcb(f"[llama].model_gguf override: {_manual_override}", "warn")
            chosen.filename = _manual_override
            chosen.hf_filename = _manual_override
        llama_cfg["model_gguf"] = chosen.filename
        llama_cfg["model_name"] = chosen.filename
    elif _manual_override:
        # No selection result but a manual override is set — respect it
        chosen.filename = _manual_override
        chosen.hf_filename = _manual_override
        llama_cfg["model_gguf"] = _manual_override
        llama_cfg["model_name"] = _manual_override
        _pcb(f"Using manual model override: {_manual_override}", "warn")

    # ── STEP 7.5 · Vision capability selection ───────────────────────────────
    _vision_cfg = _sec(buddy_cfg, "vision")
    _force_vision = _as_bool(
        _vision_cfg.get("force_vision_reselect"),
        opts.force_vision_reselect,
    )
    vision_choice: VisionChoice = get_or_select_vision(
        chosen,
        os_profile,
        config_dir=config_dir,
        show_ui=opts.show_boot_ui,
        force_reselect=_force_vision,
    )
    if vision_choice.enabled:
        _ui_ok(
            f"Vision: enabled  ·  model {vision_choice.model_quant}"
            f"  ·  mmproj {vision_choice.mmproj_quant}"
        )
        # Update hf_filename in chosen if user picked a different quant
        if vision_choice.model_hf_filename:
            chosen.hf_filename = vision_choice.model_hf_filename
            chosen.filename = vision_choice.model_hf_filename
            llama_cfg["model_gguf"] = vision_choice.model_hf_filename
    else:
        if getattr(chosen, "vision_capable", False):
            _ui_ok("Vision: disabled (text-only mode)")

    # ── STEP 8 · LLM GGUF download ───────────────────────────────────────────
    _size_lbl = f"{chosen.size_gb:.1f} GB" if chosen.size_gb else ""
    _pcb(f"LLM model: {chosen.label}  {_size_lbl}".strip(), "running")
    if not _gguf_present(models_dir, chosen.filename):
        _pcb(f"Downloading {chosen.filename} — this may take a while…", "running")
    llm_status = _ensure_llm_gguf(
        integrity,
        chosen=chosen,
        models_dir=models_dir,
        download_enabled=opts.download_models,
    )
    if llm_status.get("ok"):
        _pcb(f"LLM ready: {chosen.filename}", "ok")
    else:
        _pcb(f"LLM model not available: {chosen.filename}", "fail")

    # ── STEP 8.5 · mmproj download (vision only) ─────────────────────────────
    mmproj_path: Optional[Path] = None
    if vision_choice.enabled:
        sp = _ui_step(opts.show_boot_ui, "Checking mmproj (vision encoder)")
        mmproj_path = _ensure_mmproj(
            vision_choice,
            models_dir=models_dir,
            download_enabled=opts.download_models,
        )
        sp.stop()
        if mmproj_path:
            _ui_ok(f"mmproj ready: {mmproj_path.name}")
            # Persist resolved path back to vision config
            _vision_cfg["mmproj_path"] = str(mmproj_path)
        else:
            _ui_warn("mmproj not available — vision disabled for this session")
            vision_choice = VisionChoice(enabled=False)

    # ── STEP 9 · ST model downloads (tqdm suppressed under Textual) ──────────
    _pcb("Checking sentence-transformer models", "running")
    _ui_info("Checking sentence-transformer models…")
    if not _st_present(embedder_model, data_dir):
        _pcb(f"Downloading embedder: {embedder_model}…", "running")
    embedder_path = _ensure_st_model(
        embedder_model,
        data_dir=data_dir,
        integrity=integrity,
        label="embedder",
        download_enabled=opts.download_models,
    )
    _pcb(f"Embedder {'ready' if embedder_path else 'unavailable'}: {embedder_model}",
         "ok" if embedder_path else "warn")
    if not _st_present(reranker_model, data_dir):
        _pcb(f"Downloading reranker: {reranker_model}…", "running")
    reranker_path = _ensure_st_model(
        reranker_model,
        data_dir=data_dir,
        integrity=integrity,
        label="reranker",
        download_enabled=opts.download_models,
    )
    _pcb(f"Reranker {'ready' if reranker_path else 'unavailable'}: {reranker_model}",
         "ok" if reranker_path else "warn")

    # ── STEP 10 · Set env vars BEFORE first instantiation ────────────────────
    # EmbeddingProvider singleton reads BUDDY_EMBED_MODEL on first __new__().
    # VectorStore reads BUDDY_RERANKER_MODEL when it initialises its reranker.
    # We set both here, before any import of those classes happens.
    os.environ["BUDDY_EMBED_MODEL"] = (
        str(embedder_path) if embedder_path else embedder_model
    )
    os.environ["BUDDY_RERANKER_MODEL"] = (
        str(reranker_path) if reranker_path else reranker_model
    )
    logger.info("BUDDY_EMBED_MODEL=%s", os.environ["BUDDY_EMBED_MODEL"])
    logger.info("BUDDY_RERANKER_MODEL=%s", os.environ["BUDDY_RERANKER_MODEL"])

    # ── STEP 11 · Prompt integrity ────────────────────────────────────────────
    _pcb("Verifying prompt integrity")
    sp = _ui_step(opts.show_boot_ui, "Verifying prompt integrity")
    if opts.verify_prompts_lock:
        _verify_prompts_lock(
            integrity,
            strict=opts.strict_integrity,
            prompts_dir=prompts_dir,
            prompts_lock_file=prompts_lock_file,
        )
    sp.stop()
    (_ui_ok if integrity.prompts_lock_ok else _ui_fail)(
        "Prompts OK" if integrity.prompts_lock_ok else "Prompts FAILED"
    )

    # ── STEP 11.5 · SearXNG (if configured) ──────────────────────────────────
    _searxng_started = False
    _searxng_dir = _root_dir / "searxng"
    _searxng_host = "127.0.0.1"
    _searxng_port = int(
        _sec(buddy_cfg, "web_search")
        .get("searxng_url", "http://127.0.0.1:8888")
        .split(":")[-1]
        .split("/")[0]
        if ":" in str(_sec(buddy_cfg, "web_search").get("searxng_url", ""))
        else 8888
    )
    _searxng_scan_ports: List[int] = [
        int(p)
        for p in (
            _sec(buddy_cfg, "web_search").get("searxng_scan_ports")
            or [8888, 8889, 8890, 4000, 5000]
        )
        if str(p).isdigit()
    ]

    if _web_engine == "searxng":
        # ── Port scan: detect already-running SearXNG ─────────
        sp = _ui_step(opts.show_boot_ui, "Scanning for existing SearXNG")
        _found_searxng_port = _scan_searxng_ports(_searxng_host, _searxng_scan_ports)
        sp.stop()
        if _found_searxng_port and _found_searxng_port != _searxng_port:
            _searxng_port = _found_searxng_port
            _searxng_url = f"http://{_searxng_host}:{_searxng_port}"
            _ui_ok(
                f"SearXNG found on alternate port {_found_searxng_port}: {_searxng_url}"
            )
            _searxng_started = True
        elif _found_searxng_port == _searxng_port:
            _searxng_started = True  # already on configured port

        if not _searxng_started:
            sp = _ui_step(opts.show_boot_ui, "Starting SearXNG")
            if not _searxng_installed(_searxng_dir):
                sp.stop()
                _ui_info("SearXNG not installed — running setup (first time only)...")

                def _ask_python() -> bool:
                    return _wizard_yn(
                        "SearXNG needs Python 3.8+ but none was found on this system.\n"
                        "  Download a bundled Python runtime now? (~30 MB)",
                        default=True,
                    )

                _ok_setup = setup_searxng(
                    _searxng_dir,
                    port=_searxng_port,
                    python_dir=_root_dir / "python",
                    ask_install_python=_ask_python,
                    on_progress=lambda m, _d: _ui_info(m),
                )
                if not _ok_setup:
                    _ui_warn("SearXNG setup failed — falling back to DuckDuckGo")
                    _web_engine = "duckduckgo"
                sp = _ui_step(opts.show_boot_ui, "Starting SearXNG")

            if _web_engine == "searxng":
                _searxng_started = start_searxng(
                    _searxng_dir,
                    _state_dir,
                    port=_searxng_port,
                    on_progress=lambda m, _d: _ui_info(m),
                )
                sp.stop()
                if _searxng_started:
                    _ui_ok(f"SearXNG online: http://{_searxng_host}:{_searxng_port}")
                else:
                    _ui_warn("SearXNG failed to start — falling back to DuckDuckGo")
                    _web_engine = "duckduckgo"
            else:
                sp.stop()
        else:
            _ui_ok(f"SearXNG READY: http://{_searxng_host}:{_searxng_port}")

    # ── STEP 12 · llama-server ────────────────────────────────────────────────
    _pcb("Starting llama-server")
    _proc_ref: List[Optional[subprocess.Popen]] = [None]
    _owned_ref: List[bool] = [False]

    host = llama_cfg["host"]
    port = int(llama_cfg["port"])
    timeout = float(llama_cfg["ready_timeout_s"])

    # ── Port scan: detect already-running llama-server ────────────────────────
    _llama_scan_ports: List[int] = [
        int(p)
        for p in (llama_cfg.get("scan_ports") or [8080, 8081, 8082, 8088, 8888, 11434])
        if str(p).isdigit()
    ]
    # Ensure configured port is first in the list
    if port not in _llama_scan_ports:
        _llama_scan_ports = [port] + _llama_scan_ports

    sp = _ui_step(opts.show_boot_ui, "Scanning for existing llama-server")
    _found_port = _scan_llama_ports(host, _llama_scan_ports)
    sp.stop()
    if _found_port and _found_port != port:
        _ui_ok(f"llama-server found on alternate port {_found_port} — using it")
        port = _found_port
        llama_cfg["port"] = port

    base_url = f"http://{host}:{port}"
    llama_cfg["base_url"] = base_url

    llama_info: Dict[str, Any] = {
        "base_url": base_url,
        "host": host,
        "port": port,
        "model_gguf": llama_cfg["model_gguf"],
        "started": False,
        "pid": None,
        "owned_by_buddy": False,
        "listening": False,
        "http_online": False,
        "ready": False,
        "status": "unknown",
        "cmd": [],
        "log_file": str(llama_server_log),
    }

    pre = _probe(base_url, host=host, port=port)
    llama_info.update(pre)

    if pre["listening"]:
        if pre["ready"]:
            _ui_ok(f"llama-server READY (external): {base_url}")
            llama_info["status"] = "ready"
        else:
            sp = _ui_step(opts.show_boot_ui, "Waiting for llama-server")
            ok, status = _wait_external(
                base_url=base_url,
                host=host,
                port=port,
                timeout_s=timeout,
                show_wait=lambda e, p: logger.info("llama wait %.1fs: %s", e, p),
            )
            sp.stop()
            llama_info.update(_probe(base_url, host=host, port=port))
            llama_info["status"] = status
            (_ui_ok if ok else _ui_fail)(
                f"llama-server {'READY' if ok else 'NOT READY'}"
            )
            if not ok:
                integrity.violations.append(f"llama_not_ready:{status}")
    else:
        model_path = _find_gguf(llama_cfg["model_gguf"], data_dir=data_dir)
        if model_path is None:
            _ui_fail(f"GGUF not found: {llama_cfg['model_gguf']}")
            integrity.violations.append(
                f"llama_gguf_not_found:{llama_cfg['model_gguf']}"
            )
        else:
            srv_args = _flatten(llama_cfg.get("server_args"))

            # ── Dynamic context budget ────────────────────────────
            try:
                from buddy.buddy_core.context_budget import ContextBudget

                os_profile_data = {}
                try:
                    import json as _json

                    _op_path = paths.get("os_profile")
                    if _op_path and Path(_op_path).exists():
                        with open(_op_path, "r", encoding="utf-8") as _f:
                            os_profile_data = _json.load(_f)
                except Exception:
                    pass

                _budget = ContextBudget.from_hardware(os_profile_data)

                # Apply toml override if configured
                _cb_cfg = buddy_cfg.get("context_budget") or {}
                _budget = ContextBudget.from_override(_budget, _cb_cfg)

                # Replace --ctx-size in server_args with computed n_ctx
                srv_args = _inject_ctx_size(srv_args, _budget.n_ctx)
                state.context_budget = _budget
                _ui_ok(
                    f"Context budget: {_budget.tier_label} "
                    f"n_ctx={_budget.n_ctx} turns={_budget.recent_turns} "
                    f"top_k={_budget.top_k_memories}"
                )
            except Exception as _cb_err:
                logger.warning(
                    "context_budget failed, using toml server_args: %r", _cb_err
                )
            # ─────────────────────────────────────────────────────

            clean, removed = _sanitize_args(srv_args)
            if removed:
                integrity.warnings.append(f"server_args_removed:{removed}")

            cmd = [
                "llama-server",
                "-m",
                str(model_path),
                "--host",
                str(host),
                "--port",
                str(int(port)),
                *clean,
            ]

            # ── Inject --mmproj when vision is enabled ────────────────
            if vision_choice.enabled and mmproj_path is not None:
                cmd.extend(["--mmproj", str(mmproj_path)])
                logger.info("Vision: injecting --mmproj %s", mmproj_path)
            # ─────────────────────────────────────────────────────────

            sp = _ui_step(opts.show_boot_ui, "Starting llama-server")
            proc, cmd = _spawn(cmd=cmd, log=llama_server_log)
            sp.stop()

            _proc_ref[0] = proc
            _owned_ref[0] = proc is not None
            llama_info.update({
                "cmd": cmd,
                "started": proc is not None,
                "pid": proc.pid if proc else None,
                "owned_by_buddy": proc is not None,
            })

            if proc is None:
                _ui_fail("Failed to spawn llama-server")
                integrity.violations.append("llama_spawn_failed")
            else:
                sp = _ui_step(
                    opts.show_boot_ui, "Waiting for llama-server to become READY"
                )
                ok, status = _wait_spawned(
                    proc=proc,
                    base_url=base_url,
                    host=host,
                    port=port,
                    timeout_s=timeout,
                    show_wait=lambda e, p: logger.info("llama boot %.1fs: %s", e, p),
                )
                sp.stop()
                llama_info.update(_probe(base_url, host=host, port=port))
                llama_info["status"] = status

                if ok:
                    _ui_ok(f"llama-server READY: {base_url}")
                else:
                    _ui_fail(f"llama-server NOT READY: {status}")
                    integrity.violations.append(f"llama_not_ready:{status}")
                    _terminate(proc)
                    _proc_ref[0] = None
                    _owned_ref[0] = False
                    llama_info.update({"owned_by_buddy": False, "pid": None})

            if opts.show_boot_ui and cmd:
                pretty = _pretty_cmd(cmd)
                msg = (
                    ["", _center_visible(_c("LLAMA SERVER FLAGS", "cyan"), 72), ""]
                    + pretty
                    + [""]
                )
                print(_color_frame(_frame(msg, 72)))

    llama_info["ready"] = bool(llama_info.get("ready"))
    if not llama_info["ready"]:
        integrity.violations.append("llama_not_ready_blocking_core_init")

    # ── Shutdown hook ─────────────────────────────────────────────────────────
    def _shutdown() -> None:
        proc = _proc_ref[0]
        owned = _owned_ref[0]
        if proc and owned:
            logger.info("Shutting down llama-server pid=%s", proc.pid)
            _terminate(proc)
            _proc_ref[0] = None
            _owned_ref[0] = False
        if _searxng_started:
            logger.info("Shutting down SearXNG")
            stop_searxng(_state_dir)

    # ── STEP 13 · Core objects ────────────────────────────────────────────────
    _pcb("Creating core objects")
    if llama_info["ready"]:
        sp = _ui_step(opts.show_boot_ui, "Creating core objects")
        artifacts = _create_artifacts(
            db_path,
            integrity,
            llama_model=llama_cfg["model_name"],
            llama_base_url=base_url,
            os_profile=os_profile,
            config={"buddy": buddy_cfg, "tools": tools_cfg, "runtime": runtime},
        )
        sp.stop()
        missing_comps = artifacts.validate()
        if missing_comps:
            msg = f"missing_components:{missing_comps}"
            (
                integrity.violations if opts.strict_integrity else integrity.warnings
            ).append(msg)
            _ui_warn(f"Missing components: {missing_comps}")
        else:
            _ui_ok("Core objects ready")
    else:
        artifacts = BootstrapArtifacts()

    # ── STEP 14 · Strict enforcement ──────────────────────────────────────────
    _pcb("Checking integrity")
    if opts.strict_integrity and integrity.violations:
        _shutdown()
        _ui_fail("Bootstrap failed — see violations below")
        for v in integrity.violations:
            print(_c(f"    ✗  {v}", "bright_red"))
        raise RuntimeError(f"Buddy bootstrap failed. violations={integrity.violations}")

    # ── STEP 15 · Boot report (only on success) ───────────────────────────────
    _pcb("Writing boot report")
    report = {
        "version": 2,
        "generated_at": now_iso,
        "integrity": {
            "prompts_lock_ok": integrity.prompts_lock_ok,
            "os_profile_ok": integrity.os_profile_ok,
            "violations": integrity.violations,
            "warnings": integrity.warnings,
            "tainted": integrity.tainted,
        },
        "deps": {"present": present, "missing": missing, "auto_installed": installed},
        "models": {
            "llm": llm_status,
            "embedder": {
                "hf_model": embedder_model,
                "local_path": str(embedder_path) if embedder_path else None,
            },
            "reranker": {
                "hf_model": reranker_model,
                "local_path": str(reranker_path) if reranker_path else None,
            },
            "chosen": {
                "filename": chosen.filename,
                "label": chosen.label,
                "tier": chosen.tier.value,
            },
        },
        "llama": llama_info,
        "os_summary": {
            "username": os_profile.get("username"),
            "preferred_name": os_profile.get("user_preferred_name"),
            "platform": os_profile.get("platform", {}).get("system"),
            "cpu_model": os_profile.get("cpu", {}).get("model"),
            "logical_cores": os_profile.get("cpu", {}).get("logical_cores"),
            "ram_gb": os_profile.get("ram", {}).get("total_gb"),
            "gpu_backend": os_profile.get("gpu", {}).get("backend"),
            "gpu_name": os_profile.get("gpu", {}).get("name"),
            "disk_free_gb": os_profile.get("disk", {}).get("free_gb"),
        },
    }

    if opts.write_boot_report:
        try:
            _write_json(boot_report_file, report)
            logger.info("Boot report written: %s", boot_report_file)
        except Exception as ex:
            integrity.warnings.append(f"boot_report_write_failed:{ex!r}")

    # ── STEP 16 · Online banner — live server data ──────────────────────────
    _pcb("Buddy ready", "ok")
    if opts.show_boot_ui and llama_info.get("ready"):
        _term_clear()

        # Query the running server for its actual runtime state.
        # This reflects what the server is *actually* loaded with — correct
        # even when an external server was already running with a different
        # model or flags before Buddy started.
        srv = _fetch_llama_server_props(base_url)

        # Model: prefer the GGUF filename the server loaded from disk (/props),
        # fall back to the /v1/models id, last resort the config value.
        model_display = (
            srv.get("model_file") or srv.get("model_id") or chosen.filename or "unknown"
        )
        if model_display in ("", "—"):
            model_display = chosen.filename

        # VRAM — append to gpu label if available
        vram_gb = gpu_info.get("total_vram_gb")
        vram_str = f"  ·  {vram_gb} GB VRAM" if vram_gb else ""
        gpu_banner = f"{gpu_lbl}{vram_str}"

        # Print the aurora gradient banner centred in the live terminal width.
        print_banner_centered(
            user_name=pref_name,
            gpu_label=gpu_banner,
            ram_gb=str(ram_gb),
            llm_label=f"llama.cpp  ·  {model_display}",
            web_engine=_web_engine,
            stt=_stt,
            tts=_tts,
        )

        # ── Live server detail block ──────────────────────────────────────
        # Rows are built from what the server actually reported.
        # Any field the server did not expose is omitted so the block stays
        # clean regardless of llama.cpp version.
        # detail_rows = [
        #     ("endpoint", base_url),
        #     ("model", model_display),
        #     (
        #         "context",
        #         f"{srv['n_ctx']} tokens" if srv.get("n_ctx", "—") != "—" else "—",
        #     ),
        #     ("gpu layers", srv.get("n_gpu_layers", "—")),
        #     ("threads", srv.get("n_threads", "—")),
        #     ("batch", srv.get("n_batch", "—")),
        #     ("kv cache", srv.get("kv_cache", "—")),
        #     ("flash attn", srv.get("flash_attn", "—")),
        #     ("slots", srv.get("n_slots", "—")),
        # ]
        # build = srv.get("build", "—")
        # if build and build != "—":
        #     detail_rows.append(("backend", build))

        # # Drop rows the server did not populate
        # detail_rows = [(k, v) for k, v in detail_rows if v and v != "—"]

        # if detail_rows:
        #     cols, _ = _term_size()
        #     label_w = max(len(k) for k, _ in detail_rows)
        #     inner = min(cols - 4, 80)
        #     indent = " " * max(0, (cols - inner) // 2)
        #     sep = indent + _c("─" * inner, "dim")

        #     print()
        #     print(sep)
        #     print(indent + _c("  LLAMA SERVER  ·  LIVE CONFIGURATION", "dim"))
        #     print(sep)
        #     for key, val in detail_rows:
        #         key_str = _raw_c(_logo_row_code(0), f"  {key:<{label_w}}  ")
        #         val_str = _c(val, "white")
        #         print(indent + key_str + val_str)
        #     print(sep)
        #     print()

    return BootstrapState(
        project_root=str(PROJECT_ROOT),
        package_root=str(PACKAGE_ROOT),
        integrity=integrity,
        artifacts=artifacts,
        config={"buddy": buddy_cfg, "tools": tools_cfg, "runtime": runtime},
        shutdown=_shutdown,
    )


if __name__ == "__main__":
    st = bootstrap()
    print("tainted:", st.integrity.tainted)
    if st.integrity.warnings:
        print("warnings:", st.integrity.warnings)
