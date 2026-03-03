from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal

from buddy.logger.logger import get_logger

logger = get_logger("conversations")

Role = Literal["User", "Buddy"]


# ==========================================================
# Data model
# ==========================================================


@dataclass(frozen=True)
class ConversationMessage:
    role: Role
    time: str  # ISO-8601 (prefer Z)
    text: str


# ==========================================================
# Conversations (RAM + snapshot)
# ==========================================================


class Conversations:
    """
    RAM-only conversation buffer with crash-safe snapshotting (time-based).

    - Stores last N messages (max_turns == max_messages)
    - Bidirectional: User/Buddy can speak in any order
    - Tracks a single pending message (the last message awaiting a reply)
    - Automatically loads snapshot on construction (if snapshot_path provided)
    - Automatically saves snapshot after any mutation (if snapshot_path provided)

    Pending rule:
    - If the last message in the buffer is unanswered (no opposite-role reply yet),
      it is treated as PENDING in the formatted output.
    """

    def __init__(
        self,
        max_turns: int = 20,
        snapshot_path: Optional[str] = None,
        use_utc: bool = True,
        autosave: bool = True,
    ):
        if max_turns <= 0:
            raise ValueError("max_turns must be > 0")

        self.max_turns = max_turns
        self.use_utc = use_utc
        self.autosave = autosave

        self.snapshot_path: Optional[str] = snapshot_path
        self._snapshot_file: Optional[Path] = self._normalize_snapshot_path(
            snapshot_path
        )

        self._messages: List[ConversationMessage] = []

        if self._snapshot_file is not None:
            loaded = self.load_snapshot()
            logger.info(
                "Conversations init: snapshot=%s loaded=%s messages=%d",
                str(self._snapshot_file),
                loaded,
                len(self._messages),
            )
        else:
            logger.info("Conversations init: no snapshot_path; starting empty.")

    # ==========================================================
    # Snapshot path
    # ==========================================================

    def _normalize_snapshot_path(self, snapshot_path: Optional[str]) -> Optional[Path]:
        if not snapshot_path:
            return None
        try:
            return Path(snapshot_path).expanduser()
        except Exception as e:
            logger.warning(
                "Invalid snapshot_path=%r (%s). Snapshot disabled.", snapshot_path, e
            )
            return None

    # ==========================================================
    # Time helpers
    # ==========================================================

    def _now_iso(self) -> str:
        now = datetime.now(timezone.utc) if self.use_utc else datetime.now()
        return now.isoformat(timespec="seconds").replace("+00:00", "Z")

    # ==========================================================
    # Pruning
    # ==========================================================

    def _prune(self) -> None:
        overflow = len(self._messages) - self.max_turns
        if overflow > 0:
            del self._messages[:overflow]

    # ==========================================================
    # Autosave
    # ==========================================================

    def _autosave_snapshot(self) -> None:
        if self.autosave and self._snapshot_file is not None:
            self.save_snapshot()

    # ==========================================================
    # Public add APIs
    # ==========================================================

    def add_user(self, text: str) -> None:
        self._add_message(role="User", text=text)

    def add_buddy(self, text: str) -> None:
        self._add_message(role="Buddy", text=text)

    def _add_message(self, role: Role, text: str) -> None:
        msg = (text or "").strip()
        ts = self._now_iso()

        self._messages.append(ConversationMessage(role=role, time=ts, text=msg))
        self._prune()
        self._autosave_snapshot()

    # ==========================================================
    # Pending detection
    # ==========================================================

    def _pending_index(self) -> Optional[int]:
        """
        Returns the index of the pending message (if any).

        Definition:
        - A message is pending if it is the last message AND there is no later opposite-role reply.
          With a pure timeline, that means: the last message is always pending unless you decide
          to treat "pending" only when Buddy asked a question.

        Practical rule (recommended):
        - Pending exists if the last message is from Buddy (Buddy is waiting for user),
          OR if you explicitly want "pending always" then return last index.
        """
        if not self._messages:
            return None

        # Recommended: only Buddy-waiting is "pending"
        last = self._messages[-1]
        if last.role == "Buddy":
            return len(self._messages) - 1
        return None

    # ==========================================================
    # Read (LLM-friendly timeline)
    # ==========================================================

    def get_recent_conversations(self, include_pending: bool = False) -> str:
        """
        Returns the conversation as a pure message timeline:

        [time] User: ...
        [time] Buddy: ...

        If include_pending=True and a pending message exists, it is shown at the end under:
        --- PENDING ---
        """
        if not self._messages:
            return ""

        pending_i = self._pending_index() if include_pending else None

        lines: List[str] = []

        if pending_i is None:
            # no pending section, print everything
            for m in self._messages:
                lines.append(f"[{m.time}] {m.role}: {m.text}")
            return "\n".join(lines).rstrip()

        # Print everything BEFORE pending
        for m in self._messages[:pending_i]:
            lines.append(f"[{m.time}] {m.role}: {m.text}")

        # Pending section
        lines.append("--- PENDING ---")
        pm = self._messages[pending_i]
        lines.append(f"[{pm.time}] {pm.role}: {pm.text}")

        return "\n".join(lines).rstrip()

    # ==========================================================
    # Snapshot (STRICT v1)
    # ==========================================================

    def save_snapshot(self) -> None:
        if self._snapshot_file is None:
            return

        p = self._snapshot_file
        try:
            p.parent.mkdir(parents=True, exist_ok=True)

            payload: Dict[str, Any] = {
                "version": 1,  # time-based message log + PENDING rendering at read-time
                "messages": [
                    {"role": m.role, "time": m.time, "text": m.text}
                    for m in self._messages
                ],
            }

            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(p)

            logger.debug(
                "Snapshot saved: %s (messages=%d)", str(p), len(self._messages)
            )
        except Exception as e:
            logger.warning("Snapshot save failed: path=%s error=%s", str(p), e)

    def load_snapshot(self) -> bool:
        if self._snapshot_file is None:
            return False

        p = self._snapshot_file
        tmp = p.with_suffix(p.suffix + ".tmp")

        candidate = p if p.exists() else (tmp if tmp.exists() else None)
        if candidate is None or not candidate.exists():
            logger.info("Snapshot not found: %s", str(p))
            return False

        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(
                "Snapshot load failed (bad JSON): path=%s error=%s", str(candidate), e
            )
            return False

        if payload.get("version") != 1:
            logger.warning(
                "Snapshot version mismatch: expected=1 got=%r — snapshot ignored",
                payload.get("version"),
            )
            return False

        try:
            restored: List[ConversationMessage] = []

            for obj in payload.get("messages", []):
                role = obj.get("role")
                t = str(obj.get("time", "")).strip()
                text = str(obj.get("text", "")).strip()

                if role not in ("User", "Buddy"):
                    continue
                if not t:
                    continue

                restored.append(ConversationMessage(role=role, time=t, text=text))

            self._messages = restored[-self.max_turns :]

            logger.info(
                "Snapshot loaded: %s (messages=%d)", str(candidate), len(self._messages)
            )
            return True
        except Exception as e:
            logger.warning(
                "Snapshot load failed (unexpected): path=%s error=%s", str(candidate), e
            )
            return False

    # ==========================================================
    # Utility
    # ==========================================================

    def clear(self) -> None:
        self._messages.clear()
        self._autosave_snapshot()

    def __len__(self) -> int:
        return len(self._messages)
