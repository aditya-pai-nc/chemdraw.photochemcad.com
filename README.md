# ChemDraw Processor (web)

Web version of the ChemDraw desktop app: upload a `.cdx` file, split molecules, enrich with PubChem, and download Excel plus generated files.

The backend still drives **ChemDraw via COM**, so real processing requires a **Windows** machine with ChemDraw installed. The UI can be developed on macOS; the pipeline will report that ChemDraw is missing.

## Layout

```
chemdraw.photochemcad.com/
├── backend/     FastAPI + the original 3-stage Python pipeline
└── frontend/    Next.js (App Router) UI (home → processing → results)
```

The frontend is a **static export** (`output: 'export'`), so Node is a build-time
dependency only. `next build` writes plain HTML/JS to `frontend/out`, which FastAPI
serves itself — the production server runs one Python process and nothing else.

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

Open [http://localhost:3000](http://localhost:3000). In dev the UI calls the API
directly at `http://127.0.0.1:8000` via `NEXT_PUBLIC_API_BASE` (see
`frontend/.env.development`); FastAPI's CORS middleware already allows it. No dev
proxy is involved. In production the variable is unset, so `/api` is same-origin.

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

## Pipeline (unchanged)

1. **CDX → CDXML** — ChemDraw COM opens the file and saves XML
2. **Split molecules** — parse CDXML into one file per structure
3. **Process & enrich** — ChemDraw exports MOL + TIFF, RDKit computes SMILES/formula/weight, PubChem lookup, Excel workbook

Output download is a ZIP of CDXML, `split_molecules/`, `mol_files/`, `images/`, and `*_compounds.xlsx`.

## Production (Windows)

Build the UI once (on any machine with Node), then copy the whole project to the
Windows box and run only the Python side:

```bash
cd frontend && npm run build     # emits frontend/out
cd ../backend && uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1
```

`--workers 1` is required: job state lives in an in-memory dict in `jobs.py`, so a
second worker would not see jobs created by the first. If `frontend/out` exists,
FastAPI serves the UI at `/` and the API at `/api`.

ChemDraw COM automation needs an **interactive desktop session** — run uvicorn from
a logged-in user via Task Scheduler ("At log on"), not as a Windows Service.
