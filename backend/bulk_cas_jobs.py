"""Background CAS batches, independent of the ChemDraw desktop queue."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import time
import uuid

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from bulk_cas import BulkExtractor, MAX_UPLOAD_BYTES, parse_upload, write_exports

DATA_DIR = Path(os.environ.get("CHEMDRAW_BULK_CAS_DIR", Path(__file__).resolve().parent / "data" / "bulk_cas"))
TERMINAL = {"done", "cancelled", "error"}


@dataclass
class BulkJob:
    id: str
    filename: str
    entries: list[dict]
    folder: Path
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    rows: list[dict] = field(default_factory=list)
    cancel_requested: bool = False
    error: str | None = None
    exports_ready: bool = False

    def snapshot(self):
        return {"job_id": self.id, "filename": self.filename, "status": self.status,
                "created_at": self.created_at, "total": len(self.entries), "processed": len(self.rows),
                "valid": sum(bool(e["cas_rn"]) for e in self.entries),
                "unique": len({e["cas_rn"] for e in self.entries if e["cas_rn"]}),
                "rows": self.rows.copy(), "cancel_requested": self.cancel_requested,
                "error": self.error, "has_exports": self.exports_ready}


class BulkManager:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = data_dir
        self.jobs: dict[str, BulkJob] = {}
        self.semaphore = asyncio.Semaphore(1)
        self.tasks: set[asyncio.Task] = set()

    def folder(self, job_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise HTTPException(404, "Bulk CAS job not found.")
        return self.data_dir / job_id

    async def create(self, filename: str, data: bytes) -> BulkJob:
        entries = await asyncio.to_thread(parse_upload, filename, data)
        if sum(j.status not in TERMINAL for j in self.jobs.values()) >= 10:
            raise HTTPException(503, "The bulk extractor queue is full. Please try again later.")
        job_id = uuid.uuid4().hex
        folder = self.folder(job_id)
        folder.mkdir(parents=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)[:120] or "cas_list.txt"
        job = BulkJob(job_id, safe_name, entries, folder)
        self.jobs[job_id] = job
        await asyncio.to_thread(self.persist, job)
        task = asyncio.create_task(self.run(job))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return job

    def persist(self, job: BulkJob):
        temp = job.folder / "snapshot.tmp"
        temp.write_text(json.dumps(job.snapshot(), ensure_ascii=False), encoding="utf-8")
        temp.replace(job.folder / "snapshot.json")

    def snapshot(self, job_id: str) -> dict:
        folder = self.folder(job_id)
        if job_id in self.jobs:
            return self.jobs[job_id].snapshot()
        path = folder / "snapshot.json"
        if not path.is_file():
            raise HTTPException(404, "Bulk CAS job not found.")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if snapshot["status"] not in TERMINAL:
            snapshot.update(status="error", error="The server restarted before this batch finished. Please upload the file again.")
        return snapshot

    async def finish(self, job: BulkJob, status: str):
        await asyncio.to_thread(write_exports, job.rows, job.folder, len(job.entries), status)
        job.exports_ready = True
        job.status = status
        await asyncio.to_thread(self.persist, job)

    async def run(self, job: BulkJob):
        try:
            async with self.semaphore:
                if job.status in TERMINAL:
                    return
                job.status = "running"
                extractor = BulkExtractor()
                for entry in job.entries:
                    if job.cancel_requested:
                        break
                    row = await asyncio.to_thread(extractor.extract, entry)
                    job.rows.append(row)
                    await asyncio.to_thread(self.persist, job)
                await self.finish(job, "cancelled" if job.cancel_requested else "done")
        except Exception:
            job.status = "error"
            job.error = "This batch could not be completed. Results already retrieved are shown below."
            await asyncio.to_thread(self.persist, job)

    async def cancel(self, job_id: str) -> dict:
        snapshot = self.snapshot(job_id)
        if snapshot["status"] in TERMINAL:
            return snapshot
        job = self.jobs[job_id]
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
            await self.finish(job, "cancelled")
        return job.snapshot()


manager = BulkManager()
router = APIRouter(prefix="/api/bulk-cas", tags=["Bulk CAS extractor"])


@router.get("/sample.csv")
async def sample_file():
    return Response("CAS RN\r\n50-78-2\r\n58-08-2\r\n64-17-5\r\n", media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="sample_cas_numbers.csv"'})


@router.post("")
async def create_batch(file: UploadFile = File(...)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    try:
        job = await manager.create(file.filename or "cas_list.txt", data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return job.snapshot()


@router.get("/{job_id}")
async def get_batch(job_id: str):
    return manager.snapshot(job_id)


@router.post("/{job_id}/cancel")
async def cancel_batch(job_id: str):
    return await manager.cancel(job_id)


@router.get("/{job_id}/download/{format}")
async def download_batch(job_id: str, format: str):
    if format not in {"xlsx", "csv"}:
        raise HTTPException(404, "Download format not found.")
    snapshot = manager.snapshot(job_id)
    if not snapshot["has_exports"]:
        raise HTTPException(409, "Downloads will be ready when the batch finishes or stops.")
    path = manager.folder(job_id) / f"bulk_cas_results.{format}"
    if not path.is_file():
        raise HTTPException(404, "The result file is missing.")
    media = "text/csv" if format == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return FileResponse(path, filename=path.name, media_type=media)
