"""Opening identification: ECO code + human-readable name.

Resolution order (cheapest and most trustworthy first):

1. PGN headers. chess.com and lichess both ship ``[ECO]``, ``[Opening]`` and
   ``[ECOUrl]``; Shredder/ChessBase files usually ship ``[ECO]`` too. If the
   headers already answer the question, nothing else runs.
2. A compact built-in move-prefix table covering the openings a junior coach
   actually meets week to week. Longest prefix wins.
3. Optionally, the public Lichess opening-explorer API for master-level
   frequency and score. This is *only* used when ``fetch_online=True``, and a
   network failure is never fatal — the offline answer stands.

Nothing here invents an opening name. If no table entry matches, the result is
reported as unidentified rather than guessed at.
"""

from __future__ import annotations

import io
import os
from typing import Any, Dict, List, Optional, Tuple

import chess
import chess.pgn

# --------------------------------------------------------------------------
# Built-in table: (UCI move prefix, ECO, name)
#
# Ordered longest-prefix-wins at lookup time, so entries may overlap freely.
# Keep this list focused on openings that realistically show up in a junior
# programme rather than attempting a full ECO database.
# --------------------------------------------------------------------------

_OPENINGS: List[Tuple[str, str, str]] = [
    # --- 1.e4 e5 ---
    ("e2e4 e7e5 g1f3 b8c6 f1b5", "C60", "Ruy Lopez (Spanish Opening)"),
    ("e2e4 e7e5 g1f3 b8c6 f1b5 a7a6", "C70", "Ruy Lopez: Morphy Defence"),
    ("e2e4 e7e5 g1f3 b8c6 f1b5 a7a6 b5a4 g8f6 e1g1 f8e7", "C84", "Ruy Lopez: Closed"),
    ("e2e4 e7e5 g1f3 b8c6 f1b5 g8f6", "C67", "Ruy Lopez: Berlin Defence"),
    ("e2e4 e7e5 g1f3 b8c6 f1c4", "C50", "Italian Game"),
    ("e2e4 e7e5 g1f3 b8c6 f1c4 f8c5", "C50", "Italian Game: Giuoco Piano"),
    ("e2e4 e7e5 g1f3 b8c6 f1c4 f8c5 b2b4", "C51", "Evans Gambit"),
    ("e2e4 e7e5 g1f3 b8c6 f1c4 g8f6", "C55", "Italian Game: Two Knights Defence"),
    ("e2e4 e7e5 g1f3 b8c6 d2d4", "C44", "Scotch Game"),
    ("e2e4 e7e5 g1f3 b8c6 d2d4 e5d4 g1f3", "C45", "Scotch Game: Schmidt Variation"),
    ("e2e4 e7e5 g1f3 b8c6 b1c3", "C44", "Four Knights Game"),
    ("e2e4 e7e5 g1f3 g8f6", "C42", "Petrov Defence (Russian Game)"),
    ("e2e4 e7e5 g1f3 d7d6", "C41", "Philidor Defence"),
    ("e2e4 e7e5 b1c3", "C44", "Vienna Game"),
    ("e2e4 e7e5 f2f4", "C33", "King's Gambit"),
    ("e2e4 e7e5", "C20", "Open Game (1.e4 e5)"),
    # --- Sicilian ---
    ("e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6 b1c3 a7a6", "B90", "Sicilian: Najdorf Variation"),
    ("e2e4 c7c5 g1f3 d7d6 d2d4 c5d4 f3d4 g8f6 b1c3 g7g6", "B70", "Sicilian: Dragon Variation"),
    ("e2e4 c7c5 g1f3 e7e6 d2d4 c5d4 f3d4", "B40", "Sicilian: Taimanov/Kan"),
    ("e2e4 c7c5 g1f3 b8c6 d2d4 c5d4 f3d4", "B30", "Sicilian: Old Sicilian"),
    ("e2e4 c7c5 g1f3 d7d6 f1b5", "B51", "Sicilian: Moscow Variation"),
    ("e2e4 c7c5 b1c3", "B23", "Sicilian: Closed"),
    ("e2e4 c7c5 d2d4", "B20", "Sicilian: Smith-Morra Gambit"),
    ("e2e4 c7c5", "B20", "Sicilian Defence"),
    # --- French / Caro-Kann / Scandinavian ---
    ("e2e4 e7e6 d2d4 d7d5 e4e5", "C02", "French: Advance Variation"),
    ("e2e4 e7e6 d2d4 d7d5 b1c3 g8f6", "C11", "French: Classical"),
    ("e2e4 e7e6 d2d4 d7d5 e4d5 e6d5", "C01", "French: Exchange Variation"),
    ("e2e4 e7e6", "C00", "French Defence"),
    ("e2e4 c7c6 d2d4 d7d5 e4e5", "B12", "Caro-Kann: Advance Variation"),
    ("e2e4 c7c6 d2d4 d7d5 b1c3", "B15", "Caro-Kann: Main Line"),
    ("e2e4 c7c6", "B10", "Caro-Kann Defence"),
    ("e2e4 d7d5 e4d5 d8d5", "B01", "Scandinavian Defence"),
    ("e2e4 d7d5", "B01", "Scandinavian Defence"),
    ("e2e4 g8f6", "B02", "Alekhine Defence"),
    ("e2e4 d7d6", "B07", "Pirc Defence"),
    ("e2e4 g7g6", "B06", "Modern Defence"),
    ("e2e4 b8c6", "B00", "Nimzowitsch Defence"),
    ("e2e4", "B00", "King's Pawn Opening"),
    # --- 1.d4 d5 ---
    ("d2d4 d7d5 c2c4 e7e6 b1c3 g8f6", "D37", "Queen's Gambit Declined"),
    ("d2d4 d7d5 c2c4 c7c6", "D10", "Slav Defence"),
    ("d2d4 d7d5 c2c4 d5c4", "D20", "Queen's Gambit Accepted"),
    ("d2d4 d7d5 c2c4 e7e6", "D30", "Queen's Gambit Declined"),
    ("d2d4 d7d5 c2c4", "D06", "Queen's Gambit"),
    ("d2d4 d7d5 g1f3 g8f6 c1f4", "D02", "London System"),
    ("d2d4 d7d5 g1f3 g8f6 e2e3", "D02", "Queen's Pawn: Colle/London"),
    ("d2d4 d7d5 g1f3", "D02", "Queen's Pawn Game"),
    ("d2d4 d7d5", "D00", "Closed Game"),
    # --- Indian defences ---
    ("d2d4 g8f6 c2c4 e7e6 b1c3 f8b4", "E20", "Nimzo-Indian Defence"),
    ("d2d4 g8f6 c2c4 e7e6 g1f3 b7b6", "E12", "Queen's Indian Defence"),
    ("d2d4 g8f6 c2c4 g7g6 b1c3 f8g7 e2e4 d7d6", "E60", "King's Indian Defence"),
    ("d2d4 g8f6 c2c4 g7g6 b1c3 d7d5", "D80", "Grünfeld Defence"),
    ("d2d4 g8f6 c2c4 g7g6", "E60", "King's Indian Defence"),
    ("d2d4 g8f6 c2c4 c7c5 d4d5", "A56", "Benoni Defence"),
    ("d2d4 g8f6 c2c4 c7c5 d4c5", "A50", "Benoni: Modern/Benko"),
    ("d2d4 g8f6 c2c4 b7b6", "E12", "Queen's Indian Defence"),
    ("d2d4 g8f6 c2c4 e7e6", "E00", "Indian Game: East Indian"),
    ("d2d4 g8f6 c2c4", "A50", "Indian Game"),
    # --- 1.d4 others / flank ---
    ("d2d4 f7f5", "A80", "Dutch Defence"),
    ("d2d4 e7e6 c2c4 f7f5", "A80", "Dutch Defence: Classical"),
    ("d2d4 d7d6", "A41", "Modern Defence / Rat"),
    ("d2d4", "A40", "Queen's Pawn Opening"),
    # --- Flank / irregular ---
    ("c2c4 e7e5", "A20", "English Opening: Reversed Sicilian"),
    ("c2c4 g8f6", "A15", "English Opening: Anglo-Indian"),
    ("c2c4 c7c5", "A30", "English Opening: Symmetrical"),
    ("c2c4", "A10", "English Opening"),
    ("g1f3", "A04", "Zukertort/Réti Opening"),
    ("g2g3", "A00", "Hungarian Opening"),
    ("b2b3", "A01", "Nimzo-Larsen Attack"),
    ("f2f4", "A02", "Bird's Opening"),
    ("b1c3", "A00", "Dunst Opening"),
    ("e2e3", "A00", "Van 't Kruijs Opening"),
    ("d2d3", "A00", "Mieses Opening"),
]

# Longest prefixes first so "Ruy Lopez: Closed" beats "Ruy Lopez".
_ORDERED: List[Tuple[List[str], str, str]] = sorted(
    ((uci.split(), eco, name) for uci, eco, name in _OPENINGS),
    key=lambda row: -len(row[0]),
)

# --------------------------------------------------------------------------
# Full opening dataset (lichess-org/chess-openings): ~3,870 named positions.
# Downloaded to data/openings.tsv. Loaded lazily and keyed by EPD so a game
# transposes into the right name regardless of move order.
# --------------------------------------------------------------------------

_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "openings.tsv")
_EPD_MAP: Optional[Dict[str, List[Tuple[str, str]]]] = None


def _san_to_epd(movetext: str) -> Optional[str]:
    """Play a bare SAN movetext and return the resulting EPD."""
    try:
        game = chess.pgn.read_game(io.StringIO('[Event "?"]\n\n' + movetext.strip() + "\n"))
        if game is None:
            return None
        return game.end().board().epd()
    except Exception:
        return None


def _load_dataset() -> Dict[str, List[Tuple[str, str]]]:
    """Return EPD -> [(eco, name), ...] from the bundled openings dataset.

    The source TSV has three columns (eco, name, pgn); the EPD is computed here
    by playing each opening's movetext with python-chess.
    """
    global _EPD_MAP
    if _EPD_MAP is not None:
        return _EPD_MAP
    mapping: Dict[str, List[Tuple[str, str]]] = {}
    try:
        with open(_DATA_PATH, encoding="utf-8") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if not line or line.startswith("eco"):
                    continue
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                eco, name, movetext = parts[0], parts[1], parts[2]
                epd = _san_to_epd(movetext)
                if epd:
                    mapping.setdefault(epd, []).append((eco, name))
    except OSError:
        mapping = {}
    _EPD_MAP = mapping
    return _EPD_MAP


def _normalise_pgn(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def moves_to_uci_prefix(moves: List[str]) -> str:
    """Space-joined UCI moves, the form the table is keyed on."""
    return " ".join(moves)


def identify_from_headers(headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Use ``[ECO]`` / ``[Opening]`` / ``[ECOUrl]`` when the PGN already has them."""
    eco = (headers.get("ECO") or "").strip()
    name = (headers.get("Opening") or "").strip()
    variation = (headers.get("Variation") or "").strip()

    if name and variation and variation.lower() not in name.lower():
        name = f"{name}: {variation}"

    # Lichess puts a readable slug in ECOUrl, e.g. .../openings/Ruy-Lopez_...
    eco_url = (headers.get("ECOUrl") or headers.get("ECOURL") or "").strip()
    if not name and eco_url:
        slug = eco_url.rstrip("/").rsplit("/", 1)[-1]
        if slug:
            name = slug.replace("-", " ").replace("_", ": ").strip()

    if eco or name:
        return {
            "eco": eco or None,
            "name": name or None,
            "source": "pgn-headers",
        }
    return None


def identify_from_moves(uci_moves: List[str]) -> Optional[Dict[str, Any]]:
    """Identify by replaying the moves and matching each position against the
    full lichess chess-openings dataset (EPD lookup, so transpositions are
    handled). Falls back to the compact built-in prefix table."""
    mapping = _load_dataset()
    if mapping:
        board = chess.Board()
        best: Optional[Dict[str, Any]] = None
        for ply, uci in enumerate(uci_moves, start=1):
            try:
                board.push_uci(uci)
            except Exception:
                break
            entries = mapping.get(board.epd())
            if entries:
                # A shared position may name several openings; most specific wins.
                eco, name = max(entries, key=lambda row: len(row[1]))
                best = {
                    "eco": eco,
                    "name": name,
                    "source": "lichess-openings",
                    "matched_plies": ply,
                }
        if best:
            return best

    for prefix, eco, name in _ORDERED:
        if len(uci_moves) >= len(prefix) and uci_moves[: len(prefix)] == prefix:
            return {
                "eco": eco,
                "name": name,
                "source": "builtin-table",
                "matched_plies": len(prefix),
            }
    return None


def identify(
    headers: Optional[Dict[str, str]] = None,
    uci_moves: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Identify the opening, preferring PGN headers, then the dataset/table."""
    hdr = identify_from_headers(headers or {})
    moves = identify_from_moves(uci_moves or [])

    if hdr and hdr.get("name"):
        hdr["matched_plies"] = None
        return hdr

    if moves:
        # Carry a header ECO forward when the move-based result lacks one.
        if hdr and hdr.get("eco") and not moves.get("eco"):
            moves["eco"] = hdr["eco"]
        return moves

    if hdr and hdr.get("eco"):
        return {**hdr, "matched_plies": None}

    return {
        "eco": None,
        "name": None,
        "source": "unidentified",
        "matched_plies": 0,
        "note": (
            "Opening not recognised from the PGN headers or the opening dataset. "
            "Do not state an opening name for this game."
        ),
    }


# --------------------------------------------------------------------------
# Optional online enrichment (Lichess opening explorer — public, no key)
# --------------------------------------------------------------------------

EXPLORER_URL = "https://explorer.lichess.ovh/masters"


def explorer_stats(fen: str, timeout: float = 12.0) -> Optional[Dict[str, Any]]:
    """Master-game frequency and score for the position, or None on any failure.

    Public endpoint, no credentials. Used only when explicitly requested so a
    lesson can be built offline. Never raises.
    """
    try:
        import urllib.error
        import urllib.parse
        import urllib.request
    except ImportError:  # pragma: no cover
        return None

    query = urllib.parse.urlencode({"fen": fen, "moves": 6, "topGames": 0})
    request = urllib.request.Request(
        f"{EXPLORER_URL}?{query}",
        headers={"User-Agent": "chessintel-mcp/1.0"},
    )
    try:
        import json

        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception:
        return None

    total = {
        "white": int(payload.get("white", 0) or 0),
        "draws": int(payload.get("draws", 0) or 0),
        "black": int(payload.get("black", 0) or 0),
    }
    games = sum(total.values())

    moves = []
    for row in payload.get("moves", []) or []:
        tries = int(row.get("white", 0) or 0) + int(
            row.get("draws", 0) or 0
        ) + int(row.get("black", 0) or 0)
        moves.append(
            {
                "san": row.get("san"),
                "uci": row.get("uci"),
                "games": tries,
                "share_pct": round(100.0 * tries / games, 1) if games else 0.0,
                "white_pct": round(100.0 * int(row.get("white", 0) or 0) / tries, 1)
                if tries
                else 0.0,
                "draw_pct": round(100.0 * int(row.get("draws", 0) or 0) / tries, 1)
                if tries
                else 0.0,
                "black_pct": round(100.0 * int(row.get("black", 0) or 0) / tries, 1)
                if tries
                else 0.0,
                "avg_rating": row.get("averageRating"),
            }
        )

    return {
        "source": "lichess-masters",
        "total_games": games,
        "results": total,
        "top_moves": moves[:6],
    }
