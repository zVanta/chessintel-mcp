"""ChessIntel MCP server — engine-verified chess analysis and lecture generation.

Exposes tools your LibreChat agents can call. Every tool returns *verified*
facts (Stockfish evaluations, computed board state, detected tactics); the LLM
writes the prose on top. That is the whole point: the model cannot hallucinate
a move, an evaluation or a result that it never received.

Transports
----------
CHESSINTEL_TRANSPORT=stdio   (default) — for LibreChat `command`-style MCP servers
CHESSINTEL_TRANSPORT=http              — streamable HTTP on /mcp for Docker

Run (dev):  fastmcp run chessintel/server.py
Run (http): CHESSINTEL_TRANSPORT=http python -m chessintel.server
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import chess
import chess.engine

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response

try:
    from . import analysis, famous_games, fetchers, lectures as lectures_mod, master_games, openings, renderers, storage
    from . import coach_core as core
except ImportError:  # allows `python /path/chessintel/server.py` directly
    import analysis
    import famous_games
    import fetchers
    import lectures as lectures_mod
    import master_games
    import openings
    import renderers
    import storage
    import coach_core as core

# ── .env ────────────────────────────────────────────────────────────────────
# Loaded before anything reads os.environ. Next to the project in dev, next to
# the executable when frozen.

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).resolve().parent
else:
    _BASE = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(_BASE / ".env")
except ImportError:  # pragma: no cover - optional dependency
    pass

# ── Config ──────────────────────────────────────────────────────────────────

def _resolve_stockfish() -> str:
    """Resolve the Stockfish binary with sane fallbacks.

    The systemd unit supplies STOCKFISH_PATH via EnvironmentFile, but any
    manual run (selftest, debugging, a different supervisor) would otherwise
    fall back to the literal string "stockfish" and fail with ENOENT.
    """
    candidates = [
        os.environ.get("STOCKFISH_PATH"),
        "/opt/chessintel/bin/stockfish",
        shutil.which("stockfish"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return os.environ.get("STOCKFISH_PATH") or "stockfish"


STOCKFISH_PATH = _resolve_stockfish()
STOCKFISH_THREADS = int(os.environ.get("STOCKFISH_THREADS", "2"))
STOCKFISH_HASH_MB = int(os.environ.get("STOCKFISH_HASH_MB", "128"))
DEFAULT_DEPTH = int(os.environ.get("CHESSINTEL_DEPTH", "14"))
OUTPUT_DIR = os.environ.get("CHESSINTEL_OUT", "./out")
PUBLIC_BASE = os.environ.get("CHESSINTEL_PUBLIC_BASE", "https://njxai.com").rstrip("/")
TRANSPORT = os.environ.get("CHESSINTEL_TRANSPORT", "stdio").lower()
HOST = os.environ.get("CHESSINTEL_HOST", "0.0.0.0")
PORT = int(os.environ.get("CHESSINTEL_PORT", "8080"))

mcp = FastMCP("chessintel")

# ── Engine pool ─────────────────────────────────────────────────────────────
#
# One long-lived Stockfish process, serialised behind a re-entrant lock.
# SimpleEngine is not thread-safe and MCP tool calls can arrive concurrently,
# so every engine use is guarded. A dead engine is discarded and rebuilt once.

_engine_lock = threading.RLock()
_engine: Optional[chess.engine.SimpleEngine] = None


def _spawn_engine() -> chess.engine.SimpleEngine:
    engine = core._open_engine(STOCKFISH_PATH)
    try:
        engine.configure({"Threads": STOCKFISH_THREADS, "Hash": STOCKFISH_HASH_MB})
    except Exception:
        pass
    return engine


def _drop_engine() -> None:
    global _engine
    with _engine_lock:
        if _engine is not None:
            try:
                _engine.quit()
            except Exception:
                pass
            _engine = None


def _with_engine(worker):
    """Run ``worker(engine)`` against the shared engine, rebuilding once on failure."""
    global _engine
    with _engine_lock:
        last_error: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                if _engine is None:
                    _engine = _spawn_engine()
                return worker(_engine)
            except (
                chess.engine.EngineTerminatedError,
                chess.engine.EngineError,
                BrokenPipeError,
                OSError,
            ) as exc:
                last_error = exc
                if _engine is not None:
                    try:
                        _engine.quit()
                    except Exception:
                        pass
                    _engine = None
        raise RuntimeError(
            f"Stockfish at {STOCKFISH_PATH!r} is not usable: {last_error}. "
            "Check STOCKFISH_PATH points at a working binary."
        )


def _engine_ready() -> bool:
    try:
        _with_engine(lambda engine: engine.ping())
        return True
    except Exception:
        return False


# ── Output helpers ──────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str, limit: int = 60) -> str:
    slug = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return (slug[:limit] or "lecture").strip("-")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _output_dir() -> str:
    path = os.path.abspath(OUTPUT_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def _write_output(name: str, content: str) -> str:
    path = os.path.join(_output_dir(), name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def _public_url(relpath: str) -> str:
    """Map a generated lecture file to its public download URL."""
    return f"{PUBLIC_BASE}/chessintel/{relpath}"


@mcp.custom_route("/files/{name}", methods=["GET"])
async def _serve_output_file(request: Request) -> Response:
    """Serve a generated lecture file (PDF/HTML/MD) over HTTP.

    The name is strictly basename-scoped to OUTPUT_DIR so nothing outside the
    output directory can ever be read.
    """
    name = request.path_params.get("name", "")
    safe = os.path.basename(name)
    if not safe or safe != name or safe.startswith("."):
        return PlainTextResponse("not found", status_code=404)
    path = os.path.join(_output_dir(), safe)
    if not os.path.isfile(path):
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(path, filename=safe)


WRITING_INSTRUCTIONS = (
    "Write a plain-language coach report for a young chess player using ONLY the "
    "verified data returned above. Explain every mistake the way a coach would to "
    "a kid: what they were probably trying to do, why it backfired, and the one "
    "concrete rule to remember. No engine jargon — say 'loses a piece', not "
    "'loses 2.3 pawns of material'. Include: (1) one thing they do well with "
    "evidence, (2) the single biggest recurring pattern, (3) the clearest turning "
    "point with its move number and SAN, (4) one concrete drill that targets the "
    "pattern. Do not invent moves, evaluations, results, or ratings."
)



# ── Analysis plumbing ───────────────────────────────────────────────────────

def _analyze_one(pgn: str, depth: int, **extra: Any) -> Dict[str, Any]:
    """One engine pass over one PGN, through the shared serialised engine."""
    payload: Dict[str, Any] = {"pgn": pgn}
    payload.update(extra)
    return _with_engine(lambda engine: core.analyze_game(payload, engine, depth))


def _aggregate(analyses: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Cross-game view: habits, phase distribution, results and tactic totals."""
    results = {"white_wins": 0, "black_wins": 0, "draws": 0, "unknown": 0}
    phases = {"opening": 0, "middlegame": 0, "endgame": 0}
    tactics: Dict[str, int] = {}

    for row in analyses:
        result = (row.get("result") or "").strip()
        if result.startswith("1-0"):
            results["white_wins"] += 1
        elif result.startswith("0-1"):
            results["black_wins"] += 1
        elif result.startswith("1/2"):
            results["draws"] += 1
        else:
            results["unknown"] += 1

        for phase, count in (row.get("phase_blunders") or {}).items():
            if phase in phases:
                phases[phase] += int(count or 0)

        for blunder in row.get("blunders") or []:
            motif = blunder.get("threat")
            if motif:
                tactics[motif] = tactics.get(motif, 0) + 1

    return {
        "games_analyzed": len(analyses),
        "habits": core.aggregate_habits(analyses),
        "results": results,
        "phase_errors": phases,
        "tactic_counts": dict(sorted(tactics.items(), key=lambda kv: -kv[1])),
        "total_big_errors": sum(len(row.get("blunders") or []) for row in analyses),
        "average_accuracy": round(
            sum(float(row.get("accuracy") or 0) for row in analyses) / len(analyses)
        )
        if analyses
        else 0,
    }


@mcp.tool()
def get_tool_help() -> str:
    """Explain what each tool does so the agent can choose correctly."""
    return (
        "CHESSINTEL TOOLS\n"
        "\n"
        "FIND A GAME (when the user names players + year/event but gives no PGN)\n"
        "- find_game(query): searches the club library and the chessgames.com master\n"
        "  database by players, year or event — e.g. \"Tal vs Fischer, Zagreb 1959\".\n"
        "  Returns the exact PGN with verified White/Black names. If it returns\n"
        "  found=false, ask the user to paste the PGN or moves; never guess.\n"
        "  NEVER correct the user's player names, colours or the opening — the\n"
        "  White/Black headers in the returned PGN are the only truth. If the\n"
        "  search comes back empty, do not suggest a different game and do not\n"
        "  tell the user to \"double-check the colours\".\n"
        "\n"
        "ANALYSIS (engine-verified facts; never invent numbers)\n"
        "- analyze_pgn(pgn, depth): full analysis of one game — per-move evaluations,\n"
        "  accuracy (CAPS-style), error classes, phase breakdown, clock flags, and\n"
        "  every tactical motif with the engine's own refutation.\n"
        "- analyze_games(pgns, depth): the above for several games plus cross-game habits.\n"
        "- find_patterns(pgns, depth): just the recurring habits and phase/tactic totals.\n"
        "- build_report_context(pgns, depth, kid_name): analysis plus writing\n"
        "  instructions for composing a coach report.\n"
        "- tactics_breakdown(pgn, depth): motif counts only (hanging pieces, forks,\n"
        "  pins, skewers, back-rank mates, traps).\n"
        "\n"
        "ONLINE (public APIs, no credentials)\n"
        "- fetch_chesscom_games / fetch_lichess_games(username, max_games): recent\n"
        "  games as PGN.\n"
        "- fetch_lichess_game(game_id_or_url): fetch ONE game's PGN from a lichess\n"
        "  game ID or URL (use when the user shares a lichess link).\n"
        "- analyze_online_games(username, platform, max_games, depth): fetch + analyze.\n"
        "- player_summary(username): ratings, titles and account age across platforms.\n"
        "- daily_puzzle(): today's Lichess puzzle (position, rating, themes, solution).\n"
        "\n"
        "POSITIONS (single position, no PGN needed)\n"
        "- evaluate_position(fen, depth, multipv): evaluation plus top lines.\n"
        "- best_moves(fen, depth, num): ranked candidate moves.\n"
        "- compare_moves(fen, moves, depth): grade the moves a student is choosing between.\n"
        "\n"
        "LESSONS (the ONLY lesson tool)\n"
        "- njcc_onepager(pgn, level, title?, big_ideas?, things_to_remember?, ...):\n"
        "  THE lesson tool. Returns a 1-page lesson PDF PLUS a homework PDF\n"
        "  (questions to answer). Use it for ANY of: lesson, lecture, study,\n"
        "  one-pager, handout, printable, class sheet, homework, NJCC, or a\n"
        "  request to follow a named teaching format (e.g. Ken Thomas). There is\n"
        "  NO multi-page lesson format — never look for another lesson tool.\n"
        "- export_annotated_pgn(pgn, depth): PGN with [%eval] annotations,\n"
        "  importable into Shredder or ChessBase.\n"
        "\n"
        "LIBRARY & DRILLS (SQLite longitudinal memory)\n"
        "- save_lecture / list_lectures, add_drills_from_lecture, due_drills,\n"
        "  grade_drill, store_games, progress_report, save_report.\n"
        "\n"
        "META\n"
        "- health_check(): engine and configuration status.\n"
        "\n"
        "Rules: these tools are the ONLY source of truth. Quote their evaluations\n"
        "exactly. Never describe a move, tactic or result they did not report, and\n"
        "never claim an opening that the analysis reported as unidentified. Never\n"
        "use web_search to find a game — use find_game. Never correct a player's\n"
        "name or colour from memory — trust find_game's PGN headers. Every\n"
        "lesson, lecture, study or handout request uses njcc_onepager only — the\n"
        "club produces one-page lessons, nothing else."
    )


@mcp.tool()
def analyze_pgn(pgn: str, depth: int = 14) -> dict[str, Any]:
    """Full Stockfish analysis of one PGN.

    Returns per-move evaluations, a CAPS-style accuracy score per colour, error
    classifications, a phase breakdown, clock/reflex flags, and every tactical
    motif together with the engine's own refutation line. This is the ground
    truth every lecture tool builds on.
    """
    result = _analyze_one(pgn, depth)
    node, headers, uci_moves = lectures_mod._load_game(pgn)
    if node is not None:
        result["opening"] = openings.identify(headers, uci_moves)
    return result


@mcp.tool()
def analyze_games(pgns: list[str], depth: int = 14) -> dict[str, Any]:
    """Analyze several PGNs, then add the cross-game habit and tactic totals."""
    analyses = [_analyze_one(pgn, depth) for pgn in pgns]
    return {"per_game": analyses, "patterns": _aggregate(analyses)}


@mcp.tool()
def fetch_chesscom_games(
    username: str, max_games: int = 20, last_months: int = 3
) -> list[dict[str, Any]]:
    """Pull recent games from chess.com by username (public API, no credentials)."""
    return fetchers.fetch_chesscom_games(username, max_games, last_months)


@mcp.tool()
def fetch_lichess_games(username: str, max_games: int = 20) -> list[dict[str, Any]]:
    """Pull recent games from lichess by username (public API, no credentials)."""
    return fetchers.fetch_lichess_games(username, max_games)


@mcp.tool()
def fetch_lichess_game(game_id_or_url: str) -> dict[str, Any]:
    """Fetch one game's PGN from a lichess game ID or URL.

    Use this when the user shares a lichess game link (for example
    https://lichess.org/Ab12Cd34) instead of pasting the PGN. Returns the PGN
    with its headers, so the player names, result and moves are exact.
    """
    return fetchers.fetch_lichess_game(game_id_or_url)


def _fetch_daily_puzzle() -> dict[str, Any]:
    """Fetch today's Lichess puzzle. Returns ok/error plus the puzzle data."""
    import urllib.request

    url = "https://lichess.org/api/puzzle/daily"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "chessintel/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    puzzle = data.get("puzzle") or {}
    game = data.get("game") or {}
    return {
        "ok": True,
        "id": puzzle.get("id"),
        "rating": puzzle.get("rating"),
        "themes": puzzle.get("themes") or [],
        "fen": data.get("fen") or game.get("fen"),
        "solution_uci": puzzle.get("solution") or [],
        "pgn": game.get("pgn"),
    }


@mcp.tool()
def daily_puzzle() -> dict[str, Any]:
    """Today's Lichess puzzle — position, rating, themes and solution.

    Useful when the user wants a fresh tactical position, or as extra credit
    on a homework sheet (njcc_onepager adds it automatically).
    """
    return _fetch_daily_puzzle()


@mcp.tool()
def find_game(query: str) -> dict[str, Any]:
    """Find a game's PGN from the players and year (or event).

    Examples: "Tal vs Fischer, Zagreb 1959" or "Kasparov vs Karpov, 1987".
    Searches the club's curated library first, then the chessgames.com master
    database. Returns the best match's PGN plus its verified White/Black names,
    result and opening. When nothing matches it returns ``found: false`` — in
    that case ask the user to paste the PGN or the moves rather than
    substituting a different game.
    """
    return master_games.find_game(query)


def _to_core_game(fetched: Dict[str, Any], platform: str) -> Dict[str, Any]:
    """Map a fetcher record onto the shape the coach core expects."""
    game = {
        "pgn": fetched.get("pgn") or "",
        "source": fetched.get("platform") or platform,
        "external_id": None,
        "game_url": fetched.get("game_url"),
        "white": fetched.get("white"),
        "black": fetched.get("black"),
        "result": fetched.get("result"),
        "played_at": fetched.get("played_at"),
    }
    # The public APIs do not always hand back names/results separately for
    # every shape, so fall back to the PGN headers rather than leaving blanks.
    if not game["white"] or not game["black"] or not game["result"]:
        node, headers, _ = lectures_mod._load_game(game["pgn"])
        if node is not None:
            game["white"] = game["white"] or headers.get("White")
            game["black"] = game["black"] or headers.get("Black")
            game["result"] = game["result"] or headers.get("Result")
            game["played_at"] = game["played_at"] or headers.get("Date")
    return game


@mcp.tool()
def analyze_online_games(
    username: str, platform: str = "lichess", max_games: int = 5, depth: int = 14
) -> dict[str, Any]:
    """Fetch a player's recent online games and analyze them in one call.

    ``platform`` must be 'lichess' or 'chess.com'. Keep ``max_games`` small —
    each game runs a full engine pass, so five games at depth 14 is roughly
    a minute of work.
    """
    key = (platform or "").lower().replace("chess.com", "chesscom")
    if key == "lichess":
        fetched = fetchers.fetch_lichess_games(username, max_games)
    elif key == "chesscom":
        fetched = fetchers.fetch_chesscom_games(username, max_games)
    else:
        raise ValueError("platform must be 'lichess' or 'chess.com'")

    if not fetched:
        return {
            "username": username,
            "platform": platform,
            "games_fetched": 0,
            "note": "No games returned — check the username spelling.",
        }

    analyses = [
        _with_engine(
            lambda engine, g=_to_core_game(item, key): core.analyze_game(g, engine, depth)
        )
        for item in fetched
    ]

    return {
        "username": username,
        "platform": platform,
        "games_fetched": len(analyses),
        "per_game": analyses,
        "patterns": _aggregate(analyses),
    }


@mcp.tool()
def find_patterns(pgns: list[str], depth: int = 14) -> dict[str, Any]:
    """Find the recurring habits across games: blunder clusters, phases, time pressure."""
    return _aggregate([_analyze_one(pgn, depth) for pgn in pgns])


@mcp.tool()
def build_report_context(
    pgns: list[str], depth: int = 14, kid_name: str = ""
) -> dict[str, Any]:
    """Assemble a self-contained, engine-verified context block for writing a report."""
    analyses = [_analyze_one(pgn, depth) for pgn in pgns]
    return {
        "kid_name": kid_name,
        "per_game": analyses,
        "patterns": _aggregate(analyses),
        "writing_instructions": WRITING_INSTRUCTIONS,
    }


@mcp.tool()
def tactics_breakdown(pgn: str, depth: int = 14) -> dict[str, Any]:
    """Count the tactical motifs in a game, with the refutation for each.

    Motifs are only ever reported when the engine's own best reply proves them,
    so this never claims a fork or a pin the position does not actually contain.
    """
    result = _analyze_one(pgn, depth)
    blunders = result.get("blunders") or []
    motifs: Dict[str, List[Dict[str, Any]]] = {}
    for blunder in blunders:
        motif = blunder.get("threat")
        if not motif:
            continue
        motifs.setdefault(motif, []).append(
            {
                "ply": blunder.get("ply"),
                "move_number": core._move_number(blunder.get("ply")),
                "color": blunder.get("color"),
                "played": blunder.get("san"),
                "better": blunder.get("best"),
                "cp_loss": blunder.get("cp_loss"),
                "detail": blunder.get("threat_detail"),
                "refutation_line": blunder.get("line"),
            }
        )

    return {
        "counts": dict(sorted(((k, len(v)) for k, v in motifs.items()), key=lambda kv: -kv[1])),
        "total_big_errors": len(blunders),
        "motifs": motifs,
        "note": (
            "Large errors without a named motif are positional swings rather than "
            "tactical losses; they are counted in total_big_errors."
        ),
    }


# ── Single-position tools ───────────────────────────────────────────────────

@mcp.tool()
def evaluate_position(fen: str, depth: int = 16, multipv: int = 3) -> dict[str, Any]:
    """Evaluate one position: the score plus Stockfish's top ``multipv`` lines.

    ``score_cp`` is centipawns from the side-to-move's perspective; a mate shows
    as ``score_mate`` instead.
    """
    def worker(engine: chess.engine.SimpleEngine) -> Dict[str, Any]:
        board = chess.Board(fen)
        turn = board.turn
        lines = engine.analyse(
            board,
            chess.engine.Limit(depth=depth),
            multipv=max(1, min(multipv, 5)),
        )
        if not isinstance(lines, list):
            lines = [lines]

        parsed: List[Dict[str, Any]] = []
        for info in lines:
            pv = info.get("pv") or []
            if not pv:
                continue
            score = info.get("score")
            cp = core._score_to_cp(score, turn)
            san_line: List[str] = []
            probe = board.copy()
            for move in pv[:6]:
                try:
                    san_line.append(probe.san(move))
                except Exception:
                    san_line.append(move.uci())
                probe.push(move)
            parsed.append(
                {
                    "rank": len(parsed) + 1,
                    "move_san": san_line[0] if san_line else None,
                    "move_uci": pv[0].uci(),
                    "score_cp": cp if abs(cp) < 9000 else None,
                    "score_mate": score.pov(turn).mate() if score.is_mate() else None,
                    "line_san": san_line,
                }
            )

        return {
            "fen": fen,
            "side_to_move": "white" if turn == chess.WHITE else "black",
            "depth": depth,
            "best_move_san": parsed[0]["move_san"] if parsed else None,
            "score_cp": parsed[0]["score_cp"] if parsed else None,
            "score_mate": parsed[0]["score_mate"] if parsed else None,
            "lines": parsed,
        }

    return _with_engine(worker)


@mcp.tool()
def store_games(
    kid_name: str, pgns: list[str], platform: str = "manual"
) -> dict[str, Any]:
    """Store games for a player in the local SQLite database (longitudinal memory)."""
    kid_id = storage.ensure_kid(kid_name)
    stored = 0
    errors: list[str] = []
    for pgn in pgns:
        try:
            node, headers, _ = lectures_mod._load_game(pgn)
            if node is None:
                raise ValueError("could not parse PGN")
            storage.store_game(
                kid_id,
                platform,
                pgn,
                result=headers.get("Result", ""),
            )
            stored += 1
        except Exception as exc:  # one bad PGN must not kill the batch
            errors.append(str(exc))
    return {"kid": kid_name, "stored": stored, "errors": errors}


@mcp.tool()
def progress_report(kid_name: str) -> dict[str, Any]:
    """The longitudinal summary for a player: stored games, results, past reports."""
    summary = storage.get_progress(kid_name)
    summary["drills"] = storage.drill_stats(storage.ensure_kid(kid_name))
    summary["lectures"] = storage.list_lectures(storage.ensure_kid(kid_name), limit=10)
    return summary


@mcp.tool()
def save_report(kid_name: str, report_text: str) -> dict[str, Any]:
    """Save a finished coach report to the player's history."""
    kid_id = storage.ensure_kid(kid_name)
    report_id = storage.save_report(kid_id, report_text)
    return {"kid": kid_name, "saved": True, "report_id": report_id}


@mcp.tool()
def player_summary(username: str) -> dict[str, Any]:
    """Ratings, titles and account age for a player across chess.com and lichess.

    Public APIs only, no credentials. Read-only context for pitching a lecture at
    the right level — it is not used for any analysis.
    """
    return fetchers.fetch_player(username)


@mcp.tool()
def best_moves(fen: str, depth: int = 14, num: int = 5) -> dict[str, Any]:
    """Ranked candidate moves for a position, best first.

    Useful for building a repertoire or for showing a student the alternatives
    they had. ``cp`` is from the side-to-move's perspective.
    """
    def worker(engine: chess.engine.SimpleEngine) -> Dict[str, Any]:
        board = chess.Board(fen)
        legal = board.legal_moves.count()
        if legal == 0:
            return {"fen": fen, "moves": [], "note": "No legal moves in this position."}
        wanted = max(1, min(num, legal))
        lines = engine.analyse(
            board, chess.engine.Limit(depth=depth), multipv=wanted
        )
        if not isinstance(lines, list):
            lines = [lines]

        moves: List[Dict[str, Any]] = []
        for info in lines:
            pv = info.get("pv") or []
            if not pv:
                continue
            score = info.get("score")
            cp = core._score_to_cp(score, board.turn)
            try:
                san = board.san(pv[0])
            except Exception:
                san = pv[0].uci()
            moves.append(
                {
                    "rank": len(moves) + 1,
                    "san": san,
                    "uci": pv[0].uci(),
                    "cp": cp if abs(cp) < 9000 else None,
                    "mate": score.pov(board.turn).mate() if score.is_mate() else None,
                }
            )
        return {"fen": fen, "depth": depth, "moves": moves}

    return _with_engine(worker)


@mcp.tool()
def compare_moves(
    fen: str, moves: list[str], depth: int = 14
) -> dict[str, Any]:
    """Grade specific moves a student is choosing between.

    ``moves`` are SAN strings (e.g. ["Nf3", "d4", "Bc4"]). Each is scored
    against the engine's best move in that position so the difference is
    explicit rather than a matter of opinion.
    """
    def worker(engine: chess.engine.SimpleEngine) -> Dict[str, Any]:
        board = chess.Board(fen)
        turn = board.turn

        best_info = engine.analyse(board, chess.engine.Limit(depth=depth))
        best_cp = core._score_to_cp(best_info["score"], turn)
        best_pv = best_info.get("pv") or []
        try:
            best_san = board.san(best_pv[0]) if best_pv else None
        except Exception:
            best_san = best_pv[0].uci() if best_pv else None

        graded: List[Dict[str, Any]] = []
        for raw in moves:
            try:
                move = board.parse_san(raw)
            except Exception:
                try:
                    move = chess.Move.from_uci(raw)
                except Exception:
                    graded.append({"input": raw, "error": "illegal or unparseable"})
                    continue

            probe = board.copy()
            probe.push(move)
            if probe.is_checkmate():
                cp_after = 10000
            else:
                info = engine.analyse(probe, chess.engine.Limit(depth=depth))
                cp_after = core._score_to_cp(info["score"], turn)

            loss = best_cp - cp_after
            graded.append(
                {
                    "input": raw,
                    "san": board.san(move),
                    "uci": move.uci(),
                    "cp_after": cp_after if abs(cp_after) < 9000 else None,
                    "cp_loss_vs_best": loss if abs(best_cp) < 9000 else None,
                    "class": core._classify_loss(
                        max(
                            0.0,
                            core._winning_chances(best_cp)
                            - core._winning_chances(cp_after),
                        )
                    ),
                }
            )

        graded.sort(
            key=lambda row: (
                row.get("cp_loss_vs_best") is None,
                -(row.get("cp_loss_vs_best") or 0),
            )
        )
        return {
            "fen": fen,
            "depth": depth,
            "best_move": best_san,
            "best_cp": best_cp if abs(best_cp) < 9000 else None,
            "graded": graded,
        }

    return _with_engine(worker)


# ── Lecture tools ───────────────────────────────────────────────────────────

def _render_and_write(
    lecture: Dict[str, Any],
    formats: List[str],
    filename: str = "",
) -> Dict[str, Any]:
    """Render the requested formats to disk. Returns paths, sizes and notes."""
    base = filename or f"{_slug(lecture.get('meta', {}).get('title', 'lecture'))}-{_stamp()}"
    base = base.rsplit(".", 1)[0]
    formats = [f.strip().lower() for f in formats if f and f.strip()]

    files: Dict[str, str] = {}
    sizes: Dict[str, int] = {}
    notes: List[str] = []

    if "md" in formats or "markdown" in formats:
        path = _write_output(f"{base}.md", renderers.to_markdown(lecture))
        files["md"] = path
        sizes["md"] = os.path.getsize(path)

    if "html" in formats:
        path = _write_output(f"{base}.html", renderers.to_html(lecture))
        files["html"] = path
        sizes["html"] = os.path.getsize(path)

    if "pdf" in formats:
        target = os.path.join(_output_dir(), f"{base}.pdf")
        written = renderers.to_pdf(lecture, target)
        if written:
            files["pdf"] = written
            sizes["pdf"] = os.path.getsize(written)
        else:
            notes.append(
                "PDF rendering is unavailable (WeasyPrint or a native library is "
                "missing). The Markdown and HTML versions are complete."
            )

    if "pgn" in formats:
        path = _write_output(f"{base}.pgn", lecture.get("annotated_pgn") or "")
        files["pgn"] = path
        sizes["pgn"] = os.path.getsize(path)

    if "worksheet" in formats or "txt" in formats:
        path = _write_output(f"{base}-worksheet.md", renderers.to_worksheet(lecture))
        files["worksheet"] = path
        sizes["worksheet"] = os.path.getsize(path)

    urls = {fmt: _public_url(os.path.basename(path)) for fmt, path in files.items()}
    return {
        "filename_base": base,
        "files": files,
        "bytes": sizes,
        "notes": notes,
        "urls": urls,
    }


def build_lecture(
    pgn: str,
    player: str = "",
    title: str = "",
    subtitle: str = "",
    level: str = "",
    rating: int = 0,
    focus: str = "",
    depth: int = 0,
    max_moments: int = 0,
    include_quiz: bool = True,
    fetch_opening_stats: bool = False,
) -> dict[str, Any]:
    """Build a structured lecture model from a PGN — the single source of truth.

    Computes the engine pass, selects the most instructive moments, writes a
    deterministic four-beat explanation for each, and assembles facts, tactics,
    drills, a quiz, homework and an annotated PGN. Nothing is invented: fields
    that could not be computed are left empty.

    ``level`` is one of beginner / improver / club / expert (or pass ``rating``
    and it is derived). ``player`` names the student so the lecture focuses on
    their moves. ``focus`` is a free-text theme, e.g. "tactics" or "endgame".
    """
    lecture = lectures_mod.build_lecture(
        pgn,
        title=title or None,
        subtitle=subtitle or None,
        player=player or None,
        level=level or None,
        rating=rating or None,
        focus=focus or None,
        depth=depth or None,
        max_moments=max_moments or None,
        include_quiz=include_quiz,
        fetch_opening_stats=fetch_opening_stats,
    )
    return lecture


def render_lecture(
    lecture: dict,
    formats: Optional[list[str]] = None,
    filename: str = "",
) -> dict[str, Any]:
    """Render a built lecture to Markdown / HTML / PDF / annotated PGN / worksheet.

    ``formats`` defaults to ``["md", "html", "pdf"]``. Files are written to the
    service output directory and their absolute paths returned. The Markdown is
    also returned inline so the agent can quote from it directly.
    """
    requested = formats or ["md", "html", "pdf"]
    rendered = _render_and_write(lecture, requested, filename)
    return {
        "title": (lecture.get("meta") or {}).get("title"),
        **rendered,
        "markdown": renderers.to_markdown(lecture),
    }


def lecture_from_pgn(
    pgn: str,
    player: str = "",
    title: str = "",
    level: str = "",
    rating: int = 0,
    focus: str = "",
    depth: int = 0,
    formats: Optional[list[str]] = None,
    kid_name: str = "",
    include_model: bool = False,
) -> dict[str, Any]:
    """Internal helper — NOT exposed to the agent (no @mcp.tool decorator).

    The club only wants one-page lessons, so any lesson/lecture/study request
    must go through ``njcc_onepager``. This multi-page report renderer is kept
    for potential internal use only.
    """
    lecture = lectures_mod.build_lecture(
        pgn,
        title=title or None,
        player=player or None,
        level=level or None,
        rating=rating or None,
        focus=focus or None,
        depth=depth or None,
    )

    rendered = _render_and_write(lecture, formats or ["md", "html", "pdf"])

    saved: Dict[str, Any] = {}
    if kid_name:
        kid_id = storage.ensure_kid(kid_name)
        lecture_id = storage.save_lecture(
            kid_id,
            lecture["meta"]["title"],
            json.dumps(lecture),
            source=lecture["meta"].get("source") or "",
            level=lecture["meta"].get("level") or "",
            files_json=json.dumps(rendered["files"]),
        )
        cards = [
            {
                "fen": puzzle["fen"],
                "answer_san": puzzle["answer_san"],
                "answer_line": puzzle.get("answer_line") or "",
                "theme": puzzle.get("theme") or "",
                "origin": f"lecture:{lecture_id}",
            }
            for puzzle in lecture.get("quiz") or []
        ]
        saved = {
            "lecture_id": lecture_id,
            "drills": storage.add_drill_cards(kid_id, cards) if cards else {"added": 0},
        }

    result: Dict[str, Any] = {
        "title": lecture["meta"]["title"],
        "subtitle": lecture["meta"].get("subtitle"),
        "level": lecture["meta"].get("level_label"),
        **rendered,
        "pdf_url": rendered.get("urls", {}).get("pdf"),
        "html_url": rendered.get("urls", {}).get("html"),
        "facts": lecture.get("facts") or [],
        "moments": len(lecture.get("moments") or []),
        "quiz_positions": len(lecture.get("quiz") or []),
        "writing_instructions": lecture.get("writing_instructions"),
        "markdown": renderers.to_markdown(lecture),
        "library": saved or None,
    }
    if include_model:
        result["lecture"] = lecture
    return result


@mcp.tool()
def njcc_onepager(
    pgn: str,
    level: str = "beginner",
    title: str = "",
    key_moment: str = "",
    big_ideas: Optional[list[str]] = None,
    things_to_remember: Optional[list[str]] = None,
    chess_words: Optional[list[dict]] = None,
    include_homework: bool = True,
) -> dict[str, Any]:
    """Build a one-page NJCC club lesson (plus a homework sheet) from a PGN.

    Use this when the teacher wants a printable single-sheet lesson for a class
    (New Jersey Chess Club format: final position, key moment, big ideas, the
    complete game, three things to remember, and a chess-words glossary — all
    on one A4 page).

    Player names, result, opening, accuracy and evaluations come from the PGN
    headers and Stockfish, so they are never invented. The optional prose
    fields (``title``, ``key_moment``, ``big_ideas``, ``things_to_remember``)
    personalise the wording; leave them empty for automatic plain-language text.

    The chess-words glossary is generated automatically from the game's verified
    motifs. You MAY pass ``chess_words`` as a list of {"word", "meaning"} pairs
    (e.g. for a named teaching format such as Ken Thomas) — but entries missing a
    meaning are replaced by the automatic verified definitions, so the column is
    never blank.
    """
    from . import njcc

    lecture = lectures_mod.build_lecture(pgn, title=title or None, level=level or None)

    prose: Dict[str, Any] = {}
    if title:
        prose["title"] = title
    if key_moment:
        prose["key_moment"] = key_moment
    if big_ideas:
        prose["big_ideas"] = big_ideas
    if things_to_remember:
        prose["things_to_remember"] = things_to_remember
    if chess_words:
        prose["chess_words"] = chess_words

    base = f"{_slug(lecture['meta']['title'])}-{_stamp()}"
    out_dir = _output_dir()

    lesson_html = njcc.to_onepager_html(lecture, prose)
    lesson_html_path = _write_output(f"{base}-onepager.html", lesson_html)
    lesson_pdf_path = os.path.join(out_dir, f"{base}-onepager.pdf")
    lesson_pdf = njcc.html_to_pdf(lesson_html, lesson_pdf_path)

    result: Dict[str, Any] = {
        "title": lecture["meta"]["title"],
        "white": lecture["game"].get("white"),
        "black": lecture["game"].get("black"),
        "result": lecture["game"].get("result"),
        "opening": (lecture["game"].get("opening") or {}).get("name"),
        "eco": (lecture["game"].get("opening") or {}).get("eco"),
        "accuracy": lecture["summary"]["accuracy"],
        "lesson_html_url": _public_url(os.path.basename(lesson_html_path)),
        "lesson_pdf_url": _public_url(os.path.basename(lesson_pdf)) if lesson_pdf else None,
        "verified_facts": lecture.get("facts") or [],
    }

    if include_homework:
        puzzle = None
        try:
            dp = _fetch_daily_puzzle()
            if dp.get("ok") and dp.get("fen"):
                first = ""
                try:
                    sol = dp.get("solution_uci") or []
                    if sol:
                        first = chess.Board(dp["fen"]).san(chess.Move.from_uci(sol[0]))
                except Exception:
                    first = ""
                puzzle = {
                    "fen": dp["fen"],
                    "rating": dp.get("rating"),
                    "themes": dp.get("themes"),
                    "first_move_san": first,
                }
        except Exception:
            puzzle = None
        hw_html = njcc.to_homework_html(lecture, puzzle=puzzle)
        hw_html_path = _write_output(f"{base}-homework.html", hw_html)
        hw_pdf_path = os.path.join(out_dir, f"{base}-homework.pdf")
        hw_pdf = njcc.html_to_pdf(hw_html, hw_pdf_path)
        result["homework_html_url"] = _public_url(os.path.basename(hw_html_path))
        result["homework_pdf_url"] = _public_url(os.path.basename(hw_pdf)) if hw_pdf else None

    return result


@mcp.tool()
def export_annotated_pgn(pgn: str, depth: int = 14, filename: str = "") -> dict[str, Any]:
    """Annotate a PGN with Stockfish ``[%eval]`` comments and save it.

    The result opens in Shredder, ChessBase and any other GUI that understands
    ``%eval`` annotations, with the tactical reason for each big error written
    into the move comment.
    """
    analysis = _analyze_one(pgn, depth)
    annotated = lectures_mod._build_annotated_pgn(pgn, analysis)
    base = filename or f"{_slug('annotated')}-{_stamp()}"
    path = _write_output(f"{base.rsplit('.', 1)[0]}.pgn", annotated)
    return {
        "file": path,
        "bytes": os.path.getsize(path),
        "big_errors": len(analysis.get("blunders") or []),
        "accuracy": {
            "white": analysis.get("accuracy_white"),
            "black": analysis.get("accuracy_black"),
        },
        "preview": annotated[:1200],
    }


def make_worksheet(pgn: str, player: str = "", filename: str = "") -> dict[str, Any]:
    """Internal helper — NOT exposed to the agent.

    ``njcc_onepager`` already returns the homework sheet, so this standalone
    worksheet is kept for internal use only.
    """
    lecture = lectures_mod.build_lecture(pgn, player=player or None)
    text = renderers.to_worksheet(lecture)
    base = filename or f"{_slug('worksheet')}-{_stamp()}"
    path = _write_output(f"{base.rsplit('.', 1)[0]}.md", text)
    return {
        "file": path,
        "positions": len(lecture.get("quiz") or []),
        "worksheet": text,
    }


# ── Library ────────────────────────────────────────────────────────────────

@mcp.tool()
def save_lecture(
    kid_name: str, title: str, lecture_json: str, files: Optional[dict] = None
) -> dict[str, Any]:
    """File a lecture (as its JSON model) in a player's library."""
    kid_id = storage.ensure_kid(kid_name)
    lecture_id = storage.save_lecture(
        kid_id,
        title,
        lecture_json,
        files_json=json.dumps(files) if files else None,
    )
    return {"kid": kid_name, "lecture_id": lecture_id, "saved": True}


@mcp.tool()
def list_lectures(kid_name: str, limit: int = 20) -> dict[str, Any]:
    """List a player's previously filed lectures."""
    kid_id = storage.ensure_kid(kid_name)
    return {"kid": kid_name, "lectures": storage.list_lectures(kid_id, limit)}


# ── Drills (spaced repetition) ─────────────────────────────────────────────

@mcp.tool()
def add_drills_from_lecture(kid_name: str, lecture: dict) -> dict[str, Any]:
    """Turn a lecture's quiz positions into spaced-repetition drill cards."""
    kid_id = storage.ensure_kid(kid_name)
    cards = [
        {
            "fen": puzzle["fen"],
            "answer_san": puzzle["answer_san"],
            "answer_line": puzzle.get("answer_line") or "",
            "theme": puzzle.get("theme") or "",
            "origin": "lecture",
        }
        for puzzle in lecture.get("quiz") or []
    ]
    if not cards:
        return {"kid": kid_name, "added": 0, "note": "That lecture has no quiz positions."}
    return {"kid": kid_name, **storage.add_drill_cards(kid_id, cards)}


@mcp.tool()
def due_drills(kid_name: str, limit: int = 20) -> dict[str, Any]:
    """The drill cards a player should practise now.

    Ask the student for the best move in each position, then report how they did
    with ``grade_drill``. Never reveal the answer before they attempt it.
    """
    kid_id = storage.ensure_kid(kid_name)
    return {
        "kid": kid_name,
        "stats": storage.drill_stats(kid_id),
        "cards": storage.due_drill_cards(kid_id, limit),
    }


@mcp.tool()
def grade_drill(card_id: int, grade: str) -> dict[str, Any]:
    """Record how a student did on a drill card and reschedule it.

    ``grade`` is one of: again (wrong), hard, good, easy. The card moves further
    out the better they did, which is what makes the practice stick.
    """
    return storage.grade_drill_card(card_id, grade)


# ── Meta ───────────────────────────────────────────────────────────────────

@mcp.tool()
def health_check() -> dict[str, Any]:
    """Report engine, database and configuration status."""
    engine_name = None
    engine_error = None
    try:
        engine_name = _with_engine(lambda engine: engine.id.get("name"))
    except Exception as exc:
        engine_error = str(exc)

    return {
        "status": "ok" if engine_name else "degraded",
        "stockfish": {
            "path": STOCKFISH_PATH,
            "name": engine_name,
            "threads": STOCKFISH_THREADS,
            "hash_mb": STOCKFISH_HASH_MB,
            "error": engine_error,
        },
        "default_depth": DEFAULT_DEPTH,
        "database": storage.DB_PATH,
        "output_dir": os.path.abspath(OUTPUT_DIR),
        "transport": TRANSPORT,
        "server": mcp.name,
    }


# ── Entry point ────────────────────────────────────────────────────────────

def _http_transport_name() -> str:
    """FastMCP renamed the streamable-HTTP transport to 'http' in v3."""
    override = os.environ.get("CHESSINTEL_TRANSPORT_NAME")
    if override:
        return override
    try:
        import fastmcp

        major = int(str(getattr(fastmcp, "__version__", "2")).split(".")[0])
    except Exception:
        major = 2
    return "http" if major >= 3 else "streamable-http"


def main() -> None:
    """Start the server on the transport chosen by ``CHESSINTEL_TRANSPORT``."""
    if TRANSPORT in ("http", "streamable-http", "streamable_http", "sse"):
        name = _http_transport_name()
        print(
            f"chessintel MCP listening on http://{HOST}:{PORT}/mcp "
            f"(transport={name})",
            flush=True,
        )
        mcp.run(transport=name, host=HOST, port=PORT, path="/mcp")
        return

    mcp.run()  # stdio


if __name__ == "__main__":
    main()
