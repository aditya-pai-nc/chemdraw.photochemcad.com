# ChemDraw Processor (web)

Web version of the ChemDraw desktop app: upload a `.cdx` file, split molecules, enrich with PubChem, and download Excel plus generated files.

The backend still drives **ChemDraw via COM**, so real processing requires a **Windows** machine with ChemDraw installed. The UI can be developed on macOS; the pipeline will report that ChemDraw is missing.

The InChIKey → PubChem → structure flow and the AI identification pass need neither, so both
can be exercised and verified on a Mac — see [Verifying it without ChemDraw](#verifying-it-without-chemdraw)
and `/api/ai/selftest`.

## Layout

```
chemdraw.photochemcad.com/
├── backend/     FastAPI: ChemDraw pipeline + spectral interpolation
└── frontend/    Next.js (App Router) UI + server-side API proxy
```

**The browser never calls FastAPI directly.** It talks only to Next; Next's
`app/api/[...path]/route.ts` proxies every `/api/*` call to FastAPI server-side.
So FastAPI binds to `127.0.0.1` and is never reachable from the internet, there is
no CORS to configure, and auth or rate limiting has one place to live.

```
browser ──▶ nginx :443 ──▶ next start :3000 ──▶ uvicorn 127.0.0.1:8000 ──▶ worker.py ──▶ ChemDraw
                             (UI + /api proxy)      (localhost only)
```

The proxy streams in both directions: SSE progress arrives live, and .xlsx/.zip
downloads are never buffered in Node.

## Run locally

**Backend** (Python 3.10+):

```bash
cd backend
python -m venv venv
# Windows: venv\Scripts\activate
# macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # optional — only the AI pass needs anything in it
uvicorn app:app --reload --port 8000
```

`backend/.env` is read at startup and is gitignored. Everything in it is
optional: with an empty file the pipeline behaves exactly as it did before the
InChIKey and AI columns existed. To turn on AI identification, put a key in it:

```ini
ANTHROPIC_API_KEY=sk-ant-...
```

Real environment variables always beat the file, so a shell export or a CI
secret still overrides it. The file exists mainly for the production setup
below, where uvicorn is started by Windows Task Scheduler and inherits almost
nothing from any shell.

**Frontend:**

```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). Next proxies `/api/*` to
uvicorn using `CHEMDRAW_API_URL` (see `frontend/.env.example`; copy it to
`.env.local`). Same code path in dev and production — nothing special to switch.

## ChemDraw queue

ChemDraw is a single desktop application driven over COM, so only one job may
touch it at a time. Uploads join a FIFO queue (`ChemDrawQueue` in `backend/jobs.py`)
that a single drain task services one job at a time — the second and third uploads
wait their turn instead of fighting over the same ChemDraw instance.

Waiting jobs receive `queue` events on their SSE stream as the line moves
(`position` = jobs ahead, `depth` = total in the queue), and the UI shows
"Waiting for ChemDraw — N jobs ahead of you". Cancelling a job that is still
waiting removes it from the line without ever starting ChemDraw.

- `GET /api/queue` — what is running and who is waiting
- `POST /api/jobs` returns `queue_position` and `queue_depth`
- `CHEMDRAW_MAX_QUEUE` (default 20) caps the line; further uploads get HTTP 503

This is per-process state, which is another reason the server must run with
`--workers 1`.

## Spectral interpolation

Ported from `ChemDrawAutomationScripts/interpolation.py` — the algorithms are
byte-identical; only its unused matplotlib import was dropped, since charts are
drawn in the browser.

Upload a `.txt`/`.tsv`/`.csv`/`.xlsx`/`.xls` file (2 columns = wavelength +
coefficient, 3 columns = id + wavelength + coefficient) and every compound is
interpolated with **cubic spline, Akima spline, linear, RBF (Gaussian), and a
Gaussian Mixture Model**. Each is then verified by the "generated points only"
technique from `VERIFICATION_TECHNIQUE.md` — predict the measured points using
*only* the generated ones — and ranked by MSE.

Interpolation needs no ChemDraw and no Windows, so it deliberately does **not**
use the ChemDraw queue; it runs in worker threads (`CHEMDRAW_INTERP_CONCURRENCY`,
default 2) so the event loop stays responsive.

- `POST /api/interpolation` (file + step_size), `GET /api/interpolation/{id}`
- `GET /api/interpolation/{id}/events` — SSE progress
- `GET /api/interpolation/{id}/compounds/{compound_id}` — series for the charts
- `GET /api/interpolation/{id}/excel` — the workbook

## Pipeline

1. **CDX → CDXML** — ChemDraw COM opens the file and saves XML
2. **Split molecules** — parse CDXML into one file per structure
3. **Structure & PubChem** — ChemDraw exports MOL, TIFF and its own **InChI**; RDKit
   computes SMILES/formula/weight and a second, independent **InChIKey**; PubChem is
   searched by InChIKey first and the winning structure is downloaded and rebuilt locally
4. **Identify & reconcile** — Claude identifies each compound from the drawing, then a
   second model reconciles every source into one answer *(skipped when no API key is set)*

Output download is a ZIP of CDXML, `split_molecules/`, `mol_files/`, `images/`,
`pubchem_structures/`, `chemdraw_inchi/`, and `*_compounds.xlsx`.

## InChIKey matching

An InChIKey is a hash of the structure itself, so a key hit is an identification, whereas a
name hit is only ever a guess that two people spelled a compound the same way. The pipeline
therefore searches PubChem in order of how much each route can be trusted — **exact
InChIKey → InChIKey skeleton → name → SMILES** — and records in `PubChem Source` which one
actually produced the answer.

Two InChIKeys are gathered for every molecule, from ChemDraw's own InChI export and from
RDKit reading ChemDraw's MOL. When they agree, the structure survived the ChemDraw → MOL →
RDKit handoff intact; when they disagree, the handoff changed something, and
`ChemDraw vs RDKit InChIKey` says so before any downstream match is believed. ChemDraw's
InChI is tried three ways (`Objects.Data("chemical/x-inchi")`, `SaveAs .inchi`, then its MOL
block through RDKit) because the COM surface differs by version; `ChemDraw InChI Source`
names the route that worked — the third one is *not* independent of RDKit and is labelled
as such.

The **14-character skeleton** is the first block of the key, covering connectivity only. A
drawing whose stereocentres were left flat — very common in a scheme — produces a different
full key from the same compound in PubChem while sharing the skeleton. Those are matched and
reported as `🟡` rather than being passed off as exact or thrown away as failures.

### Verifying it without ChemDraw

The InChIKey half of the pipeline is plain HTTP plus RDKit, so it runs anywhere — which is
the point, since ChemDraw needs Windows and cannot start on a Mac at all.

```bash
# Push a known key into PubChem, pull the structure back, rebuild it in RDKit,
# and check the recomputed key still matches. Defaults to aspirin.
curl localhost:8000/api/inchikey/selftest

# Any key. Falls back to the skeleton when the full key misses, and says which matched.
curl localhost:8000/api/inchikey/BSYNRYMUTXBXSQ-UHFFFAOYSA-N
curl 'localhost:8000/api/inchikey/RYYVLZVUVIJVGH-UHFFFAOYSA-N?save=true'   # keep the MOL
```

`round_trip_ok` is the real assertion. PubChem's stored InChIKey and a key recomputed from
PubChem's own connection table are two different artefacts, and if they disagree then the
structure the backend is about to use is not the structure that was asked for.

## AI identification and consensus

Two models, deliberately asymmetric:

| | Model | Sees | Job |
|---|---|---|---|
| **Identifier** | `claude-opus-5` + web search | caption, structure image, molecular formula | works out what the molecule is |
| **Referee** | `claude-sonnet-5` | everything, including *how* PubChem was found | reconciles the sources into one answer |

The identifier is **not** given the SMILES that ChemDraw and RDKit derived. An opinion that
has already been shown the answer is not evidence, and its whole value in the vote is that
it arrives independently — so when it agrees with PubChem, the agreement means something.

**A model is never trusted for an InChIKey.** A key is a hash; it cannot be reasoned out,
only computed or recalled, and a model asked for one will produce something that looks
exactly right and is not. The identifier is asked for a SMILES and the key is computed from
it locally with RDKit. Anything it volunteers is kept in `AI Reported InChIKey`, clearly
labelled, and never used for matching.

The referee weighs the sources by strength rather than by majority — an exact InChIKey hit
outranks a name hit, and two sources agreeing because one was derived from the other is not
corroboration — and sets `Consensus Needs Review` whenever a human should look.

ChemDraw's TIFF exports are converted to PNG and downscaled to 1568px before being sent, so
a 35 MB export becomes a ~100 KB payload. The AI pass runs several compounds at a time
(`CHEMDRAW_AI_CONCURRENCY`) because, unlike ChemDraw, it has no single-instance constraint.

Without `ANTHROPIC_API_KEY` the whole pass is skipped and the AI columns read `—`;
everything else works exactly as before.

```bash
curl localhost:8000/api/ai            # is it configured, and with which models
curl localhost:8000/api/ai/selftest   # both models against a known compound, no ChemDraw
```

## The three match columns

The workbook reports each verdict separately instead of collapsing them, because how often
the InChIKey route succeeds where the formula route fails is exactly the question the extra
column exists to answer.

| Column | Asks | Strength |
|---|---|---|
| `Match?` | Does PubChem's formula match, and its weight to within 0.5? | Two molecules can weigh the same and be unrelated |
| `InChIKey Match?` | Does the structure hash match? | Conclusive — this *is* the structure |
| `AI Match?` | Does Claude's independent identification match the drawing? | Catches drawings that are self-consistent but wrong |
| `Manual Match` | *(left empty)* | The researcher's own verdict |

`✅` agreement · `🟡` same skeleton, different stereochemistry or protonation · `❌`
disagreement · `—` nothing to compare. Each machine verdict has a matching `… Detail`
column giving the reason in words, so no tick has to be taken on faith.

## Production

Two services on the same host:

```bash
# 1. API — localhost only, single worker
cd backend && venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1

# 2. UI + proxy
cd frontend && npm run build && CHEMDRAW_API_URL=http://127.0.0.1:8000 npm start
```

`--workers 1` is required: job state and the ChemDraw queue live in memory in
`jobs.py`, so a second worker would not see the first one's jobs.

Put nginx in front of port 3000 for TLS, and set `proxy_buffering off` on `/api/`
or the live progress stream will arrive in one lump at the end.

**ChemDraw needs Windows and an interactive desktop session** — run uvicorn from a
logged-in user via Task Scheduler ("At log on"), never as a Windows Service, which
starts in Session 0 with no desktop. Interpolation has no such requirement and runs
anywhere. The two halves can also be split across hosts: point `CHEMDRAW_API_URL`
at a Windows box over a private network.

### Environment

Backend values can go in `backend/.env` (copy `backend/.env.example`, which
documents all of them) or be set as real environment variables — the latter win.
Frontend values follow the existing `frontend/.env.local` convention.

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `CHEMDRAW_API_URL` | frontend | `http://127.0.0.1:8000` | FastAPI address the proxy calls |
| `CHEMDRAW_DATA_DIR` | backend | `backend/data/jobs` | ChemDraw job output |
| `CHEMDRAW_INTERP_DIR` | backend | `backend/data/interpolation` | Interpolation output |
| `CHEMDRAW_MAX_QUEUE` | backend | `20` | Max jobs in the ChemDraw line |
| `CHEMDRAW_INTERP_CONCURRENCY` | backend | `2` | Parallel interpolation jobs |
| `CHEMDRAW_CORS_ORIGINS` | backend | *(unset)* | Only if exposing the API to a browser directly |
| `ANTHROPIC_API_KEY` | backend | *(unset)* | Enables the AI pass. Without it those columns read `—` |
| `CHEMDRAW_AI_ENABLED` | backend | `auto` | `auto` = on when a key is present; `0` forces it off |
| `CHEMDRAW_AI_MODEL` | backend | `claude-opus-5` | The identifier — needs to be able to do research |
| `CHEMDRAW_AI_CONSENSUS_MODEL` | backend | `claude-sonnet-5` | The referee — only weighs evidence it is given |
| `CHEMDRAW_AI_CONCURRENCY` | backend | `4` | Compounds identified in parallel |
| `CHEMDRAW_AI_WEB_SEARCH` | backend | `1` | Lets the identifier look compounds up |
| `CHEMDRAW_AI_EFFORT` | backend | `high` | Identifier thinking depth (`low`…`max`) |
| `CHEMDRAW_AI_MAX_IMAGE_PX` | backend | `1568` | Long edge the TIFF is downscaled to |
| `CHEMDRAW_INCHIKEY_DIR` | backend | `backend/data/inchikey` | Where `?save=true` writes structures |

Neither `data/` directory is ever cleaned up — add a scheduled job to delete
folders older than a week.
