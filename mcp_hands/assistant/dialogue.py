"""Память разговора Джарвиса: последние реплики в локальном SQLite.

Даёт «а теперь второй», «повтори», «объясни, что сделал» — старший мозг
получает историю (по умолчанию 8 реплик). Всё локально, в logs/assistant.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "logs" / "assistant" / "jarvis_memory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    role TEXT NOT NULL,        -- user | assistant
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT ''   -- local | smart (у ответов ассистента)
);
"""


class Dialogue:
    """Кольцевая история разговора; лимит хранимых реплик — с запасом."""

    def __init__(self, db_path: Path | str = DB_PATH, keep: int = 200) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.keep = keep

    def remember(self, role: str, text: str, kind: str = "") -> None:
        text = (text or "").strip()
        if not text:
            return
        self.conn.execute("INSERT INTO messages (ts, role, text, kind) VALUES (?,?,?,?)",
                          (time.time(), role, text[:2000], kind))
        self.conn.execute(
            "DELETE FROM messages WHERE id NOT IN "
            "(SELECT id FROM messages ORDER BY id DESC LIMIT ?)", (self.keep,))
        self.conn.commit()

    def history(self, limit: int = 8) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, text FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"role": row["role"], "content": row["text"]} for row in reversed(rows)]

    def clear(self) -> int:
        cursor = self.conn.execute("DELETE FROM messages")
        self.conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self.conn.close()
