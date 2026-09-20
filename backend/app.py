"""
HTTP API for ChemDraw Processor.
Uploads a .cdx file, runs the desktop pipeline in a worker process, streams progress over SSE.
"""
from __future__ import annotations

import io
import json
import os
import secrets
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

# Must come before ai_curate: that module turns several settings into constants
# the moment it is imported, so a .env loaded after it would arrive too late.
import config  # noqa: F401  (imported for its side effect)

import ai_curate
import common_chemistry
import pubchem
from inchi_tools import normalize_inchikey
from interpolation_jobs import InterpolationManager
from jobs import JobManager, QueueFullError, check_chemdraw
from bulk_cas_jobs import router as bulk_cas_router

manager = JobManager()
interpolation_manager = InterpolationManager()

# Structures downloaded by the InChIKey endpoints when ?save=true.
INCHIKEY_DIR = Path(
    os.environ.get("CHEMDRAW_INCHIKEY_DIR", Path(__file__).resolve().parent / "data" / "inchikey")
)
INCHIKEY_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ChemDraw Processor", version="1.0.0")
app.include_router(bulk_cas_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
# The original security model was one sentence: FastAPI binds to 127.0.0.1 and
# is never reachable from the internet. That holds when Next and uvicorn sit on
# the same host. It does not hold when the UI is served from elsewhere — Vercel,
# say — and proxies across the public internet to a Windows box, which leaves
# every endpoint open: upload a .cdx and drive ChemDraw on that machine, read
# any job's output, spend the Anthropic key through the curation pass.
#
# So: a shared secret, injected by the Next proxy server-side and required here.
# The browser never sees it. Unset means disabled, which keeps a localhost-only
# development setup working exactly as before — but `/api/health` reports which
# way it is configured, so a deployment cannot be open without saying so.

API_TOKEN = (os.environ.get("CHEMDRAW_API_TOKEN") or "").strip()
AUTH_HEADER = "x-chemdraw-token"

# Open regardless: a health check has to work for whatever is watching the
# service, and it discloses nothing beyond the fact that the process is up.
AUTH_EXEMPT_PATHS = {"/api/health"}


@app.middleware("http")
async def require_token(request: Request, call_next):
    if not API_TOKEN:
        return await call_next(request)

    path = request.url.path
    # Only the API is protected. The static UI mount below is not secret, and
    # CORS preflights carry no headers to check.
    if not path.startswith("/api/") or path in AUTH_EXEMPT_PATHS or request.method == "OPTIONS":
        return await call_next(request)

    supplied = request.headers.get(AUTH_HEADER) or ""
    if not supplied:
        header = request.headers.get("authorization") or ""
        if header.lower().startswith("bearer "):
            supplied = header[7:].strip()

    # compare_digest so a wrong token cannot be narrowed down by timing.
    if not secrets.compare_digest(supplied, API_TOKEN):
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid API token."},
        )

    return await call_next(request)


@app.get("/api/health")
async def health() -> dict[str, str | bool]:
    return {"status": "ok", "auth_required": bool(API_TOKEN)}


@app.get("/api/chemdraw")
async def chemdraw_status() -> dict:
    return await check_chemdraw()


@app.get("/api/cas")
async def cas_status() -> dict:
    return common_chemistry.status()


@app.get("/api/cas/selftest")
async def cas_selftest() -> dict:
    """Verify aspirin against CAS Common Chemistry without driving ChemDraw."""
    return await run_in_threadpool(common_chemistry.selftest)


@app.get("/api/queue")
async def queue_status() -> dict:
    return manager.queue_snapshot()


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...)) -> dict:
    filename = file.filename or "upload.cdx"
    if not filename.lower().endswith(".cdx"):
        raise HTTPException(status_code=400, detail="Please upload a .cdx ChemDraw file.")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        job = await manager.create(filename, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ChemDraw takes one job at a time; this puts the job in line for it.
    try:
        position = await manager.submit(job)
    except QueueFullError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {
        "job_id": job.id,
        "filename": job.filename,
        "queue_position": position,
        "queue_depth": manager.queue.depth(),
    }


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return manager.snapshot(job)


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")

    # A browser reconnecting a dropped EventSource replays its last id back to
    # us, so resume after it rather than re-sending the whole job.
    start_index = 0
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        try:
            start_index = int(last_event_id) + 1
        except ValueError:
            start_index = 0

    async def generate():
        async for index, event in manager.stream(job, start_index):
            if event.get("type") == "ping":
                yield ": ping\n\n"
            else:
                yield f"id: {index}\ndata: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, bool]:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    await manager.cancel(job)
    return {"cancelled": True}


@app.get("/api/jobs/{job_id}/excel")
async def download_excel(job_id: str) -> FileResponse:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if not job.excel_path or not Path(job.excel_path).is_file():
        raise HTTPException(status_code=409, detail="Excel file is not ready yet.")
    path = Path(job.excel_path)
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/jobs/{job_id}/archive")
async def download_archive(job_id: str) -> Response:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if not job.done or job.cancelled:
        raise HTTPException(status_code=409, detail="Output archive is not ready yet.")
    if not job.output_dir.is_dir():
        raise HTTPException(status_code=404, detail="Output folder is missing.")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in job.output_dir.rglob("*"):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(job.output_dir))

    stem = Path(job.filename).stem
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{stem}_output.zip"'},
    )


# ---------------------------------------------------------------------------
# Interpolation — no ChemDraw involved, so these never touch the ChemDraw queue
# ---------------------------------------------------------------------------


@app.post("/api/interpolation")
async def create_interpolation(
    file: UploadFile = File(...),
    step_size: float = Form(1.0),
) -> dict:
    filename = file.filename or "upload.csv"

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        job = await interpolation_manager.create(filename, data, step_size)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    interpolation_manager.start(job)
    return {"job_id": job.id, "filename": job.filename, "step_size": job.step_size}


@app.get("/api/interpolation/{job_id}")
async def get_interpolation(job_id: str) -> dict:
    job = interpolation_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Interpolation job not found.")
    return job.snapshot()


@app.get("/api/interpolation/{job_id}/events")
async def interpolation_events(job_id: str, request: Request) -> StreamingResponse:
    job = interpolation_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Interpolation job not found.")

    start_index = 0
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        try:
            start_index = int(last_event_id) + 1
        except ValueError:
            start_index = 0

    async def generate():
        async for index, event in interpolation_manager.stream(job, start_index):
            if event.get("type") == "ping":
                yield ": ping\n\n"
            else:
                yield f"id: {index}\ndata: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/interpolation/{job_id}/cancel")
async def cancel_interpolation(job_id: str) -> dict[str, bool]:
    job = interpolation_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Interpolation job not found.")
    await interpolation_manager.cancel(job)
    return {"cancelled": True}


@app.get("/api/interpolation/{job_id}/compounds/{compound_id}")
async def get_interpolation_compound(job_id: str, compound_id: str) -> dict:
    """Full series for one compound: original points, every method, verification."""
    job = interpolation_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Interpolation job not found.")
    entry = job.compounds.get(compound_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Compound not found in this job.")
    return {"compound_id": compound_id, **entry}


@app.get("/api/interpolation/{job_id}/excel")
async def download_interpolation_excel(job_id: str) -> FileResponse:
    job = interpolation_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Interpolation job not found.")
    if not job.excel_path or not Path(job.excel_path).is_file():
        raise HTTPException(status_code=409, detail="Results are not ready yet.")
    path = Path(job.excel_path)
    stem = Path(job.filename).stem
    return FileResponse(
        path,
        filename=f"interpolation_results_{stem}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------------------
# InChIKey → PubChem → structure
# ---------------------------------------------------------------------------
# None of this touches ChemDraw, so unlike the pipeline it runs anywhere. That
# is the point: the InChIKey half of the enrichment can be exercised and trusted
# on a Mac, while ChemDraw itself needs Windows and cannot even start there.
# The calls are blocking HTTP, so they go to a worker thread rather than
# stalling the event loop and every SSE stream hanging off it.


@app.get("/api/ai")
async def ai_status() -> dict:
    """Whether the curation pass is configured, and with which model."""
    return ai_curate.status()


@app.get("/api/ai/selftest")
async def ai_selftest() -> dict:
    """
    Put the curator through a mismatch whose answer is already known.

    Aspirin as drawn against salicylic acid from PubChem: same family, different
    formula, so a working curator must reject the match. Verifies the
    credentials, the JSON contract and the InChIKey guard on a machine that
    cannot run the pipeline at all. Costs one small-model request per call.
    """
    report = await run_in_threadpool(ai_curate.selftest)
    if not report.get("status", {}).get("enabled"):
        raise HTTPException(
            status_code=409, detail=report.get("message") or "Curation is not configured."
        )
    return report


@app.get("/api/inchikey/selftest")
async def inchikey_selftest(inchikey: str | None = None, save: bool = False) -> dict:
    """
    Run the whole InChIKey flow against a compound whose answer is already known.

    Push a known key into PubChem, pull the structure back, rebuild it in RDKit,
    and recompute the key: if it comes back changed, the structure the pipeline
    would have used is not the one it asked for. Defaults to aspirin; pass
    `?inchikey=` for any other key, and `?save=true` to keep the MOL file.
    """
    key = inchikey or pubchem.DEFAULT_SELFTEST_KEY
    save_path = str(INCHIKEY_DIR / f"{key}.mol") if save else None
    report = await run_in_threadpool(pubchem.selftest, key, save_path)
    report["chemdraw_required"] = False
    return report


@app.get("/api/inchikey/{inchikey}")
async def resolve_inchikey(inchikey: str, save: bool = False) -> dict:
    """
    Resolve one InChIKey through PubChem and hand back the rebuilt structure.

    Falls back to the 14-character skeleton when the full key misses, which is
    the common case for a drawing whose stereocentres were left flat — the
    response says which of the two matched, since they are not equally strong
    evidence.
    """
    key = normalize_inchikey(inchikey)
    if key is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{inchikey}' is not a valid InChIKey. Expected 14 letters, a hyphen, "
                "10 letters, a hyphen, then 1 letter — e.g. BSYNRYMUTXBXSQ-UHFFFAOYSA-N."
            ),
        )

    save_path = str(INCHIKEY_DIR / f"{key}.mol") if save else None
    report = await run_in_threadpool(pubchem.round_trip_inchikey, key, save_path)
    if not report.get("found"):
        raise HTTPException(status_code=404, detail=report.get("message") or "Not found in PubChem.")
    return report


class ExportedStaticFiles(StaticFiles):
    """
    Serves a Next.js static export.

    `next build` writes each route as `<route>.html` (interpolation.html), but
    StaticFiles only looks for `<route>/index.html`. This retries the `.html`
    sibling before giving up, so /interpolation resolves, and falls back to the
    exported 404 page for anything else.
    """

    async def get_response(self, path: str, scope):
        # With html=True Starlette *returns* the exported 404 page rather than
        # raising, so check the status as well as catching.
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            response = None

        if response is not None and response.status_code != 404:
            return response

        candidate = path.strip("/")
        if candidate and not candidate.endswith(".html"):
            try:
                retry = await super().get_response(f"{candidate}.html", scope)
                if retry.status_code == 200:
                    return retry
            except StarletteHTTPException:
                pass

        if response is not None:
            return response
        return await super().get_response("404.html", scope)


# `next build` (output: "export") emits the static UI into frontend/out
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend" / "out"
if FRONTEND_DIR.is_dir():
    app.mount("/", ExportedStaticFiles(directory=FRONTEND_DIR, html=True), name="ui")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
