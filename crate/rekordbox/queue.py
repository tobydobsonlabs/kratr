"""Crash-safe staging for rekordbox changes.

The colour-page import files a track and writes its rekordbox changes as one continuous
user action. A persisted queue still separates those two technical phases: it prevents
a crash or partial database failure from losing work, and lets a retry resume the write
without filing the audio twice.
"""

from __future__ import annotations

import enum
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import config

logger = logging.getLogger(__name__)


class OpKind(enum.StrEnum):
    ADD_CONTENT = "add_content"
    RELOCATE = "relocate"
    SET_RELATIONS = "set_relations"
    ADD_TO_PLAYLIST = "add_to_playlist"
    SET_MYTAGS = "set_mytags"
    SET_COLOUR = "set_colour"
    SET_COMMENT = "set_comment"
    TAG_MANAGER = "tag_manager_change"
    LIBRARY_MANAGER = "library_manager_change"


class OpStatus(enum.StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    FAILED = "failed"


@dataclass(slots=True)
class Op:
    """One queued change."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    #: Human label, so the queue reads as tracks rather than opaque operations.
    label: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created: float = field(default_factory=time.time)
    status: str = OpStatus.PENDING
    error: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.status == OpStatus.PENDING


@dataclass(slots=True)
class ApplyResult:
    applied: list[Op] = field(default_factory=list)
    failed: list[Op] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        if self.ok:
            return f"Applied {len(self.applied)} change(s)."
        return (
            f"Applied {len(self.applied)} change(s); {len(self.failed)} failed. "
            f"The failures are still queued and can be retried."
        )


#: A handler takes (session, op) and does the work. Registered by the modules that
#: own each operation, so the queue stays ignorant of rekordbox specifics.
Handler = Callable[[Any, Op], None]


class OpQueue:
    """Persistent list of pending changes."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.QUEUE_PATH
        self.ops: list[Op] = []
        self._handlers: dict[str, Handler] = {}

    # ------------------------------------------------------------ persistence
    def load(self) -> "OpQueue":
        if not self.path.is_file():
            self.ops = []
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.ops = [Op(**item) for item in raw.get("ops", [])]
        except (OSError, json.JSONDecodeError, TypeError):
            logger.exception("Could not read the queue; starting empty")
            self.ops = []
        return self

    def save(self) -> None:
        config.ensure_dirs()
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"ops": [asdict(o) for o in self.ops]}, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    # ----------------------------------------------------------------- edits
    def add(self, kind: str, payload: dict[str, Any], label: str = "") -> Op:
        op = Op(kind=str(kind), payload=payload, label=label)
        self.ops.append(op)
        self.save()
        return op

    def remove(self, op_id: str) -> None:
        self.ops = [o for o in self.ops if o.id != op_id]
        self.save()

    def clear_applied(self) -> int:
        before = len(self.ops)
        self.ops = [o for o in self.ops if o.status != OpStatus.APPLIED]
        self.save()
        return before - len(self.ops)

    def clear(self) -> None:
        self.ops = []
        self.save()

    @property
    def pending(self) -> list[Op]:
        return [o for o in self.ops if o.is_pending]

    def pending_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for op in self.pending:
            counts[op.kind] = counts.get(op.kind, 0) + 1
        return counts

    # -------------------------------------------------------------- handlers
    def register(self, kind: str, handler: Handler) -> None:
        self._handlers[str(kind)] = handler

    def register_all(self, handlers: dict[str, Handler]) -> None:
        for kind, handler in handlers.items():
            self.register(kind, handler)

    # ----------------------------------------------------------------- apply
    def apply(self, session: Any, *, progress: Callable[[int, int, Op], None] | None = None) -> ApplyResult:
        """Run every pending op against an open session.

        One failing op does not abort the batch — it is recorded and left pending so
        it can be retried, while the rest still go through. Losing eleven good changes
        because the twelfth was broken would be the worse outcome.
        """
        result = ApplyResult()
        pending = self.pending
        total = len(pending)

        for index, op in enumerate(pending, start=1):
            if progress:
                progress(index, total, op)

            handler = self._handlers.get(op.kind)
            if handler is None:
                op.status = OpStatus.FAILED
                op.error = f"No handler registered for {op.kind!r}"
                logger.error("%s (%s)", op.error, op.label)
                result.failed.append(op)
                continue

            try:
                handler(session, op)
            except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
                op.status = OpStatus.FAILED
                op.error = f"{type(exc).__name__}: {exc}"
                logger.error("Op %s (%s) failed: %s", op.kind, op.label, exc, exc_info=True)
                result.failed.append(op)
            else:
                op.status = OpStatus.APPLIED
                op.error = None
                result.applied.append(op)

        # Failed ops go back to pending so a retry picks them up next time.
        for op in result.failed:
            op.status = OpStatus.PENDING

        self.save()
        logger.info(result.summary())
        return result
