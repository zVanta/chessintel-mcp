"""Curated library of famous/teaching games with verified PGNs.

The fetch tools (chess.com / lichess) only return a live user's recent games,
so they cannot retrieve a named historical game such as "Tal vs Fischer,
Zagreb 1959". This module fills that gap for the club's staple lessons.

Each entry's PGN carries correct White/Black headers, result and ECO, so the
names, result and opening in the lecture are always right.

To add a game, append a dict to GAMES. ``keywords`` should include player
surnames, event, year and opening so lookup is forgiving.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Dict, List

GAMES: List[Dict[str, Any]] = [
    {
        "id": "tal-fischer-zagreb-1959",
        "event": "Candidates Tournament, Zagreb 1959",
        "white": "Mikhail Tal",
        "black": "Robert James Fischer",
        "year": 1959,
        "result": "1-0",
        "opening": "King's Indian Defence, Petrosian System (E93)",
        "keywords": [
            "tal", "fischer", "zagreb", "1959", "candidates", "kings indian",
            "king's indian", "petrosian", "bobby", "mikhail",
        ],
        "pgn": (
            '[Event "Candidates Tournament, Zagreb 1959"]\n'
            '[Site "Zagreb, Yugoslavia"]\n'
            '[Date "1959.??.??"]\n'
            '[Round "6"]\n'
            '[White "Tal, Mikhail"]\n'
            '[Black "Fischer, Robert James"]\n'
            '[Result "1-0"]\n'
            '[ECO "E93"]\n'
            '[Opening "King\'s Indian Defence, Petrosian System"]\n\n'
            "1. d4 Nf6 2. c4 g6 3. Nc3 Bg7 4. e4 d6 5. Be2 O-O 6. Nf3 e5 7. d5 Nbd7 "
            "8. Bg5 h6 9. Bh4 a6 10. Nd2 Qe8 11. O-O Nh7 12. b4 Ng5 13. f3 f5 "
            "14. Bf2 Qe7 15. Rc1 Nf6 16. c5 Bd7 17. Qc2 Nh5 18. b5 fxe4 19. Ndxe4 Nxe4 "
            "20. fxe4 Nf4 21. c6 Qg5 22. Bf3 bxc6 23. dxc6 Bg4 24. Bxg4 Qxg4 25. Be3 axb5 "
            "26. Bxf4 exf4 27. Nxb5 Rf7 28. Qc4 Rc8 29. Rf3 Be5 30. Rcf1 Kg7 31. a4 Ra8 "
            "32. Kh1 Qg5 33. g3 Raf8 34. gxf4 Bxf4 35. Nd4 Qh4 36. Rxf4 Rxf4 37. Ne6+ Kh8 "
            "38. Qd4+ R8f6 39. Nxf4 Kh7 40. e5 dxe5 41. Qd7+ 1-0"
        ),
    },
    {
        "id": "gruenfeld-alekhine-karlsbad-1923",
        "event": "Karlsbad International Tournament 1923",
        "white": "Ernst Grünfeld",
        "black": "Alexander Alekhine",
        "year": 1923,
        "result": "0-1",
        "opening": "Queen's Gambit Declined, Orthodox Defense (D64)",
        "white_aliases": ["Gruenfeld", "Grunfeld"],
        "black_aliases": ["Alechin", "Aljechin", "Alechine"],
        "keywords": [
            "gruenfeld", "grunfeld", "alekhine", "aljechin", "karlsbad",
            "karlovy vary", "1923", "orthodox", "queens gambit", "declined",
            "d64", "sixth sense", "bishop check", "ernst", "alexander",
            "brilliancy prize",
        ],
        "pgn": (
            '[Event "Karlsbad International Tournament 1923"]\n'
            '[Site "Karlsbad, Czechoslovakia"]\n'
            '[Date "1923.04.29"]\n'
            '[Round "2"]\n'
            '[White "Ernst Grünfeld"]\n'
            '[Black "Alexander Alekhine"]\n'
            '[Result "0-1"]\n'
            '[ECO "D64"]\n'
            '[Opening "Queen\'s Gambit Declined, Orthodox Defense"]\n\n'
            "1. d4 Nf6 2. c4 e6 3. Nc3 d5 4. Bg5 Be7 5. Nf3 Nbd7 "
            "6. e3 O-O 7. Rc1 c6 8. Qc2 a6 9. a3 h6 10. Bh4 Re8 "
            "11. Bd3 dxc4 12. Bxc4 b5 13. Ba2 c5 14. Rd1 cxd4 15. Nxd4 Qb6 "
            "16. Bb1 Bb7 17. O-O Rac8 18. Qd2 Ne5 19. Bxf6 Bxf6 20. Qc2 g6 "
            "21. Qe2 Nc4 22. Be4 Bg7 23. Bxb7 Qxb7 24. Rc1 e5 25. Nb3 e4 "
            "26. Nd4 Red8 27. Rfd1 Ne5 28. Na2 Nd3 29. Rxc8 Qxc8 30. f3 Rxd4 "
            "31. fxe4 Nf4 32. exf4 Qc4 33. Qxc4 Rxd1+ 34. Qf1 Bd4+ 0-1"
        ),
    },
]


def _norm(text: str) -> str:
    """Lower-case and fold accents (Grünfeld → grunfeld), then split into words."""
    folded = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return " ".join(folded.lower().replace("'", " ").replace("-", " ").split())


def _player_tokens(game: Dict[str, Any], key: str) -> set:
    """Player name tokens plus any alternate spellings for that player."""
    out = set(_norm(game[key]).split())
    for alias in game.get(key + "_aliases") or []:
        out |= set(_norm(alias).split())
    return out


def search(query: str) -> List[Dict[str, Any]]:
    """Return matching games (metadata + PGN) for a free-text query.

    A game only matches when the query mentions *both* players — a query that
    shares just one name (e.g. "Fischer vs Panno" vs the "Tal vs Fischer"
    entry) is a different game and must fall through to the master-game lookup.
    """
    q = _norm(query)
    tokens = set(q.split())
    hits: List[Dict[str, Any]] = []
    for game in GAMES:
        white_names = _player_tokens(game, "white")
        black_names = _player_tokens(game, "black")
        white_match = bool(white_names & tokens) or any(t in q for t in white_names)
        black_match = bool(black_names & tokens) or any(t in q for t in black_names)
        if not (white_match and black_match):
            continue
        hay = _norm(
            " ".join(
                [game["event"], game["white"], game["black"], game["opening"]]
                + game["keywords"]
                + list(game.get("white_aliases") or [])
                + list(game.get("black_aliases") or [])
                + [str(game["year"])]
            )
        )
        score = sum(1 for t in tokens if t in hay)
        hits.append((score, game))
    hits.sort(key=lambda pair: -pair[0])
    return [g for _, g in hits]


def get(query: str) -> Dict[str, Any]:
    """Return the best match for a query, or an empty dict when none exists."""
    matches = search(query)
    return matches[0] if matches else {}
