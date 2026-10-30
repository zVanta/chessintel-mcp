#!/usr/bin/env python3
"""Weekly New Jersey Chess Club newsletter generator (runs on a systemd timer).

Produces a Markdown newsletter: the Lichess puzzle of the day plus a rotating
"Game of the Week" from the club's famous-games library. Writes an archived
copy and a `newsletter_latest.md` into the public output dir so the club
website (or anyone) can pull it from /chessintel/newsletter_latest.md.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/opt/chessintel/app")

from chessintel import famous_games  # noqa: E402

OUT = os.environ.get("CHESSINTEL_OUT", "/var/lib/chessintel/out")
ARCHIVE = os.environ.get("CHESSINTEL_NEWSLETTER_DIR", "/var/lib/chessintel/newsletter")
PUBLIC_BASE = os.environ.get("CHESSINTEL_PUBLIC_BASE", "https://njxai.com")


def fetch_puzzle() -> dict | None:
    try:
        req = urllib.request.Request(
            "https://lichess.org/api/puzzle/daily",
            headers={"User-Agent": "ChessIntel-Newsletter/1.0"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def game_of_week() -> dict | None:
    games = famous_games.GAMES
    if not games:
        return None
    week = datetime.now(timezone.utc).isocalendar()[1]
    return games[week % len(games)]


def main() -> None:
    now = datetime.now(timezone.utc)
    puzzle = fetch_puzzle()
    game = game_of_week()

    lines = ["# New Jersey Chess Club — Weekly Newsletter", ""]
    lines.append(f"**Week {now.isocalendar()[1]}, {now.strftime('%B %d, %Y')}**")
    lines.append("")

    lines.append("## ♟ Puzzle of the Week")
    lines.append("")
    if puzzle:
        meta = puzzle.get("puzzle") or {}
        puzzle_game = puzzle.get("game") or {}
        rating = meta.get("rating")
        themes = meta.get("themes", [])
        puzzle_id = meta.get("id")
        if rating:
            lines.append(f"- **Rating:** {rating}")
        if themes:
            lines.append(f"- **Themes:** {', '.join(themes)}")
        pgn = puzzle_game.get("pgn", "")
        if pgn:
            lines.append("")
            lines.append("```pgn")
            lines.append(pgn.strip())
            lines.append("```")
        if puzzle_id:
            lines.append(f"- Try it: https://lichess.org/training/{puzzle_id}")
    else:
        lines.append("_(Puzzle unavailable this week — Lichess API error.)_")

    lines.append("")
    if game:
        lines.append(
            f"## 📖 Game of the Week: {game.get('white')} vs {game.get('black')}"
        )
        lines.append("")
        lines.append(f"- **Event:** {game.get('event')}")
        lines.append(f"- **Result:** {game.get('result')}")
        lines.append(f"- **Opening:** {game.get('opening')}")
        lines.append("")
        lines.append(
            "Study the full game below, then ask the ChessIntel coach to turn it "
            "into a one-page lesson with the key moment and three big ideas."
        )
        lines.append("")
        lines.append("```pgn")
        lines.append((game.get("pgn") or "").strip())
        lines.append("```")

    lines.append("")
    lines.append("---")
    lines.append("_Generated automatically by ChessIntel. Questions: info@njchess.club_")
    text = "\n".join(lines) + "\n"

    os.makedirs(ARCHIVE, exist_ok=True)
    stamp = now.strftime("%Y%m%d")
    archive_path = os.path.join(ARCHIVE, f"newsletter_{stamp}.md")
    with open(archive_path, "w", encoding="utf-8") as handle:
        handle.write(text)

    os.makedirs(OUT, exist_ok=True)
    latest_path = os.path.join(OUT, "newsletter_latest.md")
    with open(latest_path, "w", encoding="utf-8") as handle:
        handle.write(text)

    print(f"newsletter written: {archive_path}")
    print(f"public: {PUBLIC_BASE}/chessintel/newsletter_latest.md")
    print(
        f"puzzle: {'ok' if puzzle else 'unavailable'}, "
        f"game: {game.get('id') if game else 'none'}"
    )


if __name__ == "__main__":
    main()
