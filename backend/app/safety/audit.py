"""Audit trail for every mutating call.

Written twice per order: once as "attempt" before the broker is touched, once as
"result" after. A crash mid-flight therefore still leaves evidence that the attempt
happened, which a single post-hoc write would lose.

Two sinks, both cheap: a daily JSONL file for grepping, and a SQLite table for
querying. Failures to audit never block a trade - they are logged and swallowed,
because losing the log is better than losing the ability to square off a position.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_audit (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    phase TEXT NOT NULL,
    tool TEXT NOT NULL,
    session_id TEXT,
    run_id TEXT,
    args TEXT,
    analyzer_mode TEXT,
    risk_verdict TEXT,
    risk_reason TEXT,
    ok INTEGER,
    response TEXT,
    order_ids TEXT
);
CREATE INDEX IF NOT EXISTS idx_order_audit_ts ON order_audit(ts);
CREATE INDEX IF NOT EXISTS idx_order_audit_session ON order_audit(session_id);
"""


class AuditLog:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.audit_dir: Path = self.settings.audit_dir
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.db_path: Path = self.settings.db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.executescript(_SCHEMA)
        except Exception:  # noqa: BLE001
            log.warning("audit db init failed", exc_info=True)

    def _jsonl_path(self) -> Path:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.audit_dir / f"orders-{day}.jsonl"

    def record(self, phase: str, tool: str, args: dict[str, Any],
               session_id: str | None = None, run_id: str | None = None,
               analyzer_mode: str | None = None,
               risk_verdict: str | None = None, risk_reason: str | None = None,
               ok: bool | None = None, response: Any = None) -> str:
        """Append one audit entry. Returns its id. Never raises."""
        entry_id = uuid.uuid4().hex
        ts = datetime.now(timezone.utc).isoformat()
        order_ids = _extract_order_ids(response)

        row = {
            "id": entry_id, "ts": ts, "phase": phase, "tool": tool,
            "session_id": session_id, "run_id": run_id,
            "args": args, "analyzer_mode": analyzer_mode,
            "risk_verdict": risk_verdict, "risk_reason": risk_reason,
            "ok": ok, "response": response, "order_ids": order_ids,
        }

        with self._lock:
            try:
                with self._jsonl_path().open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
            except Exception:  # noqa: BLE001
                log.warning("audit jsonl write failed", exc_info=True)

            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute(
                        "INSERT INTO order_audit (id, ts, phase, tool, session_id, run_id,"
                        " args, analyzer_mode, risk_verdict, risk_reason, ok, response,"
                        " order_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (entry_id, ts, phase, tool, session_id, run_id,
                         json.dumps(args, default=str), analyzer_mode,
                         risk_verdict, risk_reason,
                         None if ok is None else int(ok),
                         json.dumps(response, default=str)[:8000],
                         json.dumps(order_ids)),
                    )
            except Exception:  # noqa: BLE001
                log.warning("audit db write failed", exc_info=True)

        log.info("audit %s tool=%s mode=%s verdict=%s ok=%s",
                 phase, tool, analyzer_mode, risk_verdict, ok)
        return entry_id

    def recent(self, limit: int = 50, session_id: str | None = None) -> list[dict]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                if session_id:
                    cur = conn.execute(
                        "SELECT * FROM order_audit WHERE session_id = ?"
                        " ORDER BY ts DESC LIMIT ?", (session_id, limit))
                else:
                    cur = conn.execute(
                        "SELECT * FROM order_audit ORDER BY ts DESC LIMIT ?", (limit,))
                return [dict(r) for r in cur.fetchall()]
        except Exception:  # noqa: BLE001
            log.warning("audit read failed", exc_info=True)
            return []

    def count_since(self, iso_ts: str, session_id: str | None = None) -> int:
        try:
            with sqlite3.connect(self.db_path) as conn:
                if session_id:
                    cur = conn.execute(
                        "SELECT COUNT(*) FROM order_audit WHERE ts >= ? AND phase = 'result'"
                        " AND ok = 1 AND session_id = ?", (iso_ts, session_id))
                else:
                    cur = conn.execute(
                        "SELECT COUNT(*) FROM order_audit WHERE ts >= ? AND phase = 'result'"
                        " AND ok = 1", (iso_ts,))
                return int(cur.fetchone()[0])
        except Exception:  # noqa: BLE001
            return 0


def _extract_order_ids(response: Any) -> list[str]:
    """Pull order ids out of any of OpenAlgo's write-response shapes."""
    found: list[str] = []
    if not isinstance(response, dict):
        return found
    data = response.get("data", response)
    if isinstance(data, dict):
        for key in ("orderid", "order_id", "trigger_id"):
            if data.get(key):
                found.append(str(data[key]))
        for item in data.get("results", []) or []:
            if isinstance(item, dict) and item.get("orderid"):
                found.append(str(item["orderid"]))
    return found


_audit: AuditLog | None = None
_audit_lock = threading.Lock()


def get_audit_log() -> AuditLog:
    global _audit
    if _audit is None:
        with _audit_lock:
            if _audit is None:
                _audit = AuditLog()
    return _audit
