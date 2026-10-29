"""Find a chess game by player names and year.

Two sources, in order:

1. A small curated library of club staple games (:mod:`famous_games`).
2. Chessgames.com's public master database — a general lookup for any
   historical game. We search by players and year, download each candidate's
   PGN and verify the headers against the request before returning anything.
"""

from __future__ import annotations

import io
import re
import unicodedata
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import chess
import chess.pgn

from . import famous_games

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

_SEARCH = "https://www.chessgames.com/perl/chess.pl"
_PGN = "https://www.chessgames.com/njs/api/game/downloadPGN/{gid}"
_STOP = {"vs", "vs.", "the", "of", "and", "dr", "gm", "mr", "for"}


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", "replace")


def _tokens(text: str) -> List[str]:
    words = re.sub(r"[^a-zA-Z0-9' ]", " ", text.lower()).split()
    return [w for w in words if len(w) >= 3 and w not in _STOP]


def _parse_query(query: str) -> Dict[str, Any]:
    q = re.sub(r"\s+", " ", str(query)).strip()
    parts = re.split(r"\bvs\.?\b", q, flags=re.I)
    white_side = parts[0].strip() if parts else ""
    black_side = parts[1].strip() if len(parts) > 1 else ""

    year = ""
    m = re.search(r"(1[89]\d{2}|20\d{2})", q)
    if m:
        year = m.group(0)

    black_side = re.sub(r"(1[89]\d{2}|20\d{2})", " ", black_side)

    white_tokens = _tokens(white_side)
    black_tokens = _tokens(black_side)
    white_set = set(white_tokens)
    black_set = set(black_tokens)
    event_tokens = [t for t in _tokens(q) if t not in white_set and t not in black_set]

    return {
        "white": white_tokens[-1] if white_tokens else "",
        "black": black_tokens[0] if black_tokens else "",
        "white_tokens": white_tokens,
        "black_tokens": black_tokens,
        "event_tokens": event_tokens,
        "year": year,
        "raw": q,
    }


def _search(white: str, black: str, year: str) -> List[Dict[str, str]]:
    params = {"player": white}
    if black:
        params["player2"] = black
    if year:
        params["year"] = year
    html = _get(_SEARCH + "?" + urllib.parse.urlencode(params))
    out: List[Dict[str, str]] = []
    for m in re.finditer(
        r'href="[^"]*gid=(\d+)"[^>]*>(.*?)</a>\s*(.*?)(?:<|$)', html, re.S
    ):
        gid = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        rest = re.sub(r"<[^>]+>", " ", m.group(3))
        rest = re.sub(r"\s+", " ", rest).strip()
        m2 = re.match(r"([\d/]+-[\d/]+)\s+(\d+)\s+(\d{4})", rest)
        out.append(
            {
                "gid": gid,
                "title": title,
                "result": m2.group(1) if m2 else "",
                "moves": m2.group(2) if m2 else "",
                "year": m2.group(3) if m2 else "",
            }
        )
    return out


def _pgn_for(gid: str) -> Optional[str]:
    try:
        return _get(_PGN.format(gid=gid))
    except Exception:
        return None


def _fold(text: str) -> str:
    """Lower-case, strip accents, and collapse umlaut digraphs for name matching.

    So Gruenfeld / Grünfeld / Grunfeld all fold to 'grunfeld'.
    """
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return t.replace("ue", "u").replace("oe", "o").replace("ae", "a")


def _name_hit(query_token: str, header_name: str) -> bool:
    """True when a requested player token (folded) is present in a PGN header name."""
    q = _fold(query_token)
    h = _fold(header_name)
    return bool(q) and bool(h) and (q in h or h in q)


def _header(pgn: str, key: str) -> str:
    m = re.search(rf'\[{key}\s+"([^"]+)"\]', pgn)
    return _fold(m.group(1)) if m else ""


def _move_count(pgn: str) -> int:
    """Number of mainline moves in a PGN (0 for match/collection stubs)."""
    try:
        node = chess.pgn.read_game(io.StringIO(pgn))
        return len(list(node.mainline_moves())) if node else 0
    except Exception:
        return 0


def _score(pgn: str, parsed: Dict[str, Any]) -> int:
    head = _fold(pgn[:700])
    white_header = _header(pgn, "White")
    black_header = _header(pgn, "Black")
    score = 0
    if parsed["year"] and parsed["year"] in head:
        score += 3
    if parsed["white"] and _name_hit(parsed["white"], white_header):
        score += 2
    if any(_name_hit(t, black_header) for t in parsed["black_tokens"]):
        score += 2
    for token in parsed["event_tokens"]:
        if _fold(token) in head:
            score += 1
    return score


def find_game(query: str) -> Dict[str, Any]:
    """Return the best-matching game for a free-text query, or found=False."""
    local = famous_games.get(query)
    if local:
        return {
            "found": True,
            "source": "library",
            "event": local.get("event"),
            "white": local.get("white"),
            "black": local.get("black"),
            "result": local.get("result"),
            "opening": local.get("opening"),
            "pgn": local.get("pgn"),
        }

    parsed = _parse_query(query)
    if not parsed["white"]:
        return {"found": False, "message": "Could not make out the White player from that request."}

    black_candidates: List[str] = []
    for idx in (0, -1):
        if 0 <= abs(idx) < len(parsed["black_tokens"]):
            t = parsed["black_tokens"][idx]
            if t not in black_candidates:
                black_candidates.append(t)
    black_candidates.append("")

    candidates: List[Dict[str, str]] = []
    for black in black_candidates:
        try:
            candidates = _search(parsed["white"], black, parsed["year"])
        except Exception as exc:
            return {"found": False, "message": f"Game database lookup failed: {exc}"}
        if candidates:
            break

    if not candidates:
        return {
            "found": False,
            "message": "No game matching those players/year was found. Ask the user to paste the PGN or the moves.",
        }

    def _matches_players(pgn: str) -> bool:
        """A candidate is only acceptable when BOTH requested players appear in
        its headers — never return a game against the wrong opponent."""
        white_h = _header(pgn, "White")
        black_h = _header(pgn, "Black")
        if parsed["white"] and not _name_hit(parsed["white"], white_h):
            return False
        if parsed["black"] and not any(_name_hit(t, black_h) for t in parsed["black_tokens"]):
            return False
        return True

    best_pgn: Optional[str] = None
    best_score = 0
    for cand in candidates[:10]:
        pgn = _pgn_for(cand["gid"])
        if not pgn:
            continue
        if not _matches_players(pgn):
            continue
        # A match/collection page has the right names but zero moves — unusable.
        if _move_count(pgn) == 0:
            continue
        score = _score(pgn, parsed)
        if score > best_score:
            best_score = score
            best_pgn = pgn

    if not best_pgn or best_score < 2:
        return {
            "found": False,
            "message": "No game matching those players/year was found. Ask the user to paste the PGN or the moves.",
        }

    def _hdr(key: str) -> str:
        m = re.search(rf'\[{key}\s+"([^"]+)"\]', best_pgn)
        return m.group(1) if m else ""

    return {
        "found": True,
        "source": "chessgames.com",
        "event": _hdr("Event"),
        "white": _hdr("White"),
        "black": _hdr("Black"),
        "result": _hdr("Result"),
        "pgn": best_pgn,
    }
