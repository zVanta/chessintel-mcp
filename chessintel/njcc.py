"""NJCC one-page lesson and homework renderers.

Turns the verified ``build_lecture`` model into a dense, readable, single A4
study sheet in the New Jersey Chess Club style: masthead, two diagram panels
(final position and the key moment, each with an actual board), five big
ideas, the full move list, three things to remember, and a chess vocabulary
table. A companion homework sheet produces the "questions to answer" version.

Everything here formats; nothing is invented. Player names, result, opening,
accuracy and evaluations come straight from the engine-verified model.
"""

from __future__ import annotations

import html as _html
import io
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import chess

from . import renderers as _r


def _esc(text: Any) -> str:
    return _html.escape("" if text is None else str(text))


# --------------------------------------------------------------------------
# Model helpers
# --------------------------------------------------------------------------

def _headers(lecture: Dict[str, Any]) -> Dict[str, str]:
    return (lecture.get("game") or {}).get("headers") or {}


def _white(lecture: Dict[str, Any]) -> str:
    return (lecture.get("game") or {}).get("white") or _headers(lecture).get("White") or "White"


def _black(lecture: Dict[str, Any]) -> str:
    return (lecture.get("game") or {}).get("black") or _headers(lecture).get("Black") or "Black"


def _short_name(name: str) -> str:
    if not name:
        return ""
    if "," in name:
        return name.split(",")[0].strip()
    parts = name.split()
    return parts[-1] if parts else name


def _final_board(lecture: Dict[str, Any]) -> tuple[str, Optional[str]]:
    """Return (final_fen, last_move_uci)."""
    pgn = (lecture.get("game") or {}).get("pgn") or ""
    try:
        node = chess.pgn.read_game(io.StringIO(pgn))
        if node is None:
            return "", None
        end = node.end()
        moves = list(node.mainline_moves())
        last = moves[-1] if moves else None
        return end.board().fen(), last.uci() if last else None
    except Exception:
        return "", None


def _final_cp(lecture: Dict[str, Any]) -> Optional[float]:
    """Final evaluation, normalised to White's point of view."""
    graph = lecture.get("eval_graph") or []
    if not graph:
        return None
    try:
        ply = int(graph[-1].get("ply"))
        cp = float(graph[-1].get("cp"))
    except (TypeError, ValueError, AttributeError):
        return None
    return cp if ply % 2 == 1 else -cp


def _final_is_mate(lecture: Dict[str, Any]) -> bool:
    fen, _ = _final_board(lecture)
    if not fen:
        return False
    try:
        return chess.Board(fen).is_checkmate()
    except Exception:
        return False


def _verdict(cp: Optional[float], is_checkmate: bool = False) -> str:
    if cp is None:
        return "Final position shown on the board."
    if is_checkmate:
        return "Checkmate is on the board."
    if abs(cp) >= 9000:
        return "White has a forced mate." if cp > 0 else "Black has a forced mate."
    if cp >= 400:
        return f"White is completely winning — up about {cp / 100:.1f} pawns."
    if cp >= 150:
        return f"White is clearly better (+{cp / 100:.1f})."
    if cp <= -400:
        return f"Black is completely winning — up about {abs(cp) / 100:.1f} pawns."
    if cp <= -150:
        return f"Black is clearly better ({cp / 100:.1f})."
    return "The position is about equal."


def _result_phrase(lecture: Dict[str, Any]) -> str:
    result = ((lecture.get("game") or {}).get("result") or "").strip()
    white = _white(lecture)
    black = _black(lecture)
    if result == "1-0":
        return f"{white} won 1-0"
    if result == "0-1":
        return f"{black} won 0-1"
    if result in ("1/2-1/2", "½-½"):
        return "Drawn ½-½"
    return f"Result: {result or 'unknown'}"


def _accuracy_line(lecture: Dict[str, Any]) -> str:
    acc = (lecture.get("summary") or {}).get("accuracy") or {}
    w, b = acc.get("white"), acc.get("black")
    if w is None and b is None:
        return ""
    parts = []
    if w is not None:
        parts.append(f"White {w}%")
    if b is not None:
        parts.append(f"Black {b}%")
    return " · ".join(parts)


def _opening(lecture: Dict[str, Any]) -> str:
    opening = (lecture.get("game") or {}).get("opening") or {}
    name = opening.get("name")
    if not name:
        return "Opening not identified"
    eco = opening.get("eco")
    return name + (f" ({eco})" if eco else "")


def _move_text(lecture: Dict[str, Any]) -> str:
    pgn = (lecture.get("game") or {}).get("pgn") or ""
    try:
        node = chess.pgn.read_game(io.StringIO(pgn))
        if node is None:
            return ""
        board = node.board()
        out: List[str] = []
        for index, mv in enumerate(node.mainline_moves()):
            san = board.san(mv)
            board.push(mv)
            out.append(f"{index // 2 + 1}.{san}" if index % 2 == 0 else san)
        return " ".join(out)
    except Exception:
        return ""


def _sans_list(lecture: Dict[str, Any]) -> List[str]:
    """Replay the PGN and return the SAN of every move, in order."""
    pgn = (lecture.get("game") or {}).get("pgn") or ""
    try:
        node = chess.pgn.read_game(io.StringIO(pgn))
        if node is None:
            return []
        board = node.board()
        out: List[str] = []
        for mv in node.mainline_moves():
            out.append(board.san(mv))
            board.push(mv)
        return out
    except Exception:
        return []


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _top_moment(lecture: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    moments = lecture.get("moments") or []
    if not moments:
        return None
    errors = [m for m in moments if _is_error(m)]
    pool = errors or moments
    return max(
        pool,
        key=lambda m: abs(float(m.get("eval_after") or 0) - float(m.get("eval_before") or 0)),
    )


def _eval_str(value: Optional[float]) -> str:
    if value is None:
        return "?"
    if abs(value) >= 9000:
        return "mate" if value > 0 else "-mate"
    return f"{value / 100.0:+.2f}"


def _swing_str(moment: Dict[str, Any]) -> str:
    """Eval swing from the mover's own point of view, e.g. '-5.94 → -10.09 (black's view)'."""
    before = float(moment.get("eval_before") or 0)
    after = float(moment.get("eval_after") or 0)
    color = moment.get("color")
    if color == "black":
        before, after = -before, -after
    who = f"{color}'s" if color else "the mover's"
    return f"{_eval_str(before)} → {_eval_str(after)} ({who} view)"


def _move_num(moment: Dict[str, Any]) -> str:
    num = str(moment.get("move_number") or "")
    return num.rstrip(".") if num else ""


def _key_moment_text(moment: Optional[Dict[str, Any]]) -> str:
    if not moment:
        return "The engine found no single decisive moment — the result built up steadily."
    beats = moment.get("beats") or {}
    parts = [f"{_move_label(moment)}?"]
    if moment.get("motif") and str(beats.get("why_it_failed") or "").strip():
        parts.append(f" {str(beats['why_it_failed']).strip().rstrip('.')}.")
    else:
        parts.append(f" The score swung {_swing_str(moment)}.")
    best = moment.get("best_san")
    if best:
        parts.append(f" Better was {best}.")
    return "".join(parts)


def _move_label(moment: Dict[str, Any]) -> str:
    num = _move_num(moment)
    dots = "..." if moment.get("color") == "black" else "."
    return f"{num}{dots} {moment.get('san')}"


def _auto_big_ideas(lecture: Dict[str, Any]) -> List[str]:
    """Five concise, distinct, educational ideas from the verified analysis."""
    ideas: List[str] = []
    seen: set = set()

    def add(idea: str):
        key = idea.lower().strip()
        if idea and key not in seen:
            seen.add(key)
            ideas.append(idea)

    errors = [m for m in (lecture.get("moments") or []) if _is_error(m)]
    highlights = lecture.get("highlights") or []

    # 1. The decisive mistake and the fix.
    if errors:
        m = errors[0]
        label = _move_label(m)
        best = m.get("best_san")
        if best:
            add(f"The decisive slip: at {label}, the right move was {best}, not {m.get('san')}.")
        else:
            add(f"The decisive slip was {label} — it changed the course of the game.")
        concept = (m.get("beats") or {}).get("the_concept")
        if m.get("motif") and concept:
            add(f"{re.sub(r'[.?!]+$', '', str(concept).capitalize())} — the idea to remember here.")

    # 2. The winning idea / brilliancy — prefer a capture over a bare check.
    if highlights:
        h = next((x for x in highlights if "x" in str(x.get("san") or "")), highlights[0])
        add(f"The winning idea: {h.get('san')}! {h.get('reason')}.")

    # 3. The opening verdict.
    lead = _phase_lead(lecture, "Opening")
    if lead:
        add(re.sub(r"\.$", "", lead) + " — develop, castle, and control the centre first.")

    # 4. How the game was decided — the numbers behind the result.
    acc = (lecture.get("summary") or {}).get("accuracy") or {}
    w, b = acc.get("white"), acc.get("black")
    if w is not None and b is not None:
        if errors:
            add(f"Accuracy: White {w}%, Black {b}% — the decisive error was {_move_label(errors[0])}.")
        else:
            add(f"Accuracy: White {w}%, Black {b}% — a very accurate game.")
    else:
        add(f"{_result_phrase(lecture)} — {_verdict(_final_cp(lecture), _final_is_mate(lecture))}")

    # 5. Recurring habit / universal rule.
    habits = lecture.get("habits") or []
    if habits and habits[0].get("habit"):
        add(f"Recurring habit to fix: {str(habits[0]['habit']).lower()}.")
    add("Ask two questions after every move: what is it threatening, and is everything I own defended?")
    return ideas[:5]


_RULE_LIBRARY = {
    "opening": [
        "Develop all your pieces and castle before starting an attack.",
        "Don't move the same piece twice in the opening.",
        "Fight for the centre with your pawns and pieces.",
    ],
    "middlegame": [
        "Ask what your opponent is threatening before every move.",
        "After each move, check that every one of your pieces is defended.",
        "Put rooks on open files and knights on strong outposts.",
    ],
    "endgame": [
        "Bring your king to the centre — it becomes a strong piece.",
        "Push your passed pawns; they want to become queens.",
        "When you are ahead, trade pieces, not pawns.",
    ],
}


def _auto_remember(lecture: Dict[str, Any]) -> List[str]:
    """Three distinct rules — the sharpest specific lesson plus phase rules."""
    rules: List[str] = []
    seen: set = set()

    def add(rule: str) -> None:
        key = re.sub(r"[.?!]+$", "", str(rule)).strip().lower()
        if rule and key not in seen:
            seen.add(key)
            rules.append(str(rule).strip())

    # 1. The sharpest specific lesson (a named tactic), if the game has one.
    error_moments = [m for m in (lecture.get("moments") or []) if _is_error(m)]
    for m in error_moments:
        if m.get("motif"):
            rule = (m.get("beats") or {}).get("pattern_to_remember")
            if rule:
                add(rule)
                break

    # 2. Phase rules for the phases where the mistakes actually happened.
    phases = [str(m.get("phase") or "").lower() for m in error_moments]
    for phase in ("opening", "middlegame", "endgame"):
        if phase not in phases:
            continue
        for rule in _RULE_LIBRARY.get(phase, []):
            if len(rules) >= 3:
                break
            add(rule)
        if len(rules) >= 3:
            break

    # 3. Universal fallbacks so there are always three.
    for rule in (
        "What is my opponent threatening, and is everything I own safe?",
        "Develop and castle before starting a fight.",
        "Push your passed pawns — they want to become queens.",
    ):
        if len(rules) >= 3:
            break
        add(rule)

    return rules[:3]


_GLOSSARY = {
    "pin": "A piece that cannot move because it would expose a more valuable piece behind it.",
    "fork": "One move attacking two pieces at once — the knight's superpower.",
    "skewer": "A piece in front of a valuable piece is attacked and must move, exposing the piece behind.",
    "hanging piece": "An undefended piece that can be captured for free.",
    "trapped piece": "A piece with no safe square to move to.",
    "discovered attack": "Moving one piece reveals an attack from another piece behind it.",
    "double check": "Two pieces give check at the same time — the king must move.",
    "checkmate": "The king is in check and has no way to escape — the game is over.",
    "passed pawn": "A pawn with no enemy pawns blocking its path to promotion.",
    "open file": "A file with no pawns on it — a highway for rooks.",
    "isolated pawn": "A pawn with no friendly pawns on the files next to it.",
    "backward pawn": "A pawn left behind that cannot be protected by its neighbours.",
    "outpost": "A square deep in enemy territory that no enemy pawn can attack.",
    "development": "Getting pieces out quickly — don't move the same piece twice.",
    "castling": "The king moves two squares toward a rook and the rook jumps over — the fastest way to safety.",
    "tempo": "A move that forces the opponent to respond — time is an advantage.",
    "f7 square": "The weakest square in Black's camp — only the king guards it.",
    "back rank": "The row the king starts on — always give the king an escape square.",
    "knight sacrifice": "Giving up a knight to smash open the enemy king's position.",
    "zugzwang": "Any move makes your position worse — the worst kind of 'must move'.",
    "opposition": "Kings facing each other with one square between — the key endgame skill.",
}


def _auto_glossary(lecture: Dict[str, Any]) -> List[Dict[str, str]]:
    words: List[Dict[str, str]] = []
    seen: set = set()
    for m in lecture.get("moments") or []:
        motif = str(m.get("motif") or "").lower()
        concept = str((m.get("beats") or {}).get("the_concept") or "").lower()
        for term, meaning in _GLOSSARY.items():
            if (term in motif or term in concept) and term not in seen:
                seen.add(term)
                words.append({"word": term, "meaning": meaning})
            if len(words) >= 5:
                return words
    for term in ("development", "hanging piece", "passed pawn", "pin", "fork"):
        if term not in seen and len(words) < 5:
            seen.add(term)
            words.append({"word": term, "meaning": _GLOSSARY[term]})
    return words[:5]


def _white_eval_at(lecture: Dict[str, Any], ply: int) -> Optional[float]:
    """White-perspective centipawns after ``ply`` (0 = start)."""
    for row in lecture.get("eval_graph") or []:
        try:
            if int(row["ply"]) != ply:
                continue
            cp = float(row["cp"])
        except (TypeError, ValueError, AttributeError):
            continue
        return cp if ply % 2 == 1 else -cp
    return None


def _adv_phrase(cp: float) -> str:
    if abs(cp) >= 9000:
        return "checkmate is on the board" if cp > 0 else "Black has a forced mate"
    if cp >= 300:
        return f"White is completely winning (+{cp / 100:.1f})"
    if cp >= 120:
        return f"White is clearly better (+{cp / 100:.1f})"
    if cp <= -300:
        return f"Black is completely winning (+{abs(cp) / 100:.1f})"
    if cp <= -120:
        return f"Black is clearly better ({cp / 100:.1f})"
    return "the position is about equal"


def _phase_lead(lecture: Dict[str, Any], phase: str) -> str:
    if phase == "Opening":
        cp = _white_eval_at(lecture, 10)
        if cp is None:
            cp = _white_eval_at(lecture, 12) or _white_eval_at(lecture, 8)
        if cp is None:
            return ""
        return "After the opening: " + _adv_phrase(cp) + "."
    if phase == "Endgame":
        return "The endgame: " + _result_phrase(lecture) + "."
    return ""


def _moment_note(moment: Dict[str, Any]) -> str:
    cls = str(moment.get("classification") or "inaccuracy")
    if moment.get("motif"):
        cause = str(moment["motif"])
    else:
        loss = float(moment.get("cp_loss") or 0) / 100.0
        cause = f"costs {loss:.1f} pawns"
    parts = [cls, cause]
    best = moment.get("best_san")
    if best:
        parts.append(f"better was {best}")
    return "; ".join(parts)


_ERROR_CLASSES = {"blunder", "mistake", "inaccuracy"}


def _is_error(moment: Dict[str, Any]) -> bool:
    """Only annotate real errors — 'good' moves are not worth a note."""
    return str(moment.get("classification") or "").lower() in _ERROR_CLASSES


def _story_html(lecture: Dict[str, Any]) -> str:
    """The full game as a move-by-move story with engine-grounded notes."""
    pgn = (lecture.get("game") or {}).get("pgn") or ""
    try:
        node = chess.pgn.read_game(io.StringIO(pgn))
    except Exception:
        node = None
    if node is None:
        return '<div class="moves">' + _esc(_move_text(lecture)) + "</div>"

    moves = list(node.mainline_moves())
    if not moves:
        return '<div class="moves">—</div>'

    moments: Dict[int, Dict[str, Any]] = {}
    for m in lecture.get("moments") or []:
        try:
            moments[int(m["ply"])] = m
        except (TypeError, ValueError):
            continue

    highlights: Dict[int, Dict[str, Any]] = {}
    for h in lecture.get("highlights") or []:
        try:
            highlights[int(h["ply"])] = h
        except (TypeError, ValueError):
            continue

    def phase_of(ply: int) -> str:
        if ply <= 10:
            return "Opening"
        if ply <= 30:
            return "Middlegame"
        return "Endgame"

    board = node.board()
    blocks: List[tuple] = []
    current: Optional[str] = None
    tokens: List[str] = []

    for idx, mv in enumerate(moves):
        ply = idx + 1
        san = board.san(mv)
        board.push(mv)

        phase = phase_of(ply)
        if phase != current:
            if current is not None and tokens:
                blocks.append((current, tokens))
            current = phase
            tokens = []

        if idx % 2 == 0:
            tokens.append(f"{idx // 2 + 1}.{_esc(san)}")
        else:
            tokens.append(_esc(san))

        if ply in highlights:
            reason = str(highlights[ply].get("reason") or "a strong move")
            tokens.append(f'<span class="note">(! {_esc(reason)})</span>')
        elif ply in moments and _is_error(moments[ply]):
            tokens.append(f'<span class="note">({_esc(_moment_note(moments[ply]))})</span>')

    if tokens:
        blocks.append((current or "Middlegame", tokens))

    out: List[str] = []
    for label, toks in blocks:
        lead = _phase_lead(lecture, label)
        header = f'<div class="phase">{_esc(label)}</div>'
        if lead:
            header += f'<div class="phasenote">{_esc(lead)}</div>'
        out.append(header + '<div class="moves">' + " ".join(toks) + "</div>")
    return "\n".join(out)


def _moments_html(lecture: Dict[str, Any]) -> str:
    moments = [m for m in (lecture.get("moments") or []) if _is_error(m)]
    if not moments:
        return '<p class="moments-none">No real mistakes — this was a clean, well-played game. ' \
               "The strongest move is shown in The Key Moment above.</p>"
    rows: List[str] = []
    seen_idea: set = set()
    seen_rule: set = set()
    for m in moments[:4]:
        beats = m.get("beats") or {}
        num = _move_num(m)
        dots = "..." if m.get("color") == "black" else "."
        san = m.get("san") or "?"
        best = m.get("best_san")

        if m.get("motif") and str(beats.get("why_it_failed") or "").strip():
            why = str(beats["why_it_failed"]).strip().rstrip(".")
        else:
            loss = float(m.get("cp_loss") or 0) / 100.0
            why = f"cost {loss:.1f} pawns — the score went {_swing_str(m)}"

        parts = [f"<strong>{_esc(num)}{dots} {_esc(san)}</strong>"]
        if best:
            parts.append(f"<em>(better was {_esc(best)})</em>")
        parts.append(f"{why[0].upper()}{why[1:]}.")

        # Show each distinct idea and rule only once — never repeat the same
        # generic advice across several moments.
        concept = re.sub(r"[.?!]+$", "", str(beats.get("the_concept") or "")).strip()
        if concept and concept.lower() not in seen_idea:
            seen_idea.add(concept.lower())
            parts.append(f"Idea: {concept}.")
        rule = re.sub(r"[.?!]+$", "", str(beats.get("pattern_to_remember") or "")).strip()
        if rule and rule.lower() not in seen_rule:
            seen_rule.add(rule.lower())
            parts.append(f"Remember: {rule}.")

        rows.append("<li>" + " ".join(parts) + "</li>")
    return '<ol class="moments">\n' + "\n".join(f"  {r}" for r in rows) + "\n</ol>"


_LEVEL_LABELS = {"beginner": "Beginner", "improver": "Improver", "club": "Club", "expert": "Expert"}
_LEVEL_NOUNS = {"beginner": "Beginners", "improver": "Improvers", "club": "Club Players", "expert": "Experts"}

_MOTIF_TITLES = {
    "walked into mate": "King Safety First!",
    "walked into a fork": "Knights Love Forks!",
    "walked into a pin": "Pins Paralyse Pieces!",
    "walked into a skewer": "Beware the Skewer!",
    "trapped a piece": "Every Piece Needs a Way Out!",
    "hung a piece": "Loose Pieces Lose Games!",
}


def _level_label(lecture: Dict[str, Any]) -> str:
    level = str((lecture.get("meta") or {}).get("level") or "club").lower()
    return _LEVEL_LABELS.get(level, "Club")


def _level_noun(lecture: Dict[str, Any]) -> str:
    level = str((lecture.get("meta") or {}).get("level") or "club").lower()
    return _LEVEL_NOUNS.get(level, "Club Players")


def _theme_title(lecture: Dict[str, Any]) -> str:
    """A friendly thematic title from the opening name — never invented."""
    opening = (lecture.get("game") or {}).get("opening") or {}
    name = str(opening.get("name") or "")
    short = name.split(":")[0].split(",")[0].strip()
    if short:
        return f"The {short}!"
    white = _white(lecture)
    black = _black(lecture)
    return f"{_short_name(white)} vs {_short_name(black)}"


def _highlight_title(san: Optional[str]) -> str:
    if not san:
        return "The Winning Idea!"
    if san.endswith("#"):
        return "Checkmate!"
    return {
        "N": "A Knight's Fury!",
        "B": "The Bishop Strikes!",
        "Q": "The Queen Invades!",
        "R": "Rook Power!",
        "K": "The King Marches!",
    }.get(san[0], "The Winning Idea!")


def _best_highlight(lecture: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The most instructive brilliancy — prefer a piece capture over a pawn recapture."""
    highlights = lecture.get("highlights") or []
    if not highlights:
        return None
    for h in highlights:
        san = str(h.get("san") or "")
        if "x" in san and san[0] in "NBRQ":
            return h
    for h in highlights:
        if "x" in str(h.get("san") or ""):
            return h
    return highlights[0]


def _grandmaster_ideas(lecture: Dict[str, Any]) -> List[tuple]:
    """Five (bold lead-in, explanation) pairs from the verified analysis."""
    ideas: List[tuple] = []
    errors = [m for m in (lecture.get("moments") or []) if _is_error(m)]
    highlights = lecture.get("highlights") or []

    # 1. The decisive mistake and the fix.
    if errors:
        m = errors[0]
        label = _move_label(m)
        best = m.get("best_san")
        lead = _MOTIF_TITLES.get(m.get("motif"), "The Decisive Moment!")
        body = (f"At {label}, the right move was {best}, not {m.get('san')}."
                if best else f"{label} changed the whole game.")
        ideas.append((lead, body))

    # 2. The winning idea / brilliancy.
    h = _best_highlight(lecture)
    if h:
        san = str(h.get("san") or "the best move")
        ideas.append((_highlight_title(san), f"{san}! {h.get('reason')}."))

    # 3. The opening.
    op = _phase_lead(lecture, "Opening") or "The opening set the stage."
    ideas.append(("The Opening", op.rstrip(".") + " — develop, castle, and control the centre first."))

    # 4. The numbers behind the result.
    acc = (lecture.get("summary") or {}).get("accuracy") or {}
    w, b = acc.get("white"), acc.get("black")
    if w is not None and b is not None:
        body = f"White {w}%, Black {b}%."
        if errors:
            body += f" The decisive error was {_move_label(errors[0])}."
        ideas.append(("The Numbers", body))
    else:
        ideas.append(("The Result", _result_phrase(lecture) + "."))

    # 5. The golden rule.
    ideas.append(("The Golden Rule",
                  "Ask two questions after every move: what is it threatening, and is everything I own defended?"))

    # Pad to five with phase rules if any slot was skipped.
    for rule in _auto_remember(lecture):
        if len(ideas) >= 5:
            break
        if any(rule.lower() == str(body).lower() for _, body in ideas):
            continue
        ideas.append(("Remember", rule))
    return ideas[:5]


def _scoresheet_html(lecture: Dict[str, Any]) -> str:
    """The game as a two-column NJCC scoresheet with ?/?!/! marks."""
    pgn = (lecture.get("game") or {}).get("pgn") or ""
    try:
        node = chess.pgn.read_game(io.StringIO(pgn))
    except Exception:
        node = None
    if node is None:
        return ""
    moves = list(node.mainline_moves())
    if not moves:
        return ""

    marks: Dict[int, str] = {}
    for m in lecture.get("moments") or []:
        try:
            ply = int(m["ply"])
        except (TypeError, ValueError):
            continue
        if not _is_error(m):
            continue
        marks[ply] = {"blunder": "??", "mistake": "?", "inaccuracy": "?!"}.get(
            str(m.get("classification") or ""), "?"
        )
    for h in lecture.get("highlights") or []:
        try:
            marks[int(h["ply"])] = "!"
        except (TypeError, ValueError):
            continue

    board = node.board()
    sans: List[str] = []
    for idx, mv in enumerate(moves):
        ply = idx + 1
        san = board.san(mv)
        board.push(mv)
        sans.append(san + marks.get(ply, ""))

    pairs = []
    total = (len(sans) + 1) // 2
    for n in range(1, total + 1):
        w = sans[2 * (n - 1)] if 2 * (n - 1) < len(sans) else ""
        b = sans[2 * (n - 1) + 1] if 2 * (n - 1) + 1 < len(sans) else ""
        pairs.append((n, w, b))

    # Very long games (100+ plies) do not fit one page — abridge the middle.
    abridged = ""
    if len(pairs) > 50:
        head, tail = pairs[:30], pairs[-8:]
        first_omitted = head[-1][0] + 1
        last_omitted = tail[0][0] - 1
        pairs = head + [("…", "", "")] + tail
        abridged = (f'<p class="abridged">Moves {first_omitted}–{last_omitted} '
                    f'omitted to fit one page — the full score is in the annotated PGN.</p>')

    half = (len(pairs) + 1) // 2
    left, right = pairs[:half], pairs[half:]
    header = ("<tr><th>No.</th><th>White</th><th>Black</th>"
              "<th>No.</th><th>White</th><th>Black</th></tr>")
    rows = []
    for i in range(half):
        l = left[i] if i < len(left) else ("", "", "")
        r = right[i] if i < len(right) else ("", "", "")
        rows.append(
            f"<tr><td class='no'>{l[0]}</td><td>{_esc(l[1])}</td><td>{_esc(l[2])}</td>"
            f"<td class='no'>{r[0] or ''}</td><td>{_esc(r[1])}</td><td>{_esc(r[2])}</td></tr>"
        )
    return "<table class='score'>" + header + "".join(rows) + "</table>" + abridged


def _key_arrow(moment: Optional[Dict[str, Any]], fen: str) -> Optional[List[Tuple[str, str]]]:
    """Arrow for the key-moment board: the better move, or the move played."""
    if not moment or not fen:
        return None
    san = str(moment.get("best_san") or moment.get("san") or "").strip()
    if not san:
        return None
    try:
        move = chess.Board(fen).parse_san(san)
    except Exception:
        return None
    return [(chess.square_name(move.from_square), chess.square_name(move.to_square))]


def _did_you_know(lecture: Dict[str, Any]) -> str:
    highlights = lecture.get("highlights") or []
    errors = [m for m in (lecture.get("moments") or []) if _is_error(m)]
    for h in highlights:
        san = str(h.get("san") or "")
        if san.startswith("N") and "x" in san:
            return ("Knights are at their best in closed positions — they jump over pawns "
                    "while bishops and rooks stay blocked. A knight sacrifice to open the "
                    "enemy king is one of the oldest attacking ideas in chess!")
    if any(m.get("motif") == "walked into a pin" for m in errors):
        return ("A pinned piece cannot move, so any defence counting on it is imaginary. "
                "Always defend a pinned piece with something else!")
    if any(m.get("motif") == "walked into a fork" for m in errors):
        return ("Knights are the only pieces that can jump, so a knight fork attacks two "
                "pieces at once. Keep your queen and rooks off knight-fork squares!")
    if errors and errors[0].get("phase") == "endgame":
        return ("Endgames are decided by king activity and passed pawns, not by tricks. "
                "Activate your king early!")
    opening = ((lecture.get("game") or {}).get("opening") or {}).get("name") or ""
    if "King's Indian" in opening:
        return ("The King's Indian is a fighting opening — Black lets White build a big "
                "centre, then attacks it. Expect a sharp struggle!")
    return ("Every master game tells a story. Replay this one slowly and ask on every move: "
            "what is the threat, and what would I play?")


# --------------------------------------------------------------------------
# CSS
# --------------------------------------------------------------------------

_CSS = """
  @page { size: 8.5in 11in; margin: 9mm; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: #fff; }
  body { font-family: Helvetica, Arial, sans-serif; color: #17181a;
         font-size: 8.8pt; line-height: 1.23; }
  .masthead { text-align: center; text-transform: uppercase; letter-spacing: .12em;
    font-size: 7pt; line-height: 1.4; border-top: 2.5px double #17181a;
    border-bottom: 2.5px double #17181a; padding: 1px 0 2px; margin-bottom: 4px; }
  h1 { font-size: 14.5pt; margin: 0; text-align: center; line-height: 1.1; }
  .sub { text-align: center; font-style: italic; font-size: 8.6pt; margin: 0 0 3px; }
  .infobar { background: #f3f3ef; border: 1px solid #c9c9c1; padding: 3px 6px;
    font-size: 7.8pt; text-align: center; margin-bottom: 5px; }
  .cols { width: 100%; border-collapse: collapse; }
  .cols td { vertical-align: top; }
  .col-l { width: 46%; padding-right: 8px; }
  .col-r { width: 54%; padding-left: 8px; border-left: 1px solid #c9c9c1; }
  h2 { font-size: 8.4pt; text-transform: uppercase; letter-spacing: .05em;
    margin: 4px 0 1px; padding-bottom: 1px; border-bottom: 1px solid #17181a; }
  h2.top { margin-top: 0; }
  p { margin: 2px 0; }
  ol, ul { margin: 2px 0 2px 16px; padding: 0; }
  li { margin: 1.5px 0; }
  .board { text-align: center; margin: 2px 0; }
  .board svg { display: inline-block; }
  .caption { font-style: italic; font-size: 8pt; text-align: center; margin: 2px 0 3px; }
  .box { border: 1px solid #c9c9c1; background: #f7f7f4; padding: 3px 6px; margin: 3px 0; }
  .box.blue { background: #eef3f8; border-color: #b8ccdd; }
  .box h3 { margin: 0 0 2px; font-size: 8.6pt; text-transform: uppercase; letter-spacing: .04em; }
  .kmtbl { width: 100%; border-collapse: collapse; }
  .kmtbl td { vertical-align: top; }
  .kmtbl .bd { width: 180px; }
  .kmtbl .bd svg { display: block; }
  table.score { width: 100%; border-collapse: collapse; font-family: 'Courier New', monospace;
    font-size: 7.1pt; margin: 2px 0; }
  table.score th, table.score td { border: 1px solid #c9c9c1; padding: 0.5px 2px; text-align: left; }
  table.score th { background: #f3f3ef; font-size: 7pt; text-transform: uppercase; }
  table.score td.no { width: 7%; text-align: right; color: #555; background: #fbfbf8; }
  .resultline { text-align: center; font-weight: bold; margin: 2px 0 4px; }
  table.words { width: 100%; border-collapse: collapse; font-size: 7.8pt; margin: 2px 0; }
  table.words th, table.words td { border: 1px solid #c9c9c1; padding: 2px 5px; text-align: left; }
  table.words th { background: #f3f3ef; font-size: 7.2pt; text-transform: uppercase; letter-spacing: .03em; }
  .ident { font-size: 9pt; margin: 2px 0 6px; }
  .ident .ul { display: inline-block; width: 150px; border-bottom: 1px solid #17181a; }
  .instr { font-style: italic; margin: 0 0 6px; }
  .ans { height: 13px; border-bottom: 1px solid #999; margin: 1px 0 4px; }
  .abridged { font-size: 6.8pt; font-style: italic; text-align: center; margin: 1px 0 2px; color: #555; }
  .answerkey { font-size: 6.8pt; color: #666; margin: 1px 0 2px; }
  .foot { margin-top: 3px; border-top: 2.5px double #17181a; padding-top: 2px;
    text-align: center; font-size: 6.8pt; letter-spacing: .05em; }
"""


# --------------------------------------------------------------------------
# One-page lesson
# --------------------------------------------------------------------------

def to_onepager_html(lecture: Dict[str, Any], prose: Optional[Dict[str, Any]] = None) -> str:
    prose = prose or {}
    white = _white(lecture)
    black = _black(lecture)
    opening = _opening(lecture)
    level_label = _level_label(lecture)

    theme = prose.get("title") or _theme_title(lecture)
    subtitle = f"{white} (White) vs {black} (Black)" + (f" — {opening}" if opening else "")

    infobar = " · ".join(p for p in [
        f"Opening: {opening}" if opening else "",
        f"White: {white}",
        f"Black: {black}",
        _result_phrase(lecture),
        _accuracy_line(lecture),
    ] if p)

    final_fen, last_move = _final_board(lecture)
    verdict = _verdict(_final_cp(lecture), _final_is_mate(lecture))
    final_diagram = _r.board_svg(final_fen, size=280, last_move=last_move) if final_fen else ""

    top = _top_moment(lecture)
    key_text = prose.get("key_moment") or _key_moment_text(top)
    key_fen = (top or {}).get("fen_before") or ""
    key_arrows = _key_arrow(top, key_fen)
    key_diagram = _r.board_svg(key_fen, size=180, arrows=key_arrows) if key_fen else ""

    ideas = prose.get("big_ideas")
    if isinstance(ideas, str):
        ideas = [ideas]
    if ideas:
        ideas_html = "\n".join(f"  <li>{_esc(str(i))}</li>" for i in ideas[:5])
    else:
        ideas_html = "\n".join(
            f"  <li><strong>{_esc(lead)}</strong> — {_esc(body)}</li>"
            for lead, body in _grandmaster_ideas(lecture)
        )

    lessons = prose.get("things_to_remember") or _auto_remember(lecture)
    if isinstance(lessons, str):
        lessons = [lessons]
    lessons_html = "\n".join(f"  <li>{_esc(str(r))}</li>" for r in lessons[:3])

    # Vocabulary defaults to the verified glossary. Model-supplied words are
    # honoured ONLY when they include a real definition, so the column can
    # never render blank while still allowing a named format (e.g. Ken Thomas).
    glossary = _auto_glossary(lecture)
    custom = prose.get("chess_words")
    if isinstance(custom, list):
        valid = []
        for g in custom:
            if not isinstance(g, dict):
                continue
            word = str(g.get("word") or g.get("term") or "").strip()
            meaning = str(g.get("meaning") or g.get("definition") or "").strip()
            if word and meaning:
                valid.append({"word": word, "meaning": meaning})
        if valid:
            glossary = valid[:5]
    glossary_rows = "\n".join(
        f"<tr><td><strong>{_esc(str(g.get('word', '')).title())}</strong></td>"
        f"<td>{_esc(str(g.get('meaning', '')))}</td></tr>"
        for g in glossary[:5]
    )

    scoresheet = _scoresheet_html(lecture)
    dyk = _did_you_know(lecture)

    key_box = (f'<div class="box"><h3>&#9658; Key Moment</h3>'
               f'<table class="kmtbl"><tr><td class="bd">{key_diagram}</td>'
               f'<td><p>{_esc(str(key_text))}</p></td></tr></table></div>')

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{_esc(str(theme))}</title>
<style>{_CSS}</style>
</head>
<body>
  <div class="masthead">&#9812; N E W &nbsp; J E R S E Y &nbsp; C H E S S &nbsp; C L U B &#9812;<br>
  {_esc(level_label)} L E S S O N &nbsp; S E R I E S &nbsp; &middot; &nbsp; G R A N D M A S T E R &nbsp; G A M E &nbsp; A N A L Y S I S</div>

  <h1>{_esc(str(theme))}</h1>
  <p class="sub">{_esc(str(subtitle))}</p>
  <div class="infobar">{_esc(str(infobar))}</div>

  <table class="cols">
    <tr>
      <td class="col-l">
        <div class="board">{final_diagram}</div>
        <div class="caption">{_esc(str(verdict))}</div>
{key_box}
      </td>
      <td class="col-r">
        <h2 class="top">&#9823; The Complete Game</h2>
{scoresheet}
        <div class="resultline">{_esc(str(_result_phrase(lecture)))}</div>

        <div class="box blue">
          <h3>&#9733; 3 Big Lessons For {_esc(_level_noun(lecture))}</h3>
          <ol>
{lessons_html}
          </ol>
        </div>
      </td>
    </tr>
  </table>

  <div class="box">
    <h3>&#9670; Grandmaster Ideas (Made Simple)</h3>
    <ol>
{ideas_html}
    </ol>
  </div>

  <table class="cols bottom">
    <tr>
      <td class="col-l">
        <h2>&#8801; Chess Words To Know</h2>
        <table class="words">
          <tr><th>Word</th><th>What It Means</th></tr>
{glossary_rows}
        </table>
      </td>
      <td class="col-r">
        <div class="box">
          <h3>Did You Know?</h3>
          <p>{_esc(str(dyk))}</p>
        </div>
      </td>
    </tr>
  </table>

  <div class="foot">New Jersey Chess Club &middot; {_esc(level_label)} Lesson &middot; {_esc(str(theme))} &middot; PRINT AND STUDY!</div>
</body>
</html>
"""
    return doc


# --------------------------------------------------------------------------
# Homework sheet
# --------------------------------------------------------------------------

def to_homework_html(lecture: Dict[str, Any], puzzle: Optional[Dict[str, Any]] = None) -> str:
    white = _white(lecture)
    black = _black(lecture)
    opening = _opening(lecture)
    level_label = _level_label(lecture)
    theme = _theme_title(lecture)
    summary = lecture.get("summary") or {}
    acc = summary.get("accuracy") or {}
    w_acc, b_acc = acc.get("white"), acc.get("black")
    errors = [m for m in (lecture.get("moments") or []) if _is_error(m)]
    top = _top_moment(lecture)
    result = str((lecture.get("game") or {}).get("result") or "?")

    def blank(n: int = 1) -> str:
        return "".join('<div class="ans"></div>' for _ in range(n))

    # Diagram for the "find the better move" exercise — the position before the key error.
    km_fen = (top or {}).get("fen_before") or ""
    km_diagram = _r.board_svg(km_fen, size=120) if km_fen else ""
    km_caption = ""
    if km_diagram and top:
        c = str(top.get("color") or "the player").title()
        km_caption = f'<div class="caption">{_esc(c)} to move — what would you play?</div>'

    sans = _sans_list(lecture)
    n_plies = len(sans)
    final_san = sans[-1] if sans else ""
    eco = str(((lecture.get("game") or {}).get("opening") or {}).get("eco") or "")
    # A White move about a third of the way in (always inside the printed scoresheet).
    probe_idx = 2 * min(n_plies // 6, 24)
    probe_num = probe_idx // 2 + 1
    probe_san = sans[probe_idx] if 0 <= probe_idx < n_plies else ""

    a_lines = (
        f"<li>Who played White, and who played Black?{blank()}</li>"
        f"<li>What opening was played"
        + (f" (ECO {_esc(eco)})" if eco else "")
        + f"?{blank()}</li>"
        f"<li>What was the result?{blank()}</li>"
    )

    if top:
        color = str(top.get("color") or "the player").title()
        san = str(top.get("san"))
        best = top.get("best_san")
        b_lines = (
            f"<li>The engine says ONE move changed the whole game. Which move was it, and who played it?{blank()}</li>"
            f"<li>Set up the position in the diagram above. It is {_esc(color)} to move. "
            f"Before you look at the answer, what would YOU play, and why?{blank(2)}</li>"
        )
        if best:
            b_lines += (
                f"<li>The better move was {_esc(best)}. What does it do that {_esc(san)} did not?{blank(2)}</li>"
                f"<li>What did {_esc(san)} allow the opponent to do?{blank(2)}</li>"
            )
        else:
            b_lines += f"<li>What did {_esc(san)} let the opponent do?{blank(2)}</li>"
    else:
        b_lines = (
            f"<li>Replay the game and find the move where the score changed the most. "
            f"Which move was it, and who played it?{blank()}</li>"
            f"<li>What would you have played instead, and why?{blank(2)}</li>"
        )

    c_lines = ""
    if w_acc is not None and b_acc is not None:
        c_lines += f"<li>Which player was more accurate, and by how much?{blank()}</li>"
    if top:
        c_lines += (
            f"<li>The score swung the most at {_esc(_move_label(top))}. "
            f"Roughly how much did the evaluation change, and in whose favour?{blank(2)}</li>"
        )
    if errors:
        c_lines += f"<li>Which single move was the biggest error, and who played it?{blank()}</li>"
    if w_acc is not None and b_acc is not None:
        loser = _short_name(white) if result.startswith("0-1") else (_short_name(black) if result.startswith("1-0") else "")
        loser_acc = w_acc if result.startswith("0-1") else (b_acc if result.startswith("1-0") else None)
        if loser and loser_acc is not None:
            c_lines += (
                f"<li>{_esc(loser)} played {loser_acc}% accurate but still lost. "
                f"Why is high accuracy not always enough to win?{blank(2)}</li>"
            )
    if not c_lines:
        c_lines = f"<li>Who had the better position at the end — White or Black, and how can you tell?{blank()}</li>"

    d_lines = ""
    if top and str(top.get("motif") or "").lower() not in ("", "good"):
        d_lines += f"<li>Name ONE tactic that appears in this game, and the move where it happened.{blank()}</li>"
    if errors:
        concept = re.sub(r"[.?!]+$", "", str((errors[0].get("beats") or {}).get("the_concept") or "")).strip()
        if concept:
            d_lines += f"<li>In your own words, explain this idea from the lesson: {_esc(concept)}.{blank(2)}</li>"
    h = _best_highlight(lecture)
    if h:
        d_lines += f"<li>The lesson calls {_esc(str(h.get('san')))}! the winning idea. What does it achieve?{blank(2)}</li>"
    if not d_lines:
        d_lines = f"<li>In your own words, what was the single most important idea in this game?{blank(2)}</li>"

    # Recall questions that force the student to actually replay the game.
    p_lines = ""
    if probe_san:
        p_lines += f"<li>Replay the game from the start. What was White's {_ordinal(probe_num)} move?{blank()}</li>"
    if final_san:
        p_lines += f"<li>What was the very last move of the game?{blank()}</li>"
    if n_plies:
        p_lines += f"<li>How many moves were played in total?{blank()}</li>"
    if not p_lines:
        p_lines = f"<li>Replay the game and write down the first ten moves from memory.{blank(3)}</li>"

    winner_side = "White" if result.startswith("1-0") else "Black" if result.startswith("0-1") else "the winner"
    e1 = (f"Set up the final position and play it out against a friend or the computer. "
          f"Your job is {winner_side} — find the winning plan.")
    rules = _auto_remember(lecture)
    rule = (rules[0] if rules else "ask what your opponent is threatening before every move").rstrip(".")
    e2 = (f"Play one game this week and follow this rule: {_esc(rule)}. "
          f"Write the move you were most tempted to break the rule:")
    e_lines = (
        f"<li>{_esc(e1)}{blank()}</li>"
        f"<li>{_esc(e2)}{blank()}</li>"
        f"<li>Explain this game to a friend in two minutes. What is the first thing you will tell them?{blank(2)}</li>"
    )

    b_diagram = (f'<div class="board">{km_diagram}</div>{km_caption}') if km_diagram else ""

    puzzle_html = ""
    if puzzle and puzzle.get("fen"):
        try:
            pb = chess.Board(puzzle["fen"])
            side = "White" if pb.turn == chess.WHITE else "Black"
            p_diagram = _r.board_svg(puzzle["fen"], size=130)
            rating = puzzle.get("rating")
            themes = ", ".join((puzzle.get("themes") or [])[:3])
            cap = f"{side} to move — find the best move."
            if rating:
                cap += f" Rating {rating}."
            if themes:
                cap += f" Themes: {themes}."
            ans = puzzle.get("first_move_san") or ""
            puzzle_html = (
                '<h2>&#9823; G. Today&#8217;s Puzzle (Lichess)</h2>'
                f'<div class="board">{p_diagram}</div>'
                f'<p class="caption">{_esc(cap)}</p>'
                '<ol><li>What is the winning move, and what does it threaten?'
                + blank(2) + '</li></ol>'
            )
            if ans:
                puzzle_html += f'<p class="answerkey">Answer key: {_esc(ans)}</p>'
        except Exception:
            puzzle_html = ""

    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Homework — {_esc(str(theme))}</title>
<style>{_CSS}</style>
</head>
<body>
  <div class="masthead">&#9632; N E W &nbsp; J E R S E Y &nbsp; C H E S S &nbsp; C L U B &#9632;<br>
  {_esc(level_label)} L E S S O N &nbsp; S E R I E S &nbsp; : &nbsp; H O M E W O R K</div>

  <h1>Homework: {_esc(str(theme))}</h1>
  <div class="ident">
    Name: <span class="ul"></span> &nbsp; Date: <span class="ul"></span> &nbsp; Group: <span class="ul"></span>
  </div>
  <p class="instr">You may use the lesson sheet. Write in full sentences — explain WHY, not just what.</p>

  <h2>&#9733; A. Warm-up</h2>
  <ol>
{a_lines}
  </ol>

  <h2>&#9889; B. Find the Turning Point</h2>
{b_diagram}
  <ol>
{b_lines}
  </ol>

  <h2>&#8801; C. The Numbers</h2>
  <ol>
{c_lines}
  </ol>

  <h2>&#9670; D. Tactics &amp; Ideas</h2>
  <ol>
{d_lines}
  </ol>

  <h2>&#9823; E. Prove You Studied</h2>
  <ol>
{p_lines}
  </ol>

  <h2>&#9733; F. Do This At Home</h2>
  <ol>
{e_lines}
  </ol>

{puzzle_html}
  <div class="foot">New Jersey Chess Club &middot; {_esc(level_label)} Lesson &middot; {_esc(str(theme))} &middot; HAND THIS BACK NEXT WEEK!</div>
</body>
</html>
"""
    return doc


def html_to_pdf(html_doc: str, out_path: str) -> Optional[str]:
    """Render an HTML string to PDF via WeasyPrint. Returns path or None."""
    try:
        from weasyprint import HTML  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        HTML(string=html_doc).write_pdf(out_path)
    except Exception:
        return None
    return out_path
