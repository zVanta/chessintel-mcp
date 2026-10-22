"""Analysis pipeline for junior chess coach reports.

This module fetches recent games from Lichess or chess.com, runs a Stockfish
engine analysis on each game, aggregates recurring mistakes ("habits"), and
produces a coach-style report (optionally via DeepSeek or LibreChat).

Environment variables
---------------------
STOCKFISH_PATH  : path to the Stockfish binary (default: "stockfish")
AI_PROVIDER     : "deepseek" (default) or "librechat". See llm.py for the
                  DEEPSEEK_* and LIBRECHAT_* connection variables.

CLI
---
    python chessintel_clone.py --platform lichess --username eric-rosen \
        --games 25 --kid Alice
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import random
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

import chess
import chess.engine
import chess.pgn

# Vendored into chessintel-lite: these two are only used by the original
# standalone CLI/report path. The MCP server never calls that path, and
# LibreChat's LLM writes the prose, so they stay optional.
try:  # pragma: no cover - optional dependency
    import llm  # type: ignore[import-not-found]  # noqa: F401
except ImportError:
    llm = None  # type: ignore[assignment]

try:  # pragma: no cover - optional dependency
    import requests  # type: ignore[import-not-found]  # noqa: F401
except ImportError:
    requests = None  # type: ignore[assignment]


def _load_dotenv(path: str) -> None:
    """Minimal .env loader so the Python service reads the same config as Next."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


for _env_candidate in (".env", os.path.join(os.path.dirname(__file__), ".env")):
    _load_dotenv(_env_candidate)


def _resolve_stockfish() -> str:
    """Resolve the Stockfish binary with sane fallbacks.

    systemd supplies STOCKFISH_PATH via EnvironmentFile, but manual runs
    (selftest, debugging) should still find /opt/chessintel/bin/stockfish.
    """
    for candidate in (
        os.environ.get("STOCKFISH_PATH"),
        "/opt/chessintel/bin/stockfish",
        shutil.which("stockfish"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return os.environ.get("STOCKFISH_PATH") or "stockfish"


STOCKFISH_PATH: str = _resolve_stockfish()

# chess.com requires a descriptive User-Agent; the requests default is 403'd.
USER_AGENT: str = "CheckmateCoach/1.0 (https://github.com/zVanta/ChessIntel)"

ANALYSIS_DEPTH: int = int(os.environ.get("ANALYSIS_DEPTH", "14"))
BLUNDER_THRESHOLD_CP: int = 250
# Once the mover is already this far behind, later moves are noise — the game
# is decided, so don't flag them as fresh blunders.
DECISIVE_CP: int = 600
# A forced mate flattens to ±10000 centipawns (mate_score). Comparing that
# against a normal eval creates a phantom ~90-pawn "loss" (e.g. mate-in-8 →
# still-winning +7.1 reads as 92.9 pawns). Treat anything ≥ this as a mate.
MATE_SCORE_CP: int = 9000

LICHESS_GAMES_URL: str = "https://lichess.org/api/games/user/{username}"
CHESSCOM_ARCHIVES_URL: str = "https://api.chess.com/pub/player/{username}/games/archives"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def phase_of(ply: int) -> str:
    """Return the game phase for a ply count.

    Opening: ply <= 20, endgame: ply >= 70, everything else is the middlegame.
    """
    if ply <= 20:
        return "opening"
    if ply >= 70:
        return "endgame"
    return "middlegame"


def _score_to_cp(score: Any, color: chess.Color) -> int:
    """Return an engine score in centipawns from ``color``'s perspective.

    Compatible with python-chess v1 (``Score`` with ``.white()``/``.score()``)
    and the v2 rewrite (``PovScore`` with ``.pov()``, and ``Cp``/``Mate``).
    """
    # v2 PovScore has .pov(color) but no .score() method. Delegate to
    # python-chess's own Score.score(mate_score=...) conversion, which already
    # handles every mate case — including mate-in-0 (a delivered checkmate) —
    # with the correct sign: the mated side gets -10000, the winner +10000.
    if not (hasattr(score, "score") and callable(score.score)):
        return score.pov(color).score(mate_score=10000)

    # Raw score from White's perspective (v1 Score, or v2 Cp/Mate).
    if score.is_mate():
        # Score.mate(): positive => White mates, negative => Black mates.
        winner = chess.WHITE if score.mate() > 0 else chess.BLACK
        return 10000 if winner == color else -10000
    cp = score.score(mate_score=10000)
    return cp if color == chess.WHITE else -cp


# ---------------------------------------------------------------------------
# Move classification (winning-chances based)
#
# Thresholds and labels follow the Chesskit colour scheme as adopted by
# Macintosh-Fan/ChessIntelligence (MIT). A move's "advantage loss" is measured
# in winning-percentage points and bucketed into blunder / mistake / inaccuracy
# / good / excellent / best. "Brilliant" and "perfect" require move-type
# detection and are intentionally omitted.
# ---------------------------------------------------------------------------

_CLASS_THRESHOLDS = [
    ("blunder", 20.0),
    ("mistake", 10.0),
    ("inaccuracy", 5.0),
    ("good", 2.0),
    ("excellent", 0.0),
]


def _winning_chances(cp: float) -> float:
    """Map a centipawn score to a winning percentage (0–100)."""
    return 50.0 + 50.0 * (2.0 / (1.0 + math.exp(-0.00368208 * float(cp))) - 1.0)


def _classify_loss(loss_pct: float) -> str:
    """Classify an advantage loss (in win-% points) into a move-quality label."""
    for label, floor in _CLASS_THRESHOLDS:
        if loss_pct > floor:
            return label
    return "best"


def _accuracy_from_loss(loss_pct: float) -> float:
    """Map an advantage loss (win-%) to a 0–100 move accuracy (CAPS-style)."""
    acc = 103.1668 * math.exp(-0.04354 * max(0.0, float(loss_pct))) - 3.1669
    return max(0.0, min(100.0, acc))


EVAL_CAP_CP: int = 1000


def _cap_eval(cp: int) -> int:
    """Clamp an eval to ±1000cp (the Lichess/Chess.com convention) so extreme
    or mate-flattened scores don't distort ACPL and points-lost figures."""
    return max(-EVAL_CAP_CP, min(EVAL_CAP_CP, cp))


def _cp_loss(score_before: int, score_after: int) -> int:
    """Centipawns lost on a move, ignoring the phantom loss from turning a
    forced mate into a merely winning position.

    ``score_before``/``score_after`` are flattened centipawns where a forced
    mate reads as ±10000 (``mate_score``). A move that converts mate-in-N into
    a still-clearly-winning position is NOT a blunder, so its loss reads 0.
    Both evals are then capped at ±1000cp before the subtraction.
    """
    if score_before >= MATE_SCORE_CP and score_after >= DECISIVE_CP:
        return 0
    return _cap_eval(score_before) - _cap_eval(score_after)


# ---------------------------------------------------------------------------
# Tactical threat detection (hung pieces / forks) for blunder classification
# ---------------------------------------------------------------------------

_PIECE_VALUES = {"p": 1, "n": 3, "b": 3, "r": 5, "q": 9, "k": 0}


def _see(board: chess.Board, square: chess.Square, side: chess.Color) -> int:
    """Static exchange evaluation of capturing on ``square``.

    Returns the material gain (in pawns) for ``side`` of playing the capture
    on ``square``, each side recapturing with its least-valuable piece and
    free to stop the exchange at any point. Positive means the capture wins
    material; zero or negative means it does not. Kings are excluded — a king
    recapturing a defended piece is illegal anyway.
    """
    piece = board.piece_at(square)
    if piece is None:
        return 0
    victim_value = _FORK_TARGET_VALUE.get(piece.piece_type, 0)
    b = board.copy()
    attackers = [
        s for s in b.attackers(side, square)
        if b.piece_at(s).piece_type != chess.KING
    ]
    if not attackers:
        return 0
    att_sq = min(
        attackers,
        key=lambda s: _FORK_TARGET_VALUE.get(b.piece_at(s).piece_type, 0),
    )
    attacker = b.piece_at(att_sq)
    b.remove_piece_at(att_sq)
    b.remove_piece_at(square)
    b.set_piece_at(square, attacker)
    return max(0, victim_value - _see(b, square, not side))


def _hanging_squares(board: chess.Board,
                     color: Optional[bool] = None) -> List[chess.Square]:
    """Squares where the side that just moved left a piece en prise.

    A piece is en prise only when the opponent can capture it and come out
    ahead through the recapture sequence — i.e. it is genuinely loseable. A
    static-exchange evaluation decides this; the old attacker/defender count
    misread a rook-and-king "attacking" a pawn-defended knight as a hanging
    piece even though the pawn recaptures the rook. Kings are excluded.
    """
    mover = color if color is not None else (not board.turn)
    enemy = not mover
    hanging: List[chess.Square] = []
    for square, piece in board.piece_map().items():
        if piece.color != mover or piece.piece_type == chess.KING:
            continue
        if not board.is_attacked_by(enemy, square):
            continue
        if _see(board, square, enemy) > 0:
            hanging.append(square)
    return hanging


# Value of a fork target when ranking forks. The king is priceless — a royal
# fork (a check that also attacks a piece) is the whole story, even though the
# king's material value is nil.
_FORK_TARGET_VALUE = {
    chess.PAWN: 1,
    chess.KNIGHT: 3,
    chess.BISHOP: 3,
    chess.ROOK: 5,
    chess.QUEEN: 9,
    chess.KING: 100,
}


def _opponent_forks(board: chess.Board) -> List[Dict[str, Any]]:
    """Opponent moves where the *moved* piece attacks two or more of the
    mover's pieces (a fork). Kings count, so a check that also attacks a piece
    is a royal fork. Sorted most valuable first.

    Only the moved piece's own attacks count. Counting every square the whole
    army attacks after the move is what turned every quiet move (even ``Kh1``)
    into a "fork" — the bishops, knights and pawns keep their old attacks on
    the board no matter what moved.
    """
    opp = board.turn
    mover = not opp
    forks: List[Dict[str, Any]] = []
    for move in board.legal_moves:
        try:
            san = board.san(move)
        except Exception:
            san = move.uci()
        board.push(move)
        targets: List[chess.Square] = []
        moved_sq = move.to_square
        if board.piece_at(moved_sq) is not None:
            for square, piece in board.piece_map().items():
                if piece.color == mover and moved_sq in board.attackers(opp, square):
                    targets.append(square)
        if len(targets) >= 2:
            targets.sort(
                key=lambda s: _FORK_TARGET_VALUE.get(board.piece_at(s).piece_type, 0),
                reverse=True,
            )
            value = sum(
                _FORK_TARGET_VALUE.get(board.piece_at(s).piece_type, 0)
                for s in targets
            )
            forks.append({
                "san": san,
                "squares": [chess.square_name(s) for s in targets],
                "value": value,
            })
        board.pop()
    forks.sort(key=lambda f: -f["value"])
    return forks


def _fork_phrase(f: Dict[str, Any], board: chess.Board) -> str:
    """Plain-language description of a fork's two most valuable targets.

    Phrased as a threat the move allowed, not as the opponent's actual reply —
    the opponent may have chosen a different (sometimes better) move.
    """
    named = []
    for square_name in f["squares"][:2]:
        piece = board.piece_at(chess.parse_square(square_name))
        pname = chess.piece_name(piece.piece_type) if piece else "piece"
        named.append(f"the {pname} on {square_name}")
    return f"allowed a fork: {f['san']} attacks {' and '.join(named)}"


def _fork_after_move(board_after: chess.Board,
                     reply: Optional[chess.Move]) -> Optional[Dict[str, Any]]:
    """Return a fork dict when the opponent's *actual* best reply ``reply``
    forks the mover's pieces, else None.

    A fork is only worth reporting when it is the move the engine actually
    prefers as the refutation. Scanning every legal move for "a move that
    attacks two of the mover's pieces" is what made every quiet move read as a
    fork — there is almost always SOME forking move available somewhere on the
    board, so the old scan described forks that never happened.
    """
    if reply is None:
        return None
    board = board_after.copy()
    opp = board.turn
    mover = not opp
    try:
        san = board.san(reply)
    except Exception:
        san = reply.uci()
    moved_sq = reply.to_square
    board.push(reply)
    # A "fork" means the reply attacks two or more of the mover's real pieces
    # (knight, bishop, rook, queen, king). Attacking two pawns is noise — it
    # would otherwise label every recapture that hits a couple of pawns a fork.
    targets = [
        sq for sq, piece in board.piece_map().items()
        if piece.color == mover
        and piece.piece_type != chess.PAWN
        and moved_sq in board.attackers(opp, sq)
    ]
    if len(targets) < 2:
        return None
    # A non-royal fork only counts when it actually wins material: at least one
    # attacked (non-king) piece must be en prise. A double attack on two
    # defended pieces wins nothing, so it is not a blunder motif.
    royal = any(board.piece_at(s).piece_type == chess.KING for s in targets)
    if not royal:
        won = [
            s for s in targets
            if board.piece_at(s).piece_type != chess.KING
            and _see(board, s, opp) > 0
        ]
        if not won:
            return None
    targets.sort(
        key=lambda s: _FORK_TARGET_VALUE.get(board.piece_at(s).piece_type, 0),
        reverse=True,
    )
    return {
        "san": san,
        "squares": [chess.square_name(s) for s in targets],
        "value": sum(
            _FORK_TARGET_VALUE.get(board.piece_at(s).piece_type, 0)
            for s in targets
        ),
    }


_SLIDER_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


def _pin_info(board: chess.Board, square: chess.Square) -> Optional[Dict[str, Any]]:
    """If the piece on ``square`` is pinned by an enemy slider, return
    ``{"slider_square", "shielded_square"}``; else None.

    A pin counts only when the pinned piece shields its own king or a piece
    worth more than itself along a bishop/rook/queen line — a real tactical
    liability, not merely "an enemy piece attacks this square".
    """
    piece = board.piece_at(square)
    if piece is None or piece.piece_type == chess.KING:
        return None
    color = piece.color
    enemy = not color
    piece_val = _FORK_TARGET_VALUE.get(piece.piece_type, 0)
    f, r = chess.square_file(square), chess.square_rank(square)
    for df, dr in _SLIDER_DIRS:
        ff, rr, shielded_sq = f + df, r + dr, None
        while 0 <= ff < 8 and 0 <= rr < 8:
            s = chess.square(ff, rr)
            if board.piece_at(s) is not None:
                shielded_sq = s
                break
            ff += df
            rr += dr
        if shielded_sq is None:
            continue
        shielded = board.piece_at(shielded_sq)
        if shielded.color != color:
            continue
        ff, rr, slider_sq = f - df, r - dr, None
        while 0 <= ff < 8 and 0 <= rr < 8:
            s = chess.square(ff, rr)
            p = board.piece_at(s)
            if p is not None:
                if p.color == enemy and p.piece_type in (chess.BISHOP, chess.ROOK, chess.QUEEN):
                    slider_sq = s
                break
            ff -= df
            rr -= dr
        if slider_sq is None:
            continue
        slider = board.piece_at(slider_sq)
        if slider.piece_type == chess.BISHOP and df * dr == 0:
            continue
        if slider.piece_type == chess.ROOK and df * dr != 0:
            continue
        if shielded.piece_type == chess.KING:
            return {"slider_square": slider_sq, "shielded_square": shielded_sq}
        if piece.piece_type == chess.PAWN:
            # A pawn shielding a non-king piece is ordinary pawn structure
            # (e.g. a rook behind a pawn chain), not a tactical pin. Only an
            # absolute pin against the king is worth reporting for a pawn.
            continue
        if _FORK_TARGET_VALUE.get(shielded.piece_type, 0) > piece_val:
            return {"slider_square": slider_sq, "shielded_square": shielded_sq}
    return None


def _pinned_pieces(board: chess.Board,
                   color: Optional[bool] = None) -> List[chess.Square]:
    """Squares of the given side (default: side that just moved) whose piece
    is genuinely pinned."""
    mover = color if color is not None else (not board.turn)
    return [
        sq for sq, p in board.piece_map().items()
        if p.color == mover and p.piece_type != chess.KING and _pin_info(board, sq)
    ]


def _pin_phrase(board: chess.Board, square: chess.Square) -> str:
    piece = board.piece_at(square)
    info = _pin_info(board, square)
    pname = chess.piece_name(piece.piece_type) if piece else "piece"
    shielded = board.piece_at(info["shielded_square"])
    if shielded.piece_type == chess.KING:
        return f"pinned the {pname} on {chess.square_name(square)} to the king"
    sname = chess.piece_name(shielded.piece_type)
    return (
        f"pinned the {pname} on {chess.square_name(square)} to the "
        f"{sname} on {chess.square_name(info['shielded_square'])}"
    )


def _skewer_info(board: chess.Board, square: chess.Square) -> Optional[Dict[str, Any]]:
    """If the piece on ``square`` is skewered by an enemy slider — a LESS
    valuable same-color piece sits behind it on the slider's line, so moving
    the front piece loses the piece behind — return
    ``{"slider_square", "behind_square", "front_square"}``; else None."""
    piece = board.piece_at(square)
    if piece is None or piece.piece_type == chess.KING:
        return None
    color = piece.color
    enemy = not color
    piece_val = _FORK_TARGET_VALUE.get(piece.piece_type, 0)
    f, r = chess.square_file(square), chess.square_rank(square)
    for df, dr in _SLIDER_DIRS:
        ff, rr, behind_sq = f + df, r + dr, None
        while 0 <= ff < 8 and 0 <= rr < 8:
            s = chess.square(ff, rr)
            p = board.piece_at(s)
            if p is not None:
                if p.color == color:
                    behind_sq = s
                break
            ff += df
            rr += dr
        if behind_sq is None:
            continue
        behind = board.piece_at(behind_sq)
        if behind.piece_type == chess.KING:
            continue
        if _FORK_TARGET_VALUE.get(behind.piece_type, 0) >= piece_val:
            continue
        ff, rr, slider_sq = f - df, r - dr, None
        while 0 <= ff < 8 and 0 <= rr < 8:
            s = chess.square(ff, rr)
            p = board.piece_at(s)
            if p is not None:
                if p.color == enemy and p.piece_type in (chess.BISHOP, chess.ROOK, chess.QUEEN):
                    slider_sq = s
                break
            ff -= df
            rr -= dr
        if slider_sq is None:
            continue
        slider = board.piece_at(slider_sq)
        if slider.piece_type == chess.BISHOP and df * dr == 0:
            continue
        if slider.piece_type == chess.ROOK and df * dr != 0:
            continue
        return {"slider_square": slider_sq, "behind_square": behind_sq, "front_square": square}
    return None


def _skewered_pieces(board: chess.Board,
                     color: Optional[bool] = None) -> List[chess.Square]:
    """Squares of the given side (default: side that just moved) whose piece
    is genuinely skewered."""
    mover = color if color is not None else (not board.turn)
    return [
        sq for sq, p in board.piece_map().items()
        if p.color == mover and p.piece_type != chess.KING and _skewer_info(board, sq)
    ]


def _skewer_phrase(board: chess.Board, square: chess.Square) -> str:
    info = _skewer_info(board, square)
    front = board.piece_at(square)
    behind = board.piece_at(info["behind_square"])
    fname = chess.piece_name(front.piece_type) if front else "piece"
    bname = chess.piece_name(behind.piece_type) if behind else "piece"
    return (
        f"skewered the {fname} on {chess.square_name(square)} to the "
        f"{bname} on {chess.square_name(info['behind_square'])}"
    )


def _reply_mate(board_after: chess.Board,
                reply: Optional[chess.Move]) -> Optional[str]:
    """Return the SAN of the opponent's engine-best reply when it delivers
    checkmate immediately (mate in one); else None."""
    if reply is None:
        return None
    board = board_after.copy()
    try:
        san = board.san(reply)
        board.push(reply)
    except Exception:
        return None
    return san if board.is_checkmate() else None


def _is_back_rank_mate(board_after: chess.Board,
                       reply: Optional[chess.Move]) -> bool:
    """True when ``reply`` delivers a back-rank mate: the mated king is on
    rank 1 or 8 and the mating piece is a rook or queen on that same rank."""
    if reply is None:
        return False
    board = board_after.copy()
    try:
        board.push(reply)
    except Exception:
        return False
    if not board.is_checkmate():
        return False
    mated = board.turn  # after the reply, board.turn is the side in checkmate
    king_sq = board.king(mated)
    if king_sq is None:
        return False
    rank = chess.square_rank(king_sq)
    if rank not in (0, 7):
        return False
    mat = board.piece_at(reply.to_square)
    if mat is None or mat.piece_type not in (chess.ROOK, chess.QUEEN):
        return False
    return chess.square_rank(reply.to_square) == rank


def _trapped_pieces(board: chess.Board,
                    color: Optional[bool] = None) -> List[chess.Square]:
    """Squares of the given side (default: side that just moved) whose piece
    is trapped: it is genuinely en prise (not defended) and has no safe escape
    — every legal move still leaves it capturable, or it cannot move at all.
    Pawns and kings are excluded (a "trapped pawn" is not a meaningful motif)."""
    mover = color if color is not None else (not board.turn)
    mover_board = board.copy()
    mover_board.turn = mover  # legal_moves must be the MOVER's moves
    trapped: List[chess.Square] = []
    for square, piece in board.piece_map().items():
        if piece.color != mover or piece.piece_type in (chess.KING, chess.PAWN):
            continue
        if _see(board, square, not mover) <= 0:
            continue  # defended where it stands — not trapped, not even en prise
        moves = [m for m in mover_board.legal_moves if m.from_square == square]
        if not moves:
            trapped.append(square)
            continue
        safe = False
        for m in moves:
            b2 = mover_board.copy()
            b2.push(m)
            if b2.piece_at(m.to_square) is None:
                continue
            if _see(b2, m.to_square, b2.turn) <= 0:
                safe = True
                break
        if not safe:
            trapped.append(square)
    return trapped


def _trapped_phrase(board: chess.Board, square: chess.Square) -> str:
    piece = board.piece_at(square)
    name = chess.piece_name(piece.piece_type) if piece else "piece"
    return f"trapped the {name} on {chess.square_name(square)} with no safe escape"


def _new_motifs(detector, board_before, board_after, mover) -> List[chess.Square]:
    """Squares ``detector`` flags in ``board_after`` that it did NOT already
    flag in ``board_before`` — i.e. motifs the move actually caused."""
    before = set(detector(board_before, mover)) if board_before is not None else set()
    after = detector(board_after, mover)
    return [sq for sq in after if sq not in before]


def _blunder_threat(board_after: chess.Board,
                    reply: Optional[chess.Move] = None,
                    board_before: Optional[chess.Board] = None) -> Optional[str]:
    """Classify the immediate tactical damage of a just-played move.

    Returns a short label ("walked into mate", "walked into a fork",
    "walked into a pin", "walked into a skewer", "trapped a piece",
    "hung a piece") or None. Mate and fork are claimed only from the
    opponent's engine-best reply; pin, skewer, trap and hang are static but
    only reported when the move itself caused them (not pre-existing).
    """
    board = board_after.copy()
    mover = not board.turn
    if _reply_mate(board, reply) is not None:
        return "walked into mate"
    if _fork_after_move(board, reply) is not None:
        return "walked into a fork"
    if _new_motifs(_pinned_pieces, board_before, board, mover):
        return "walked into a pin"
    if _new_motifs(_skewered_pieces, board_before, board, mover):
        return "walked into a skewer"
    if _new_motifs(_trapped_pieces, board_before, board, mover):
        return "trapped a piece"
    if _new_motifs(_hanging_squares, board_before, board, mover):
        return "hung a piece"
    return None


def _threat_detail(board_after: chess.Board,
                   reply: Optional[chess.Move] = None,
                   board_before: Optional[chess.Board] = None) -> Optional[str]:
    """Plain-language detail of the tactical damage, computed in code.

    Used by the report so the LLM never has to read a raw FEN and hallucinate
    piece positions. Mate and fork are reported only from the opponent's actual
    best reply; otherwise a pin, skewer, trap, then a hung piece, is named —
    and only when the move itself caused it.
    """
    board = board_after.copy()
    mover = not board.turn
    mate = _reply_mate(board, reply)
    if mate:
        if _is_back_rank_mate(board, reply):
            return f"allowed a back-rank mate ({mate})"
        return f"allowed checkmate ({mate})"
    fork = _fork_after_move(board, reply)
    if fork:
        return _fork_phrase(fork, board)
    pinned = _new_motifs(_pinned_pieces, board_before, board, mover)
    if pinned:
        return _pin_phrase(board, pinned[0])
    skewered = _new_motifs(_skewered_pieces, board_before, board, mover)
    if skewered:
        return _skewer_phrase(board, skewered[0])
    trapped = _new_motifs(_trapped_pieces, board_before, board, mover)
    if trapped:
        return _trapped_phrase(board, trapped[0])
    hung = _new_motifs(_hanging_squares, board_before, board, mover)
    if hung:
        square = hung[0]
        piece = board.piece_at(square)
        name = chess.piece_name(piece.piece_type) if piece else "piece"
        return f"left a {name} on {chess.square_name(square)} hanging"
    return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Game fetching
# ---------------------------------------------------------------------------

def fetch_lichess_games(username: str, max_games: int = 50, since_days: int = 30) -> List[Dict[str, Any]]:
    """Fetch recent games for a Lichess user.

    Returns a list of game dicts with keys: source, external_id, pgn, white,
    black, result, played_at.
    """
    since_ms = int((_now_utc() - timedelta(days=since_days)).timestamp() * 1000)
    url = LICHESS_GAMES_URL.format(username=username)
    params = {"max": max_games, "since": since_ms, "pgnInJson": "true", "opening": "true"}
    resp = requests.get(
        url,
        params=params,
        headers={"Accept": "application/x-ndjson", "User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()

    games: List[Dict[str, Any]] = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        pgn = data.get("pgn") or data.get("moves") or ""
        if not pgn:
            continue
        players = data.get("players", {}) or {}
        opening = data.get("opening") or {}
        games.append({
            "source": "lichess",
            "external_id": str(data.get("id") or ""),
            "pgn": pgn,
            "white": (players.get("white") or {}).get("user", {}).get("name", "White"),
            "black": (players.get("black") or {}).get("user", {}).get("name", "Black"),
            "result": _lichess_result(data),
            "played_at": data.get("createdAt"),
            "opening": (opening.get("name") or "").strip() or None,
        })
        if len(games) >= max_games:
            break
    return games


def _lichess_result(data: Dict[str, Any]) -> str:
    status = data.get("status") or ""
    winner = data.get("winner")
    if status == "draw":
        return "1/2-1/2"
    if winner == "white":
        return "1-0"
    if winner == "black":
        return "0-1"
    return "*"


def fetch_chesscom_games(username: str, max_games: int = 50, since_days: int = 30) -> List[Dict[str, Any]]:
    """Fetch recent games for a chess.com user.

    Returns a list of game dicts with keys: source, external_id, pgn, white,
    black, result, played_at.
    """
    archives_resp = requests.get(
        CHESSCOM_ARCHIVES_URL.format(username=username),
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    archives_resp.raise_for_status()
    archives = archives_resp.json().get("archives", [])

    cutoff = _now_utc() - timedelta(days=since_days)
    games: List[Dict[str, Any]] = []
    # Archives are oldest-first; walk newest-first so we stop early.
    for archive_url in reversed(archives):
        if len(games) >= max_games:
            break
        month_resp = requests.get(archive_url, headers={"User-Agent": USER_AGENT}, timeout=30)
        month_resp.raise_for_status()
        month_games = month_resp.json().get("games", [])
        for g in reversed(month_games):
            if len(games) >= max_games:
                break
            end_time = g.get("end_time")
            if end_time:
                played = datetime.utcfromtimestamp(int(end_time)).replace(tzinfo=timezone.utc)
                if played < cutoff:
                    continue
            pgn = g.get("pgn") or ""
            if not pgn:
                continue
            games.append({
                "source": "chesscom",
                "external_id": str(g.get("url") or g.get("uuid") or ""),
                "pgn": pgn,
                "white": (g.get("white") or {}).get("username", "White"),
                "black": (g.get("black") or {}).get("username", "Black"),
                "result": _chesscom_result(g),
                "played_at": end_time,
                "opening": _chesscom_opening(g.get("eco")),
            })
    return games[:max_games]


def _chesscom_opening(eco_url: Any) -> Optional[str]:
    """Extract a human opening name from a chess.com ``eco`` URL slug."""
    if not eco_url or not isinstance(eco_url, str):
        return None
    slug = eco_url.rstrip("/").rsplit("/", 1)[-1]
    words: List[str] = []
    for part in slug.split("-"):
        # The move sequence begins at the first token starting with a digit.
        if part and part[0].isdigit():
            break
        if part:
            words.append(part)
    name = " ".join(words).strip()
    return name or None


def _chesscom_result(g: Dict[str, Any]) -> str:
    white = (g.get("white") or {}).get("result")
    black = (g.get("black") or {}).get("result")
    if white == "win":
        return "1-0"
    if black == "win":
        return "0-1"
    if white in ("agreed", "repetition", "stalemate", "insufficient", "timevsinsufficient") or \
            black in ("agreed", "repetition", "stalemate", "insufficient", "timevsinsufficient"):
        return "1/2-1/2"
    return "*"


# ---------------------------------------------------------------------------
# Engine analysis
# ---------------------------------------------------------------------------

INSTANT_SECONDS: float = 3.0  # A move played faster than this is reflex.

_CLOCK_RE = re.compile(r"\[%clk\s+(\d+):(\d+):(\d+)\]|(\d+(?::\d+){0,2})\s*([sm])\b")


def _parse_clock_seconds(comment: str) -> Optional[float]:
    """Seconds remaining on the player's clock from a PGN move comment.

    Handles ``{3s}`` / ``{1:01m}`` / ``{book 0s}`` and ``[%clk 0:03:00]``.
    Returns None when the comment has no clock.
    """
    if not comment:
        return None
    m = _CLOCK_RE.search(comment)
    if not m:
        return None
    if m.group(1) is not None:  # [%clk h:mm:ss]
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    raw, unit = m.group(4), m.group(5)
    parts = [int(x) for x in raw.split(":")]
    if unit == "m":
        return parts[0] * 60 + (parts[1] if len(parts) > 1 else 0)
    if len(parts) == 1:
        return float(parts[0])
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _fmt_seconds(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _puzzle_fen(pgn: str, initial_ply: int) -> Optional[str]:
    """FEN of the position ``initial_ply`` plies into a game PGN.

    The Lichess puzzle API includes ``puzzle.fen``, but this is kept as a
    fallback for payloads that omit it: replay the game PGN to the puzzle's
    starting ply with python-chess.
    """
    try:
        node = chess.pgn.read_game(io.StringIO(pgn or ""))
        if node is None:
            return None
        children = [c for c in node.mainline() if c.move is not None]
        if not children:
            return None
        start_fen = (node.headers.get("FEN") or "").strip()
        board = chess.Board(start_fen) if start_fen else chess.Board()
        for ply, child in enumerate(children, start=1):
            if ply > int(initial_ply or 0):
                break
            board.push(child.move)
        return board.fen()
    except Exception:
        return None


def _chesscom_puzzle_solution(pgn: str) -> List[str]:
    """UCI moves of the chess.com daily-puzzle solution line.

    chess.com's puzzle PGN starts at the puzzle position (its ``FEN`` header)
    and its mainline IS the solution, alternating player/opponent moves.
    """
    try:
        node = chess.pgn.read_game(io.StringIO(pgn or ""))
        if node is None:
            return []
        return [c.move.uci() for c in node.mainline() if c.move is not None]
    except Exception:
        return []


def _time_stats(reports: List[Dict[str, Any]], kid_color: Optional[str]) -> Optional[Dict[str, Any]]:
    """Clock-based insight: do mistakes get less thought than sound moves?"""
    rows = [
        m for r in reports for m in (r.get("moves_detail") or [])
        if (not kid_color or m.get("color") == kid_color)
    ]
    timed = [m for m in rows if m.get("seconds") is not None]
    if not timed:
        return None
    errors = [m for m in timed if m.get("class") in ("blunder", "mistake", "inaccuracy")]
    clean = [m for m in timed if m.get("class") in ("good", "excellent", "best")]

    def _median(items: List[Dict[str, Any]]) -> Optional[float]:
        vals = sorted(m["seconds"] for m in items)
        if not vals:
            return None
        mid = len(vals) // 2
        return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2

    return {
        "moves_with_clock": len(timed),
        "instant_moves": sum(1 for m in timed if m["seconds"] < INSTANT_SECONDS),
        "reflex_errors": [m for m in errors if m["seconds"] < INSTANT_SECONDS],
        "median_clean": _median(clean),
        "median_error": _median(errors),
    }


def analyze_game(game: Dict[str, Any], engine: chess.engine.SimpleEngine,
                 depth: int = ANALYSIS_DEPTH) -> Dict[str, Any]:
    """Analyze a single game (dict with a ``pgn`` key) with a chess engine.

    Returns a "report" dict describing blunders, phase breakdown, points lost
    and habit tags for this game.
    """
    pgn = (game or {}).get("pgn") or ""
    node = chess.pgn.read_game(io.StringIO(pgn))

    def _header(key: str) -> Optional[str]:
        """A cleaned PGN header value, or None when absent or '?'."""
        if node is None:
            return None
        val = (node.headers.get(key) or "").strip()
        return val if val and val != "?" else None

    white = (game or {}).get("white") or _header("White")
    black = (game or {}).get("black") or _header("Black")
    result = (game or {}).get("result") or _header("Result")
    empty: Dict[str, Any] = {
        "source": (game or {}).get("source"),
        "external_id": (game or {}).get("external_id"),
        "white": white,
        "black": black,
        "result": result,
        "played_at": (game or {}).get("played_at"),
        "opening": (game or {}).get("opening"),
        "pgn": pgn,
        "blunders": [],
        "phase_blunders": {"opening": 0, "middlegame": 0, "endgame": 0},
        "points_lost": 0.0,
        "points_lost_white": 0.0,
        "points_lost_black": 0.0,
        "moves": 0,
        "acpl": 0,
        "evals": [],
        "moves_detail": [],
        "class_counts": {},
        "class_counts_white": {},
        "class_counts_black": {},
        "accuracy": 0,
        "accuracy_white": 0,
        "accuracy_black": 0,
        "habit_tags": [],
    }
    if node is None:
        return empty

    board = chess.Board()
    blunders: List[Dict[str, Any]] = []
    total_cp_lost = 0
    total_cp_white = 0
    total_cp_black = 0
    total_cp_lost_all = 0
    total_moves = 0
    evals: List[List[Any]] = []
    class_counts: Dict[str, int] = {}
    class_white: Dict[str, int] = {}
    class_black: Dict[str, int] = {}
    accuracies: List[float] = []
    acc_white: List[float] = []
    acc_black: List[float] = []
    moves_detail: List[Dict[str, Any]] = []
    prev_was_capture = False

    for ply, child in enumerate((c for c in node.mainline() if c.move is not None), start=1):
        move = child.move
        seconds = _parse_clock_seconds(child.comment or "")
        after_capture = prev_was_capture
        mover = board.turn
        try:
            san = board.san(move)
        except Exception:
            san = str(move)
        info_before = None
        try:
            info_before = engine.analyse(board, chess.engine.Limit(depth=depth))
            score_before = _score_to_cp(info_before["score"], mover)
        except Exception:
            score_before = 0
        best_san = None
        best_uci = None
        pv_line = None
        fen_before = board.fen()
        try:
            pv = info_before.get("pv") if isinstance(info_before, dict) else None
            if pv:
                best_san = board.san(pv[0])
                best_uci = pv[0].uci()
                tmp = board.copy()
                pv_sans = []
                for m in pv[:4]:
                    pv_sans.append(tmp.san(m))
                    tmp.push(m)
                pv_line = " ".join(pv_sans)
        except Exception:
            best_san = None
            best_uci = None
            pv_line = None
        is_capture = board.is_capture(move)
        board_before = board.copy()
        board.push(move)
        info_after = None
        try:
            info_after = engine.analyse(board, chess.engine.Limit(depth=depth))
            score_after = _score_to_cp(info_after["score"], mover)
        except Exception:
            score_after = score_before

        cp_loss = score_before - score_after
        # Playing the engine's own top move is never a blunder. Guard against
        # mate-score sign flips: delivering the mate Stockfish also found makes
        # score_after read as Mate(0), which would otherwise look like a loss.
        if best_uci is not None and move.uci() == best_uci:
            cp_loss = 0
        else:
            cp_loss = _cp_loss(score_before, score_after)
        total_moves += 1
        total_cp_lost_all += max(0, cp_loss)
        evals.append([ply, score_after, best_uci])
        loss_pct = max(0.0, _winning_chances(score_before) - _winning_chances(score_after))
        class_label = _classify_loss(loss_pct)
        class_counts[class_label] = class_counts.get(class_label, 0) + 1
        bucket = class_white if mover == chess.WHITE else class_black
        bucket[class_label] = bucket.get(class_label, 0) + 1
        move_accuracy = _accuracy_from_loss(loss_pct)
        accuracies.append(move_accuracy)
        (acc_white if mover == chess.WHITE else acc_black).append(move_accuracy)
        moves_detail.append({
            "ply": ply,
            "color": "white" if mover == chess.WHITE else "black",
            "san": san,
            "class": class_label,
            "seconds": seconds,
            "after_capture": after_capture,
        })
        if cp_loss >= BLUNDER_THRESHOLD_CP and score_before > -DECISIVE_CP:
            phase = phase_of(ply)
            # NOTE: board.san(move) already includes the correct check (+) /
            # checkmate (#) annotation, so never append another one.
            # The opponent's engine-best reply (pv[0] of the post-move
            # analysis) is what fork detection is grounded in — never a
            # speculative scan of every legal move.
            reply = None
            if isinstance(info_after, dict) and info_after.get("pv"):
                reply = info_after["pv"][0]
            blunders.append({
                "ply": ply,
                "color": "white" if mover == chess.WHITE else "black",
                "san": san,
                "best": best_san,
                "phase": phase,
                "cp_loss": cp_loss,
                "loss_pct": round(loss_pct, 1),
                "class": class_label,
                "fen": fen_before,
                "line": pv_line,
                "threat": _blunder_threat(board, reply, board_before),
                "threat_detail": _threat_detail(board, reply, board_before),
                "seconds": seconds,
                "after_capture": after_capture,
            })
            total_cp_lost += cp_loss
            if mover == chess.WHITE:
                total_cp_white += cp_loss
            else:
                total_cp_black += cp_loss
        prev_was_capture = is_capture

    phase_blunders = {"opening": 0, "middlegame": 0, "endgame": 0}
    for b in blunders:
        phase_blunders[b["phase"]] += 1

    # A PGN pasted without a Result header still ends in a checkmate the engine
    # walk just replayed. Infer the outcome from the final position so the
    # report never says "result unknown" for a game that ended in mate.
    if not result or result in ("?", "*", ""):
        if board.is_checkmate():
            result = "0-1" if board.turn == chess.WHITE else "1-0"
        elif board.is_stalemate() or board.is_insufficient_material():
            result = "1/2-1/2"

    return {
        "source": (game or {}).get("source"),
        "external_id": (game or {}).get("external_id"),
        "white": white,
        "black": black,
        "result": result,
        "played_at": (game or {}).get("played_at"),
        "opening": (game or {}).get("opening"),
        "pgn": pgn,
        "blunders": blunders,
        "phase_blunders": phase_blunders,
        "points_lost": round(total_cp_lost / 100.0, 2),
        "points_lost_white": round(total_cp_white / 100.0, 2),
        "points_lost_black": round(total_cp_black / 100.0, 2),
        "moves": total_moves,
        "acpl": round(total_cp_lost_all / max(1, total_moves)),
        "evals": evals,
        "moves_detail": moves_detail,
        "class_counts": class_counts,
        "class_counts_white": class_white,
        "class_counts_black": class_black,
        "accuracy": round(sum(accuracies) / len(accuracies)) if accuracies else 0,
        "accuracy_white": round(sum(acc_white) / len(acc_white)) if acc_white else 0,
        "accuracy_black": round(sum(acc_black) / len(acc_black)) if acc_black else 0,
        "habit_tags": _tag_blunders(blunders),
    }


def _tag_blunders(blunders: List[Dict[str, Any]]) -> List[str]:
    """Map blunders to coarse habit tags (deterministic, no LLM needed)."""
    tags: List[str] = []
    for b in blunders:
        phase = b.get("phase", "middlegame")
        if phase == "opening":
            tags.append("Opening preparation")
        elif phase == "endgame":
            tags.append("Endgame technique")
        else:
            tags.append("Piece safety")
        threat = b.get("threat")
        if threat == "hung a piece":
            tags.append("Hung pieces")
        elif threat == "walked into a fork":
            tags.append("Fork awareness")
        elif threat == "walked into a pin":
            tags.append("Pin awareness")
        elif threat == "walked into a skewer":
            tags.append("Skewer awareness")
        elif threat == "walked into mate":
            tags.append("Mate awareness")
        elif threat == "trapped a piece":
            tags.append("Trapped pieces")
    # De-duplicate while preserving frequency order for aggregation.
    seen = set()
    result = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


# ---------------------------------------------------------------------------
# Habit aggregation
# ---------------------------------------------------------------------------

def aggregate_habits(reports: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate habit tags across per-game reports.

    Returns a list of {"habit": str, "count": int} dicts sorted by descending
    count (then alphabetically for determinism).
    """
    counts: Dict[str, int] = {}
    for report in reports:
        for tag in report.get("habit_tags", []) or []:
            counts[tag] = counts.get(tag, 0) + 1
    return [
        {"habit": habit, "count": count}
        for habit, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

_DRILLS: Dict[str, str] = {
    "Opening preparation": (
        "Rehearse your first ten moves in your two most common openings and write "
        "them down before checking the book — you pass when you nail all ten from "
        "memory."
    ),
    "Piece safety": (
        "Before every move, do a one-second 'hang check' for undefended pieces on "
        "both sides. Do 10 hang-check puzzles a day until the scan is automatic."
    ),
    "Endgame technique": (
        "Practice king-and-pawn endings and simple rook endings against the engine "
        "until you can win the same winning position three times in a row without "
        "slipping."
    ),
    "Hung pieces": (
        "Before every move, run a three-second 'is anything hanging?' scan over "
        "every piece you own — do 10 hang-check puzzles a day until it's automatic."
    ),
    "Fork awareness": (
        "After each opponent move, name every square their pieces attack; the "
        "moment one move hits two of your pieces at once, say 'fork!' and defend "
        "before touching anything."
    ),
}

_DEFAULT_DRILL = (
    "Re-play the key mistake moments from this report on a board and write down a "
    "safer alternative for each one — you pass when you can spot the fix in under "
    "ten seconds."
)


def _make_drill(habit: str, ctx: Dict[str, Any]) -> str:
    """Build a player-specific drill from this report's actual key moments.

    Falls back to the generic habit drill only when there are no moments to
    reference (e.g. a game with no detected blunders).
    """
    moments = ctx.get("moments") or []
    if moments:
        top = moments[0]
        opponent = top.get("opponent") or "your opponent"
        san = top.get("san") or "that move"
        best = top.get("best")
        if best and best != san:
            return (
                f"In the game vs {opponent}, rewind to {san} and play {best} instead — "
                f"then say out loud why it's safer. Repeat for the other key moments "
                f"in this report until the check is automatic."
            )
        return (
            f"Set up the position just before {san} vs {opponent}, name what the move "
            f"left undefended, and find the safer alternative. Repeat for each key "
            f"moment in this report."
        )
    return _DRILLS.get(habit, _DEFAULT_DRILL)


def _safe_best(moment: Dict[str, Any]) -> Optional[str]:
    """The engine's preferred move, or None when it equals the played move."""
    best = moment.get("best")
    return best if best and best != (moment.get("san") or "") else None

# Report document branding. Kept separate from any third-party product name —
# this is the app's own brand.
SITE_NAME: str = os.environ.get("SITE_NAME", "Checkmate Coach")
SITE_URL: str = os.environ.get("SITE_URL", "chess.njxai.com")
SITE_CONTACT: str = os.environ.get("SITE_CONTACT", "info@checkmatecoach.app")

_REPORT_DISCLOSURE = (
    "*Positions analyzed by Stockfish · report generated from engine data only.*"
)


def _played_date(played_at: Any) -> Optional[datetime]:
    """Normalise an epoch (ms) or ISO timestamp to a timezone-aware datetime."""
    if played_at is None:
        return None
    if isinstance(played_at, (int, float)):
        try:
            seconds = float(played_at) / 1000.0 if float(played_at) > 1e12 else float(played_at)
            return datetime.utcfromtimestamp(seconds).replace(tzinfo=timezone.utc)
        except Exception:
            return None
    if isinstance(played_at, str):
        for fmt in ("%Y.%m.%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(played_at, fmt).replace(tzinfo=timezone.utc)
            except Exception:
                continue
    return None


def _outcome_for(report: Dict[str, Any], username: str) -> str:
    """Return 'win', 'loss', 'draw' or 'unknown' from the kid's perspective."""
    result = (report.get("result") or "").strip()
    if result == "1/2-1/2":
        return "draw"
    white = (report.get("white") or "").strip()
    black = (report.get("black") or "").strip()
    uname = (username or "").strip().lower()
    is_white = bool(uname) and white.lower() == uname
    is_black = bool(uname) and black.lower() == uname
    if result in ("1-0", "0-1"):
        if (result == "1-0" and is_white) or (result == "0-1" and is_black):
            return "win"
        if is_white or is_black:
            return "loss"
    return "unknown"


def _opponent_for(report: Dict[str, Any], username: str) -> str:
    white = (report.get("white") or "").strip()
    black = (report.get("black") or "").strip()
    uname = (username or "").strip().lower()
    if uname and black.lower() == uname:
        return white or "Opponent"
    return black or white or "Opponent"


def _outcome_for_color(report: Dict[str, Any], kid_color: Optional[str]) -> str:
    """Outcome ('win'/'loss'/'draw'/'unknown') when the kid's side is known."""
    result = (report.get("result") or "").strip()
    if result == "1/2-1/2":
        return "draw"
    if kid_color == "white":
        if result == "1-0":
            return "win"
        if result == "0-1":
            return "loss"
    elif kid_color == "black":
        if result == "0-1":
            return "win"
        if result == "1-0":
            return "loss"
    return "unknown"


def _opponent_for_color(report: Dict[str, Any], kid_color: Optional[str]) -> str:
    """The opponent's name when the kid's side is known, else a best guess."""
    if kid_color == "white":
        return (report.get("black") or "").strip() or "Opponent"
    if kid_color == "black":
        return (report.get("white") or "").strip() or "Opponent"
    return (report.get("black") or report.get("white") or "Opponent").strip() or "Opponent"


def _date_range(dates: List[datetime]) -> str:
    if not dates:
        return "recent games"
    start, end = min(dates), max(dates)
    if start.date() == end.date():
        return start.strftime("%B %d, %Y")
    if start.year == end.year and start.month == end.month:
        return f"{start.strftime('%B')} {start.day}–{end.day}, {start.year}"
    if start.year == end.year:
        return f"{start.strftime('%B %d')} – {end.strftime('%B %d, %Y')}"
    return f"{start.strftime('%B %d, %Y')} – {end.strftime('%B %d, %Y')}"


def _move_number(ply: Optional[int]) -> str:
    """Standard chess move number for a ply (half-move).

    ply 1 -> "1.", ply 2 -> "1...", ply 25 -> "13.", ply 27 -> "14.".
    Returns "" when the ply is unknown.
    """
    if ply is None:
        return ""
    move_no = (ply + 1) // 2
    suffix = "." if ply % 2 == 1 else "..."
    return f"{move_no}{suffix}"


def build_report_context(reports: List[Dict[str, Any]], platform: str,
                         username: str, habit: str,
                         notes: Optional[str] = None,
                         answers: Optional[List[str]] = None,
                         rating: Optional[int] = None,
                         history: Optional[List[Dict[str, Any]]] = None,
                         kid_color: Optional[str] = None) -> Dict[str, Any]:
    """Summarise analysed games into a compact, LLM-ready context dict."""
    # Which side of the board the kid plays. An explicit ``kid_color`` (from a
    # colour selector on the scoresheet/PGN form) wins; otherwise match the
    # platform username against the game's player names.
    uname = (username or "").strip().lower()
    if kid_color is None and uname:
        for r in reports:
            if (r.get("white") or "").strip().lower() == uname:
                kid_color = "white"
                break
            if (r.get("black") or "").strip().lower() == uname:
                kid_color = "black"
                break

    wins = losses = draws = 0
    games_brief: List[Dict[str, str]] = []
    moments: List[Dict[str, Any]] = []
    dates: List[datetime] = []

    for r in reports:
        outcome = _outcome_for_color(r, kid_color) if kid_color else _outcome_for(r, username)
        if outcome == "win":
            wins += 1
        elif outcome == "loss":
            losses += 1
        elif outcome == "draw":
            draws += 1
        opponent = _opponent_for_color(r, kid_color) if kid_color else _opponent_for(r, username)
        games_brief.append({
            "opponent": opponent,
            "outcome": outcome,
            "result": r.get("result") or "*",
            "opening": r.get("opening") or "",
            # report["moves"] counts plies; surface full moves to the writer.
            "moves": ((r.get("moves") or 0) + 1) // 2,
            "points_lost": r.get("points_lost") or 0.0,
            "acpl": r.get("acpl") or 0,
            "accuracy": r.get("accuracy") or 0,
        })
        # Only the kid's own mistakes belong in a kid's report. When the kid's
        # side is unknown, keep all blunders and label each one's colour below.
        kid_blunders = [
            b for b in (r.get("blunders") or [])
            if (not kid_color) or (b.get("color") == kid_color)
        ]
        for b in sorted(kid_blunders, key=lambda b: -(b.get("cp_loss") or 0))[:2]:
            moments.append({
                "san": b.get("san") or "?",
                "best": b.get("best"),
                "ply": b.get("ply"),
                "move": _move_number(b.get("ply")),
                "color": b.get("color"),
                "phase": b.get("phase") or "middlegame",
                "cp_loss": round((b.get("cp_loss") or 0) / 100.0, 1),
                "opponent": opponent,
                "fen": b.get("fen"),
                "line": b.get("line"),
                "threat": b.get("threat"),
                "threat_detail": b.get("threat_detail"),
                "candidates": b.get("candidates") or [],
            })
        played = _played_date(r.get("played_at"))
        if played:
            dates.append(played)

    moments = sorted(moments, key=lambda m: -m["cp_loss"])[:4]
    acpl_values = [g["acpl"] for g in games_brief if g.get("acpl")]
    avg_acpl = round(sum(acpl_values) / len(acpl_values)) if acpl_values else 0
    acc_values = [g["accuracy"] for g in games_brief if g.get("accuracy")]
    avg_accuracy = round(sum(acc_values) / len(acc_values)) if acc_values else 0
    openings = sorted({g["opening"] for g in games_brief if g.get("opening")})

    # Accuracy and move-quality, split by colour. A kid's report must never
    # claim the opponent's good moves as their own.
    acc_w = [r.get("accuracy_white") or 0 for r in reports]
    acc_b = [r.get("accuracy_black") or 0 for r in reports]
    accuracy_white = round(sum(acc_w) / len(acc_w)) if acc_w else 0
    accuracy_black = round(sum(acc_b) / len(acc_b)) if acc_b else 0

    class_counts: Dict[str, int] = {}
    class_white: Dict[str, int] = {}
    class_black: Dict[str, int] = {}
    for r in reports:
        for label, count in (r.get("class_counts") or {}).items():
            class_counts[label] = class_counts.get(label, 0) + int(count)
        for label, count in (r.get("class_counts_white") or {}).items():
            class_white[label] = class_white.get(label, 0) + int(count)
        for label, count in (r.get("class_counts_black") or {}).items():
            class_black[label] = class_black.get(label, 0) + int(count)

    platform_label = {
        "chesscom": "Chess.com",
        "lichess": "Lichess",
    }.get(platform, platform or "online")

    time_stats = _time_stats(reports, kid_color)

    return {
        "platform": platform_label,
        "game_count": len(reports),
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "date_range": _date_range(dates),
        "habit": habit,
        "acpl": avg_acpl,
        "avg_accuracy": avg_accuracy,
        "kid_color": kid_color,
        "accuracy_white": accuracy_white,
        "accuracy_black": accuracy_black,
        "rating": rating,
        "openings": openings,
        "class_counts": class_counts,
        "class_counts_white": class_white,
        "class_counts_black": class_black,
        "time_stats": time_stats,
        "games_brief": games_brief,
        "moments": moments,
        "notes": (notes or "").strip(),
        "answers": [str(a).strip() for a in (answers or []) if a and str(a).strip()],
        "history": history or [],
    }


_REPORT_SYSTEM = (
    "You are the head coach at {site_name} — a strong, plain-spoken chess coach "
    "who explains ideas the way a GM would: concrete, positional, and grounded "
    "in the actual position. You make kids better AND keep it fun, but you never "
    "write generic encouragement or fill in a form.\n\n"
    "Rules:\n"
    "- Ground every claim in the facts (played move, engine's preferred move, "
    "the engine's line, the position FEN, the cost in pawns, the opponent).\n"
    "- Copy move notation EXACTLY as given (+ / # / x / O-O / O-O-O). Never "
    "guess, add, or change it.\n"
    "- Each Moment uses five bold labels: **Why it looked good**, **Why it "
    "failed**, **What the engine recommends**, **Concept**, **Pattern**. The "
    "'Why it failed' and 'What the engine recommends' lines are FIXED facts — "
    "keep them exactly as given; never add or remove a move, square, piece, "
    "or capture.\n"
    "- NEVER describe the board or invent pieces, squares, or material. You have "
    "no board view — only the facts given (the played move, the engine's preferred "
    "move and line, the cost, and the 'what happened' note). Explain using exactly "
    "those facts; never add a square or piece that is not in them.\n"
    "- VARY YOUR WRITING: never repeat the same sentence opener, the same "
    "'Fix:' formula, or the same drill template twice. Every moment should read "
    "like a different paragraph, not a re-filled form. Vary how you deliver the "
    "takeaway — sometimes bold, sometimes a sentence, sometimes a question.\n"
    "- Be energetic and specific: short punchy sentences, occasional "
    "exclamation, real chess slang. No filler like 'chess is a game of...'.\n"
    "- Write so a kid and their parent can both follow it.\n"
    "- Never invent moves, names, ratings, dates, or match results; stick to "
    "the facts given. If a game's outcome is unknown, state the raw score "
    "(e.g. 'ended 1-0') — never claim a draw or a win that is not in the facts.\n"
    "- The ONLY moves and squares that may appear in the report are those named "
    "in the facts (played moves, engine-preferred moves, engine lines, "
    "alternative moves, and the quoted tactical issues). Never add a move, "
    "square, piece, or capture that is not in them.\n"
    "- When a rating is given, pitch the vocabulary and depth to that level — "
    "simple words for beginners, sharper shorthand for stronger kids.\n"
    "- Use every extra fact you are given: move accuracy, the type of mistake "
    "(hung piece, fork), and alternative moves with their evaluations. Show "
    "the player what else was on the table.\n"
    "- The played move and the engine's preferred move are FIXED facts. Quote "
    "them exactly as given — never change, re-annotate, or invent a move, and "
    "never write the same move on both sides of 'instead of'. The moment "
    "headings already name the moves, so do not rename them, and never reuse "
    "the same moment for Moment 1 and Moment 2.\n"
    "- When past progress is given, reference it — name what improved and what "
    "came back, so the player sees their own trajectory across reports.\n"
    "- OPENING POLICY: do not critique opening moves (1–10) unless one is a "
    "blunder or misses a tactic. Acknowledge the opening by name and move on; "
    "spend the words on the middlegame and endgame moments in the facts.\n"
    "- Explain evaluations in pawns (e.g. 'about two pawns', '+1.2 pawns'), "
    "never raw centipawns.\n"
    "- If the engine line ends in checkmate, explain the mating sequence from "
    "the line — name the final checking move.\n"
    "- Coach the move, never the player: say 'that move hung the knight', not "
    "'you are careless'. No labels about the child's character.\n"
    "- Write in Markdown, keep every heading and the '---' rules exactly as "
    "given, and vary the prose between them. Keep the report under ~700 words."
).replace("{site_name}", SITE_NAME)


def _moment_why_failed(m: Optional[Dict[str, Any]]) -> str:
    """Deterministic "Why it failed" fact line for a blunder moment.

    Written in code so the LLM can only keep it verbatim — it can no longer
    contradict Stockfish or invent a square, piece, or capture.
    """
    if not m:
        return "no moments in this set"
    bits = []
    detail = m.get("threat_detail")
    threat = m.get("threat")
    if detail:
        bits.append(f"The tactical issue: {detail}.")
    elif threat:
        bits.append(f"Classified as: {threat}.")
    else:
        bits.append("No single tactical motif was detected here.")
    cost = m.get("cp_loss")
    if cost is not None:
        bits.append(f"The move cost about {cost} pawns.")
    line = m.get("line")
    if line:
        bits.append(f"The engine line was {line}.")
    return " ".join(bits)


def _moment_recommendation(m: Optional[Dict[str, Any]]) -> str:
    """Deterministic "what the engine recommends" line for a blunder moment.

    Mirrors Lichess coach analysis: name Stockfish's top move and its
    continuation line, so the advice is literally what the engine recommends.
    """
    if not m:
        return "no moments in this set"
    best = m.get("best")
    if not best:
        return "No engine alternative was recorded."
    line = m.get("line")
    if line:
        return f"Play {best} — the engine's line is {line}."
    return f"Play {best}."


def _report_user_prompt(kid_name: str, habit: str, game_count: int,
                        drill: str, ctx: Dict[str, Any]) -> str:
    facts = [
        f"Player: {kid_name}",
        f"Games reviewed: {game_count}",
        f"Platform: {ctx.get('platform')}",
        f"Date range: {ctx.get('date_range')}",
        f"Recurring habit: {habit}",
        f"Suggested drill: {drill}",
    ]
    wins, losses, draws = ctx.get("wins") or 0, ctx.get("losses") or 0, ctx.get("draws") or 0
    if wins or losses or draws:
        facts.append(f"Results: {wins} wins, {losses} losses, {draws} draws")
    # Accuracy split by colour: the kid is never credited with the opponent's
    # good moves. When their side is unknown, show both colours honestly.
    kid_color = ctx.get("kid_color")
    acc_w = ctx.get("accuracy_white") or 0
    acc_b = ctx.get("accuracy_black") or 0
    if kid_color == "white" and acc_w:
        facts.append(f"Move accuracy (yours, playing White): {acc_w}%")
    elif kid_color == "black" and acc_b:
        facts.append(f"Move accuracy (yours, playing Black): {acc_b}%")
    elif acc_w or acc_b:
        facts.append(f"Move accuracy — White: {acc_w}%, Black: {acc_b}%")
    elif ctx.get("avg_accuracy"):
        facts.append(f"Average move accuracy (both sides): {ctx.get('avg_accuracy')}%")
    if ctx.get("rating"):
        facts.append(f"Player rating: ~{ctx['rating']}")
    openings = ctx.get("openings") or []
    if openings:
        facts.append("Openings played: " + ", ".join(openings[:6]))
    order = ["blunder", "mistake", "inaccuracy", "good", "excellent", "best"]

    def _class_summary(counts: Dict[str, int]) -> str:
        return (
            ", ".join(f"{label}s: {counts.get(label, 0)}" for label in order if counts.get(label))
            or "no classified moves"
        )

    cc_w = ctx.get("class_counts_white") or {}
    cc_b = ctx.get("class_counts_black") or {}
    if cc_w or cc_b:
        if kid_color == "white":
            facts.append("Move quality (yours, playing White): " + _class_summary(cc_w))
        elif kid_color == "black":
            facts.append("Move quality (yours, playing Black): " + _class_summary(cc_b))
        else:
            facts.append(
                "Move quality — White: " + _class_summary(cc_w)
                + "; Black: " + _class_summary(cc_b)
            )
    elif ctx.get("class_counts"):
        facts.append(
            "Move quality across the set (both sides): "
            + _class_summary(ctx.get("class_counts") or {})
        )
    brief = ctx.get("games_brief") or []
    if brief:
        game_parts = []
        for g in brief[:12]:
            outcome = g.get("outcome")
            result = g.get("result") or "*"
            # 'unknown' means we cannot tell which side the kid played — state
            # the raw result rather than letting the writer invent win/loss/draw.
            outcome_txt = outcome if outcome and outcome != "unknown" else f"result {result}"
            seg = f"vs {g['opponent']} ({outcome_txt})"
            if g.get("moves"):
                seg += f", {g['moves']} moves"
            if g.get("opening"):
                seg += f", {g['opening']}"
            game_parts.append(seg)
        facts.append("Games: " + "; ".join(game_parts))
    moments = ctx.get("moments") or []
    if moments:
        facts.append("Key mistake moments (engine line included):")
        if not kid_color:
            facts.append(
                "NOTE: the player's side of the board is unknown, so every "
                "moment below is labelled with the side that played it — do "
                "not attribute a move to the player without its colour."
            )
        for m in moments:
            move_no = m.get("move") or ""
            head = f"{move_no} {m['san']}".strip()
            parts = [f"- {head} ({m['phase']}, lost ~{m['cp_loss']} pawns)"]
            if not kid_color and m.get("color"):
                parts[0] += f" (played by {'White' if m['color'] == 'white' else 'Black'})"
            if m.get("best"):
                parts.append(f"engine preferred {m['best']}")
            else:
                parts.append("engine preferred: none recorded — do NOT invent a preferred move")
            if m.get("threat_detail"):
                parts.append(f"tactical issue: {m['threat_detail']}")
            elif m.get("threat"):
                parts.append(f"mistake type: {m['threat']}")
            else:
                parts.append(
                    "tactical issue: none detected — do NOT invent a tactic; "
                    "explain the cost with the engine line and evaluation only"
                )
            candidates = m.get("candidates") or []
            if candidates:
                parts.append("alternative moves: " + ", ".join(
                    f"{c['san']} ({c['cp'] / 100.0:+.1f})" for c in candidates
                ))
            if m.get("line"):
                parts.append(f"engine line: {m['line']}")
            if m.get("seconds") is not None:
                if m["seconds"] < INSTANT_SECONDS:
                    parts.append(f"time: {_fmt_seconds(m['seconds'])} — reflex (played instantly)")
                else:
                    parts.append(f"time: {_fmt_seconds(m['seconds'])}")
            parts.append(f"opponent: {m['opponent']}")
            facts.append("; ".join(parts))
    time_stats = ctx.get("time_stats")
    if time_stats:
        seg = [
            f"clock data: {time_stats['moves_with_clock']} moves, "
            f"{time_stats['instant_moves']} played instantly (<3s)"
        ]
        if time_stats.get("median_clean") is not None and time_stats.get("median_error") is not None:
            seg.append(
                f"median time {_fmt_seconds(time_stats['median_clean'])} on sound moves vs "
                f"{_fmt_seconds(time_stats['median_error'])} on mistakes"
            )
        reflex = time_stats.get("reflex_errors") or []
        if reflex:
            seg.append(f"{len(reflex)} mistake(s) played in under 3s — reflex, not a decision")
        facts.append("Time: " + "; ".join(seg))
    if ctx.get("notes"):
        facts.append(f"Parent note: {ctx['notes']}")
    if ctx.get("answers"):
        facts.append("What the kid said after the game:")
        for i, a in enumerate(ctx["answers"], 1):
            facts.append(f"  {i}. {a}")
    history = ctx.get("history") or []
    if history:
        facts.append("Past progress across previous reports (oldest first):")
        for h in history:
            held = h.get("held")
            if held is True:
                status = "drill held at the next check"
            elif held is False:
                status = "drill came back at the next check"
            else:
                status = "not re-checked yet"
            when = f" ({h.get('date')})" if h.get("date") else ""
            facts.append(
                f"- {h.get('habit', '?')}{when}: {h.get('points_lost', 0)} points lost — {status}"
            )

    # Pre-fill the move placeholders with the actual facts so the LLM can only
    # write prose around them, never invent or misattribute a move.
    def _moment_label(m: Dict[str, Any]) -> str:
        move_no = m.get("move") or ""
        label = m.get("san") or "?"
        best = m.get("best")
        opponent = m.get("opponent") or "your opponent"
        if best and best != (m.get("san") or ""):
            label += f" instead of {best}"
        label = f"{move_no} {label}".strip()
        return f"{label} (vs {opponent})"

    top_moment = moments[0] if moments else None
    second_moment = moments[1] if len(moments) > 1 else None
    seen_in = (
        f"Yes — {_moment_label(top_moment)}" if top_moment
        else "not this set — keep watching"
    )
    moment1 = _moment_label(top_moment) if top_moment else "no moments in this set"
    moment2 = _moment_label(second_moment) if second_moment else "skip this heading"

    # The engine-grounded beats are pre-written from the deterministic facts
    # (see _moment_why_failed / _moment_recommendation) so the LLM can only
    # keep them verbatim — it can no longer contradict Stockfish, invent a
    # square/piece/capture, or recommend a move the engine did not choose.
    why1 = _moment_why_failed(top_moment)
    why2 = _moment_why_failed(second_moment)
    rec1 = _moment_recommendation(top_moment)
    rec2 = _moment_recommendation(second_moment)

    # The "pattern" section is only about a concrete moment when one exists.
    # With no flagged blunder we must not invent a fork/move to fill the space.
    if top_moment:
        pattern_section = [
            "## The pattern: {habit}",
            "",
            "The baseline showed this, and it fired again in this set.",
            "",
            "### Moment 1 — {moment1}",
            "",
            "**Why it looked good:** <one sentence — what the player was probably trying>",
            "**Why it failed:** {why1}",
            "**What the engine recommends:** {rec1}",
            "**Concept:** <one short phrase, e.g. the habit name>",
            "**Pattern:** <one sentence on what to recognise in similar positions>",
            "",
        ]
        if second_moment:
            pattern_section += [
                "### Moment 2 — {moment2}",
                "",
                "**Why it looked good:** <...>",
                "**Why it failed:** {why2}",
                "**What the engine recommends:** {rec2}",
                "**Concept:** <...>",
                "**Pattern:** <...>",
                "",
            ]
    else:
        pattern_section = [
            "## The pattern: {habit}",
            "",
            "(No single flagrant moment fired in this set. Write only from the "
            "move-quality facts — the mistake/inaccuracy counts and the accuracy. "
            "Say plainly that the habit did not appear as one big blunder; do NOT "
            "invent a move, a fork, or a 'moment' that is not in the facts.)",
            "",
        ]

    template = [
        "# {kid} — Game Set Report",
        "",
        "{count} Online Games · {platform} · {date_range}",
        "",
        "---",
        "",
        "## Short version",
        "",
        "(2–4 punchy sentences: open with a hook about this set, name the one "
        "pattern, and name the habit to build. Make it feel like a coach hyping "
        "their player before a training session.)",
        "",
        "---",
        "",
        "**Baseline read:** {count} recent online games  ",
        "**Pattern found:** {habit}  ",
        "**Seen in this game:** {seen_in}  ",
        "**The habit to train:** <one punchy, concrete sentence>  ",
        "**Tracking:** Begins with this report  ",
        "",
        "---",
        "",
        "## First, the wide view",
        "",
        "(Two short paragraphs: what the player is genuinely good at, then where "
        "points slip away. Name real games/moves from the facts, and say it with "
        "energy — a scouting report, not a form letter.)",
        "",
        "---",
        "",
        "## Now, the games you sent us",
        "",
        "(One paragraph on this set's results and time controls — call out the "
        "best win and the loss that mattered.)",
        "",
        "---",
        "",
        "## What's working",
        "",
        "(Two or three bold-led short paragraphs naming real strengths with game "
        "examples, written like a coach high-fiving the kid.)",
        "",
        "---",
        "",
        *pattern_section,
        "---",
        "",
        "## The recurring weakness across the set",
        "",
        "(Summarise the pattern across the set in plain language and name the "
        "simple fix — one sentence the kid can repeat to themselves.)",
        "",
        "---",
        "",
        "## One drill for this week",
        "",
        "**<Drill title — make it sound fun>**",
        "",
        "(A short, concrete, game-specific drill. Steps are fine, but phrase "
        "them naturally and tie them to this report's actual moments. Add a way "
        "to know it worked.)",
        "",
        "---",
        "",
        "## For your coach",
        "",
        "(A pre-lesson brief for the coach: the student's recurring pattern, the "
        "specific games/moves where it appeared, what to watch for at the next "
        "lesson, and one 10-minute exercise to run first.)",
        "",
        "---",
        "",
        "Questions? Email [" + SITE_CONTACT + "](mailto:" + SITE_CONTACT + ")",
        "— The " + SITE_NAME + " team",
        "",
        _REPORT_DISCLOSURE,
        "",
        "---",
        "",
        "*One more thing: online games show habits, but tournament games are where "
        "they cost points. Photograph a scoresheet or paste a PGN on the Analyze "
        "page and the next report reads the real over-the-board play.*",
    ]

    body = "\n".join(template).replace("{kid}", kid_name).replace("{count}", str(game_count))
    body = body.replace("{platform}", ctx.get("platform") or "online")
    body = body.replace("{date_range}", ctx.get("date_range") or "recent games")
    body = body.replace("{habit}", habit)
    body = body.replace("{seen_in}", seen_in)
    body = body.replace("{moment1}", moment1)
    body = body.replace("{moment2}", moment2)
    body = body.replace("{why1}", why1)
    body = body.replace("{why2}", why2)
    body = body.replace("{rec1}", rec1)
    body = body.replace("{rec2}", rec2)

    return (
        "\n".join(facts)
        + "\n\nOutput ONLY Markdown. Keep every heading, the '---' rules and the "
        "bullet labels exactly as given; fill the prose between them. Replace the "
        "<...> placeholders with specifics from the facts. End each '**Label:**' "
        "stat line with two trailing spaces so it renders on its own line.\n\n"
        + body
    )


def _build_markdown_report(kid_name: str, habit: str, game_count: int,
                           ctx: Dict[str, Any]) -> str:
    """Deterministic, fully objective markdown report.

    Every line is derived directly from the engine facts — results, move
    accuracy, move-quality counts, eval loss, and the engine's preferred
    moves. There is no coaching opinion, praise, drill, or invented
    explanation, so the output can never contradict the engine.
    """
    platform = ctx.get("platform") or "online"
    date_range = ctx.get("date_range") or "recent games"
    wins = ctx.get("wins", 0)
    losses = ctx.get("losses", 0)
    draws = ctx.get("draws", 0)
    moments = ctx.get("moments") or []
    avg_accuracy = ctx.get("avg_accuracy") or 0
    acpl = ctx.get("acpl") or 0
    rating = ctx.get("rating")
    kid_color = ctx.get("kid_color")
    acc_white = ctx.get("accuracy_white") or 0
    acc_black = ctx.get("accuracy_black") or 0
    cc_w = ctx.get("class_counts_white") or {}
    cc_b = ctx.get("class_counts_black") or {}
    cc_all = ctx.get("class_counts") or {}
    games_brief = ctx.get("games_brief") or []
    time_stats = ctx.get("time_stats")

    def _plural(n: int, word: str) -> str:
        if n == 1:
            return f"1 {word}"
        return f"{n} {word}es" if word.endswith("s") else f"{n} {word}s"

    def _class_summary(counts: Dict[str, int]) -> str:
        counts = counts or {}
        return (
            ", ".join(
                f"{label}s: {counts.get(label, 0)}"
                for label in ("blunder", "mistake", "inaccuracy",
                              "good", "excellent", "best")
                if counts.get(label)
            )
            or "no classified moves"
        )

    if kid_color == "white" and acc_white:
        acc_text = f"Move accuracy (playing White) {acc_white}%"
    elif kid_color == "black" and acc_black:
        acc_text = f"Move accuracy (playing Black) {acc_black}%"
    elif acc_white or acc_black:
        acc_text = f"Move accuracy — White {acc_white}%, Black {acc_black}%"
    elif avg_accuracy:
        acc_text = f"Average move accuracy {avg_accuracy}%"
    else:
        acc_text = None

    short = f"{kid_name} played {game_count} game{'s' if game_count != 1 else ''}: " \
        f"{_plural(wins, 'win')}, {_plural(losses, 'loss')}, {_plural(draws, 'draw')}."
    if acc_text:
        short += f" {acc_text}."
    short += f" The most common mistake type was {habit}."

    blocks: List[str] = [
        f"# {kid_name} — Game Set Report",
        f"{game_count} Online Games · {platform} · {date_range}",
        "",
        "## Short version",
        "",
        short,
        "",
        "## Results",
        "",
        f"- Result: {_plural(wins, 'win')}, {_plural(losses, 'loss')}, {_plural(draws, 'draw')}",
    ]
    if acc_text:
        blocks.append(f"- {acc_text}")
    if acpl:
        blocks.append(f"- Average centipawn loss: {acpl}")
    if rating:
        blocks.append(f"- Rating: {rating}")

    quality: List[str] = []
    if cc_w or cc_b:
        if kid_color in ("white", "black"):
            counts = cc_w if kid_color == "white" else cc_b
            quality.append(f"- {kid_color.title()}: {_class_summary(counts)}")
        else:
            quality.append(f"- White: {_class_summary(cc_w)}")
            quality.append(f"- Black: {_class_summary(cc_b)}")
    elif cc_all:
        quality.append(f"- Both sides: {_class_summary(cc_all)}")
    if quality:
        blocks += ["", "## Move quality", ""] + quality

    if games_brief:
        game_lines = []
        for g in games_brief[:12]:
            seg = f"- vs {g.get('opponent') or 'Opponent'}: {g.get('result') or '*'}"
            if g.get("moves"):
                seg += f", {g['moves']} moves"
            if g.get("opening"):
                seg += f", {g['opening']}"
            game_lines.append(seg)
        blocks += ["", "## Games reviewed", ""] + game_lines

    if moments:
        mistake_blocks = []
        for m in moments:
            head = f"{m.get('move') or ''} {m.get('san') or '?'}".strip()
            lines = [
                f"### {head} (vs {m.get('opponent') or 'Opponent'})",
                f"- Phase: {m.get('phase') or 'middlegame'}",
                f"- Cost: ~{m.get('cp_loss') or 0} pawns",
                f"- Engine preferred move: {m.get('best') or 'not recorded'}",
            ]
            if m.get("line"):
                lines.append(f"- Engine line: {m['line']}")
            detail = m.get("threat_detail") or m.get("threat")
            lines.append(f"- Tactical issue: {detail}" if detail
                         else "- Tactical issue: none detected")
            mistake_blocks.append("\n".join(lines))
        blocks += ["", "## Mistakes found", ""] + mistake_blocks
    else:
        blocks += ["", "## Mistakes found", "",
                   "- No blunders were flagged in this set."]

    blocks += ["", "## Most common mistake type", "", f"- {habit}"]

    if time_stats:
        t = [f"- Moves with clock data: {time_stats.get('moves_with_clock', 0)}"]
        if time_stats.get("instant_moves"):
            t.append(f"- Moves played in under 3 seconds: {time_stats.get('instant_moves')}")
        if time_stats.get("median_clean") is not None and time_stats.get("median_error") is not None:
            t.append(
                f"- Median time: {_fmt_seconds(time_stats.get('median_clean'))} on sound moves, "
                f"{_fmt_seconds(time_stats.get('median_error'))} on mistakes"
            )
        reflex = time_stats.get("reflex_errors") or []
        if reflex:
            t.append(f"- Mistakes played in under 3 seconds: {len(reflex)}")
        blocks += ["", "## Time", ""] + t

    blocks += ["---", _REPORT_DISCLOSURE]
    return re.sub(r"\n{3,}", "\n\n", "\n\n".join(blocks)).strip()


_MOVE_TOKEN_RE = re.compile(
    r"O-O(?:-O)?(?:[+#])?"
    r"|[KQRBN][a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?"
    r"|[a-h]x[a-h][1-8](?:=[QRBN])?[+#]?"
    r"|[a-h][1-8]=[QRBN][+#]?"
)


def _collect_known_moves(reports: List[Dict[str, Any]]) -> set:
    """Every move SAN that legitimately appears in the analysis facts.

    Fed to the post-generation validator so any piece move, capture, castling,
    or promotion the LLM writes must be one the engine actually saw.
    """
    known: set = set()
    for r in reports:
        for d in r.get("moves_detail") or []:
            t = (d.get("san") or "").strip()
            if t:
                known.add(t)
                known.add(t.rstrip("+#"))
        for b in r.get("blunders") or []:
            for key in ("san", "best"):
                t = (b.get(key) or "").strip()
                if t:
                    known.add(t)
                    known.add(t.rstrip("+#"))
            for tok in (b.get("line") or "").split():
                tok = tok.strip().rstrip("+#")
                if tok:
                    known.add(tok)
            for c in b.get("candidates") or []:
                t = (c.get("san") or "").strip()
                if t:
                    known.add(t)
                    known.add(t.rstrip("+#"))
    return known


def _report_facts_intact(markdown: str, ctx: Dict[str, Any],
                         extra_moves: Optional[Iterable[str]] = None) -> bool:
    """True when the LLM report kept the engine facts intact.

    Two layers: (1) the deterministic "Why it failed" and "What the engine
    recommends" lines must survive verbatim; (2) every piece move, capture,
    castling, or promotion the model wrote must be one the engine actually saw
    (from ``extra_moves`` or the moments themselves). Bare pawn pushes are not
    checked — they are ambiguous with square names in prose.
    """
    moments = ctx.get("moments") or []
    norm = " ".join(markdown.split())
    if moments:
        why1 = _moment_why_failed(moments[0])
        rec1 = _moment_recommendation(moments[0])
        if " ".join(why1.split()) not in norm:
            return False
        if " ".join(rec1.split()) not in norm:
            return False
        if len(moments) > 1:
            why2 = _moment_why_failed(moments[1])
            rec2 = _moment_recommendation(moments[1])
            if " ".join(why2.split()) not in norm:
                return False
            if " ".join(rec2.split()) not in norm:
                return False

    allowed = set(extra_moves or ())
    for m in moments:
        for key in ("san", "best"):
            t = (m.get(key) or "").strip()
            if t:
                allowed.add(t)
                allowed.add(t.rstrip("+#"))
        for tok in (m.get("line") or "").split():
            tok = tok.strip().rstrip("+#")
            if tok:
                allowed.add(tok)
        for c in m.get("candidates") or []:
            t = (c.get("san") or "").strip()
            if t:
                allowed.add(t)
                allowed.add(t.rstrip("+#"))
    for tok in _MOVE_TOKEN_RE.findall(markdown):
        if tok.rstrip("+#") not in allowed:
            return False
    return True


def generate_report(kid_name: str, habit: str, game_count: int,
                    api_key: Optional[str] = None,
                    context: Optional[Dict[str, Any]] = None,
                    extra_moves: Optional[Iterable[str]] = None) -> str:
    """Generate a fully objective markdown coach report for a kid.

    The report is assembled deterministically from the engine facts only. No
    language model writes any part of it, so the output can never contain
    subjective prose, praise, or an invented move, square, or tactic. The
    ``api_key``/``extra_moves`` parameters are kept for call compatibility but
    are no longer used.
    """
    ctx = context or {
        "platform": "online",
        "date_range": "recent games",
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "games_brief": [],
        "moments": [],
        "notes": "",
        "answers": [],
    }
    return _build_markdown_report(kid_name, habit, game_count, ctx)


# ---------------------------------------------------------------------------
# Pipeline orchestration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ChessAgent MCP integration (best-effort)
# ---------------------------------------------------------------------------

def _mcp_game_to_internal(game: Dict[str, Any]) -> Dict[str, Any]:
    white = game.get("white") or {}
    black = game.get("black") or {}
    return {
        "source": game.get("platform") or "unknown",
        "external_id": str(game.get("id") or game.get("url") or ""),
        "pgn": game.get("pgn") or "",
        "white": white.get("username", "White") if isinstance(white, dict) else "White",
        "black": black.get("username", "Black") if isinstance(black, dict) else "Black",
        "result": game.get("result", "*"),
        "played_at": game.get("utc_date") or game.get("end_time"),
    }


def _try_mcp_fetch(username: str, platform: str, max_games: int) -> Optional[List[Dict[str, Any]]]:
    """Fetch games via ChessAgent MCP; return None so callers can fall back."""
    try:
        import chess_agent  # type: ignore

        games = chess_agent.fetch_user_games(username, platform, max_games=max_games)
        mapped = [
            _mcp_game_to_internal(g)
            for g in games
            if isinstance(g, dict) and (g.get("pgn") or "").strip()
        ]
        if mapped:
            return mapped
    except Exception:
        pass
    return None


def _try_mcp_player(username: str) -> Optional[Dict[str, Any]]:
    try:
        import chess_agent  # type: ignore

        return chess_agent.player_lookup(username)
    except Exception:
        return None


def _open_engine(path: str = STOCKFISH_PATH) -> chess.engine.SimpleEngine:
    return chess.engine.SimpleEngine.popen_uci(path)


def _analyze_candidates(engine: chess.engine.SimpleEngine, fen: str,
                        depth: int) -> List[Dict[str, Any]]:
    """Top alternative moves (Multi-PV) for a position, from the side to move."""
    try:
        board = chess.Board(fen)
    except Exception:
        return []
    turn = board.turn
    try:
        result = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=3)
    except Exception:
        return []
    lines = result if isinstance(result, list) else [result]
    candidates: List[Dict[str, Any]] = []
    for info in lines:
        pv = info.get("pv") or []
        if not pv:
            continue
        try:
            san = board.san(pv[0])
        except Exception:
            san = pv[0].uci()
        candidates.append({
            "san": san,
            "uci": pv[0].uci(),
            "cp": _score_to_cp(info.get("score"), turn),
        })
    return candidates[:3]


def suggest_moves(engine: chess.engine.SimpleEngine, fen: str,
                  depth: int, num: int = 5) -> List[Dict[str, Any]]:
    """Stockfish's top ``num`` legal moves for a position (for the repertoire
    builder's "other moves" panel). Returns ``[{"uci", "san", "cp"}, ...]``.

    ``cp`` is centipawns from the side-to-move's perspective (mate ≈ ±10000).
    """
    try:
        board = chess.Board(fen)
    except Exception:
        return []
    legal = board.legal_moves.count()
    if legal == 0:
        return []
    requested = max(1, min(num, legal))
    turn = board.turn
    try:
        result = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=requested)
    except Exception:
        return []
    lines = result if isinstance(result, list) else [result]
    suggestions: List[Dict[str, Any]] = []
    for info in lines:
        pv = info.get("pv") or []
        if not pv:
            continue
        move = pv[0]
        try:
            san = board.san(move)
        except Exception:
            san = move.uci()
        suggestions.append({
            "uci": move.uci(),
            "san": san,
            "cp": _score_to_cp(info.get("score"), turn),
        })
    return suggestions[:num]


def _spar_params(elo: int) -> tuple:
    """Map a target playing strength to (search_depth, top_k_candidates)."""
    if elo >= 1600:
        return 14, 1
    if elo >= 1200:
        return 10, 2
    if elo >= 800:
        return 7, 3
    return 5, 4


def spar_move(engine: chess.engine.SimpleEngine, fen: str, elo: int = 1200):
    """Pick a move for a sparring partner of roughly ``elo`` strength.

    Weaker play searches less deeply and draws from more candidate moves
    (weighted toward the best), so low Elo blunders like a human while high
    Elo plays near-best. Returns a ``chess.Move`` or ``None`` when the game is
    over or the position is invalid.
    """
    try:
        board = chess.Board(fen)
    except Exception:
        return None
    legal = list(board.legal_moves)
    if not legal:
        return None
    depth, top = _spar_params(max(0, elo))
    n = min(top, len(legal))
    try:
        result = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=n)
    except Exception:
        return legal[0]
    lines = result if isinstance(result, list) else [result]
    candidates = [info["pv"][0] for info in lines if info.get("pv")]
    if not candidates:
        return legal[0]
    if len(candidates) == 1:
        return candidates[0]
    # Weighted toward the strongest move; a 1/(rank+1) spread keeps low-Elo
    # play inconsistent without ever favouring a clearly bad move.
    weights = [1.0 / (i + 1) for i in range(len(candidates))]
    return random.choices(candidates, weights=weights, k=1)[0]


def _enrich_blunders(reports: List[Dict[str, Any]],
                     engine: chess.engine.SimpleEngine, depth: int,
                     top_n: int = 4) -> None:
    """Attach candidate moves to the most costly blunders across the set."""
    blunders = [b for r in reports for b in (r.get("blunders") or [])]
    blunders.sort(key=lambda b: -(b.get("cp_loss") or 0))
    for b in blunders[:top_n]:
        fen = b.get("fen")
        if not fen:
            continue
        b["candidates"] = _analyze_candidates(engine, fen, depth)


def _short_version(markdown: str) -> str:
    """Pull the '## Short version' paragraph out of a generated report."""
    marker = "## Short version"
    idx = markdown.find(marker)
    if idx == -1:
        return markdown.strip().splitlines()[0] if markdown.strip() else ""
    rest = markdown[idx + len(marker):]
    next_heading = rest.find("\n## ")
    if next_heading != -1:
        rest = rest[:next_heading]
    para = " ".join(line.strip() for line in rest.splitlines() if line.strip())
    return para or markdown.strip().splitlines()[0]


def run_analysis(platform: str, username: str, kid_name: str = "Player",
                 max_games: int = 50, since_days: int = 30,
                 engine: Optional[chess.engine.SimpleEngine] = None,
                 notes: Optional[str] = None,
                 answers: Optional[List[str]] = None,
                 rating: Optional[int] = None,
                 history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Run the full intake pipeline and return a structured result.

    The result contains the full markdown report, a short summary text, the top
    recurring habit, suggested drill, total points lost and a per-game analysis
    list, ready to be persisted by the web app.
    """
    mcp_used = False
    games = _try_mcp_fetch(username, platform, max_games)
    if games:
        mcp_used = True
    elif platform == "lichess":
        games = fetch_lichess_games(username, max_games=max_games, since_days=since_days)
    elif platform == "chesscom":
        games = fetch_chesscom_games(username, max_games=max_games, since_days=since_days)
    else:
        raise ValueError(f"Unknown platform: {platform}")

    owns_engine = engine is None
    if owns_engine:
        engine = _open_engine()
    reports: List[Dict[str, Any]] = []
    try:
        for g in games:
            try:
                reports.append(analyze_game(g, engine, ANALYSIS_DEPTH))
            except Exception:
                # One unanalysable game should not fail the whole report.
                continue
        try:
            _enrich_blunders(reports, engine, ANALYSIS_DEPTH)
        except Exception:
            pass
    finally:
        if owns_engine and engine is not None:
            try:
                engine.quit()
            except Exception:
                pass

    habits = aggregate_habits(reports)
    top_habit = habits[0]["habit"] if habits else "Piece safety"
    context = build_report_context(
        reports, platform, username, top_habit, notes=notes, answers=answers,
        rating=rating, history=history,
    )
    kid_color = context.get("kid_color")
    if kid_color == "white":
        points_lost = round(sum(r.get("points_lost_white", 0.0) for r in reports), 2)
    elif kid_color == "black":
        points_lost = round(sum(r.get("points_lost_black", 0.0) for r in reports), 2)
    else:
        points_lost = round(sum(r.get("points_lost", 0.0) for r in reports), 2)
    markdown = generate_report(
        kid_name, top_habit, len(reports), context=context,
        extra_moves=_collect_known_moves(reports),
    )

    result: Dict[str, Any] = {
        "kid_name": kid_name,
        "platform": platform,
        "username": username,
        "kid_color": kid_color,
        "game_count": len(reports),
        "habit": top_habit,
        "summary_text": _short_version(markdown),
        "report_markdown": markdown,
        "drill": _make_drill(top_habit, context),
        "points_lost": points_lost,
        "games": reports,
        "mcp_used": mcp_used,
    }

    # Best-effort enrichment from ChessAgent (player profile + ratings).
    player = _try_mcp_player(username)
    if player:
        result["player"] = player

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a junior chess coach report.")
    parser.add_argument("--platform", required=True, choices=["lichess", "chesscom"])
    parser.add_argument("--username", required=True)
    parser.add_argument("--games", type=int, default=50)
    parser.add_argument("--kid", default="Player")
    parser.add_argument("--json", action="store_true", default=False,
                        help="Print the full result as JSON (default prints the summary).")
    args = parser.parse_args(argv)

    result = run_analysis(args.platform, args.username, kid_name=args.kid, max_games=args.games)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(result["summary_text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
