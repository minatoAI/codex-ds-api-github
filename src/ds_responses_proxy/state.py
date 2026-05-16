from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
import sqlite3
import time


@dataclass
class ResponseState:
    response_id: str
    messages: list[dict]
    created_at: float
    model: str | None = None


@dataclass
class ResponseStateStore:
    ttl_seconds: int = 86400
    max_entries: int = 1000
    _entries: dict[str, ResponseState] = field(default_factory=dict)

    def get(self, response_id: str) -> ResponseState | None:
        self.prune()
        state = self._entries.get(response_id)
        if state is None:
            return None
        return ResponseState(
            response_id=state.response_id,
            messages=deepcopy(state.messages),
            created_at=state.created_at,
            model=state.model,
        )

    def save(self, response_id: str, messages: list[dict], model: str | None = None) -> None:
        self._entries[response_id] = ResponseState(
            response_id=response_id,
            messages=deepcopy(messages),
            created_at=time.time(),
            model=model,
        )
        self.prune()

    def prune(self) -> None:
        now = time.time()
        expired = [
            key
            for key, state in self._entries.items()
            if now - state.created_at > self.ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)

        overflow = len(self._entries) - self.max_entries
        if overflow <= 0:
            return
        oldest = sorted(self._entries.values(), key=lambda state: state.created_at)
        for state in oldest[:overflow]:
            self._entries.pop(state.response_id, None)


class SQLiteResponseStateStore:
    def __init__(self, db_path: str, ttl_seconds: int = 604800, max_entries: int = 10000) -> None:
        self.db_path = Path(db_path)
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def get(self, response_id: str) -> ResponseState | None:
        self.prune()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT response_id, created_at, model, messages_json FROM response_states WHERE response_id = ?",
                (response_id,),
            ).fetchone()
        if row is None:
            return None
        return ResponseState(
            response_id=row["response_id"],
            messages=json.loads(row["messages_json"]),
            created_at=row["created_at"],
            model=row["model"],
        )

    def save(self, response_id: str, messages: list[dict], model: str | None = None) -> None:
        assistant_tool_call_count = sum(
            1
            for message in messages
            if message.get("role") == "assistant" and message.get("tool_calls")
        )
        reasoning_count = sum(
            1
            for message in messages
            if message.get("role") == "assistant" and message.get("reasoning_content")
        )
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO response_states (
                    response_id, created_at, model, messages_json,
                    assistant_tool_call_count, reasoning_content_count
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(response_id) DO UPDATE SET
                    created_at = excluded.created_at,
                    model = excluded.model,
                    messages_json = excluded.messages_json,
                    assistant_tool_call_count = excluded.assistant_tool_call_count,
                    reasoning_content_count = excluded.reasoning_content_count
                """,
                (
                    response_id,
                    now,
                    model,
                    json.dumps(messages, ensure_ascii=False),
                    assistant_tool_call_count,
                    reasoning_count,
                ),
            )
        self.prune()

    def prune(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        with self._connect() as conn:
            conn.execute("DELETE FROM response_states WHERE created_at < ?", (cutoff,))
            overflow = conn.execute("SELECT COUNT(*) FROM response_states").fetchone()[0] - self.max_entries
            if overflow > 0:
                conn.execute(
                    """
                    DELETE FROM response_states
                    WHERE response_id IN (
                        SELECT response_id FROM response_states
                        ORDER BY created_at ASC
                        LIMIT ?
                    )
                    """,
                    (overflow,),
                )

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS response_states (
                    response_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    model TEXT,
                    messages_json TEXT NOT NULL,
                    assistant_tool_call_count INTEGER NOT NULL DEFAULT 0,
                    reasoning_content_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_response_states_created_at ON response_states(created_at)")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn


def create_state_store(
    backend: str,
    *,
    db_path: str,
    ttl_seconds: int,
    max_entries: int,
) -> ResponseStateStore | SQLiteResponseStateStore:
    if backend == "memory":
        return ResponseStateStore(ttl_seconds=ttl_seconds, max_entries=max_entries)
    if backend == "sqlite":
        return SQLiteResponseStateStore(db_path=db_path, ttl_seconds=ttl_seconds, max_entries=max_entries)
    raise ValueError("STATE_BACKEND must be either 'memory' or 'sqlite'.")
