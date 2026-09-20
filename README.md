# ChemDraw Processor (web)

Web version of the ChemDraw desktop app: upload a `.cdx` file, split molecules, enrich with PubChem, verify against CAS Common Chemistry, and download Excel plus generated files.

The backend still drives **ChemDraw via COM**, so real processing requires a **Windows** machine with ChemDraw installed. The UI can be developed on macOS; the pipeline will report that ChemDraw is missing.

The InChIKey → PubChem → structure flow and the curation pass need neither, so both
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
cp .env.example .env      # optional — keys enable AI curation and CAS verification
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

## Bulk chemical information extractor

Open **Bulk CAS Extractor** in the navigation (`/bulk-cas`). This is an independent
web app for extracting chemical data from a list of CAS numbers; it needs neither
a ChemDraw installation nor an AI API key.

Upload a TXT, CSV, TSV, XLSX or legacy XLS file (up to 2 MB and 500 entries).
Lists of approximately 100 CAS numbers are supported. Use one number per line,
or label a spreadsheet column `CAS RN`, `CAS Number` or `CAS No.` when the file
also has names or other columns. Excel worksheets are read in order. Keep CAS
cells formatted as **Text**, since Excel dates cannot reliably be turned back
into the original CAS number. A downloadable three-compound sample is on the page.

The extractor retrieves **molecular formula, molecular weight, SMILES, InChI,
InChIKey, name, PubChem CID, and direct PubChem / CAS Common Chemistry links**.
Input order and duplicates are preserved; duplicate CAS numbers reuse the same
lookup. Invalid formats or check digits remain in the table with an explanation.
Blank cells and lines beginning with `#` are skipped.

Both databases are queried independently. The summary uses the CAS Common Chemistry
record when one is available, otherwise PubChem. Each source's values stay together;
missing values are not borrowed from a different substance. When a CAS lookup
resolves to a current replacement RN, the source details record that RN. PubChem
can return several candidates: the one whose canonical isomeric SMILES agrees with
CAS is preferred; otherwise the first candidate is shown and candidates are noted.
Structure disagreements, unavailable services and missing records are reported
separately. Expand a table row to inspect each source's SMILES and InChI.

Downloads include a **CSV summary** and an **Excel workbook** with `Compounds`,
`Source records`, and `Read me` sheets. The workbook includes all source values,
comparison rules and CAS attribution. Strings are written as literal Excel text;
CSV entries beginning with formula-triggering characters receive a leading apostrophe.

Bulk jobs run in background threads, one batch at a time, independently of the
ChemDraw queue. The page displays progress and reconnects after a refresh. Stop
finishes the current lookup, then offers downloads of completed entries. Completed
results survive a backend restart; an interrupted active batch must be uploaded
again. Results are stored under `backend/data/bulk_cas/` (or `CHEMDRAW_BULK_CAS_DIR`).

CAS access uses the same backend `CAS_API_KEY` as ChemDraw verification. If CAS is
not configured or unavailable, PubChem still runs. Install backend requirements
to include `xlrd`, the reader required for `.xls` uploads.

```bash
curl -F "file=@cas_numbers.csv" localhost:8000/api/bulk-cas
curl localhost:8000/api/bulk-cas/<job_id>
curl -OJ localhost:8000/api/bulk-cas/<job_id>/download/xlsx
curl -OJ localhost:8000/api/bulk-cas/<job_id>/download/csv

cd backend
pip install -r requirements-dev.txt
python -m unittest test_common_chemistry test_bulk_cas -v
```

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
3. **Gather & enrich** — ChemDraw hands over **five representations** of each molecule
   (SMILES, SLN, InChI, InChIKey, MOL text); RDKit reads its MOL text for a canonical
   SMILES, formula, weight and an independent InChIKey; PubChem is searched by InChIKey
   first, every candidate CID is swept, and the winning structure is downloaded and rebuilt
4. **Curate the unmatched** — only compounds that did not match exactly go to a small model,
   which reconciles the two sources onto a second worksheet *(skipped when no API key is set)*

Output download is a ZIP of CDXML, `split_molecules/`, `mol_files/`, `images/`,
`pubchem_structures/`, and `*_compounds.xlsx`.

### Getting the five representations out of ChemDraw

Each format is tried two ways, and the workbook records which one worked in
`ChemDraw Format Routes`:

| Route | How | Cost |
|---|---|---|
| `com` | `Objects.Data(mime)` / `Objects.GetData(mime)` | Fast, works with the window hidden — but not every build answers to it |
| `keys` | `Ctrl+A`, `Alt+E`, `o`, then `s`/`l`/`n`/`k`/`m` off the clipboard | Works wherever the menu does, but needs ChemDraw raised and the machine left alone |

Both doors reach the same internal converters, so they should agree. COM is tried for all
five first; the window is only raised if something is still missing, so the focus cost is
not paid unless it is actually needed. The clipboard is emptied before every copy —
otherwise a wrong mnemonic leaves the *previous* format sitting there and it would be read
back as a success. Mnemonics are configurable (`CHEMDRAW_COPYAS_KEYS`) because they shift
between ChemDraw releases.

## InChIKey matching

An InChIKey is a hash of the structure itself, so a key hit is an identification, whereas a
name hit is only ever a guess that two people spelled a compound the same way. The pipeline
therefore searches PubChem in order of how much each route can be trusted — **exact
InChIKey → InChIKey skeleton → name → SMILES** — and records in `PubChem Source` which one
actually produced the answer.

Two InChIKeys are gathered for every molecule: ChemDraw's own, and RDKit's from ChemDraw's
MOL text. When they agree, the structure survived the ChemDraw → MOL → RDKit handoff intact;
when they disagree, the handoff changed something, and `ChemDraw vs RDKit InChIKey` says so
before any downstream match is believed.

### Matching on canonical forms

A SMILES string is not a structure — it is one of many ways to write one. `OC(=O)c1ccccc1`
and `c1ccccc1C(O)=O` are the same molecule and share not a single character. ChemDraw and
PubChem each write their own, so both are put through RDKit's canonical writer before they
are compared; comparing the raw strings answers a question nobody asked.

A compound is **matched only on exact agreement** — an identical canonical SMILES, or an
identical InChIKey across all three blocks. A shared 14-character skeleton with different
stereochemistry is reported as `🟡` and counted as **unmatched**, so it reaches curation
rather than being quietly accepted.

### Sweeping every candidate

One search term routinely resolves to several PubChem records — a parent, a salt, a labelled
isotopologue, a later duplicate deposition — and they are not equally complete. Taking the
first and accepting its blanks throws away data sitting in the next record down.

So every route collects all its candidates, keeps the best one as the answer (it decides the
CID, and therefore which compound the row is about), and fills any empty field from the first
candidate that has it. Every such borrow is named in `PubChem Borrowed Fields` and
`Reference Fields Borrowed` — a CAS number taken from a *different* record may describe a
different salt, and the row has to be able to say so.

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

## Curating the unmatched

CAS Common Chemistry verification runs during enrichment, before this pass.
Its evidence is included for unmatched compounds even when PubChem returned no record.
See [CAS Common Chemistry verification](#cas-common-chemistry-verification) for setup and verdicts.

Nothing here tries to work out what a molecule is — ChemDraw already read the structure and
PubChem was already searched every way it can be. The only question left, for the rows where
those two disagreed or where PubChem returned nothing, is what the researcher should write
down and why it did not match.

So a **small model** (`claude-haiku-4-5` by default) is given the *relevant* fields from both
sides — not the full property sweep, which is mostly 3D descriptors and fingerprints that have
no bearing on whether two structures are the same compound — and asked to reconcile them. It
returns a verdict (`same_compound`: yes / no / uncertain), curated values, and a concrete
discrepancy, likely cause and recommended action.

**A model is never trusted for an InChIKey.** A key is a hash; it cannot be reasoned out, only
copied. The curator may only quote a key that already appears in the evidence, and anything
else it returns is discarded — with the rejection recorded in `Curation Error`, so the attempt
is visible rather than silently dropped.

Curated values are written to their **own worksheet**, never merged into the machine-derived
columns, so no AI-authored value can be mistaken for something ChemDraw or PubChem said.

Without `ANTHROPIC_API_KEY` the pass is skipped; the second sheet still lists every unmatched
compound and why, just without a curated answer.

```bash
curl localhost:8000/api/ai            # is it configured, and with which model
curl localhost:8000/api/ai/selftest   # curate a known mismatch, no ChemDraw
```

## The match columns

The workbook reports each verdict separately instead of collapsing them, because how often
the structural route succeeds where the formula route fails is exactly the question the extra
column exists to answer.

| Column | Asks | Strength |
|---|---|---|
| `Match?` | Does PubChem's formula match, and its weight to within 0.5? | Two molecules can weigh the same and be unrelated |
| `Structure Match?` | Do the canonical SMILES or the InChIKeys agree? | Conclusive — this *is* the structure |
| `Matched` | `Yes` only on exact structural agreement | The gate: `No` sends the compound to curation |
| `Manual Match` | *(left empty)* | The researcher's own verdict |

`✅` agreement · `🟡` same skeleton, different stereochemistry or protonation · `❌`
disagreement · `—` nothing to compare. `Structure Match Detail` gives the reason in words,
naming which comparison decided it, so no tick has to be taken on faith.

The second worksheet, **Unmatched - AI curated**, carries one row per unmatched compound:
why it was unmatched (written locally, not by the model), the curator's verdict, its curated
fields, and enough context from sheet 1 to read on its own.

## CAS Common Chemistry verification

The existing `Match?` (PubChem formula/weight) and `InChIKey Match?`
(canonical ChemDraw SMILES vs canonical PubChem SMILES) keep their existing rules.
CAS Common Chemistry adds an independent `CAS Verification` result during stage 3.
Only the PubChem structural verdict determines which rows reach AI curation.

Request a key from [CAS API access](https://www.cas.org/services/commonchemistry-api),
then set it in `backend/.env` and restart the backend:

```ini
CAS_API_KEY=your-issued-key
```

The backend sends the key using `X-API-KEY`; it is never sent to the browser.
Without it, rows report **Not configured** and processing continues.
`CHEMDRAW_CAS_ENABLED=0` disables verification explicitly.

The client searches the drawing's full InChIKey first, then tries PubChem's CAS
annotations, the drawing's SMILES, and its caption. It inspects up to five unique
CAS candidates per compound (`CHEMDRAW_CAS_MAX_CANDIDATES`, range 1–20), stopping
on an exact structural match. Requests are paced at one per second and cached
within each job. Access errors, rate limiting and network failures stop further
network requests for that job and are reported as **Unavailable**.

| CAS Verification | Meaning |
|---|---|
| Verified | CAS and ChemDraw have identical canonical isomeric SMILES, including stereochemistry |
| Mismatch | The displayed CAS candidate has different canonical isomeric SMILES |
| Not comparable | A usable structural representation is missing, or the candidate limit was reached without a comparable record |
| Not found | No CAS record was retrieved for the available queries |
| Unavailable | Access, network or response failure prevented completing verification |
| Not configured / Disabled / Skipped | No API key, explicitly disabled, or no extracted structure |

A name or CAS-number hit alone never verifies a structure. CAS's `canonicalSmile`
can omit stereochemistry, so comparison uses its `smile` or a SMILES reconstructed
from its InChI. Neither salts nor stereoisomers are collapsed. A mismatch concerns
the displayed candidate, not every substance in CAS; truncated searches are noted.
Records are kept intact without borrowing fields from different CAS RNs.

The result, explanation, CAS RN, name, formula, molecular weight, SMILES, InChIKey
and record link appear in **Compounds** and as context on **Unmatched - AI curated**.
The results table shows the verdict and record link. The workbook's **Description**
sheet documents the comparison and attribution. CAS fields remain distinct from
the existing PubChem CAS annotations.

```bash
curl localhost:8000/api/cas           # configuration only; no external request
curl localhost:8000/api/cas/selftest  # verify aspirin; requires CAS_API_KEY, no ChemDraw

cd backend
python -m unittest test_common_chemistry -v  # offline regression checks
```

Source: [CAS Common Chemistry](https://commonchemistry.cas.org/), CAS, a division
of the American Chemical Society, [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/).
See the [CAS API reference](https://commonchemistry.cas.org/api-overview).
Names/formulas have HTML formatting removed; InChI may be converted to SMILES.

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
| `ANTHROPIC_API_KEY` | backend | *(unset)* | Enables curation. Without it the second sheet says why each compound is unmatched, with no curated answer |
| `CHEMDRAW_AI_ENABLED` | backend | `auto` | `auto` = on when a key is present; `0` forces it off |
| `CHEMDRAW_CURATE_MODEL` | backend | `claude-haiku-4-5-20251001` | The curator — only weighs evidence it is given |
| `CHEMDRAW_CURATE_CONCURRENCY` | backend | `4` | Unmatched compounds curated in parallel |
| `CHEMDRAW_CURATE_EFFORT` | backend | *(unset)* | Only for a model that supports it; the request degrades automatically |
| `CHEMDRAW_KEYS_FALLBACK` | backend | `1` | Drive Edit > Copy As when COM will not give up a format |
| `CHEMDRAW_COPYAS_KEYS` | backend | `smiles=s,sln=l,…` | Menu mnemonics, if they differ in your build |
| `CHEMDRAW_PUBCHEM_MAX_CANDIDATES` | backend | `25` | Candidate CIDs swept to fill missing fields |
| `CHEMDRAW_PUBCHEM_MAX_REFERENCE` | backend | `5` | Candidates walked for CAS / synonyms / Wikipedia |
| `CAS_API_KEY` | backend | *(unset)* | Enables CAS Common Chemistry verification |
| `CHEMDRAW_CAS_ENABLED` | backend | `1` | `0` disables CAS verification |
| `CHEMDRAW_CAS_MAX_CANDIDATES` | backend | `5` | CAS candidate records inspected per compound (1–20) |
| `CHEMDRAW_INCHIKEY_DIR` | backend | `backend/data/inchikey` | Where `?save=true` writes structures |

Neither `data/` directory is ever cleaned up — add a scheduled job to delete
folders older than a week.
