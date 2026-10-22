"""SQLite longitudinal store: kids, games, reports."""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


def _base_dir() -> str:
    # Compiled .exe: keep data next to the executable (sys.executable), NOT in
    # the temp extraction dir that onefile builds unpack into.
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


DB_PATH = os.environ.get(
    "CHESSINTEL_DB", os.path.join(_base_dir(), "data", "chessintel.db")
)

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kid_id INTEGER NOT NULL REFERENCES kids(id),
                platform TEXT,
                game_url TEXT,
                result TEXT,
                pgn TEXT NOT NULL,
                analysis_json TEXT,
                stored_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kid_id INTEGER NOT NULL REFERENCES kids(id),
                report_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lectures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kid_id INTEGER NOT NULL REFERENCES kids(id),
                title TEXT NOT NULL,
                source TEXT,
                level TEXT,
                lecture_json TEXT NOT NULL,
                files_json TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS drill_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kid_id INTEGER NOT NULL REFERENCES kids(id),
                fen TEXT NOT NULL,
                answer_san TEXT NOT NULL,
                answer_line TEXT,
                theme TEXT,
                origin TEXT,
                reps INTEGER NOT NULL DEFAULT 0,
                lapses INTEGER NOT NULL DEFAULT 0,
                interval_days REAL NOT NULL DEFAULT 0,
                ease REAL NOT NULL DEFAULT 2.5,
                due_at TEXT NOT NULL,
                last_result TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_drill_due
                ON drill_cards (kid_id, due_at);
            """
        )
        _conn.commit()
    return _conn


def ensure_kid(name: str) -> int:
    with _lock:
        conn = _get_conn()
        row = conn.execute("SELECT id FROM kids WHERE name = ?", (name,)).fetchone()
        if row:
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO kids (name, created_at) VALUES (?, ?)", (name, _now())
        )
        conn.commit()
        return int(cur.lastrowid)


def store_game(kid_id: int, platform: str, pgn: str, game_url: str = "",
               result: str = "", analysis_json: Optional[str] = None) -> int:
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """INSERT INTO games
               (kid_id, platform, game_url, result, pgn, analysis_json, stored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (kid_id, platform, game_url, result, pgn, analysis_json, _now()),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_games(kid_id: int, limit: int = 200) -> list[dict[str, Any]]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM games WHERE kid_id = ? ORDER BY id DESC LIMIT ?",
            (kid_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def save_report(kid_id: int, report_text: str) -> int:
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO reports (kid_id, report_text, created_at) VALUES (?, ?, ?)",
            (kid_id, report_text, _now()),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_reports(kid_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM reports WHERE kid_id = ? ORDER BY id DESC LIMIT ?",
            (kid_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_progress(kid_name: str) -> dict[str, Any]:
    kid_id = ensure_kid(kid_name)
    games = list_games(kid_id)
    reports = list_reports(kid_id)
    return {
        "kid": kid_name,
        "games_stored": len(games),
        "reports_stored": len(reports),
        "recent_games": [
            {
                "platform": g["platform"],
                "result": g["result"],
                "game_url": g["game_url"],
                "stored_at": g["stored_at"],
            }
            for g in games[:10]
        ],
        "recent_reports": [r["report_text"] for r in reports],
    }


# ---------------------------------------------------------------------------
# Lecture library
# ---------------------------------------------------------------------------

def save_lecture(kid_id: int, title: str, lecture_json: str,
                 source: str = "", level: str = "",
                 files_json: Optional[str] = None) -> int:
    """Persist a built lecture so it can be re-rendered or revisited later."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """INSERT INTO lectures
               (kid_id, title, source, level, lecture_json, files_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (kid_id, title, source, level, lecture_json, files_json, _now()),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_lectures(kid_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            """SELECT id, title, source, level, created_at
               FROM lectures WHERE kid_id = ? ORDER BY id DESC LIMIT ?""",
            (kid_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_lecture(lecture_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM lectures WHERE id = ?", (lecture_id,)
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Drill cards (spaced repetition)
#
# A deliberately small SM-2-style scheduler: four grades, an ease factor and an
# interval. Enough to make "did the fix hold?" answerable without shipping a
# full SRS implementation.
# ---------------------------------------------------------------------------

_GRADES = ("again", "hard", "good", "easy")


def add_drill_cards(kid_id: int, cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Add drill cards, skipping any position already stored for this player."""
    added = 0
    skipped = 0
    now = _now()
    with _lock:
        conn = _get_conn()
        for card in cards:
            fen = (card.get("fen") or "").strip()
            answer = (card.get("answer_san") or "").strip()
            if not fen or not answer:
                skipped += 1
                continue
            existing = conn.execute(
                "SELECT id FROM drill_cards WHERE kid_id = ? AND fen = ? AND answer_san = ?",
                (kid_id, fen, answer),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            conn.execute(
                """INSERT INTO drill_cards
                   (kid_id, fen, answer_san, answer_line, theme, origin,
                    due_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    kid_id,
                    fen,
                    answer,
                    card.get("answer_line") or "",
                    card.get("theme") or "",
                    card.get("origin") or "",
                    now,
                    now,
                ),
            )
            added += 1
        conn.commit()
    return {"added": added, "skipped_duplicates": skipped}


def due_drill_cards(kid_id: int, limit: int = 20) -> list[dict[str, Any]]:
    """Cards whose due date has passed, hardest-first."""
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            """SELECT id, fen, answer_san, answer_line, theme, origin, reps, lapses,
                      interval_days, ease, due_at
               FROM drill_cards
               WHERE kid_id = ? AND due_at <= ?
               ORDER BY lapses DESC, due_at ASC
               LIMIT ?""",
            (kid_id, _now(), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def grade_drill_card(card_id: int, grade: str) -> dict[str, Any]:
    """Record a grade and reschedule the card. Returns the new schedule."""
    grade = (grade or "").strip().lower()
    if grade not in _GRADES:
        raise ValueError(f"grade must be one of {', '.join(_GRADES)}")

    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM drill_cards WHERE id = ?", (card_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no drill card with id {card_id}")

        ease = float(row["ease"] or 2.5)
        interval = float(row["interval_days"] or 0.0)
        reps = int(row["reps"] or 0)
        lapses = int(row["lapses"] or 0)

        if grade == "again":
            ease = max(1.3, ease - 0.20)
            lapses += 1
            interval = 10.0 / (60 * 24)  # ten minutes
        elif grade == "hard":
            ease = max(1.3, ease - 0.15)
            interval = 1.0 if interval < 1.0 else interval * 1.2
        elif grade == "good":
            interval = 1.0 if interval < 1.0 else interval * ease
        else:  # easy
            ease = min(2.8, ease + 0.15)
            interval = 3.0 if interval < 1.0 else interval * ease * 1.3

        interval = min(interval, 365.0)
        reps += 1
        due = datetime.now(timezone.utc) + timedelta(days=interval)

        conn.execute(
            """UPDATE drill_cards
               SET reps = ?, lapses = ?, interval_days = ?, ease = ?,
                   due_at = ?, last_result = ?
               WHERE id = ?""",
            (reps, lapses, round(interval, 6), round(ease, 3),
             due.isoformat(), grade, card_id),
        )
        conn.commit()

    return {
        "card_id": card_id,
        "grade": grade,
        "reps": reps,
        "lapses": lapses,
        "ease": round(ease, 3),
        "interval_days": round(interval, 4),
        "due_at": due.isoformat(),
    }


def drill_stats(kid_id: int) -> dict[str, Any]:
    """How much practice is outstanding, and how the player is trending."""
    with _lock:
        conn = _get_conn()
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM drill_cards WHERE kid_id = ?", (kid_id,)
        ).fetchone()["n"]
        due = conn.execute(
            "SELECT COUNT(*) AS n FROM drill_cards WHERE kid_id = ? AND due_at <= ?",
            (kid_id, _now()),
        ).fetchone()["n"]
        graded = conn.execute(
            """SELECT last_result, COUNT(*) AS n FROM drill_cards
               WHERE kid_id = ? AND last_result IS NOT NULL
               GROUP BY last_result""",
            (kid_id,),
        ).fetchall()
    return {
        "cards_total": int(total),
        "cards_due": int(due),
        "by_last_result": {row["last_result"]: int(row["n"]) for row in graded},
    }

