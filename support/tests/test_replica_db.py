import sqlite3

from support.replica_db import plan_of


def make_replica_db(path) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, plan TEXT NOT NULL, created_at INTEGER NOT NULL)")
    db.execute("INSERT INTO users VALUES (7, 'pro', 1)")
    db.commit()
    db.close()


def test_plan_is_read_from_replica(tmp_path) -> None:
    path = tmp_path / "replica.db"
    make_replica_db(path)
    assert plan_of(path, 7) == "pro"


def test_unknown_user_has_no_plan(tmp_path) -> None:
    path = tmp_path / "replica.db"
    make_replica_db(path)
    assert plan_of(path, 99) is None


def test_missing_database_has_no_plan(tmp_path) -> None:
    assert plan_of(tmp_path / "absent.db", 7) is None


def test_reading_does_not_create_or_change_the_database(tmp_path) -> None:
    path = tmp_path / "replica.db"
    make_replica_db(path)
    plan_of(path, 99)
    db = sqlite3.connect(path)
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    db.close()
