"""Fetch games from chess.com / lichess public APIs. Username only — no credentials."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

UA = {"User-Agent": "chessintel-lite/1.0 (local MCP tool)"}


def _get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        raise ValueError(
            f"HTTP {exc.code} from {url} — username may not exist or the API "
            "rate-limited the request."
        ) from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Network error fetching {url}: {exc.reason}") from exc


def fetch_chesscom_games(
    username: str, max_games: int = 20, last_months: int = 3
) -> list[dict[str, Any]]:
    """Pull recent games from chess.com's public monthly archives."""
    archives = _get_json(f"https://api.chess.com/pub/player/{username}/games/archives")
    out: list[dict[str, Any]] = []
    for url in archives.get("archives", [])[-last_months:]:
        payload = _get_json(url)
        for g in payload.get("games", []):
            pgn = g.get("pgn")
            if not pgn:
                continue
            out.append({
                "platform": "chess.com",
                "game_url": g.get("url"),
                "time_class": g.get("time_class"),
                "rated": g.get("rated"),
                "pgn": pgn,
            })
            if len(out) >= max_games:
                return out
    return out


def fetch_lichess_games(username: str, max_games: int = 20) -> list[dict[str, Any]]:
    """Pull recent games from lichess's public NDJSON endpoint.

    ``pgnInJson=true`` is required: without it the endpoint returns moves and
    metadata but no ``pgn`` field at all, so every game looked empty.
    """
    url = (
        f"https://lichess.org/api/games/user/{username}"
        f"?max={max_games}&pgnInJson=true&clocks=true&evals=false&opening=true"
    )
    req = urllib.request.Request(url, headers={**UA, "Accept": "application/x-ndjson"})
    out: list[dict[str, Any]] = []
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            for line in r:
                line = line.strip()
                if not line:
                    continue
                g = json.loads(line)
                pgn = g.get("pgn", "")
                if not pgn:
                    continue
                out.append({
                    "platform": "lichess",
                    "game_url": f"https://lichess.org/{g.get('id', '')}",
                    "speed": g.get("speed"),
                    "rated": g.get("rated"),
                    "pgn": pgn,
                })
                if len(out) >= max_games:
                    break
    except urllib.error.HTTPError as exc:
        raise ValueError(
            f"HTTP {exc.code} from lichess — username may not exist or the API "
            "rate-limited the request."
        ) from exc
    return out


def _lichess_client() -> Any:
    """A berserk client, authenticated with LICHESS_TOKEN when present."""
    import berserk  # type: ignore[import-not-found]

    token = (os.environ.get("LICHESS_TOKEN") or "").strip()
    if token:
        return berserk.Client(session=berserk.TokenSession(token))
    return berserk.Client()


def fetch_lichess_game(game_id_or_url: str) -> dict[str, Any]:
    """Fetch one game's PGN by lichess game ID or URL (authenticated)."""
    raw = str(game_id_or_url).strip()
    match = re.search(r"lichess\.org/(?:game/)?([A-Za-z0-9]{8,12})", raw)
    game_id = match.group(1) if match else raw

    client = _lichess_client()
    pgn = client.games.export(game_id, as_pgn=True)
    if not isinstance(pgn, str):
        pgn = "".join(pgn)
    return {
        "platform": "lichess",
        "game_id": game_id,
        "game_url": f"https://lichess.org/{game_id}",
        "pgn": pgn,
    }


def fetch_player(username: str, platforms: tuple = ("chesscom", "lichess")) -> dict:
    """Ratings, account age and game counts across the public chess APIs.

    Read-only and credential-free. A platform that fails is reported as an
    error for that platform rather than failing the whole lookup, so a typo in
    one username still returns the other.
    """
    result: dict = {"username": username, "platforms": {}}

    if "chesscom" in platforms:
        entry: dict = {}
        try:
            profile = _get_json(f"https://api.chess.com/pub/player/{username}")
            stats = _get_json(f"https://api.chess.com/pub/player/{username}/stats")
            entry = {
                "name": profile.get("name"),
                "title": profile.get("title"),
                "country": (profile.get("country") or "").rsplit("/", 1)[-1] or None,
                "joined": profile.get("joined"),
                "last_online": profile.get("last_online"),
                "followers": profile.get("followers"),
                "status": profile.get("status"),
                "ratings": {
                    key.replace("chess_", ""): value.get("last", {}).get("rating")
                    for key, value in stats.items()
                    if isinstance(value, dict) and value.get("last", {}).get("rating")
                },
                "records": {
                    key.replace("chess_", ""): value.get("record")
                    for key, value in stats.items()
                    if isinstance(value, dict) and value.get("record")
                },
            }
        except ValueError as exc:
            entry = {"error": str(exc)}
        result["platforms"]["chesscom"] = entry

    if "lichess" in platforms:
        try:
            user = _get_json(f"https://lichess.org/api/user/{username}")
            perfs = user.get("perfs") or {}
            result["platforms"]["lichess"] = {
                "title": user.get("title"),
                "patron": user.get("patron"),
                "verified": user.get("verified"),
                "created": user.get("createdAt"),
                "seen": user.get("seenAt"),
                "play_time": user.get("playTime"),
                "ratings": {
                    key: value.get("rating")
                    for key, value in perfs.items()
                    if isinstance(value, dict) and value.get("rating")
                },
                "provisional": [
                    key
                    for key, value in perfs.items()
                    if isinstance(value, dict) and value.get("prov")
                ],
                "titled": bool(user.get("title")),
                "disabled": bool(user.get("disabled")),
                "tos_violation": bool(user.get("tosViolation")),
            }
        except ValueError as exc:
            result["platforms"]["lichess"] = {"error": str(exc)}

    return result
