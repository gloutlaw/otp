import sqlite3
from pathlib import Path


# Read-only so the support bot never writes into the replica database.
def plan_of(path: Path, user_id: int) -> str | None:
    try:
        db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return None
    try:
        row = db.execute("SELECT plan FROM users WHERE user_id = ?", (user_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        db.close()
    return row[0] if row else None
