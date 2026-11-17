"""SQLite storage.

mentions: open (unsolved) problems only. Solved ones are deleted.
closed:   just the ids of recently closed messages, so a solved problem can't
          come back as "new" through a late sync. Pruned after 30 days.
"""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "mentions.db"
CLOSED_KEEP_SECONDS = 30 * 86400

_conn: sqlite3.Connection | None = None


def init() -> None:
    global _conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mentions (
            id          INTEGER PRIMARY KEY,
            chat_id     INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            date        INTEGER NOT NULL,
            text        TEXT,
            chat_title  TEXT,
            sender      TEXT,
            link        TEXT,
            is_read     INTEGER NOT NULL DEFAULT 0,
            bot_msg_id  INTEGER,
            jump_msg_id INTEGER,
            UNIQUE (chat_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS closed (
            chat_id     INTEGER NOT NULL,
            message_id  INTEGER NOT NULL,
            closed_at   INTEGER NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        );
        CREATE TABLE IF NOT EXISTS meta (
            key         TEXT PRIMARY KEY,
            value       TEXT
        );
        """
    )
    if "jump_msg_id" not in {r["name"] for r in _conn.execute("PRAGMA table_info(mentions)")}:
        _conn.execute("ALTER TABLE mentions ADD COLUMN jump_msg_id INTEGER")  # databases from older versions
    _conn.execute("DELETE FROM closed WHERE closed_at < ?", (int(time.time()) - CLOSED_KEEP_SECONDS,))


def add(chat_id: int, message_id: int, date: int, text: str, is_read: bool = False) -> int | None:
    """Insert a mention. Returns the new row id, or None if it's already tracked or was closed."""
    if _conn.execute(
        "SELECT 1 FROM closed WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
    ).fetchone():
        return None
    cur = _conn.execute(
        "INSERT OR IGNORE INTO mentions (chat_id, message_id, date, text, is_read) VALUES (?, ?, ?, ?, ?)",
        (chat_id, message_id, date, text, int(is_read)),
    )
    return cur.lastrowid if cur.rowcount else None


def get_meta(key: str) -> str | None:
    row = _conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    _conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))


def set_details(row_id: int, chat_title: str, sender: str, link: str | None) -> None:
    _conn.execute(
        "UPDATE mentions SET chat_title = ?, sender = ?, link = ? WHERE id = ?",
        (chat_title, sender, link, row_id),
    )


def set_text(row_id: int, text: str) -> None:
    _conn.execute("UPDATE mentions SET text = ? WHERE id = ?", (text, row_id))


def set_bot_msg_id(row_id: int, bot_msg_id: int) -> None:
    _conn.execute("UPDATE mentions SET bot_msg_id = ? WHERE id = ?", (bot_msg_id, row_id))


def set_jump_msg_id(row_id: int, jump_msg_id: int | None) -> None:
    _conn.execute("UPDATE mentions SET jump_msg_id = ? WHERE id = ?", (jump_msg_id, row_id))


def mark_read(row_id: int) -> bool:
    """Returns True if the row changed from unread to read."""
    cur = _conn.execute("UPDATE mentions SET is_read = 1 WHERE id = ? AND is_read = 0", (row_id,))
    return cur.rowcount > 0


def close(row_id: int) -> None:
    """Delete an open problem, remembering its message id."""
    row = get(row_id)
    if row is None:
        return
    _conn.execute(
        "INSERT OR REPLACE INTO closed (chat_id, message_id, closed_at) VALUES (?, ?, ?)",
        (row["chat_id"], row["message_id"], int(time.time())),
    )
    _conn.execute("DELETE FROM mentions WHERE id = ?", (row_id,))


def get(row_id: int) -> sqlite3.Row | None:
    return _conn.execute("SELECT * FROM mentions WHERE id = ?", (row_id,)).fetchone()


def get_by_message(chat_id: int, message_id: int) -> sqlite3.Row | None:
    return _conn.execute(
        "SELECT * FROM mentions WHERE chat_id = ? AND message_id = ?", (chat_id, message_id)
    ).fetchone()


def all_open() -> list[sqlite3.Row]:
    return _conn.execute("SELECT * FROM mentions ORDER BY date").fetchall()


def open_in_chat(chat_id: int) -> list[sqlite3.Row]:
    return _conn.execute("SELECT * FROM mentions WHERE chat_id = ?", (chat_id,)).fetchall()


def unsent() -> list[sqlite3.Row]:
    return _conn.execute("SELECT * FROM mentions WHERE bot_msg_id IS NULL ORDER BY date").fetchall()
