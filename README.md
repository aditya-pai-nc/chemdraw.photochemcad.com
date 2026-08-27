# ChemDraw Processor (web)

Web version of the ChemDraw desktop app: upload a `.cdx` file, split molecules, enrich with PubChem, and download Excel plus generated files.

The backend still drives **ChemDraw via COM**, so real processing requires a **Windows** machine with ChemDraw installed. The UI can be developed on macOS; the pipeline will report that ChemDraw is missing.

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
uvicorn app:app --reload --port 8000
```

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

## Pipeline (unchanged)

1. **CDX → CDXML** — ChemDraw COM opens the file and saves XML
2. **Split molecules** — parse CDXML into one file per structure
3. **Process & enrich** — ChemDraw exports MOL + TIFF, RDKit computes SMILES/formula/weight, PubChem lookup, Excel workbook

Output download is a ZIP of CDXML, `split_molecules/`, `mol_files/`, `images/`, and `*_compounds.xlsx`.

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

| Variable | Where | Default | Meaning |
|---|---|---|---|
| `CHEMDRAW_API_URL` | frontend | `http://127.0.0.1:8000` | FastAPI address the proxy calls |
| `CHEMDRAW_DATA_DIR` | backend | `backend/data/jobs` | ChemDraw job output |
| `CHEMDRAW_INTERP_DIR` | backend | `backend/data/interpolation` | Interpolation output |
| `CHEMDRAW_MAX_QUEUE` | backend | `20` | Max jobs in the ChemDraw line |
| `CHEMDRAW_INTERP_CONCURRENCY` | backend | `2` | Parallel interpolation jobs |
| `CHEMDRAW_CORS_ORIGINS` | backend | *(unset)* | Only if exposing the API to a browser directly |

Neither `data/` directory is ever cleaned up — add a scheduled job to delete
folders older than a week.
