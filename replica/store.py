import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from replica.catalog import PRODUCTS, TRIAL_SMART, FREE_CHEAP_PER_DAY, WINDOW_DAY, WINDOW_PERIOD, WINDOW_WEEK, WINDOW_CYCLE

SCHEMA_VERSION = 1
DEFAULT_PLAN = "free"

USERS_COLUMNS = (
    ("status", "TEXT NOT NULL DEFAULT 'active'"),
    ("chat_model", "TEXT"),
    ("terms_version", "TEXT"),
    ("terms_accepted_at", "INTEGER"),
    ("locale", "TEXT"),
)
USAGE_COLUMNS = (
    ("cost_usd", "REAL NOT NULL DEFAULT 0"),
    ("prompt_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("completion_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("idem_key", "TEXT"),
    ("bucket_id", "INTEGER"),
    ("window_start", "INTEGER"),
    ("status", "TEXT NOT NULL DEFAULT 'committed'"),
    ("model_id", "TEXT"),
    ("reasoning_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("finish_reason", "TEXT"),
    ("latency_ms", "INTEGER"),
    ("error_code", "TEXT"),
    ("generation_id", "TEXT"),
)
TABLES = (
    """CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        plan TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS invoices (
        invoice_id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        product_code TEXT NOT NULL,
        stars INTEGER NOT NULL,
        payload TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        plan TEXT NOT NULL,
        invoice_payload TEXT NOT NULL UNIQUE,
        first_charge_id TEXT UNIQUE,
        status TEXT NOT NULL,
        auto_renew INTEGER NOT NULL DEFAULT 1,
        period_start INTEGER NOT NULL,
        period_end INTEGER NOT NULL,
        reminder_sent INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_payment_charge_id TEXT NOT NULL UNIQUE,
        user_id INTEGER NOT NULL,
        invoice_id TEXT,
        subscription_id INTEGER,
        product_code TEXT,
        stars INTEGER NOT NULL,
        is_recurring INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        raw_json TEXT,
        received_at INTEGER NOT NULL,
        refunded_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS subscription_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subscription_id INTEGER,
        event TEXT NOT NULL,
        source TEXT NOT NULL,
        ref TEXT NOT NULL,
        raw_json TEXT,
        created_at INTEGER NOT NULL,
        UNIQUE(source, ref)
    )""",
    """CREATE TABLE IF NOT EXISTS entitlements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        plan TEXT NOT NULL,
        source TEXT NOT NULL,
        source_id INTEGER,
        payment_id TEXT,
        kind TEXT NOT NULL,
        window TEXT NOT NULL,
        quota INTEGER NOT NULL,
        valid_from INTEGER NOT NULL,
        valid_until INTEGER,
        status TEXT NOT NULL DEFAULT 'active',
        created_at INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        entitlement_id INTEGER NOT NULL,
        delta INTEGER NOT NULL,
        reason TEXT NOT NULL,
        ref_type TEXT NOT NULL,
        ref_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        UNIQUE(reason, ref_type, ref_id, entitlement_id)
    )""",
    """CREATE TABLE IF NOT EXISTS answers (
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        thread_json TEXT NOT NULL,
        mode TEXT,
        pending_text TEXT,
        truncated INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        PRIMARY KEY(chat_id, message_id)
    )""",
    """CREATE TABLE IF NOT EXISTS admin_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        target_user_id INTEGER,
        payload_json TEXT,
        created_at INTEGER NOT NULL
    )""",
)
INDEXES = (
    "CREATE INDEX IF NOT EXISTS usage_user_kind_time ON usage(user_id, kind, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS usage_idem ON usage(idem_key) WHERE idem_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS usage_bucket_window ON usage(bucket_id, window_start, status)",
    "CREATE INDEX IF NOT EXISTS usage_status_time ON usage(status, created_at)",
    "CREATE INDEX IF NOT EXISTS turns_user_order ON turns(user_id, id)",
    "CREATE INDEX IF NOT EXISTS invoices_user ON invoices(user_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS subscriptions_user ON subscriptions(user_id, status)",
    "CREATE UNIQUE INDEX IF NOT EXISTS subscriptions_one_active ON subscriptions(user_id) WHERE status = 'active'",
    "CREATE INDEX IF NOT EXISTS payments_user ON payments(user_id, received_at)",
    "CREATE INDEX IF NOT EXISTS entitlements_user_kind ON entitlements(user_id, kind, status)",
    "CREATE INDEX IF NOT EXISTS entitlements_payment ON entitlements(payment_id)",
    "CREATE INDEX IF NOT EXISTS answers_created ON answers(created_at)",
)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._depth = 0
        self._migrate()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._depth:
                yield self._db
                return
            self._db.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            finally:
                self._depth = 0

    def close(self) -> None:
        self._db.close()

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def _migrate(self) -> None:
        with self.transaction():
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            for statement in TABLES:
                self._db.execute(statement)
            self._add_missing_columns("users", USERS_COLUMNS)
            self._add_missing_columns("usage", USAGE_COLUMNS)
            for statement in INDEXES:
                self._db.execute(statement)
            if version < SCHEMA_VERSION:
                self._migrate_legacy_plans()
                self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _add_missing_columns(self, table: str, columns: tuple[tuple[str, str], ...]) -> None:
        existing = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in existing:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    def _migrate_legacy_plans(self) -> None:
        now = int(time.time())
        rows = self._db.execute(
            "SELECT user_id, plan FROM users WHERE plan IN ('start', 'pro')"
        ).fetchall()
        for user_id, plan in rows:
            self._grant_admin_plan(user_id, plan, now, days=None)

    def _grant_admin_plan(self, user_id: int, plan: str, now: int, days: int | None) -> None:
        product = PRODUCTS[plan]
        until = None if days is None else now + days * 86400
        self.add_entitlement(
            user_id=user_id, plan=plan, source="admin", source_id=None, kind="cheap",
            window=WINDOW_WEEK, quota=product.cheap, valid_from=now, valid_until=until,
            payment_id=None, now=now,
        )
        self.add_entitlement(
            user_id=user_id, plan=plan, source="admin", source_id=None, kind="smart",
            window=WINDOW_PERIOD if until is not None else WINDOW_CYCLE, quota=product.smart,
            valid_from=now, valid_until=until, payment_id=None, now=now,
        )

    # users

    def ensure_user(self, user_id: int, now: int) -> None:
        with self.transaction():
            if self._one("SELECT 1 AS found FROM users WHERE user_id = ?", (user_id,)) is None:
                self._db.execute(
                    "INSERT INTO users(user_id, plan, created_at, status) VALUES (?, ?, ?, 'active')",
                    (user_id, DEFAULT_PLAN, now),
                )
            if self._one("SELECT 1 AS found FROM entitlements WHERE user_id = ? AND source = 'free'", (user_id,)) is None:
                self.add_entitlement(
                    user_id=user_id, plan="free", source="free", source_id=None, kind="cheap",
                    window=WINDOW_DAY, quota=FREE_CHEAP_PER_DAY, valid_from=now, valid_until=None,
                    payment_id=None, now=now,
                )
            if self._one("SELECT 1 AS found FROM entitlements WHERE user_id = ? AND source = 'trial'", (user_id,)) is None:
                self.add_entitlement(
                    user_id=user_id, plan="trial", source="trial", source_id=None, kind="smart",
                    window=WINDOW_PERIOD, quota=TRIAL_SMART, valid_from=now, valid_until=None,
                    payment_id=None, now=now,
                )

    def user_status(self, user_id: int) -> str:
        row = self._one("SELECT status FROM users WHERE user_id = ?", (user_id,))
        return row["status"] if row else "active"

    def set_user_status(self, user_id: int, status: str, now: int) -> None:
        with self.transaction():
            self.ensure_user(user_id, now)
            self._exec("UPDATE users SET status = ? WHERE user_id = ?", (status, user_id))

    def chat_model(self, user_id: int) -> str | None:
        row = self._one("SELECT chat_model FROM users WHERE user_id = ?", (user_id,))
        return row["chat_model"] if row else None

    def set_chat_model(self, user_id: int, slug: str, now: int) -> None:
        with self.transaction():
            self.ensure_user(user_id, now)
            self._exec("UPDATE users SET chat_model = ? WHERE user_id = ?", (slug, user_id))

    def terms_version(self, user_id: int) -> str | None:
        row = self._one("SELECT terms_version FROM users WHERE user_id = ?", (user_id,))
        return row["terms_version"] if row else None

    def accept_terms(self, user_id: int, version: str, now: int) -> None:
        with self.transaction():
            self.ensure_user(user_id, now)
            self._exec(
                "UPDATE users SET terms_version = ?, terms_accepted_at = ? WHERE user_id = ?",
                (version, now, user_id),
            )

    # entitlements and ledger

    def add_entitlement(
        self, *, user_id: int, plan: str, source: str, source_id: int | None, kind: str, window: str,
        quota: int, valid_from: int, valid_until: int | None, payment_id: str | None, now: int,
    ) -> int:
        with self.transaction():
            cursor = self._exec(
                "INSERT INTO entitlements(user_id, plan, source, source_id, payment_id, kind, window, quota, "
                "valid_from, valid_until, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (user_id, plan, source, source_id, payment_id, kind, window, quota, valid_from, valid_until, now),
            )
            entitlement_id = int(cursor.lastrowid)
            self._ledger(user_id, entitlement_id, quota, "grant", "entitlement", str(entitlement_id), now)
            return entitlement_id

    def _ledger(self, user_id: int, entitlement_id: int, delta: int, reason: str, ref_type: str, ref_id: str, now: int, actor: str = "system") -> None:
        self._exec(
            "INSERT OR IGNORE INTO ledger(user_id, entitlement_id, delta, reason, ref_type, ref_id, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, entitlement_id, delta, reason, ref_type, ref_id, actor, now),
        )

    def active_entitlements(self, user_id: int, kind: str | None, now: int) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM entitlements WHERE user_id = ? AND status = 'active' AND valid_from <= ? "
            "AND (valid_until IS NULL OR valid_until > ?)"
        )
        args: list[Any] = [user_id, now, now]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        return self._all(sql, tuple(args))

    def entitlement_ids(
        self, *, user_id: int | None = None, source: str | None = None,
        source_id: int | None = None, payment_id: str | None = None,
    ) -> list[int]:
        clauses = ["status = 'active'"]
        args: list[Any] = []
        for column, value in (("user_id", user_id), ("source", source), ("source_id", source_id), ("payment_id", payment_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                args.append(value)
        rows = self._all(f"SELECT id FROM entitlements WHERE {' AND '.join(clauses)}", tuple(args))
        return [row["id"] for row in rows]

    def revoke_entitlements(self, ids: list[int], reason: str, ref_type: str, ref_id: str, now: int, actor: str = "system") -> None:
        with self.transaction():
            for entitlement_id in ids:
                row = self._one("SELECT user_id, quota FROM entitlements WHERE id = ?", (entitlement_id,))
                self._exec("UPDATE entitlements SET status = 'revoked' WHERE id = ?", (entitlement_id,))
                self._ledger(row["user_id"], entitlement_id, -row["quota"], reason, ref_type, ref_id, now, actor)

    def expire_entitlements(self, now: int) -> list[int]:
        with self.transaction():
            rows = self._all(
                "SELECT id, user_id, quota FROM entitlements WHERE status = 'active' AND valid_until IS NOT NULL AND valid_until <= ?",
                (now,),
            )
            for row in rows:
                self._exec("UPDATE entitlements SET status = 'expired' WHERE id = ?", (row["id"],))
                self._ledger(row["user_id"], row["id"], -row["quota"], "expire", "entitlement", str(row["id"]), now)
            return [row["id"] for row in rows]

    # usage (reservations)

    def usage_by_key(self, key: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM usage WHERE idem_key = ?", (key,))

    def usage_reserve(self, *, user_id: int, kind: str, key: str, bucket_id: int, window_start: int, now: int) -> int:
        cursor = self._exec(
            "INSERT INTO usage(user_id, kind, created_at, idem_key, bucket_id, window_start, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'reserved')",
            (user_id, kind, now, key, bucket_id, window_start),
        )
        return int(cursor.lastrowid)

    def usage_count(self, bucket_id: int, window_start: int) -> int:
        row = self._one(
            "SELECT COUNT(*) AS total FROM usage WHERE bucket_id = ? AND window_start = ? "
            "AND status IN ('reserved', 'committed')",
            (bucket_id, window_start),
        )
        return int(row["total"])

    def usage_commit(self, key: str, *, cost: float, prompt: int, completion: int, reasoning: int,
                     model_id: str, finish_reason: str | None, latency_ms: int, generation_id: str | None) -> None:
        self._exec(
            "UPDATE usage SET status = 'committed', cost_usd = ?, prompt_tokens = ?, completion_tokens = ?, "
            "reasoning_tokens = ?, model_id = ?, finish_reason = ?, latency_ms = ?, generation_id = ? "
            "WHERE idem_key = ? AND status = 'reserved'",
            (cost, prompt, completion, reasoning, model_id, finish_reason, latency_ms, generation_id, key),
        )

    def usage_release(self, key: str, error_code: str) -> None:
        self._exec(
            "UPDATE usage SET status = 'released', idem_key = NULL, error_code = ? WHERE idem_key = ? AND status = 'reserved'",
            (error_code, key),
        )

    def usage_release_stale(self, cutoff: int) -> int:
        return self._exec(
            "UPDATE usage SET status = 'released', idem_key = NULL, error_code = 'stale' "
            "WHERE status = 'reserved' AND created_at < ?",
            (cutoff,),
        ).rowcount

    def usage_totals(self, user_id: int) -> tuple[int, float, int, int]:
        row = self._one(
            "SELECT COUNT(*) AS total, COALESCE(SUM(cost_usd), 0) AS cost, COALESCE(SUM(prompt_tokens), 0) AS prompt, "
            "COALESCE(SUM(completion_tokens), 0) AS completion FROM usage WHERE user_id = ? AND status = 'committed'",
            (user_id,),
        )
        return int(row["total"]), float(row["cost"]), int(row["prompt"]), int(row["completion"])

    # turns

    def add_turn(self, user_id: int, role: str, content: str, at: int) -> None:
        self._exec(
            "INSERT INTO turns(user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content, at),
        )

    def recent_turns(self, user_id: int, limit: int) -> list[dict[str, str]]:
        rows = self._all(
            "SELECT role, content FROM turns WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    def recent_questions(self, user_id: int, limit: int) -> list[str]:
        rows = self._all(
            "SELECT content FROM turns WHERE user_id = ? AND role = 'user' ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [row["content"] for row in rows]

    def clear_turns(self, user_id: int) -> None:
        self._exec("DELETE FROM turns WHERE user_id = ?", (user_id,))

    # answers

    def save_answer(self, *, chat_id: int, message_id: int, user_id: int, thread: list[dict[str, str]],
                    mode: str | None, pending_text: str | None, truncated: bool, now: int) -> None:
        self._exec(
            "INSERT OR REPLACE INTO answers(chat_id, message_id, user_id, thread_json, mode, pending_text, truncated, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, message_id, user_id, json.dumps(thread, ensure_ascii=False), mode, pending_text, int(truncated), now),
        )

    def get_answer(self, chat_id: int, message_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM answers WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
        if row is None:
            return None
        row["thread"] = json.loads(row["thread_json"])
        return row

    def prune_answers(self, cutoff: int) -> None:
        self._exec("DELETE FROM answers WHERE created_at < ?", (cutoff,))

    # invoices

    def invoice_create(self, *, invoice_id: str, user_id: int, product: str, stars: int, payload: str,
                       now: int, expires_at: int) -> None:
        self._exec(
            "INSERT INTO invoices(invoice_id, user_id, product_code, stars, payload, status, created_at, updated_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, 'created', ?, ?, ?)",
            (invoice_id, user_id, product, stars, payload, now, now, expires_at),
        )

    def invoice_by_payload(self, payload: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM invoices WHERE payload = ?", (payload,))

    def invoice_set_status(self, invoice_id: str, status: str, now: int) -> None:
        self._exec("UPDATE invoices SET status = ?, updated_at = ? WHERE invoice_id = ?", (status, now, invoice_id))

    def invoice_prechecked_since(self, user_id: int, since: int, exclude_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM invoices WHERE user_id = ? AND status = 'prechecked' AND updated_at >= ? AND invoice_id != ?",
            (user_id, since, exclude_id),
        )

    def invoices_expire(self, now: int) -> None:
        self._exec(
            "UPDATE invoices SET status = 'expired', updated_at = ? WHERE status = 'created' AND expires_at <= ?",
            (now, now),
        )

    # payments

    def payment_get(self, charge_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM payments WHERE telegram_payment_charge_id = ?", (charge_id,))

    def payment_add(self, *, charge_id: str, user_id: int, invoice_id: str | None, product: str | None,
                    stars: int, recurring: bool, source: str, status: str, subscription_id: int | None,
                    raw_json: str, now: int) -> bool:
        cursor = self._exec(
            "INSERT OR IGNORE INTO payments(telegram_payment_charge_id, user_id, invoice_id, subscription_id, product_code, "
            "stars, is_recurring, source, status, raw_json, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (charge_id, user_id, invoice_id, subscription_id, product, stars, int(recurring), source, status, raw_json, now),
        )
        return cursor.rowcount == 1

    def payment_set_status(self, charge_id: str, status: str, now: int | None = None) -> None:
        self._exec(
            "UPDATE payments SET status = ?, refunded_at = COALESCE(?, refunded_at) WHERE telegram_payment_charge_id = ?",
            (status, now if status == "refunded" else None, charge_id),
        )

    def payments_recent(self, user_id: int, limit: int) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)
        )

    def payments_since(self, since: int) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM payments WHERE received_at >= ?", (since,))

    # subscriptions

    def sub_active(self, user_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM subscriptions WHERE user_id = ? AND status = 'active'", (user_id,))

    def sub_latest(self, user_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM subscriptions WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))

    def sub_by_payload(self, payload: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM subscriptions WHERE invoice_payload = ?", (payload,))

    def sub_by_charge(self, charge_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM subscriptions WHERE first_charge_id = ?", (charge_id,))

    def sub_get(self, sub_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM subscriptions WHERE id = ?", (sub_id,))

    def sub_create(self, *, user_id: int, plan: str, payload: str, first_charge: str,
                   start: int, end: int, now: int) -> int:
        cursor = self._exec(
            "INSERT INTO subscriptions(user_id, plan, invoice_payload, first_charge_id, status, auto_renew, "
            "period_start, period_end, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', 1, ?, ?, ?, ?)",
            (user_id, plan, payload, first_charge, start, end, now, now),
        )
        return int(cursor.lastrowid)

    _SUB_FIELDS = ("status", "auto_renew", "period_start", "period_end", "reminder_sent")

    def sub_update(self, sub_id: int, now: int, **fields: Any) -> None:
        unknown = set(fields) - set(self._SUB_FIELDS)
        if unknown:
            raise ValueError(f"unknown subscription fields: {sorted(unknown)}")
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self._exec(
            f"UPDATE subscriptions SET {assignments}, updated_at = ? WHERE id = ?",
            (*fields.values(), now, sub_id),
        )

    def subs_past_end(self, cutoff: int) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM subscriptions WHERE status = 'active' AND period_end < ?", (cutoff,))

    def subs_reminder_due(self, now: int, until: int) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM subscriptions WHERE status = 'active' AND auto_renew = 0 AND reminder_sent = 0 "
            "AND period_end > ? AND period_end <= ?",
            (now, until),
        )

    def event_add(self, sub_id: int | None, event: str, source: str, ref: str, raw_json: str, now: int) -> bool:
        cursor = self._exec(
            "INSERT OR IGNORE INTO subscription_events(subscription_id, event, source, ref, raw_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sub_id, event, source, ref, raw_json, now),
        )
        return cursor.rowcount == 1

    # users for admin and metrics

    def user_ids(self) -> list[int]:
        return [row["user_id"] for row in self._all("SELECT user_id FROM users")]

    def audit_add(self, admin_id: int, action: str, target: int | None, payload: dict[str, Any], now: int) -> None:
        self._exec(
            "INSERT INTO admin_audit(admin_id, action, target_user_id, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (admin_id, action, target, json.dumps(payload, ensure_ascii=False), now),
        )

    def cost_by_model(self, since: int) -> list[dict[str, Any]]:
        return self._all(
            "SELECT model_id, COUNT(*) AS requests, COALESCE(SUM(cost_usd), 0) AS cost FROM usage "
            "WHERE status = 'committed' AND created_at >= ? GROUP BY model_id",
            (since,),
        )

    def cost_by_user(self, since: int) -> list[dict[str, Any]]:
        return self._all(
            "SELECT user_id, COUNT(*) AS requests, COALESCE(SUM(cost_usd), 0) AS cost FROM usage "
            "WHERE status = 'committed' AND created_at >= ? GROUP BY user_id",
            (since,),
        )
