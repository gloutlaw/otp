import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path

OPEN = "open"
CLOSED = "closed"
DIRECT_SOURCE = "direct"
UNKNOWN_SOURCE = "не указан"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    closed_at INTEGER
);
CREATE INDEX IF NOT EXISTS tickets_user_status ON tickets(user_id, status);
CREATE TABLE IF NOT EXISTS relay (
    owner_message_id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bans (
    user_id INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rate (
    user_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_user_time ON rate(user_id, created_at);
CREATE TABLE IF NOT EXISTS visitors (
    user_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""

TICKET_COLUMNS = "id, user_id, status, source, created_at, closed_at"


@dataclass(frozen=True)
class Ticket:
    id: int
    user_id: int
    status: str
    source: str
    created_at: int
    closed_at: int | None


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def open_ticket_for(self, user_id: int) -> Ticket | None:
        row = self._db.execute(
            f"SELECT {TICKET_COLUMNS} FROM tickets WHERE user_id = ? AND status = ? ORDER BY id DESC LIMIT 1",
            (user_id, OPEN),
        ).fetchone()
        return _ticket(row) if row else None

    def create_ticket(self, user_id: int, source: str, now: int) -> Ticket:
        cursor = self._db.execute(
            "INSERT INTO tickets(user_id, status, source, created_at) VALUES (?, ?, ?, ?)",
            (user_id, OPEN, source, now),
        )
        self._db.commit()
        return Ticket(cursor.lastrowid, user_id, OPEN, source, now, None)

    def close_open_ticket(self, user_id: int, now: int) -> Ticket | None:
        ticket = self.open_ticket_for(user_id)
        if ticket is None:
            return None
        self._db.execute(
            "UPDATE tickets SET status = ?, closed_at = ? WHERE id = ?",
            (CLOSED, now, ticket.id),
        )
        self._db.commit()
        return replace(ticket, status=CLOSED, closed_at=now)

    def list_open(self) -> list[Ticket]:
        rows = self._db.execute(
            f"SELECT {TICKET_COLUMNS} FROM tickets WHERE status = ? ORDER BY id",
            (OPEN,),
        ).fetchall()
        return [_ticket(row) for row in rows]

    # Replies in the operator chat point at message ids there, so routing is keyed by them.
    def add_relay(self, owner_message_id: int, ticket_id: int, user_id: int) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO relay(owner_message_id, ticket_id, user_id) VALUES (?, ?, ?)",
            (owner_message_id, ticket_id, user_id),
        )
        self._db.commit()

    def route(self, owner_message_id: int) -> tuple[int, int] | None:
        row = self._db.execute(
            "SELECT ticket_id, user_id FROM relay WHERE owner_message_id = ?",
            (owner_message_id,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def ban(self, user_id: int, now: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO bans(user_id, created_at) VALUES (?, ?)",
            (user_id, now),
        )
        self._db.commit()

    def unban(self, user_id: int) -> bool:
        cursor = self._db.execute("DELETE FROM bans WHERE user_id = ?", (user_id,))
        self._db.commit()
        return cursor.rowcount > 0

    def is_banned(self, user_id: int) -> bool:
        return self._db.execute("SELECT 1 FROM bans WHERE user_id = ?", (user_id,)).fetchone() is not None

    # First source wins: a later plain /start must not erase the link the user came from.
    def remember_source(self, user_id: int, source: str, now: int) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO visitors(user_id, source, created_at) VALUES (?, ?, ?)",
            (user_id, source, now),
        )
        self._db.commit()

    def source_of(self, user_id: int) -> str:
        row = self._db.execute("SELECT source FROM visitors WHERE user_id = ?", (user_id,)).fetchone()
        return row[0] if row else UNKNOWN_SOURCE

    def take_rate_slot(self, user_id: int, now: int, limit: int, window_seconds: int) -> bool:
        self._db.execute(
            "DELETE FROM rate WHERE user_id = ? AND created_at <= ?",
            (user_id, now - window_seconds),
        )
        used = self._db.execute("SELECT COUNT(*) FROM rate WHERE user_id = ?", (user_id,)).fetchone()[0]
        allowed = used < limit
        if allowed:
            self._db.execute("INSERT INTO rate(user_id, created_at) VALUES (?, ?)", (user_id, now))
        self._db.commit()
        return allowed


def _ticket(row: tuple) -> Ticket:
    return Ticket(
        id=row[0],
        user_id=row[1],
        status=row[2],
        source=row[3],
        created_at=row[4],
        closed_at=row[5],
    )
