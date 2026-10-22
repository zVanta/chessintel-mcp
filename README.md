# ChessIntel — engine-verified chess analysis and lecture generation for LibreChat

An MCP server that turns a chess game into (a) verified engine analysis and
(b) a complete, teachable **lecture** — in Markdown, interactive HTML, PDF and
annotated PGN, all rendered from one structured source.

The design principle is absolute: **the tools produce facts, the LLM writes
prose on top.** Every evaluation, move, tactic and result in the output comes
from Stockfish or python-chess. The model cannot hallucinate a move or an
evaluation it was never given, because it is never asked to produce one.

---

## What makes a lecture a *lecture*

A lecture is not a report. It is built from one **structured model** that all
renderers read, so the Markdown, HTML, PDF, worksheet and annotated PGN can
never disagree with each other.

Each lecture contains:

| Section | What it is |
| --- | --- |
| Game at a glance | Result, accuracy per colour, ACPL, opening (ECO) |
| Evaluation graph | Inline SVG of how the game swung |
| **The moments that decided it** | Key positions, each with a four-beat explanation |
| **What went right** | Moves where the player found the engine's own top move |
| Tactical summary | Motif counts (hanging piece, fork, pin, skewer, back-rank, trap) |
| Drills | One concrete drill per recurring habit, tied to a real position |
| Positions to solve | Puzzles generated from the game's own critical positions |
| Homework | Concrete, checkable tasks |
| Verified facts | Every claim in the lecture, listed and quotable |
| Annotated PGN | `[%eval]` comments, importable into Shredder/ChessBase |

### The four beats

Each key moment is explained in four derived-from-evidence steps:

1. **Why it looked good** — computed signals: won a piece on f7, gave check,
   castles, was played in under 3 seconds.
2. **Why it failed** — the engine's own refutation, or the evaluation change.
3. **The concept** — the transferable idea, keyed to the detected motif.
4. **Remember** — a rule the student can apply next game.

### Two decisions worth knowing about

**Ranking is by winning-chances lost, not raw centipawns.** Once a game is
already decided, a 900cp blunder barely moves the win percentage. Ranking on
centipawns put the Opera Game's final, already-mate-in-1 blunder at the top of
the lecture and labelled it merely "okay". Ranking on win-chance delta surfaces
the move that actually turned the game.

**Evaluations are normalised to White's point of view.** The engine walk stores
each score from the *mover's* perspective, so consecutive rows alternate sides
and are not comparable. Displaying them raw made a normal move look like a
15-pawn collapse.

---

## Layout

```
chessintel/
  server.py        MCP tools, engine pool, transports, entry point
  lectures.py      builds the structured lecture model (the single source of truth)
  renderers.py     Markdown / HTML / PDF / worksheet  <- all read the same model
  openings.py      ECO + opening name (PGN headers -> built-in table -> optional Lichess)
  coach_core.py    vendored engine core: accuracy, tactics, habits, drills
  fetchers.py      chess.com + lichess public APIs (no credentials)
  storage.py       SQLite: players, games, reports, lectures, drill cards
  analysis.py      legacy per-move analyser (kept for the standalone .exe)
deploy.sh          idempotent install/upgrade on the host
selftest_lecture.py  end-to-end pipeline test, no MCP client needed
```

`coach_core.py` is vendored from the Checkmate Coach product so the numbers here
match the coach reports exactly. Its only two external imports (`llm`,
`requests`) are optional and unused by the server.

---

## Tools (27)

**Analysis** — `analyze_pgn`, `analyze_games`, `find_patterns`,
`build_report_context`, `tactics_breakdown`

**Online** — `fetch_chesscom_games`, `fetch_lichess_games`,
`analyze_online_games`, `player_summary`

**Positions** — `evaluate_position`, `best_moves`, `compare_moves`

**Lectures** — `lecture_from_pgn` (the one to use), `build_lecture`,
`render_lecture`, `export_annotated_pgn`, `make_worksheet`

**Library & drills** — `save_lecture`, `list_lectures`,
`add_drills_from_lecture`, `due_drills`, `grade_drill`, `store_games`,
`progress_report`, `save_report`

**Meta** — `get_tool_help`, `health_check`

---

## Deployment (host-native)

This runs as a systemd service on the LibreChat host. See `deploy.sh` for the
authoritative steps; this is the summary.

### Why not Docker?

LibreChat itself runs in a container, so a `command:`-style (stdio) MCP server
would be spawned *inside that container* — which has Node and no Python, no
Stockfish, and loses anything installed on the next image pull. A sidecar
container was the alternative; a host service was chosen to avoid the extra
container. Everything below is reversible: only the `url:` in `librechat.yaml`
would change.

### One-time provisioning

```bash
# 1. Python 3.12 (Debian 11 ships 3.9, too old for FastMCP)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="/root/.local/bin:$PATH"
export UV_PYTHON_INSTALL_DIR=/opt/chessintel/python
uv python install 3.12
uv venv --python 3.12 /opt/chessintel/.venv
uv pip install --python /opt/chessintel/.venv/bin/python \
    fastmcp python-chess python-dotenv requests weasyprint

# 2. Stockfish — build from source
#    The published Linux binary needs glibc >= 2.35; Debian 11 has 2.31, so the
#    prebuilt release will NOT run. Build it against the host's own glibc:
apt-get install -y g++ make
curl -sL -o /tmp/sf.tar.gz \
  https://github.com/official-stockfish/Stockfish/releases/download/sf_19/stockfish-linux-x86-64-universal.tar.gz
tar -xzf /tmp/sf.tar.gz -C /tmp
cd /tmp/stockfish/src && make -j"$(nproc)" build ARCH=x86-64-avx2
install -m 0755 ./stockfish /opt/chessintel/bin/stockfish

# 3. WeasyPrint native libraries (PDF rendering)
apt-get install -y libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
    libharfbuzz0b libcairo2 libgdk-pixbuf-2.0-0 libffi7 shared-mime-info \
    fonts-dejavu-core fonts-liberation
```

### Install / upgrade

```bash
cd /path/to/chessintel-lite
bash deploy.sh
```

Idempotent. It replaces `/opt/chessintel/app`, writes `/etc/chessintel.env` and
the unit only if absent, restarts the service, waits for it to listen, and
reports the engine and tool count.

### Layout on the host

| Path | Purpose |
| --- | --- |
| `/opt/chessintel/app` | the application |
| `/opt/chessintel/.venv` | virtualenv |
| `/opt/chessintel/python` | uv-managed CPython 3.12 |
| `/opt/chessintel/bin/stockfish` | the engine |
| `/etc/chessintel.env` | configuration |
| `/var/lib/chessintel/chessintel.db` | SQLite library |
| `/var/lib/chessintel/out` | generated lectures |

### Operations

```bash
systemctl status chessintel
journalctl -u chessintel -f
systemctl restart chessintel
```

---

## Wiring into LibreChat

Two changes to `librechat.yaml`, then reload the API container.

### 1. Exempt the private address from the SSRF guard

LibreChat blocks private addresses for MCP transports by default, and Docker
resolves `host.docker.internal` (via `host-gateway`) to the **default bridge**
gateway `172.17.0.1` — not the compose network's gateway. Bind the service
there and exempt it:

```yaml
mcpSettings:
  allowedAddresses:
    - 'host.docker.internal:8080'
```

Use `allowedAddresses`, **not** `allowedDomains`. Adding a private host to
`allowedDomains` switches that field into strict-whitelist mode and blocks every
public destination not also listed — which would break the other MCP servers.

### 2. Register the server

```yaml
mcpServers:
  chessintel:
    type: streamable-http
    url: http://host.docker.internal:8080/mcp
    timeout: 600000      # deep analysis + PDF rendering; default is 60s
    headers: {}
```

### 3. Reload and confirm

```bash
docker restart LibreChat-API
docker logs --tail 200 LibreChat-API | grep -i chessintel
```

Expected:

```
[MCP][chessintel] URL: http://host.docker.internal:8080/mcp
[MCP][chessintel] Initialized in: 799ms
[MCP] Initialized with 4 configured servers and 27 tools.
```

Then enable the chessintel tools on the agent you want to use them (or leave them
as app-level tools).

### Security note

`CHESSINTEL_HOST` is bound to `172.17.0.1` deliberately. Binding `0.0.0.0` would
expose the service on the host's public interface, and this host has no firewall
(`iptables` INPUT policy is ACCEPT). Verify it is not public:

```bash
curl --max-time 5 http://<public-ip>:8080/mcp   # should fail
```

---

## Configuration

`/etc/chessintel.env`:

| Variable | Default | Notes |
| --- | --- | --- |
| `STOCKFISH_PATH` | `stockfish` | absolute path recommended |
| `STOCKFISH_THREADS` | `2` | engine threads |
| `STOCKFISH_HASH_MB` | `128` | engine hash |
| `CHESSINTEL_DEPTH` | `14` | default analysis depth |
| `CHESSINTEL_DB` | `./data/chessintel.db` | SQLite path |
| `CHESSINTEL_OUT` | `./out` | rendered lecture output |
| `CHESSINTEL_TRANSPORT` | `stdio` | `http` for the network service |
| `CHESSINTEL_HOST` / `_PORT` | `0.0.0.0` / `8080` | HTTP bind |

### Performance

Measured on the 6-core host:

| Workload | Time |
| --- | --- |
| 17-move game, all 5 output formats | ~7 s |
| 136-ply game, all formats | ~25 s |

Depth 14 with 4 threads. Depth scales roughly linearly, so depth 20 is about
3× slower. The LibreChat server entry sets `timeout: 600000` for this reason.

---

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export STOCKFISH_PATH=/path/to/stockfish

python selftest_lecture.py                       # full pipeline, no MCP client
CHESSINTEL_TRANSPORT=http python run_server.py   # HTTP on :8080/mcp
python run_server.py                             # stdio
```

`selftest_lecture.py` builds a lecture from a bundled game and writes every
format to `./out`, so you can see the real output without wiring up a client.

The `build.ps1` / PyInstaller path still produces a standalone
`chessintel-mcp.exe` for stdio use on a machine without Python.

---

## Agent prompt

Paste this into the LibreChat agent that will write the lectures:

```
You are a chess coach's assistant. Build every lesson from the chessintel tools
and never state a fact they did not return.

Workflow:
1. Call lecture_from_pgn with the game, the student's name in `player`, and a
   `level` (beginner / improver / club / expert), passing `kid_name` so the
   lecture is filed and drill cards are seeded.
2. The tool already returns finished Markdown and saves .md/.html/.pdf files.
   Present that Markdown. Do not re-derive any analysis yourself.
3. You may write the narrative.intro and narrative.closing prose, but only from
   the `facts` list. Quote evaluations exactly as given.
4. Never name an opening the analysis reported as unidentified. Never describe a
   move, tactic or result that is not in the tools' output.

When asked for a report rather than a lecture, use analyze_pgn or
build_report_context and structure it: (1) one thing they do well with evidence,
(2) the single biggest recurring pattern with move-numbered examples, (3) one
drill from a real position, (4) what the next report should check.
```

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Service `activating` forever | `run_server.py` or the package missing after a partial copy — run `deploy.sh`, which verifies both |
| LibreChat: no chessintel tools | Missing `mcpSettings.allowedAddresses` entry; check `docker logs LibreChat-API \| grep chessintel` |
| `stockfish: not found` in `health_check` | `STOCKFISH_PATH` wrong, or the binary was built for a newer glibc |
| PDF files missing, HTML fine | WeasyPrint native libs absent — `renderers.to_pdf` returns None rather than raising, so the other formats still work |
| Lichess fetch returns 0 games | The endpoint needs `pgnInJson=true`; already handled in `fetchers.py` |
| Analysis seems to headline a bad move in a lost position | Ranking is by win-chance loss; check `loss_pct` on the moment |

