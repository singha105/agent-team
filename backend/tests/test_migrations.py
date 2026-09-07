"""Migrations must actually stamp the database.

SQLite takes Alembic's non-transactional DDL path: CREATE TABLE auto-commits
but the alembic_version INSERT does not. Without an explicit commit in env.py,
`upgrade head` appears to succeed while leaving the database unstamped, and the
next upgrade fails with 'table agents already exists'. This test runs the real
migration twice to catch that.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"


def run_alembic(*args: str, db_path: Path) -> subprocess.CompletedProcess[str]:
    import os

    env = os.environ.copy()
    env["AGENTTEAM_DB_PATH"] = str(db_path)
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=120,
        check=False,
    )


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    return tmp_path / "migrate_test.db"


def test_upgrade_creates_every_table(fresh_db: Path) -> None:
    import sqlite3

    assert run_alembic("upgrade", "head", db_path=fresh_db).returncode == 0
    tables = {
        row[0]
        for row in sqlite3.connect(fresh_db).execute(
            "select name from sqlite_master where type='table'"
        )
    }
    assert {"agents", "tasks", "messages", "tool_calls", "usage"} <= tables


def test_upgrade_stamps_the_version_table(fresh_db: Path) -> None:
    import sqlite3

    run_alembic("upgrade", "head", db_path=fresh_db)
    stamped = sqlite3.connect(fresh_db).execute("select * from alembic_version").fetchall()
    assert stamped, "alembic_version is empty — the revision was never committed"


def test_upgrade_is_idempotent(fresh_db: Path) -> None:
    assert run_alembic("upgrade", "head", db_path=fresh_db).returncode == 0
    second = run_alembic("upgrade", "head", db_path=fresh_db)
    assert second.returncode == 0, second.stderr
    assert "already exists" not in second.stderr


def test_models_match_the_migration(fresh_db: Path) -> None:
    """No drift between the ORM models and the migration."""
    run_alembic("upgrade", "head", db_path=fresh_db)
    result = run_alembic("check", db_path=fresh_db)
    assert result.returncode == 0, f"schema drift detected:\n{result.stdout}\n{result.stderr}"


def test_downgrade_then_upgrade_round_trips(fresh_db: Path) -> None:
    run_alembic("upgrade", "head", db_path=fresh_db)
    assert run_alembic("downgrade", "base", db_path=fresh_db).returncode == 0
    assert run_alembic("upgrade", "head", db_path=fresh_db).returncode == 0


def test_round_trip_preserves_data_and_leaves_no_fk_violations(fresh_db: Path) -> None:
    """batch_alter_table rebuilds tables by drop-and-rename, which fails on
    SQLite when foreign key enforcement is on. That failure surfaced as a
    downgrade that reported success while changing nothing, so this asserts on
    exit codes and on the data, never on log text."""
    import sqlite3

    assert run_alembic("upgrade", "head", db_path=fresh_db).returncode == 0

    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "insert into agents (key,display_name,role,model,avatar_id,status) "
        "values ('backend','Ada','r','claude-opus-5','a','idle')"
    )
    for status in ("queued", "in_progress", "needs_review", "done", "failed", "budget_exceeded"):
        conn.execute(
            "insert into tasks (title,description,assigned_agent_id,status,created_by,attempt) "
            "values (?,?,1,?,'human',1)",
            (status, status, status),
        )
    conn.commit()
    conn.close()

    down = run_alembic("downgrade", "e6b85cbd5658", db_path=fresh_db)
    assert down.returncode == 0, down.stderr
    up = run_alembic("upgrade", "head", db_path=fresh_db)
    assert up.returncode == 0, up.stderr

    conn = sqlite3.connect(fresh_db)
    assert conn.execute("select count(*) from tasks").fetchone()[0] == 6
    assert conn.execute("pragma foreign_key_check").fetchall() == []
    statuses = {r[0] for r in conn.execute("select status from tasks")}
    # Every surviving status must be one the state machine recognises.
    assert statuses <= {"queued", "in_progress", "needs_review", "done", "failed",
                        "budget_exceeded"}
    conn.close()


def test_phase1_status_values_are_migrated(fresh_db: Path) -> None:
    """Status is a plain string column, so autogenerate sees no change. The
    old values have to be rewritten explicitly or historical tasks keep a
    status the state machine rejects."""
    import sqlite3

    run_alembic("upgrade", "e6b85cbd5658", db_path=fresh_db)
    conn = sqlite3.connect(fresh_db)
    conn.execute(
        "insert into agents (key,display_name,role,model,avatar_id) "
        "values ('backend','Ada','r','claude-opus-5','a')"
    )
    for old_status in ("pending", "running", "completed"):
        conn.execute(
            "insert into tasks (title,description,assigned_agent_id,status,created_by) "
            "values (?,?,1,?,'human')",
            (old_status, old_status, old_status),
        )
    conn.commit()
    conn.close()

    assert run_alembic("upgrade", "head", db_path=fresh_db).returncode == 0

    conn = sqlite3.connect(fresh_db)
    migrated = {r[0]: r[1] for r in conn.execute("select title, status from tasks")}
    assert migrated == {
        "pending": "queued",
        "running": "in_progress",
        "completed": "done",
    }
    conn.close()
