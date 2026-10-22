"""Engine analysis core — Stockfish + python-chess, no MCP dependencies.

Every function here is a plain, testable function so the same core can later
be wrapped in a FastAPI website backend without touching the logic.
"""

from __future__ import annotations

import io
import os
import re
import shutil
from typing import Any, Optional

import chess
import chess.engine
import chess.pgn

def _resolve_stockfish() -> str:
    """Resolve the Stockfish binary with sane fallbacks.

    systemd supplies STOCKFISH_PATH via EnvironmentFile, but manual runs
    (debugging, tests) should still find /opt/chessintel/bin/stockfish.
    """
    for candidate in (
        os.environ.get("STOCKFISH_PATH"),
        "/opt/chessintel/bin/stockfish",
        shutil.which("stockfish"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    return os.environ.get("STOCKFISH_PATH") or "stockfish"


STOCKFISH_PATH = _resolve_stockfish()
BLUNDER_SWING_CP = 200        # ~2 pawns: "big error" heuristic
TIME_PRESSURE_SECONDS = 30    # clock below this = under pressure


def _to_cp(ps: chess.engine.PovScore) -> float:
    """Normalise an engine score to centipawns from White's perspective."""
    s = ps.white
    if s.is_mate():
        return 100000.0 if s.mate() > 0 else -100000.0
    return float(s.score() or 0.0)


def _clock_seconds(comment: str) -> Optional[int]:
    """Parse a '[%clk 0:05:02]' clock comment into seconds."""
    m = re.search(r"\[%clk\s+(\d+):(\d{1,2}):(\d{1,2})", comment or "")
    if not m:
        return None
    h, mi, s = (int(x) for x in m.groups())
    return h * 3600 + mi * 60 + s


def parse_pgn(pgn: str) -> chess.pgn.Game:
    """Parse a PGN string into a python-chess Game."""
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None:
        raise ValueError(
            "Could not parse PGN — make sure it starts with '[Event' and has a move list."
        )
    return game


def analyze_game(game: chess.pgn.Game, depth: int = 18) -> dict[str, Any]:
    """Run Stockfish over a parsed game.

    Returns move-by-move evaluations (from White's perspective), big-swing
    flags, the single turning point, and time-pressure context when the PGN
    contains clock comments (chess.com and lichess both include them).
    """
    headers = dict(game.headers)
    moves = list(game.mainline_moves())
    rows: list[dict[str, Any]] = []

    try:
        engine = chess.engine.SimpleEngine.popen_uci(STOCKFISH_PATH)
    except (FileNotFoundError, OSError) as exc:
        raise ValueError(
            f"Stockfish not found at STOCKFISH_PATH={STOCKFISH_PATH!r}. "
            "Install it and set the env var (see README)."
        ) from exc

    with engine:
        engine.configure({"Threads": int(os.environ.get("STOCKFISH_THREADS", "1"))})
        board = game.board()
        info = engine.analyse(board, chess.engine.Limit(depth=depth))
        prev = _to_cp(info["score"])

        node = game
        for i, move in enumerate(moves):
            node = node.next()
            san = board.san(move)
            comment = node.comment or ""
            clk = _clock_seconds(comment)
            board.push(move)
            info = engine.analyse(board, chess.engine.Limit(depth=depth))
            cur = _to_cp(info["score"])
            swing = abs(cur - prev)

            rows.append({
                "ply": i + 1,
                "move_number": i // 2 + 1,
                "side": "white" if i % 2 == 0 else "black",
                "san": san,
                "uci": move.uci(),
                "eval_before_cp": round(prev, 1),
                "eval_after_cp": round(cur, 1),
                "swing_cp": round(swing, 1),
                "big_swing": swing >= BLUNDER_SWING_CP,
                "clock_seconds_after": clk,
                "time_pressure": clk is not None and clk < TIME_PRESSURE_SECONDS,
            })
            prev = cur

    big = [r for r in rows if r["big_swing"]]
    turning_point = max(big, key=lambda r: r["swing_cp"]) if big else None

    return {
        "game_headers": headers,
        "result": board.result(),
        "max_swing_cp": round(max((r["swing_cp"] for r in rows), default=0.0), 1),
        "turning_point": turning_point,
        "summary": {
            "total_moves": len(rows),
            "big_swings": len(big),
            "big_swings_by_side": {
                "white": sum(1 for r in big if r["side"] == "white"),
                "black": sum(1 for r in big if r["side"] == "black"),
            },
            "time_pressure_swings": sum(1 for r in big if r.get("time_pressure")),
        },
        "moves": rows,
    }


def find_patterns(analyses: list[dict]) -> dict[str, Any]:
    """Aggregate patterns across analyzed games — the 'longitudinal memory'.

    Returns structured stats plus plain-language top_patterns strings the LLM
    can quote directly.
    """
    n = len(analyses)
    base: dict[str, Any] = {"games_analyzed": n, "top_patterns": [], "stats": {}}
    if n == 0:
        return base

    results: list[str] = []
    openings: dict[str, int] = {}
    all_rows: list[dict] = []
    for a in analyses:
        res = a.get("result") or a.get("game_headers", {}).get("Result", "*")
        results.append(res)
        op = (
            a.get("game_headers", {}).get("Opening")
            or a.get("game_headers", {}).get("ECO")
            or "Unknown"
        )
        openings[op] = openings.get(op, 0) + 1
        all_rows.extend(a.get("moves", []))

    big = [r for r in all_rows if r["big_swing"]]
    errors_by_side = {
        "white": sum(1 for r in big if r["side"] == "white"),
        "black": sum(1 for r in big if r["side"] == "black"),
    }
    phase_swings = {"opening": 0, "middlegame": 0, "endgame": 0}
    for r in big:
        mn = r["move_number"]
        phase = "opening" if mn <= 10 else ("middlegame" if mn <= 30 else "endgame")
        phase_swings[phase] += 1
    pressure = [r for r in big if r.get("time_pressure")]

    patterns: list[str] = []
    if big:
        worst_side = max(errors_by_side, key=errors_by_side.get)
        worst_phase = max(phase_swings, key=phase_swings.get)
        patterns.append(
            f"{len(big)} big evaluation swing(s) across {n} game(s) "
            f"({max(errors_by_side.values())} by {worst_side}); heaviest cluster "
            f"in the {worst_phase}."
        )
    if pressure:
        patterns.append(
            f"{len(pressure)} of {len(big)} big swings happened with under "
            f"{TIME_PRESSURE_SECONDS}s on the clock — time pressure is a factor."
        )
    if openings:
        top_op = max(openings, key=openings.get)
        patterns.append(f"Most common opening: {top_op} ({openings[top_op]}x).")

    base["stats"] = {
        "results": {
            "wins": sum(1 for r in results if r.startswith("1-0")),
            "losses": sum(1 for r in results if r.startswith("0-1")),
            "draws": sum(1 for r in results if r.startswith("1/2")),
        },
        "big_swings_total": len(big),
        "errors_by_side": errors_by_side,
        "phase_distribution": phase_swings,
        "time_pressure_swings": len(pressure),
        "openings": dict(sorted(openings.items(), key=lambda kv: -kv[1])),
    }
    base["top_patterns"] = patterns
    return base
