"""Renderers — one lecture model, four outputs.

Input is always the dict produced by :func:`chessintel.lectures.build_lecture`.
Outputs never recompute anything; they format. That is what guarantees the
Markdown, HTML, PDF and annotated PGN say the same thing.

* ``to_markdown``  — for chat / LLM consumption, and for saving next to a PGN.
* ``to_html``      — a standalone, offline, dependency-free lecture page with
                     inline SVG diagrams and an evaluation graph.
* ``to_pdf``       — the HTML rendered through WeasyPrint with a print sheet.
* ``to_worksheet`` — student version (diagrams + blanks) plus an answer key.

Board diagrams are drawn with ``chess.svg`` (bundled with python-chess), so no
images are fetched and the output works with no network access.
"""

from __future__ import annotations

import html as _html
import os
from typing import Any, Dict, List, Optional, Tuple

import chess
import chess.svg

# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------

ACCENT = "#e2711d"  # club orange
ACCENT_DARK = "#1b1b1b"


def board_svg(
    fen: str,
    size: int = 320,
    *,
    arrows: Optional[List[Tuple[str, str]]] = None,
    last_move: Optional[str] = None,
) -> str:
    """Inline SVG for a position. Returns an error placeholder, never raises."""
    try:
        board = chess.Board(fen)
    except Exception:
        return (
            '<div class="diagram-error">Diagram unavailable '
            f"(invalid FEN: {_html.escape(fen)})</div>"
        )

    parsed_arrows: List[chess.svg.Arrow] = []
    for start, end in arrows or []:
        try:
            parsed_arrows.append(
                chess.svg.Arrow(
                    chess.parse_square(start), chess.parse_square(end), color=ACCENT
                )
            )
        except Exception:
            continue

    lm = None
    if last_move:
        try:
            lm = chess.Move.from_uci(last_move)
        except Exception:
            lm = None

    try:
        return chess.svg.board(
            board,
            size=size,
            arrows=parsed_arrows,
            lastmove=lm,
            colors={"square light": "#f6f1e7", "square dark": "#c9b79c"},
        )
    except Exception:
        return '<div class="diagram-error">Diagram unavailable</div>'


def eval_sparkline(graph: List[Dict[str, Any]], width: int = 640, height: int = 90) -> str:
    """A self-contained SVG evaluation graph (White's perspective, clamped ±6)."""
    if len(graph) < 2:
        return ""

    clamp = 600.0
    points: List[Tuple[float, float]] = []
    for index, row in enumerate(graph):
        try:
            cp = max(-clamp, min(clamp, float(row["cp"])))
        except (KeyError, TypeError, ValueError):
            continue
        x = width * index / max(1, len(graph) - 1)
        y = height / 2 - (cp / clamp) * (height / 2 - 6)
        points.append((x, y))

    if len(points) < 2:
        return ""

    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    area = f"0,{height / 2:.1f} " + line + f" {width},{height / 2:.1f}"

    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" role="img" '
        f'aria-label="Evaluation graph across the game">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#faf7f2"/>'
        f'<polygon points="{area}" fill="{ACCENT}" opacity="0.14"/>'
        f'<line x1="0" y1="{height / 2:.1f}" x2="{width}" y2="{height / 2:.1f}" '
        f'stroke="#c9b79c" stroke-width="1" stroke-dasharray="4 4"/>'
        f'<polyline points="{line}" fill="none" stroke="{ACCENT}" stroke-width="2"/>'
        f"</svg>"
    )


def _esc(text: Any) -> str:
    return _html.escape("" if text is None else str(text))


def _fmt_eval(value: Optional[float]) -> str:
    if value is None:
        return "?"
    if abs(value) >= 9000:
        return "mate" if value > 0 else "-mate"
    return f"{value / 100.0:+.2f}"


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

def to_markdown(lecture: Dict[str, Any]) -> str:
    """A complete, readable lecture in Markdown."""
    meta = lecture.get("meta") or {}
    game = lecture.get("game") or {}
    summary = lecture.get("summary") or {}
    lines: List[str] = []

    lines.append(f"# {meta.get('title', 'Chess Lecture')}")
    if meta.get("subtitle"):
        lines.append(f"*{meta['subtitle']}*")
    lines.append("")
    lines.append(
        f"**Level:** {meta.get('level_label', '—')}  "
        f"| **Engine depth:** {meta.get('depth', '—')}  "
        f"| **Generated:** {meta.get('generated_at', '—')}"
    )
    lines.append("")

    narrative = lecture.get("narrative") or {}
    if narrative.get("intro"):
        lines.append(narrative["intro"])
        lines.append("")

    # --- Game at a glance ---
    lines.append("## The game at a glance")
    lines.append("")
    white = game.get("white") or (game.get("headers") or {}).get("White") or "White"
    black = game.get("black") or (game.get("headers") or {}).get("Black") or "Black"
    lines.append(f"- **White:** {white}")
    lines.append(f"- **Black:** {black}")
    lines.append(f"- **Result:** {game.get('result') or '?'}")
    lines.append(f"- **Moves:** {game.get('move_count') or '?'}")
    opening = game.get("opening") or {}
    if opening.get("name"):
        eco = f" ({opening['eco']})" if opening.get("eco") else ""
        lines.append(f"- **Opening:** {opening['name']}{eco}")
    else:
        lines.append("- **Opening:** not identified")
    acc = summary.get("accuracy") or {}
    lines.append(
        f"- **Accuracy:** White {acc.get('white', '?')}% · "
        f"Black {acc.get('black', '?')}%"
    )
    lines.append(f"- **Average centipawn loss:** {summary.get('acpl', '?')}")
    lines.append("")

    if summary.get("one_liner"):
        lines.append(f"> {summary['one_liner']}")
        lines.append("")

    # --- Learning objectives ---
    lines.append("## What you will learn")
    lines.append("")
    for index, moment in enumerate(lecture.get("moments") or [], start=1):
        motif = moment.get("motif") or "a key decision"
        lines.append(
            f"{index}. Move {moment.get('move_number')} — {moment.get('san')} "
            f"({motif}, {_fmt_eval(moment.get('eval_before'))} → "
            f"{_fmt_eval(moment.get('eval_after'))})"
        )
    if not lecture.get("moments"):
        lines.append(
            "1. The opening plan and how the game's character was set from the start."
        )
    lines.append("")

    # --- Moments ---
    if lecture.get("moments"):
        lines.append("## The moments that decided it")
        lines.append("")
    for index, moment in enumerate(lecture.get("moments") or [], start=1):
        beats = moment.get("beats") or {}
        lines.append(
            f"### Moment {index} — {moment.get('move_number')} "
            f"{moment.get('san')} ({moment.get('classification', 'mistake')})"
        )
        lines.append("")
        lines.append(
            f"*Position before:* `{moment.get('fen_before')}`  "
            "— evaluation " + _fmt_eval(moment.get("eval_before"))
        )
        lines.append("")
        lines.append("**Why it looked good**")
        for signal in beats.get("why_it_looked_good") or []:
            lines.append(f"- {signal}")
        lines.append("")
        lines.append("**Why it failed**")
        lines.append(f"- {beats.get('why_it_failed', '—')}")
        lines.append("")
        if moment.get("best_san"):
            lines.append(
                f"**Better was** `{moment['best_san']}`"
                + (f" — line: {moment['best_line']}" if moment.get("best_line") else "")
            )
            lines.append("")
        lines.append(f"**The concept** — {beats.get('the_concept', '—')}")
        lines.append("")
        lines.append(f"**Remember** — {beats.get('pattern_to_remember', '—')}")
        lines.append("")

    # --- Highlights ---
    highlights = lecture.get("highlights") or []
    if highlights:
        lines.append("## What went right")
        lines.append("")
        lines.append(
            "*Moves the engine also chose — verifiable credit, not a compliment.*"
        )
        lines.append("")
        for index, hi in enumerate(highlights, start=1):
            lines.append(
                f"{index}. **{hi.get('move_number')} {hi.get('san')}** — "
                f"{hi.get('reason')}. Evaluation after the move: "
                f"{_fmt_eval(hi.get('eval_white'))}."
            )
        lines.append("")

    # --- Tactics ---
    tactics = lecture.get("tactics") or {}
    if tactics:
        lines.append("## Tactical summary")
        lines.append("")
        lines.append("| Pattern | Times |")
        lines.append("| --- | --- |")
        for motif, count in sorted(tactics.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {motif} | {count} |")
        lines.append("")

    # --- Drills ---
    if lecture.get("drills"):
        lines.append("## Your drills this week")
        lines.append("")
        for drill in lecture["drills"]:
            freq = drill.get("frequency")
            suffix = f" (seen {freq}x)" if freq else ""
            lines.append(f"- **{drill.get('title')}**{suffix} — {drill.get('description')}")
        lines.append("")

    # --- Quiz ---
    if lecture.get("quiz"):
        lines.append("## Positions to solve")
        lines.append("")
        lines.append("Set each position up and find the best move before checking.")
        lines.append("")
        for index, puzzle in enumerate(lecture["quiz"], start=1):
            lines.append(f"**{index}. {puzzle.get('question')}**")
            lines.append("")
            lines.append(f"`{puzzle.get('fen')}`")
            lines.append("")
            lines.append(
                f"<details><summary>Answer</summary>`{puzzle.get('answer_san')}`"
                f"{' — ' + str(puzzle['answer_line']) if puzzle.get('answer_line') else ''}"
                f"</details>"
            )
            lines.append("")

    # --- Homework ---
    homework = lecture.get("homework") or {}
    if homework.get("tasks"):
        lines.append("## Homework")
        lines.append("")
        for index, task in enumerate(homework["tasks"], start=1):
            lines.append(f"{index}. {task}")
        lines.append("")

    # --- Facts / provenance ---
    if lecture.get("facts"):
        lines.append("## Verified facts")
        lines.append("")
        lines.append(
            "*Every line below comes from the engine analysis — nothing here is inferred.*"
        )
        lines.append("")
        for fact in lecture["facts"]:
            lines.append(f"- {fact}")
        lines.append("")

    if narrative.get("closing"):
        lines.append("## Closing thought")
        lines.append("")
        lines.append(narrative["closing"])
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

_CSS = """
:root { --accent: %(accent)s; --ink: #1b1b1b; --muted: #6b6357; --rule: #e3dbcd; }
* { box-sizing: border-box; }
body { margin: 0; padding: 0; background: #f4f1ea; color: var(--ink);
  font: 16px/1.62 Georgia, "Times New Roman", serif; }
.page { max-width: 900px; margin: 0 auto; background: #fff;
  padding: 44px 52px 64px; box-shadow: 0 2px 18px rgba(0,0,0,.07); }
h1 { font-size: 2.05rem; line-height: 1.18; margin: 0 0 6px; }
h2 { font-size: 1.34rem; margin: 40px 0 12px; padding-bottom: 6px;
  border-bottom: 2px solid var(--rule); }
h3 { font-size: 1.08rem; margin: 26px 0 8px; }
.subtitle { color: var(--muted); font-style: italic; margin: 0 0 18px; }
.kicker { display: inline-block; background: var(--accent); color: #fff;
  font: 700 11px/1 system-ui, sans-serif; letter-spacing: .10em;
  text-transform: uppercase; padding: 6px 11px; border-radius: 3px; margin-bottom: 14px; }
.meta { font: 13px/1.5 system-ui, sans-serif; color: var(--muted);
  border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule);
  padding: 9px 0; margin: 16px 0 22px; }
.lede { background: #fdf7f0; border-left: 4px solid var(--accent);
  padding: 13px 17px; margin: 20px 0; font-size: 1.03rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 12px; margin: 16px 0 8px; }
.stat { background: #faf7f2; border: 1px solid var(--rule); border-radius: 6px;
  padding: 11px 13px; }
.stat .k { font: 11px/1 system-ui, sans-serif; letter-spacing: .07em;
  text-transform: uppercase; color: var(--muted); }
.stat .v { font-size: 1.24rem; font-weight: 700; margin-top: 5px; }
table { border-collapse: collapse; width: 100%%; margin: 14px 0; font-size: .93rem; }
th, td { border: 1px solid var(--rule); padding: 7px 10px; text-align: left; }
th { background: #faf7f2; font: 600 12px/1.4 system-ui, sans-serif;
  text-transform: uppercase; letter-spacing: .05em; }
.moment { border: 1px solid var(--rule); border-radius: 8px; padding: 20px 22px;
  margin: 22px 0; background: #fffdfa; page-break-inside: avoid; }
.moment h3 { margin-top: 0; }
.badge { display: inline-block; background: #f0e6d8; color: #7a5c2e;
  font: 600 11px/1 system-ui, sans-serif; padding: 5px 9px; border-radius: 3px;
  margin-left: 7px; text-transform: uppercase; letter-spacing: .05em; vertical-align: 2px; }
.badge.motif { background: var(--accent); color: #fff; }
.beat { margin: 14px 0 0; }
.beat .label { font: 700 11px/1 system-ui, sans-serif; text-transform: uppercase;
  letter-spacing: .07em; color: var(--accent); display: block; margin-bottom: 5px; }
.beat ul { margin: 0; padding-left: 21px; }
.figrow { display: flex; gap: 18px; flex-wrap: wrap; align-items: flex-start; margin: 14px 0; }
.fig { flex: 0 0 auto; text-align: center; }
.fig svg { display: block; border: 1px solid var(--rule); border-radius: 5px; }
.fig figcaption { font: 12px/1.4 system-ui, sans-serif; color: var(--muted); margin-top: 5px; }
.spark { width: 100%%; height: 90px; display: block; border: 1px solid var(--rule);
  border-radius: 5px; }
code, .fen { font: 12.5px/1.5 "Cascadia Mono", Consolas, monospace;
  background: #f5f2ec; padding: 2px 5px; border-radius: 3px; word-break: break-all; }
details { margin: 8px 0 18px; }
summary { cursor: pointer; font: 600 13px/1 system-ui, sans-serif; color: var(--accent); }
.answer { margin-top: 8px; }
.facts { background: #faf7f2; border: 1px solid var(--rule); border-radius: 6px;
  padding: 15px 20px; font-size: .93rem; }
.facts li { margin: 4px 0; }
.diagram-error { font: 13px/1.4 system-ui, sans-serif; color: #a33; padding: 14px;
  border: 1px dashed #d9b3b3; border-radius: 5px; }
footer { margin-top: 44px; padding-top: 14px; border-top: 1px solid var(--rule);
  font: 12px/1.5 system-ui, sans-serif; color: var(--muted); }
@media print {
  body { background: #fff; font-size: 11.5pt; }
  .page { box-shadow: none; padding: 0; max-width: none; }
  .moment { break-inside: avoid; }
  h2 { break-after: avoid; }
  details { display: block; }
  details > summary { display: none; }
  .answer-note { display: block !important; }
}
""" % {"accent": ACCENT}


def to_html(lecture: Dict[str, Any], *, title_suffix: str = "") -> str:
    """Standalone lecture page — inline CSS/SVG, no external requests."""
    meta = lecture.get("meta") or {}
    game = lecture.get("game") or {}
    summary = lecture.get("summary") or {}
    narrative = lecture.get("narrative") or {}

    parts: List[str] = []
    add = parts.append

    add("<!DOCTYPE html>")
    add('<html lang="en"><head><meta charset="utf-8">')
    add('<meta name="viewport" content="width=device-width, initial-scale=1">')
    add(f"<title>{_esc(meta.get('title'))}{_esc(title_suffix)}</title>")
    add(f"<style>{_CSS}</style>")
    add("</head><body><div class='page'>")

    add("<span class='kicker'>Chess Lecture</span>")
    add(f"<h1>{_esc(meta.get('title'))}</h1>")
    if meta.get("subtitle"):
        add(f"<p class='subtitle'>{_esc(meta['subtitle'])}</p>")

    header_bits = [
        f"Level: {_esc(meta.get('level_label'))}",
        f"Engine depth: {_esc(meta.get('depth'))}",
        f"Generated: {_esc(meta.get('generated_at'))}",
    ]
    add(f"<div class='meta'>{' &nbsp;·&nbsp; '.join(header_bits)}</div>")

    if narrative.get("intro"):
        add(f"<div class='lede'>{_esc(narrative['intro'])}</div>")
    elif summary.get("one_liner"):
        add(f"<div class='lede'>{_esc(summary['one_liner'])}</div>")

    # --- Scoreboard ---
    acc = summary.get("accuracy") or {}
    white = game.get("white") or (game.get("headers") or {}).get("White") or "White"
    black = game.get("black") or (game.get("headers") or {}).get("Black") or "Black"
    opening = game.get("opening") or {}

    add("<h2>The game at a glance</h2>")
    add("<div class='grid'>")
    for label, value in (
        ("Result", game.get("result") or "?"),
        ("Moves", game.get("move_count") or "?"),
        ("White accuracy", f"{acc.get('white', '?')}%"),
        ("Black accuracy", f"{acc.get('black', '?')}%"),
        ("Avg centipawn loss", summary.get("acpl", "?")),
        ("Opening", opening.get("name") or "not identified"),
    ):
        add(
            f"<div class='stat'><div class='k'>{_esc(label)}</div>"
            f"<div class='v'>{_esc(value)}</div></div>"
        )
    add("</div>")
    add(f"<p><strong>{_esc(white)}</strong> (White) vs "
        f"<strong>{_esc(black)}</strong> (Black)"
        + (f" &nbsp;—&nbsp; {_esc(opening.get('eco'))} {_esc(opening.get('name'))}"
           if opening.get("name") else "")
        + "</p>")

    # --- Eval graph ---
    graph_svg = eval_sparkline(lecture.get("eval_graph") or [])
    if graph_svg:
        add("<h2>How the game swung</h2>")
        add(graph_svg)
        add(
            "<p style='font-size:.9rem;color:#6b6357'>Above the line White is better; "
            "below it Black is better. Clamped to ±6 pawns.</p>"
        )

    # --- Moments ---
    moments = lecture.get("moments") or []
    if moments:
        add("<h2>The moments that decided it</h2>")
    for index, moment in enumerate(moments, start=1):
        beats = moment.get("beats") or {}
        add("<div class='moment'>")
        add(
            f"<h3>Moment {index} — {_esc(moment.get('move_number'))} "
            f"{_esc(moment.get('san'))}"
            f"<span class='badge'>{_esc(moment.get('classification', 'mistake'))}</span>"
            + (
                f"<span class='badge motif'>{_esc(moment['motif'])}</span>"
                if moment.get("motif")
                else ""
            )
            + "</h3>"
        )

        arrow = None
        if moment.get("best_san"):
            try:
                board = chess.Board(moment["fen_before"])
                for move in board.legal_moves:
                    if board.san(move) == moment["best_san"]:
                        arrow = [(chess.square_name(move.from_square),
                                  chess.square_name(move.to_square))]
                        break
            except Exception:
                arrow = None

        add("<div class='figrow'>")
        add("<figure class='fig'>" + board_svg(moment.get("fen_before") or "", 300)
            + "<figcaption>Position before the move</figcaption></figure>")
        if moment.get("fen_after"):
            add("<figure class='fig'>" + board_svg(moment.get("fen_after") or "", 300)
                + "<figcaption>After the move</figcaption></figure>")
        add("</div>")

        add("<div class='beat'><span class='label'>Why it looked good</span><ul>")
        for signal in beats.get("why_it_looked_good") or []:
            add(f"<li>{_esc(signal)}</li>")
        add("</ul></div>")

        add(
            "<div class='beat'><span class='label'>Why it failed</span>"
            f"<p>{_esc(beats.get('why_it_failed'))}</p></div>"
        )
        add(
            "<div class='beat'><span class='label'>Evaluation</span><p>"
            f"{_esc(_fmt_eval(moment.get('eval_before')))} → "
            f"{_esc(_fmt_eval(moment.get('eval_after')))} "
            f"({_esc(round(float(moment.get('cp_loss') or 0) / 100.0, 2))} pawns)</p></div>"
        )
        if moment.get("best_san"):
            line = f" — line: {_esc(moment['best_line'])}" if moment.get("best_line") else ""
            add(
                "<div class='beat'><span class='label'>Better was</span><p>"
                f"<code>{_esc(moment['best_san'])}</code>{line}</p></div>"
            )
            if arrow:
                add("<figure class='fig'>"
                    + board_svg(moment["fen_before"], 300, arrows=arrow)
                    + f"<figcaption>Engine's choice: {_esc(moment['best_san'])}"
                    "</figcaption></figure>")
        add(
            "<div class='beat'><span class='label'>The concept</span>"
            f"<p>{_esc(beats.get('the_concept'))}</p></div>"
        )
        add(
            "<div class='beat'><span class='label'>Remember</span>"
            f"<p><strong>{_esc(beats.get('pattern_to_remember'))}</strong></p></div>"
        )
        add("</div>")

    # --- Highlights ---
    highlights = lecture.get("highlights") or []
    if highlights:
        add("<h2>What went right</h2>")
        add(
            "<p style='color:#6b6357;font-size:.93rem'>Moves the engine also chose — "
            "verifiable credit, not a compliment.</p>"
        )
        add("<div class='figrow'>")
        for hi in highlights[:3]:
            add(
                "<figure class='fig'>"
                + board_svg(hi.get("fen") or "", 250)
                + f"<figcaption>{_esc(hi.get('move_number'))} "
                f"{_esc(hi.get('san'))}</figcaption></figure>"
            )
        add("</div>")
        add("<ul>")
        for hi in highlights:
            add(
                f"<li><strong>{_esc(hi.get('move_number'))} "
                f"{_esc(hi.get('san'))}</strong> — {_esc(hi.get('reason'))}. "
                f"Evaluation after the move: {_esc(_fmt_eval(hi.get('eval_white')))}"
                "</li>"
            )
        add("</ul>")

    # --- Tactics table ---
    tactics = lecture.get("tactics") or {}
    if tactics:
        add("<h2>Tactical summary</h2>")
        add("<table><thead><tr><th>Pattern</th><th>Times</th></tr></thead><tbody>")
        for motif, count in sorted(tactics.items(), key=lambda kv: -kv[1]):
            add(f"<tr><td>{_esc(motif)}</td><td>{_esc(count)}</td></tr>")
        add("</tbody></table>")

    # --- Drills ---
    if lecture.get("drills"):
        add("<h2>Your drills this week</h2><table>")
        add("<thead><tr><th>Pattern</th><th>Times</th><th>Drill</th></tr></thead><tbody>")
        for drill in lecture["drills"]:
            add(
                f"<tr><td>{_esc(drill.get('title'))}</td>"
                f"<td>{_esc(drill.get('frequency'))}</td>"
                f"<td>{_esc(drill.get('description'))}</td></tr>"
            )
        add("</tbody></table>")

    # --- Quiz ---
    if lecture.get("quiz"):
        add("<h2>Positions to solve</h2>")
        add("<p>Set each one up and find the best move before checking.</p>")
        for index, puzzle in enumerate(lecture["quiz"], start=1):
            add(
                f"<h3>{index}. {_esc(puzzle.get('question'))} "
                f"<span class='badge'>{_esc(puzzle.get('theme'))}</span></h3>"
            )
            add("<div class='figrow'>")
            add("<figure class='fig'>" + board_svg(puzzle.get("fen") or "", 280)
                + f"<figcaption>Move {_esc(puzzle.get('move_number'))}</figcaption></figure>")
            add("</div>")
            line = f" — {_esc(puzzle['answer_line'])}" if puzzle.get("answer_line") else ""
            add(
                "<details><summary>Show answer</summary>"
                f"<p class='answer'><code>{_esc(puzzle.get('answer_san'))}</code>{line}</p>"
                "</details>"
            )

    # --- Homework ---
    homework = lecture.get("homework") or {}
    if homework.get("tasks"):
        add("<h2>Homework</h2><ol>")
        for task in homework["tasks"]:
            add(f"<li>{_esc(task)}</li>")
        add("</ol>")

    # --- Facts ---
    if lecture.get("facts"):
        add("<h2>Verified facts</h2>")
        add("<div class='facts'><ul>")
        for fact in lecture["facts"]:
            add(f"<li>{_esc(fact)}</li>")
        add("</ul></div>")

    if narrative.get("closing"):
        add(f"<h2>Closing thought</h2><p>{_esc(narrative['closing'])}</p>")

    add("<footer>Generated by chessintel-mcp · "
        "engine analysis by Stockfish · every figure above is computed, not estimated."
        "</footer>")
    add("</div></body></html>")

    return "".join(parts)


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

def to_pdf(lecture: Dict[str, Any], out_path: str) -> Optional[str]:
    """Render the lecture to PDF via WeasyPrint. Returns the path, or None.

    Returns None (rather than raising) when WeasyPrint or its native libraries
    are unavailable, so a caller can still deliver HTML + Markdown.
    """
    try:
        from weasyprint import HTML  # type: ignore[import-not-found]
    except Exception:
        return None

    html_doc = to_html(lecture, title_suffix="")

    # The interactive <details> toggle is meaningless on paper: expand it.
    html_doc = html_doc.replace(
        "<details><summary>Show answer</summary>",
        "<div class='answer'>",
    ).replace("</details>", "</div>")

    try:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        HTML(string=html_doc, base_url=os.path.dirname(os.path.abspath(out_path))).write_pdf(
            out_path
        )
    except Exception:
        return None
    return out_path


# --------------------------------------------------------------------------
# Worksheet (student blanks + answer key)
# --------------------------------------------------------------------------

def to_worksheet(lecture: Dict[str, Any]) -> str:
    """A printable worksheet: positions with blank lines, plus a separate key."""
    meta = lecture.get("meta") or {}
    quiz = lecture.get("quiz") or []
    moments = lecture.get("moments") or []

    lines: List[str] = []
    lines.append(f"# Worksheet — {meta.get('title', 'Chess Lesson')}")
    lines.append("")
    lines.append(f"Name: ______________________________   Date: ______________")
    lines.append("")
    lines.append(
        "Set each position up on a board. Write your move, then the reason for it, "
        "before turning to the answer key."
    )
    lines.append("")

    for index, puzzle in enumerate(quiz, start=1):
        lines.append(f"## Position {index} — {puzzle.get('move_number')}")
        lines.append("")
        lines.append(f"`{puzzle.get('fen')}`")
        lines.append("")
        lines.append(f"{puzzle.get('question')}")
        lines.append("")
        lines.append("Your move: ____________")
        lines.append("")
        lines.append("Why: " + "_" * 58)
        lines.append("")
        lines.append("_" * 66)
        lines.append("")

    if moments:
        lines.append("## Replay tasks")
        lines.append("")
        for index, moment in enumerate(moments, start=1):
            lines.append(
                f"{index}. At move {moment.get('move_number')} the game move was "
                f"{moment.get('san')}. Find a better move and explain it."
            )
            lines.append("")
            lines.append("   Better move: ____________   Because: " + "_" * 34)
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("# Answer key")
    lines.append("")
    for index, puzzle in enumerate(quiz, start=1):
        line = f" — {puzzle['answer_line']}" if puzzle.get("answer_line") else ""
        lines.append(
            f"{index}. `{puzzle.get('answer_san')}`{line}  "
            f"({puzzle.get('theme') or 'best move'})"
        )
    lines.append("")
    for index, moment in enumerate(moments, start=1):
        if moment.get("best_san"):
            lines.append(f"{index}. {moment['best_san']} (instead of {moment.get('san')})")
    lines.append("")

    return "\n".join(lines)
