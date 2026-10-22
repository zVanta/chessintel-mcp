# Knowledge Base — loading PDFs for the ChessIntel agent

How to give the coach agent your own chess material (PDFs, lecture HTML, notes)
so it can use it alongside the engine tools.

---

## ✅ Resolved: ingestion fixed on 2026-09-26

**Status: working.** The knowledge base is live — ingestion writes embeddings and
retrieval returns them, verified end to end after the fix below.

What follows is the diagnosis, kept because the *symptoms* are the useful part: a
failure that produces no error anywhere.

### The fix that was applied

| Setting | Value |
| --- | --- |
| `EMBEDDINGS_PROVIDER` | `google_genai` |
| `EMBEDDINGS_MODEL` | `models/gemini-embedding-001` |
| `RAG_GOOGLE_API_KEY` | set to the existing `GEMINI_API_KEY` value |

Gemini serves embeddings through `langchain_google_genai`, which is *already
installed* in this image, so no new dependency and no new spend were needed. (The
OpenAI-compatible route —
`RAG_OPENAI_BASEURL=https://generativelanguage.googleapis.com/v1beta/openai` —
also worked in testing and remains a viable alternative.)

Note `GOOGLE_KEY` in `.env` is only 13 characters and is *not* the Gemini key, so
`RAG_GOOGLE_API_KEY` had to be set explicitly rather than relying on the
`GOOGLE_KEY` fallback.

**Important: `docker restart` is not enough.** `rag_api` loads `.env` through
`env_file`, which is read when the container is *created*, not when it restarts.
You must recreate it:

```bash
cd /root/ai
docker compose -f deploy-compose.yml -f docker-compose.override.yml up -d --no-deps rag_api
```

The previous `.env` is backed up at `/root/ai/.env.bak-kb-20260926-155729`.

Verified afterwards:

```
Initialized embeddings of type: <class 'langchain_google_genai.embeddings.GoogleGenerativeAIEmbeddings'>
POST /embed -> {"status":true,"message":"File processed successfully."}
pgvector rows: 1     /query returned the ingested text
```

### Two things to know about this stack

- **One shared collection.** `COLLECTION_NAME` defaults to `testcollection`, so
  every document lives in a single pgvector collection and is separated by
  `file_id` / `user_id` metadata. Set `COLLECTION_NAME` if you want to rename it.
- **The `embedding` column is a dimensionless `vector`**, so 3072-dimension Gemini
  vectors fit. A fixed `vector(1536)` column would have rejected them. If you ever
  change the embedding model, **all documents must be re-indexed** — vectors from
  different models are not comparable.

The agent upload path is `POST /embed-upload` (fields `file_id`, `uploaded_file`,
`entity_id`), which shares the same collection and pipeline as the `/embed`
endpoint used in the tests below.

---

### Original diagnosis (kept for reference)

The pipeline is complete — LibreChat calls `rag_api` correctly, and `rag_api`
accepts the upload — but the call to the embeddings API is rejected:

```
POST /embed  ->  HTTP 200
{"status": false, "message": "Connection error.", "file_id": "...", "known_type": true}
```

A direct test from inside the `rag_api` container against the same credentials:

```
GET https://api.openai.com/v1/models  ->  401 Unauthorized
"message": "Incorrect API key provided: sk-proj-***...jW4A"
```

**The OpenAI API key is invalid.** Both variables hold the *same dead key*
(identical SHA-256), so it is not a case of one variable being unset:

| Variable | Length | Same key? | Valid? |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | 164 | yes (sha256 `7692e72d…`) | ❌ 401 |
| `RAG_OPENAI_API_KEY` | 164 | yes (sha256 `7692e72d…`) | ❌ 401 |

Network egress from the container is fine (`google.com` → 200), so this is a
credential problem, not connectivity.

### Why this is the *only* blocker

| Piece | State |
| --- | --- |
| File Search capability | ✅ `fileSearch: true` |
| `rag_api` container | ✅ up, `/embed` reachable, accepts the upload |
| Embedding provider configured | ✅ `EMBEDDINGS_PROVIDER=openai`, `EMBEDDINGS_MODEL=text-embedding-3-small` |
| Embedding provider **working** | ❌ **401 — invalid key** |
| pgvector store | ✅ tables present, 0 rows (nothing can be written) |
| PDF text extraction | ✅ `pypdf 6.19.0` in `rag_api` |
| Scanned-PDF OCR fallback | ✅ `^application/pdf$` in `fileConfig.ocr` |
| PDF allowed for agents | ✅ `application/pdf` in agent MIME types |

`rag-api-dev-lite` supports **only** `openai` and `azure` as embedding
providers — there is no local/Ollama/Cohere/Voyage option in this image. So one
of these three is required:

| Option | What to do | Effort |
| --- | --- | --- |
| **1. Valid OpenAI key** (recommended) | Put a working key in `/root/ai/.env` as both `OPENAI_API_KEY` and `RAG_OPENAI_API_KEY`, then `docker restart ai-rag_api-1`. `text-embedding-3-small` is about **$0.02 per million tokens** — a whole coaching library costs cents. | minutes |
| **2. Azure OpenAI** | Add `EMBEDDINGS_PROVIDER=azure` plus the `AZURE_OPENAI_API_KEY` / instance / deployment variables. None are currently set (`AZURE_OPENAI_*` is absent entirely). | needs an Azure resource |
| **3. Swap the image** | Move from `librechat-rag-api-dev-lite` to the full `librechat-rag-api-dev:latest` (note `rag.yml` already references the full image) to get more providers, at the cost of a heavier service. | significant |

Note that `GEMINI_API_KEY` is present and valid-looking, but Gemini embeddings
are **not supported** by this image, so it is not a way out.

### Confirm the fix

After adding a working key and restarting `ai-rag_api-1`, re-run the ingestion
test in "Verifying ingestion worked" below. You want `status: true` and a
non-zero embedding count.

The lesson worth keeping: **this failure is completely silent.** You upload a PDF,
the UI reports success, and the agent simply never mentions it. An `HTTP 200`
proves nothing — check the response body *and* the pgvector row count.

Separately, `OPENAI_API_KEY` is also dead (same key, 401). That is unrelated to
the knowledge base and only matters if you use OpenAI endpoints for chat.

---

## What the rest of this document assumes

The limits, the curation advice, and the agent-side rules below all still apply.


---

## Limits that shape how you organise this

| Limit | Value | Where it comes from |
| --- | --- | --- |
| Files per agent | **10** | `fileConfig.endpoints.agents.fileLimit` |
| Size per file | **100 MB** | `fileConfig.endpoints.agents.fileSizeLimit` |
| Total size per agent | **500 MB** | `fileConfig.endpoints.agents.totalSizeLimit` |
| Text sent to the model per turn | **100,000 tokens** | `fileConfig.fileTokenLimit` |

**The important consequence: curate, don't dump.** Retrieval returns the closest
chunks to the question. If you load 10 unrelated books, the retrieved chunks are
more likely to be irrelevant, and the model's 100k-token file budget gets spent
on noise. Ten well-chosen documents beat five hundred.

Recommended shape: **two or three focused agents**, each with its own 10 files.

| Agent | Files to load |
| --- | --- |
| **Openings Coach** | your `NJCC LECTURES PDF\` lectures + the `Chess Openings\` material for the openings you actually teach (Ruy Lopez, Scotch, London, Pirc, Vienna) |
| **Curriculum Coach** | `Steps\Step 1..5` PDFs + `STUDENT\` handouts + `GOLDEN RULES.pdf` |
| **Tactics Coach** | `Puzzle PDF\` + `OFF\tactics.pgn` converted to text/HTML |

---

## Where your material lives

On the PC, under `C:\Users\vince\OneDrive\Documents\chess\`:

**Best knowledge-base material — your own writing, clean text:**
- `Opening PDF\NJCC LECUTURES PDF\` — `Owens Defense - NJCC Lecture.html`,
  `Italian Game - NJCC Lecture.html`, `Tal vs Bobotsov 1958 - Complete Lesson Package.html`,
  `New Jersey Chess Club – Knight Sacrifice Lesson.pdf`
- `STUDENT\` — `NJCC STUDENT - London System.pdf`, `- Pirc Defense.pdf`,
  `- Scotch Game.pdf`, `- Vienna Game.pdf`, `GOLDEN RULES.pdf`,
  `Educational Objectives in Chess.pdf`, `NJCC Coach Progress Checklist.pdf`
- `Steps\Step 1..5\` — your graded curriculum
- Anything the chessintel server generates in `/var/lib/chessintel/out` (the
  `.html` and `.pdf` lectures) — these are ideal, because they are already
  engine-verified and consistent with what the tools will say

**Usable but noisy:**
- `Opening PDF\` books — mostly scanned or image-heavy. Chess notation OCRs
  badly, and diagram text is lost entirely. Fine for plans and prose, unreliable
  for concrete lines.
- `Puzzle PDF\` — workable, but puzzles are better served by the tools'
  auto-generated quiz positions from real games.

**Do not load these:**
- `PGN\DATABASE4U.pgn` — **8.8 GB.** It is a move database, not prose. It cannot
  be embedded usefully and would poison retrieval. If you want master-game
  statistics, that belongs behind a purpose-built tool, not a knowledge base.
- Large `.pgn` collections generally — the engine tools analyse PGNs directly.

> Only load documents you have the right to use. Your own club and student
> material is ideal; third-party books are fine for internal coaching use but
> do not redistribute them.

---

## How to attach documents

### Agent-level (permanent knowledge base) — what you want

1. Open the agent in the Side Panel (**Agents** → your agent).
2. Find the agent's **Files / Knowledge** section.
3. Upload your PDFs there (up to 10 per agent, 100 MB each).
4. Make sure **File Search** is enabled under Capabilities.
5. Save the agent.

The files are chunked, embedded, and stored in pgvector against that agent.

### Conversation-level (one-off)

Attach a file in the chat composer and make sure **File Search** is on for that
conversation. Use this for a one-time document you do not want in the agent's
permanent knowledge base.

### Per-student context

For a specific student's scoresheet or game, just paste the PGN into the chat
and ask for a lecture — the engine tools handle it. Save the knowledge base for
material that applies across sessions.

---

## How the agent should use it

The agent instructions (`AGENT_INSTRUCTIONS.md`) enforce this split:

- **Tools = facts.** Evaluations, moves, accuracy, tactics, results.
- **Knowledge base = theory and method.** Opening plans, drills, lesson
  structure, vocabulary.
- The agent names the source whenever it uses a document, and never attributes
  an engine evaluation to a book — or a book's claim to the engine.
- When a book and the engine disagree on an evaluation, the engine wins and the
  agent says so.

This matters because a scanned opening book and Stockfish 19 will disagree
regularly. Without this discipline you get lectures where a 1980s book's
assessment is presented as if it were verified analysis.

---

## Verifying ingestion worked

**Do not use the `rag_api` logs for this.** A `POST /text -> 200` is *not*
ingestion — `/text` is `Body_extract_text_from_file_text_post`, i.e. plain text
extraction (that is what runs when you attach a PGN or a document as text
context). It creates no embeddings. This is exactly how an earlier version of
this document got it wrong.

Also note `/embed` returns **HTTP 200 even when it fails** — the failure is in
the body:

```json
{"status": true}                                    <- worked
{"status": false, "message": "Connection error."}   <- failed
```

So check the body and the row count, not the status code.

### The check that matters: pgvector row count

```bash
ssh root@162.247.153.48
docker exec ai-vectordb-1 psql -U myuser -d mydatabase -t \
  -c "select count(*) from langchain_pg_embedding;"
```

`0` before your first successful upload; hundreds afterwards (one row per chunk).

### End-to-end ingestion test

Run this after fixing the embedding key, and any time you suspect ingestion has
broken. It mints a short-lived token for the admin user, posts a small document
to `/embed`, and reports the real result.

```bash
ssh root@162.247.153.48
```

```bash
USER_JSON=$(docker exec chat-mongodb mongosh LibreChat --quiet --eval \
  'const u=db.users.findOne({},{_id:1,email:1,username:1});
   print(JSON.stringify({id:String(u._id),email:u.email,username:u.username}))' \
  2>/dev/null | tail -1)

docker exec -i -e USER_JSON="$USER_JSON" ai-rag_api-1 python - <<'PY'
import base64, hashlib, hmac, json, os, time, urllib.request, uuid

secret = os.environ["JWT_SECRET"]
user = json.loads(os.environ["USER_JSON"])
b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=")
now = int(time.time())

h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
p = b64(json.dumps({"id": user["id"], "username": user["username"],
                    "email": user["email"], "iat": now, "exp": now + 900},
                   separators=(",", ":")).encode())
token = (h + b"." + p + b"." +
         b64(hmac.new(secret.encode(), h + b"." + p, hashlib.sha256).digest())).decode()

boundary = "----kbtest" + uuid.uuid4().hex
parts = []
for name, value in (("file_id", "kb-selftest-1"), ("entity_id", "kb-selftest-1")):
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"t.txt\"\r\nContent-Type: text/plain\r\n\r\n".encode())
parts.append(b"The Owens Defence begins 1.e4 b6, fianchettoing the bishop to b7.\r\n")
parts.append(f"--{boundary}--\r\n".encode())

req = urllib.request.Request("http://127.0.0.1:8000/embed", data=b"".join(parts),
    method="POST", headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                            "Authorization": f"Bearer {token}"})
r = urllib.request.urlopen(req, timeout=180)
print("HTTP", r.status, "->", r.read().decode()[:200])
PY
```

A working setup prints `"status":true`. Then confirm the rows landed:

```bash
docker exec ai-vectordb-1 psql -U myuser -d mydatabase -t \
  -c "select count(*) from langchain_pg_embedding;"
```

### Clean up the test document

```bash
docker exec ai-vectordb-1 psql -U myuser -d mydatabase -c \
  "delete from langchain_pg_embedding where cmetadata->>'file_id' = 'kb-selftest-1';"
```

### Listing collections

```bash
docker exec ai-vectordb-1 psql -U myuser -d mydatabase -t \
  -c "select name from langchain_pg_collection;"
```

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| **Uploading a PDF appears to work but the agent never cites it** | **Most likely the current blocker**: the embedding key is invalid, so nothing was indexed. Run the end-to-end test above — it must print `"status":true` *and* the row count must be non-zero. An `HTTP 200` proves nothing. |
| Upload succeeds but the agent never cites the document | File Search not enabled on the agent, or the file was attached without it. Re-check Capabilities. |
| Agent says the documents don't cover it, but they do | Retrieval missed. Ask a more specific question; retrieval is semantic, so "what does the Owens Defence lecture say about the d6 break?" finds more than "tell me about Owens". |
| Garbled or nonsense citations from a book | Scanned PDF whose OCR is poor. Replace it with a text-based version, or accept that only the prose is usable. |
| Number of embeddings stays 0 | The embedding provider failed. Confirm `/embed` returns `"status":true` and check `RAG_OPENAI_API_KEY` in `/root/ai/.env`. See the Blocker section. |
| Only the first N pages of a big PDF are searchable | The document exceeded the chunk budget or the upload was truncated. Split it. |
| Agent starts quoting book evaluations as if they were engine output | The instructions were not applied, or were overridden. Re-paste `AGENT_INSTRUCTIONS.md` into the agent's Instructions field. |

---

## Relationship to the MCP server

Nothing in this document changes the `chessintel` MCP service. The knowledge
base is LibreChat's own RAG stack (`rag_api` + pgvector); the chessintel service
is a separate host process on `172.17.0.1:8080`. They meet only in the agent:
one supplies retrieved prose, the other supplies verified engine facts.
