"""Lecture builder — one structured source, many outputs.

A *lecture* here is a self-contained teaching document about a real game (or a
theme built from several games). It is assembled purely from engine-verified
facts: Stockfish evaluations, python-chess board state, and the tactical
primitives in :mod:`chessintel.coach_core`. Nothing in the model is a
guess — if a fact could not be computed, the field is left empty and the
renderers print "not available" rather than inventing a value.

Everything downstream (`renderers.py` → Markdown / HTML / PDF / annotated PGN)
reads this one dict, so all four outputs are guaranteed to agree.

The model deliberately separates:

* ``facts``      — computed, quotable, never invented.
* ``moments``    — key positions with a four-beat explanation, each beat
                   derived from a concrete engine/board signal.
* ``narrative``  — empty prose slots plus ``writing_instructions``. An LLM
                   (e.g. the LibreChat agent) may fill these; the deterministic
                   renderers already produce a complete lecture without them.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import chess
import chess.pgn

try:  # package context (MCP server)
    from . import coach_core as core
    from . import openings as openings_mod
except ImportError:  # direct script execution
    import coach_core as core  # type: ignore[no-redef]
    import openings as openings_mod  # type: ignore[no-redef]


# --------------------------------------------------------------------------
# Audience levels
# --------------------------------------------------------------------------

LEVELS: Dict[str, Dict[str, Any]] = {
    "beginner": {
        "label": "Beginner (under 800)",
        "rating": 700,
        "depth": 10,
        "vocabulary": "plain",
        "max_moments": 4,
        "guidance": "Short sentences. No jargon — say 'loses a piece', not 'loses material'.",
    },
    "improver": {
        "label": "Improver (800-1200)",
        "rating": 1000,
        "depth": 12,
        "vocabulary": "plain",
        "max_moments": 5,
        "guidance": "Introduce one or two named ideas per lecture (fork, pin, hanging piece).",
    },
    "club": {
        "label": "Club player (1200-1800)",
        "rating": 1500,
        "depth": 16,
        "vocabulary": "technical",
        "max_moments": 6,
        "guidance": "Use standard chess vocabulary and explain the strategic 'why'.",
    },
    "expert": {
        "label": "Expert (1800+)",
        "rating": 1900,
        "depth": 20,
        "vocabulary": "technical",
        "max_moments": 8,
        "guidance": "Discuss candidate moves, prophylaxis and long-term plans.",
    },
}


def resolve_level(level: Optional[str], rating: Optional[int]) -> Tuple[str, Dict[str, Any]]:
    """Pick a level from an explicit name, or derive one from a rating."""
    if level and level.lower() in LEVELS:
        return level.lower(), LEVELS[level.lower()]
    if rating:
        if rating < 800:
            return "beginner", LEVELS["beginner"]
        if rating < 1200:
            return "improver", LEVELS["improver"]
        if rating < 1800:
            return "club", LEVELS["club"]
        return "expert", LEVELS["expert"]
    return "improver", LEVELS["improver"]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

_PIECE_NAME = {
    chess.PAWN: "pawn",
    chess.KNIGHT: "knight",
    chess.BISHOP: "bishop",
    chess.ROOK: "rook",
    chess.QUEEN: "queen",
    chess.KING: "king",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pawn_str(cp: Optional[float]) -> str:
    """+1.24 / -0.35 / mate formatting for an eval in centipawns (White's view)."""
    if cp is None:
        return "?"
    if abs(cp) >= 9000:
        return "mate" if cp > 0 else "-mate"
    return f"{cp / 100.0:+.2f}"


def _move_number(ply: int) -> str:
    return f"{(ply + 1) // 2}{'.' if ply % 2 == 1 else '...'}"


def _san_plus(sans: List[str]) -> str:
    return " ".join(sans) if sans else "—"


def _load_game(pgn: str) -> Tuple[Optional[chess.pgn.Game], Dict[str, str], List[str]]:
    """Parse a PGN and return ``(node, headers, uci_moves)``."""
    node = chess.pgn.read_game(io.StringIO(pgn))
    if node is None:
        return None, {}, []
    headers = {k: v for k, v in node.headers.items()}
    uci = [move.uci() for move in node.mainline_moves()]
    return node, headers, uci


def _play_to(node: chess.pgn.Game, ply: int) -> chess.Board:
    """Board position after ``ply`` half-moves (ply 0 = start position)."""
    board = chess.Board()
    for index, move in enumerate(node.mainline_moves()):
        if index >= ply:
            break
        board.push(move)
    return board


def _side_of(headers: Dict[str, str], player: Optional[str]) -> Optional[bool]:
    """chess.WHITE / chess.BLACK for ``player``, or None when unknown."""
    if not player:
        return None
    target = player.strip().lower()
    if (headers.get("White") or "").strip().lower() == target:
        return chess.WHITE
    if (headers.get("Black") or "").strip().lower() == target:
        return chess.BLACK
    return None


# --------------------------------------------------------------------------
# Four-beat moment explanations (deterministic, engine-grounded)
# --------------------------------------------------------------------------

def _looked_good_signals(
    board_before: chess.Board,
    move: chess.Move,
    san: str,
    seconds: Optional[float],
    was_engine_top: bool,
) -> List[str]:
    """Concrete, checkable reasons the played move was tempting.

    Every signal is read off the board or the clock — none of it is inferred
    from an LLM's impression of the position.
    """
    signals: List[str] = []

    if san.endswith("#"):
        signals.append("it delivers checkmate")
    elif "+" in san:
        signals.append("it gives check, which feels forcing")

    if board_before.is_capture(move):
        victim_sq = move.to_square
        victim = board_before.piece_at(victim_sq)
        if victim is not None:
            signals.append(
                f"it wins the {_PIECE_NAME[victim.piece_type]} on "
                f"{chess.square_name(victim_sq)}"
            )
        else:  # en passant
            signals.append("it wins a pawn en passant")

    # Castling / development.
    if board_before.is_castling(move):
        signals.append("it castles, which looks safe and natural")

    piece = board_before.piece_at(move.from_square)
    if piece is not None and not board_before.is_capture(move):
        home_rank = 0 if piece.color == chess.WHITE else 7
        if chess.square_rank(move.from_square) == home_rank and piece.piece_type in (
            chess.KNIGHT,
            chess.BISHOP,
        ):
            signals.append("it develops a piece toward the centre")

    if was_engine_top:
        signals.append("it is the engine's own top choice here")

    if seconds is not None and seconds < core.INSTANT_SECONDS:
        signals.append(f"it was played in under {core.INSTANT_SECONDS:g} seconds")

    if not signals:
        signals.append("it looks natural and does not obviously lose anything")
    return signals


def _concept_for(motif: Optional[str], phase: str) -> str:
    """The transferable idea behind a mistake, from its verified motif."""
    table = {
        "walked into mate": "King safety — you must see your opponent's threats before making your own.",
        "walked into a fork": "Loose pieces — a single move attacking two of your pieces wins material.",
        "walked into a pin": "Pins — a pinned piece cannot move, so any defence counting on it is imaginary.",
        "walked into a skewer": "Skewers — a valuable piece in front of a lesser one gets chased away.",
        "trapped a piece": "Retreat squares — every piece needs a way out before it goes forward.",
        "hung a piece": "Undefended pieces — an unguarded piece is a target, not a threat.",
    }
    if motif and motif in table:
        return table[motif]

    phase_ideas = {
        "opening": "Opening principles — develop, castle, and connect the rooks before starting a fight.",
        "middlegame": "Every move must answer: what is my opponent threatening?",
        "endgame": "Endgames are decided by king activity and pawn structure, not by tricks.",
    }
    return phase_ideas.get(phase, phase_ideas["middlegame"])


def _pattern_rule(motif: Optional[str], phase: str) -> str:
    """A memorable rule the student can actually apply next game."""
    table = {
        "walked into mate": "Before you move, count your opponent's checks. If any is mate, deal with it first.",
        "walked into a fork": "After every enemy move, ask: which of my pieces are on the same colour lines as their knights and pawns?",
        "walked into a pin": "If a piece is pinned, defend it with something else — do not rely on the pinned piece to move.",
        "walked into a skewer": "Never put your king or queen in front of your own lesser pieces on an open line.",
        "trapped a piece": "Before advancing a piece, name the square it will retreat to.",
        "hung a piece": "After your move, check every one of your pieces: is it attacked and undefended?",
    }
    if motif and motif in table:
        return table[motif]
    return (
        "Ask the two questions on every move: what did their last move threaten, "
        "and is everything I own defended?"
    )


# --------------------------------------------------------------------------
# Moment selection
# --------------------------------------------------------------------------

def _swing_entries(
    node: chess.pgn.Game,
    analysis: Dict[str, Any],
    min_loss_pct: float = 3.0,
) -> List[Dict[str, Any]]:
    """Every move ranked by how much it *cost*, reconstructed from the evals.

    ``coach_core`` only records errors at or above 250cp (``blunders``), which is
    right for a cheat/pattern report but leaves a clean, well-played game with no
    teaching material at all — a lecture with zero moments is useless. This walks
    the engine's own per-ply evaluations to rank *every* move, and records the
    engine's preferred move at each point, so a lecture always has something
    concrete to work on.

    Ranking is by **winning-chances lost**, not raw centipawns. That distinction
    matters: once a game is already decided, a 900cp blunder barely moves the
    win percentage, while the move that actually turned a balanced game into a
    lost one moves it a lot. Ranking on centipawns put the fabled Opera Game's
    final, already-mate-in-1 blunder at the top of the lecture and labelled it
    merely "good".

    Entries are returned in the same shape as ``blunders`` so the moment builder
    can treat both identically. Motif fields are None here — tactic detection
    only runs inside the 250cp path, and inventing a motif would be exactly the
    kind of guess this design forbids.
    """
    timeline: Dict[int, Tuple[float, Optional[str]]] = {}
    for row in analysis.get("evals") or []:
        try:
            ply = int(row[0])
            cp = float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        best_uci = row[2] if len(row) > 2 else None
        timeline[ply] = (cp, best_uci)

    if not timeline:
        return []

    detail: Dict[int, Dict[str, Any]] = {}
    for row in analysis.get("moves_detail") or []:
        try:
            detail[int(row.get("ply"))] = row
        except (TypeError, ValueError):
            continue

    def white_eval(ply: int) -> Optional[float]:
        """Evaluation after ``ply`` from White's perspective (0 = start position)."""
        if ply <= 0:
            return 20.0
        entry = timeline.get(ply)
        if entry is None:
            return None
        cp, _ = entry
        return cp if ply % 2 == 1 else -cp

    moves = [move.uci() for move in node.mainline_moves()]
    entries: List[Dict[str, Any]] = []

    for ply in range(1, max(timeline) + 1):
        before = white_eval(ply - 1)
        after = white_eval(ply)
        if before is None or after is None:
            continue

        mover_is_white = ply % 2 == 1
        # Both figures from the mover's own point of view.
        score_before = before if mover_is_white else -before
        score_after = after if mover_is_white else -after
        loss = score_before - score_after

        loss_pct = max(
            0.0,
            core._winning_chances(score_before) - core._winning_chances(score_after),
        )
        if loss_pct < min_loss_pct:
            continue

        row = detail.get(ply) or {}
        best_uci = timeline[ply][1]

        fen = None
        best_san = None
        try:
            board = _play_to(node, ply - 1)
            fen = board.fen()
            if best_uci:
                candidate = chess.Move.from_uci(best_uci)
                if candidate in board.legal_moves:
                    best_san = board.san(candidate)
        except Exception:
            fen = None
            best_san = None

        entries.append(
            {
                "ply": ply,
                "color": row.get("color")
                or ("white" if mover_is_white else "black"),
                "san": row.get("san")
                or (moves[ply - 1] if ply - 1 < len(moves) else "?"),
                "best": best_san,
                "best_uci": best_uci,
                "phase": core.phase_of(ply),
                "cp_loss": round(loss, 1),
                "loss_pct": round(loss_pct, 1),
                "class": core._classify_loss(loss_pct),
                "fen": fen,
                "line": None,
                "threat": None,
                "threat_detail": None,
                "seconds": row.get("seconds"),
            }
        )

    entries.sort(key=lambda entry: -float(entry.get("loss_pct") or 0))
    return entries


def _rank_moments(
    analysis: Dict[str, Any],
    side: Optional[bool],
    max_moments: int,
    fallback_entries: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Pick the teaching moments, most instructive first.

    Priority:
      1. mistakes by the focal player that carried a named tactical motif
         (these are the ones that actually teach something)
      2. the largest swings by the focal player
      3. the largest swings in the game overall
      4. moves below the 250cp blunder threshold, from the eval timeline, so a
         clean game still produces a lecture worth reading

    ``side`` is the focal player's colour, or None to consider both.
    """
    blunders: List[Dict[str, Any]] = list(analysis.get("blunders") or [])

    def _side_matches(entry: Dict[str, Any]) -> bool:
        if side is None:
            return True
        return entry.get("color") == ("white" if side == chess.WHITE else "black")

    def _score(entry: Dict[str, Any]) -> float:
        # A named tactic outranks a bigger but unexplained swing. Within each
        # group, rank by winning chances lost rather than raw centipawns, so an
        # already-decided game's tail does not crowd out the real turning point.
        tactic_bonus = 1000.0 if entry.get("threat") else 0.0
        return tactic_bonus + float(entry.get("loss_pct") or 0)

    ordered = sorted(
        [b for b in blunders if _side_matches(b)], key=_score, reverse=True
    )
    ordered += sorted(
        [b for b in blunders if not _side_matches(b)], key=_score, reverse=True
    )

    if fallback_entries:
        ordered += sorted(
            fallback_entries,
            key=lambda entry: (
                not _side_matches(entry),
                -float(entry.get("loss_pct") or 0),
            ),
        )

    seen: set = set()
    picked: List[Dict[str, Any]] = []
    for entry in ordered:
        ply = entry.get("ply")
        if ply in seen or not entry.get("fen"):
            continue
        seen.add(ply)
        picked.append(entry)
        if len(picked) >= max_moments:
            break
    return picked


def _build_moment(
    node: chess.pgn.Game,
    entry: Dict[str, Any],
    analysis: Dict[str, Any],
) -> Dict[str, Any]:
    """Turn one blunder record into a four-beat teaching moment."""
    ply = int(entry.get("ply") or 0)
    board_before = _play_to(node, ply - 1)
    move = None
    for index, candidate in enumerate(node.mainline_moves()):
        if index == ply - 1:
            move = candidate
            break

    was_engine_top = False
    best_uci = entry.get("best_uci")
    if move is not None and best_uci and move.uci() == best_uci:
        was_engine_top = True

    looked_good: List[str] = []
    if move is not None:
        looked_good = _looked_good_signals(
            board_before,
            move,
            str(entry.get("san") or ""),
            entry.get("seconds"),
            was_engine_top,
        )

    eval_before = _white_eval_at(analysis, ply - 1)
    eval_after = _white_eval_at(analysis, ply)
    motif = entry.get("threat")
    detail = entry.get("threat_detail")
    phase = str(entry.get("phase") or core.phase_of(ply))

    fen_after = board_before.fen()
    if move is not None:
        pushed = board_before.copy()
        pushed.push(move)
        fen_after = pushed.fen()

    if detail:
        failed = detail
    elif entry.get("cp_loss"):
        drop = float(entry["cp_loss"]) / 100.0
        failed = (
            f"it throws away {drop:.1f} pawns of the mover's own advantage "
            f"(the evaluation goes from {_pawn_str(eval_before)} to "
            f"{_pawn_str(eval_after)}, from White's point of view)"
        )
    else:
        failed = "the engine rates this move well below the alternative"

    return {
        "ply": ply,
        "move_number": _move_number(ply),
        "color": entry.get("color"),
        "san": entry.get("san"),
        "best_san": entry.get("best"),
        "phase": phase,
        "eval_before": eval_before,
        "eval_after": eval_after,
        "eval_before_str": _pawn_str(eval_before),
        "eval_after_str": _pawn_str(eval_after),
        "cp_loss": entry.get("cp_loss"),
        "win_loss_pct": entry.get("loss_pct"),
        "classification": entry.get("class"),
        "motif": motif,
        "fen_before": entry.get("fen") or board_before.fen(),
        "fen_after": fen_after,
        "best_line": entry.get("line"),
        "seconds": entry.get("seconds"),
        "was_engine_top": was_engine_top,
        "beats": {
            "why_it_looked_good": looked_good,
            "why_it_failed": failed,
            "the_concept": _concept_for(motif, phase),
            "pattern_to_remember": _pattern_rule(motif, phase),
        },
    }


def _white_eval_at(analysis: Dict[str, Any], ply: int) -> Optional[float]:
    """Evaluation after ``ply``, always from White's perspective.

    ``coach_core`` stores ``evals`` as ``[ply, score_after, best_uci]`` where
    ``score_after`` is from the *mover's* point of view — so consecutive rows
    alternate sides and are NOT comparable. Displaying them raw makes a
    normal move look like a 15-pawn collapse. Everything user-facing is
    normalised to White here.
    """
    if ply <= 0:
        return 20.0  # conventional small White edge at the start position
    for row in analysis.get("evals") or []:
        try:
            if int(row[0]) != ply:
                continue
            cp = float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        return cp if ply % 2 == 1 else -cp
    return None


# --------------------------------------------------------------------------
# Quiz / worksheet / drills / homework
# --------------------------------------------------------------------------

def _build_quiz(
    node: chess.pgn.Game,
    entries: List[Dict[str, Any]],
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Puzzles taken from the game's own critical positions.

    For each moment the *side to move* is asked to find the engine's best move.
    The answer is the engine's verified top move, never a suggestion.
    """
    quiz: List[Dict[str, Any]] = []
    seen: set = set()

    for entry in sorted(entries, key=lambda row: -float(row.get("loss_pct") or 0)):
        best = entry.get("best")
        fen = entry.get("fen")
        ply = entry.get("ply")
        if not best or not fen or ply in seen:
            continue
        seen.add(ply)
        quiz.append(
            {
                "ply": ply,
                "move_number": _move_number(int(ply or 0)),
                "fen": fen,
                "question": f"{str(entry.get('color', 'White')).title()} to play — find the best move.",
                "answer_san": best,
                "answer_line": entry.get("line"),
                "theme": entry.get("threat") or "best move",
                "points_lost_if_you_play_the_game_move": round(
                    float(entry.get("cp_loss") or 0) / 100.0, 2
                ),
            }
        )
        if len(quiz) >= limit:
            break
    return quiz


def _build_highlights(
    node: chess.pgn.Game,
    analysis: Dict[str, Any],
    side: Optional[bool],
    limit: int = 4,
) -> List[Dict[str, Any]]:
    """Positions where the focal player found the engine's own best move.

    A lecture that only catalogues mistakes teaches half the lesson — a student
    who played a brilliant combination should see it named. Candidates are moves
    the engine also preferred, filtered to the decisive ones (captures, checks,
    mates) so the list is not padded with routine best moves, then ranked by how
    good the position became for the player who made them.

    Everything here is verified: the move is the engine's top choice at that ply
    and the evaluations are the engine's own.
    """
    timeline: Dict[int, Tuple[float, Optional[str]]] = {}
    for row in analysis.get("evals") or []:
        try:
            ply = int(row[0])
            cp = float(row[1])
        except (TypeError, ValueError, IndexError):
            continue
        timeline[ply] = (cp, row[2] if len(row) > 2 else None)

    if not timeline:
        return []

    def white_eval(ply: int) -> Optional[float]:
        if ply <= 0:
            return 20.0
        entry = timeline.get(ply)
        if entry is None:
            return None
        cp, _ = entry
        return cp if ply % 2 == 1 else -cp

    played = [move.uci() for move in node.mainline_moves()]
    detail: Dict[int, Dict[str, Any]] = {}
    for row in analysis.get("moves_detail") or []:
        try:
            detail[int(row.get("ply"))] = row
        except (TypeError, ValueError):
            continue

    highlights: List[Dict[str, Any]] = []
    for ply in range(1, len(played) + 1):
        entry = timeline.get(ply)
        if entry is None:
            continue
        _, best_uci = entry
        if not best_uci or best_uci != played[ply - 1]:
            continue

        mover_is_white = ply % 2 == 1
        if side is not None and mover_is_white != (side == chess.WHITE):
            continue

        row = detail.get(ply) or {}
        san = str(row.get("san") or played[ply - 1])
        decisive = ("x" in san) or ("+" in san) or ("#" in san)
        if not decisive:
            continue

        after_white = white_eval(ply)
        if after_white is None:
            continue
        advantage = after_white if mover_is_white else -after_white

        try:
            board = _play_to(node, ply - 1)
            fen = board.fen()
        except Exception:
            fen = None

        reason = []
        if "#" in san:
            reason.append("it delivers checkmate")
        elif "+" in san:
            reason.append("it gives check")
        if "x" in san:
            reason.append("it wins material")

        highlights.append(
            {
                "ply": ply,
                "move_number": _move_number(ply),
                "color": "white" if mover_is_white else "black",
                "san": san,
                "fen": fen,
                "eval_white": after_white,
                "advantage_for_mover": advantage,
                "reason": " and ".join(reason) or "it is the engine's top move",
                "classification": row.get("class"),
            }
        )

    highlights.sort(key=lambda row: -float(row.get("advantage_for_mover") or 0))
    return highlights[:limit]


def _build_drills(
    habits: List[Dict[str, Any]],
    moments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """One concrete drill per recurring habit, grounded in a real moment.

    Wording comes from the vendored coach core so the drills match the coach
    reports exactly. ``aggregate_habits`` only returns ``{habit, count}``; the
    drill text is generated from the lecture's own top moments.
    """
    ctx_moments: List[Dict[str, Any]] = [
        {
            "san": moment.get("san"),
            "best": moment.get("best_san"),
            "fen": moment.get("fen_before"),
            "move_number": moment.get("move_number"),
        }
        for moment in moments
    ]

    drills: List[Dict[str, Any]] = []
    for habit in habits[:4]:
        name = habit.get("habit") or "recurring pattern"
        drills.append(
            {
                "title": name,
                "frequency": habit.get("count"),
                "description": core._make_drill(name, {"moments": ctx_moments}),
                "example_fen": ctx_moments[0]["fen"] if ctx_moments else None,
                "example_move": ctx_moments[0]["san"] if ctx_moments else None,
            }
        )

    # A well-played game produces no habit tags at all. Rather than ship a
    # lecture with no practice, build one drill from the sharpest moment.
    if not drills and ctx_moments:
        top = ctx_moments[0]
        drills.append(
            {
                "title": "Sharpest moment in the game",
                "frequency": None,
                "description": core._make_drill(
                    "Key moments", {"moments": ctx_moments}
                ),
                "example_fen": top["fen"],
                "example_move": top["san"],
            }
        )

    return drills


def _build_homework(
    moments: List[Dict[str, Any]],
    analysis: Dict[str, Any],
    quiz: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Concrete, checkable tasks — not vague advice."""
    tasks: List[str] = []

    if quiz:
        tasks.append(
            f"Solve the {len(quiz)} positions at the end of this lecture without "
            "moving the pieces, then check your answers."
        )

    motifs = [m.get("motif") for m in moments if m.get("motif")]
    if motifs:
        top = max(set(motifs), key=motifs.count)
        tasks.append(
            f"Play 10 games this week and, after every single move, say out loud "
            f"whether you have allowed: {top.replace('walked into ', '')}."
        )

    if analysis.get("blunders"):
        tasks.append(
            "Replay this game from move 1 and stop at each moment in this lecture. "
            "Find the better move yourself before reading the answer."
        )

    instant = [
        row
        for row in (analysis.get("blunders") or [])
        if isinstance(row.get("seconds"), (int, float))
        and float(row["seconds"]) < core.INSTANT_SECONDS
    ]
    if instant:
        tasks.append(
            f"{len(instant)} of the mistakes were played in under "
            f"{core.INSTANT_SECONDS:g} seconds. This week, use at least 3 seconds on "
            "every move — even the obvious ones."
        )

    tasks.append("Bring this game back next lesson and show the better move at each moment.")

    return {
        "tasks": tasks,
        "positions_to_practise": [q["fen"] for q in quiz],
    }


def _build_annotated_pgn(pgn: str, analysis: Dict[str, Any]) -> str:
    """PGN with ``[%eval]`` annotations, importable into Shredder/ChessBase.

    Existing comments (clock stamps, etc.) are preserved and the engine eval is
    prepended, which is the convention those GUIs expect.
    """
    node = chess.pgn.read_game(io.StringIO(pgn))
    if node is None:
        return pgn

    evals = {int(row[0]): float(row[1]) for row in (analysis.get("evals") or [])}
    failures = {
        int(b["ply"]): b
        for b in (analysis.get("blunders") or [])
        if b.get("ply") is not None
    }

    out = io.StringIO()
    board = node.board()
    for index, child in enumerate(node.mainline()):
        if child.move is None:
            continue
        ply = index + 1
        board.push(child.move)

        parts: List[str] = []
        cp = evals.get(ply)
        if cp is not None:
            if abs(cp) >= 9000:
                mate_in = 0
                try:
                    mating = board.copy()
                    for distance in range(1, 40):
                        if mating.is_checkmate():
                            mate_in = distance
                            break
                        mating.push(list(mating.legal_moves)[0])
                except Exception:
                    mate_in = 0
                parts.append(f"[%eval #{mate_in if cp > 0 else -mate_in}]")
            else:
                parts.append(f"[%eval {cp / 100.0:.2f}]")

        failure = failures.get(ply)
        if failure:
            note = failure.get("threat_detail") or "inaccuracy"
            label = failure.get("class") or "mistake"
            if failure.get("best"):
                note = f"{note}; engines prefer {failure['best']}"
            parts.append(f"{label}: {note}")

        original = (child.comment or "").strip()
        if original:
            parts.append(original)

        child.comment = ("{" + " ".join(parts) + "}") if parts else ""

    exporter = chess.pgn.FileExporter(out)
    node.accept(exporter)
    return out.getvalue()


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

def build_lecture(
    pgn: str,
    *,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    player: Optional[str] = None,
    level: Optional[str] = None,
    rating: Optional[int] = None,
    focus: Optional[str] = None,
    depth: Optional[int] = None,
    max_moments: Optional[int] = None,
    include_quiz: bool = True,
    fetch_opening_stats: bool = False,
    store_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a complete lecture model from one PGN.

    The heavy lifting (engine pass, tactical classification, habit tagging) is
    delegated to the vendored :mod:`chessintel.coach_core` pipeline so the
    numbers here match the coach reports exactly.
    """
    level_key, level_cfg = resolve_level(level, rating)
    depth = int(depth or level_cfg["depth"])
    moment_cap = int(max_moments or level_cfg["max_moments"])

    node, headers, uci_moves = _load_game(pgn)
    if node is None:
        raise ValueError(
            "Could not parse that PGN. It must start with a [Event or [FEN header "
            "and contain a move list."
        )

    side = _side_of(headers, player)

    engine = core._open_engine()
    try:
        analysis = core.analyze_game({"pgn": pgn, "source": store_name or "manual"}, engine, depth)
    finally:
        try:
            engine.quit()
        except Exception:
            pass

    opening = openings_mod.identify(headers, uci_moves)
    opening_stats = None
    if fetch_opening_stats and opening.get("name"):
        # Stats for the position after the matched opening prefix.
        prefix_len = opening.get("matched_plies") or 0
        board = _play_to(node, prefix_len)
        opening_stats = openings_mod.explorer_stats(board.fen())

    swing_entries = _swing_entries(node, analysis)
    ranked = _rank_moments(analysis, side, moment_cap, swing_entries)
    moments = [_build_moment(node, entry, analysis) for entry in ranked]
    highlights = _build_highlights(node, analysis, side)

    quiz_pool = sorted(
        list(analysis.get("blunders") or []) + swing_entries,
        key=lambda row: -float(row.get("loss_pct") or 0),
    )
    quiz = _build_quiz(node, quiz_pool) if include_quiz else []
    habits = core.aggregate_habits([analysis]) if analysis.get("blunders") else []
    drills = _build_drills(habits, moments)

    tactic_counts: Dict[str, int] = {}
    for entry in analysis.get("blunders") or []:
        motif = entry.get("threat")
        if motif:
            tactic_counts[motif] = tactic_counts.get(motif, 0) + 1

    time_note = _time_summary(analysis)

    facts: List[str] = []
    facts.append(
        f"{headers.get('White', 'White')} vs {headers.get('Black', 'Black')}, "
        f"result {analysis.get('result') or headers.get('Result') or 'unknown'}."
    )
    facts.append(
        f"Accuracy: White {analysis.get('accuracy_white')}%, "
        f"Black {analysis.get('accuracy_black')}% (game {analysis.get('accuracy')}%)."
    )
    facts.append(f"Average centipawn loss: {analysis.get('acpl')} per move.")
    if opening.get("name"):
        eco = opening.get("eco")
        facts.append(f"Opening: {opening['name']}{f' ({eco})' if eco else ''}.")
    else:
        facts.append("Opening: not identified — do not name one.")
    if analysis.get("blunders"):
        facts.append(
            f"Big errors (>= {core.BLUNDER_THRESHOLD_CP}cp): "
            f"{len(analysis['blunders'])}."
        )
    for key, count in sorted(tactic_counts.items(), key=lambda kv: -kv[1]):
        facts.append(f"Motif '{key}': {count}x.")
    if time_note:
        facts.append(time_note)

    title = title or _default_title(headers, opening, focus, side)
    subtitle = subtitle or _default_subtitle(analysis, moments, side, player)

    generated_at = _utc_now()
    lecture: Dict[str, Any] = {
        "format": "chessintel-lecture",
        "format_version": 1,
        "generated_at": generated_at,
        "meta": {
            "title": title,
            "subtitle": subtitle,
            "generated_at": generated_at,
            "focus": focus,
            "player": player,
            "player_side": {chess.WHITE: "white", chess.BLACK: "black"}.get(side) if side is not None else None,
            "level": level_key,
            "level_label": level_cfg["label"],
            "rating": rating or level_cfg["rating"],
            "vocabulary": level_cfg["vocabulary"],
            "depth": depth,
            "engine": "Stockfish",
            "source": store_name or "manual",
        },
        "game": {
            "headers": headers,
            "result": analysis.get("result"),
            "white": analysis.get("white"),
            "black": analysis.get("black"),
            "pgn": pgn,
            "move_count": analysis.get("moves"),
            "opening": opening,
            "opening_stats": opening_stats,
        },
        "facts": facts,
        "summary": {
            "one_liner": _one_liner(headers, analysis, moments, side),
            "accuracy": {
                "white": analysis.get("accuracy_white"),
                "black": analysis.get("accuracy_black"),
                "game": analysis.get("accuracy"),
            },
            "acpl": analysis.get("acpl"),
            "class_counts": analysis.get("class_counts"),
            "phase_errors": analysis.get("phase_blunders"),
            "tactic_counts": tactic_counts,
            "time": time_note,
        },
        "eval_graph": [
            {"ply": int(row[0]), "cp": float(row[1])}
            for row in (analysis.get("evals") or [])
        ],
        "moments": moments,
        "highlights": highlights,
        "tactics": tactic_counts,
        "habits": habits,
        "drills": drills,
        "quiz": quiz,
        "homework": _build_homework(moments, analysis, quiz),
        "annotated_pgn": _build_annotated_pgn(pgn, analysis),
        "narrative": {
            "intro": "",
            "why_this_game_matters": "",
            "closing": "",
        },
        "writing_instructions": (
            "Write the narrative sections ONLY from the facts listed in 'facts' and "
            "'moments'. You may explain a concept in your own words, but never state "
            "a move, evaluation, result, rating or opening that is not present in the "
            "data. Quote evaluations exactly as given. Keep the tone pitched to "
            f"{level_cfg['label']}: {level_cfg['guidance']}"
        ),
    }

    return lecture


def _time_summary(analysis: Dict[str, Any]) -> Optional[str]:
    timed = [
        row
        for row in (analysis.get("blunders") or [])
        if isinstance(row.get("seconds"), (int, float))
    ]
    if not timed:
        return None
    instant = [row for row in timed if float(row["seconds"]) < core.INSTANT_SECONDS]
    if not instant:
        return f"Clocks available for {len(timed)} errors; none were instant moves."
    return (
        f"{len(instant)} of {len(timed)} errors were played in under "
        f"{core.INSTANT_SECONDS:g} seconds — rushed rather than unknown."
    )


def _default_title(
    headers: Dict[str, str],
    opening: Dict[str, Any],
    focus: Optional[str],
    side: Optional[bool],
) -> str:
    white = headers.get("White") or "White"
    black = headers.get("Black") or "Black"
    if focus:
        return f"{focus.title()} — {white} vs {black}"
    if opening.get("name"):
        return f"{opening['name']}: {white} vs {black}"
    return f"{white} vs {black}"


def _default_subtitle(
    analysis: Dict[str, Any],
    moments: List[Dict[str, Any]],
    side: Optional[bool],
    player: Optional[str],
) -> str:
    who = player or (
        "White" if side == chess.WHITE else "Black" if side == chess.BLACK else None
    )
    count = len(moments)
    worst = analysis.get("phase_blunders") or {}
    worst_phase = None
    if any(worst.values()):
        worst_phase = max(worst, key=lambda key: worst[key])
    parts = [f"{count} key moment{'s' if count != 1 else ''}"]
    if who:
        parts.append(f"from {who}'s game")
    if worst_phase:
        parts.append(f"most errors in the {worst_phase}")
    return " · ".join(parts)


def _one_liner(
    headers: Dict[str, str],
    analysis: Dict[str, Any],
    moments: List[Dict[str, Any]],
    side: Optional[bool],
) -> str:
    if not moments:
        return (
            "No single move lost enough to be singled out — this lecture focuses on "
            "the opening plan and the general flow of the game."
        )
    top = moments[0]
    motif = top.get("motif")
    if motif:
        return (
            f"The biggest lesson is at move {top['move_number']}: {top['san']} "
            f"{motif}, costing {float(top.get('cp_loss') or 0) / 100.0:.1f} pawns."
        )
    return (
        f"The biggest swing is at move {top['move_number']}: {top['san']} drops the "
        f"evaluation from {top['eval_before_str']} to {top['eval_after_str']}."
    )
