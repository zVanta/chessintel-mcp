"""Self-test: build and render a lecture from a bundled sample game.

Exercises the whole chain without needing an MCP client — engine analysis,
tactic detection, lecture assembly, and all three renderers.

    set STOCKFISH_PATH=C:\\path\\to\\stockfish.exe   (Windows)
    export STOCKFISH_PATH=/opt/chessintel/bin/stockfish
    python selftest_lecture.py

Exit code 0 means every stage worked. Any missing Stockfish binary is reported
explicitly rather than surfacing as a traceback.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chessintel import coach_core, lectures, renderers  # noqa: E402

# A well-known miniature: both sides blunder, so there is plenty for the
# tactic detector and the moment selector to find.
SAMPLE_PGN = """[Event "Blackburne Shilling Gambit"]
[Site "London"]
[Date "1880.??.??"]
[White "Amateur"]
[Black "Shilling"]
[Result "0-1"]

1. e4 e5 2. Nf3 Nc6 3. Bc4 Nd4 4. Nxe5 Qg5 5. Nxf7 Qxg2 6. Rf1 Qxe4+
7. Be2 Nf3# 0-1
"""


def main() -> int:
    engine_path = coach_core.STOCKFISH_PATH
    print(f"engine: {engine_path}")

    try:
        lecture = lectures.build_lecture(
            SAMPLE_PGN,
            player="Amateur",
            level="improver",
            depth=12,
        )
    except Exception as exc:  # noqa: BLE001 - a self-test reports, it does not crash
        print(f"FAILED to build lecture: {exc}")
        print(
            "\nIf this mentions Stockfish, set STOCKFISH_PATH to a working UCI binary."
        )
        return 1

    meta = lecture["meta"]
    print()
    print(f"title      : {meta['title']}")
    print(f"subtitle   : {meta.get('subtitle')}")
    print(f"level      : {meta['level_label']} (depth {meta['depth']})")
    print(f"opening    : {(lecture['game'].get('opening') or {}).get('name')}")
    print(f"result     : {lecture['game'].get('result')}")
    print(f"accuracy   : {lecture['summary']['accuracy']}")
    print(f"acpl       : {lecture['summary']['acpl']}")
    print(f"tactics    : {lecture['tactics']}")
    print(f"moments    : {len(lecture['moments'])}")
    print(f"quiz       : {len(lecture['quiz'])}")
    print(f"drills     : {len(lecture['drills'])}")
    print(f"habits     : {[h['habit'] for h in lecture['habits']]}")
    print()
    print("verified facts:")
    for fact in lecture["facts"]:
        print(f"  - {fact}")

    if lecture["moments"]:
        print()
        print("first moment beats:")
        moment = lecture["moments"][0]
        print(f"  move {moment['move_number']} {moment['san']} ({moment['classification']})")
        for key, value in moment["beats"].items():
            shown = value if isinstance(value, str) else "; ".join(value)
            print(f"  {key}: {shown}")

    out_dir = Path("out")
    out_dir.mkdir(exist_ok=True)

    md = renderers.to_markdown(lecture)
    (out_dir / "selftest.md").write_text(md, encoding="utf-8")
    print(f"\nmarkdown   : {len(md)} chars -> out/selftest.md")

    html = renderers.to_html(lecture)
    (out_dir / "selftest.html").write_text(html, encoding="utf-8")
    print(f"html       : {len(html)} chars -> out/selftest.html")

    pdf = renderers.to_pdf(lecture, str(out_dir / "selftest.pdf"))
    if pdf:
        print(f"pdf        : {os.path.getsize(pdf)} bytes -> {pdf}")
    else:
        print("pdf        : SKIPPED (WeasyPrint unavailable)")

    (out_dir / "selftest-annotated.pgn").write_text(
        lecture["annotated_pgn"], encoding="utf-8"
    )
    print(f"annotated  : -> out/selftest-annotated.pgn")

    (out_dir / "selftest-worksheet.md").write_text(
        renderers.to_worksheet(lecture), encoding="utf-8"
    )
    print("worksheet  : -> out/selftest-worksheet.md")

    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
