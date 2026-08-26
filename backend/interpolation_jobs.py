"""
Interpolation jobs: parse an uploaded spectrum file, run every interpolation
method over each compound, verify the result, and stream progress over SSE.

Unlike the ChemDraw pipeline this needs no ChemDraw and no Windows, so it does
NOT go through the ChemDraw queue — it would be wrong to make a pure-numeric job
wait behind a desktop application. The heavy numeric work is handed to worker
threads so the event loop (and every other client's live log) stays responsive.
"""
from __future__ import annotations

import asyncio
import os
import re
import uuid
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import pandas as pd

import interpolation

BACKEND_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CHEMDRAW_INTERP_DIR", BACKEND_DIR / "data" / "interpolation"))
MAX_UPLOAD_BYTES = 16 * 1024 * 1024  # matches the original Flask app's limit
ALLOWED_EXTENSIONS = {"txt", "tsv", "csv", "xlsx", "xls"}
METHODS = ["cubic_spline", "akima_spline", "linear", "rbf", "gmm"]

# How many interpolation jobs may crunch numbers at once. GMM fitting is CPU
# bound, so this is deliberately small for a minimum-spec server.
MAX_CONCURRENT = int(os.environ.get("CHEMDRAW_INTERP_CONCURRENCY", "2"))


def _sanitize_filename(name: str) -> str:
    base = Path(name).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return (base or "upload")[:120]


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def parse_interpolation_file(file_path: str) -> dict:
    """
    Parse an uploaded file and extract data for interpolation.

    Ported from ChemDrawAutomationScripts/app.py — same column conventions:
    3+ columns means (id, wavelength, coefficient); 2 columns means
    (wavelength, coefficient) with the compound id taken from the filename.
    """
    ext = file_path.rsplit(".", 1)[1].lower()

    try:
        if ext == "csv":
            df = pd.read_csv(file_path)
        elif ext in ("tsv", "txt"):
            # Try to detect separator
            with open(file_path, "r") as f:
                first_line = f.readline()
            if "\t" in first_line:
                df = pd.read_csv(file_path, sep="\t")
            else:
                df = pd.read_csv(file_path)
        elif ext in ("xlsx", "xls"):
            df = pd.read_excel(file_path)
        else:
            raise ValueError(f"Unsupported file format: {ext}")

        if len(df.columns) < 2:
            raise ValueError(
                "File must have at least 2 columns: wavelength/absorption/emission, coefficient"
            )

        original_col_count = len(df.columns)

        if len(df.columns) >= 3:
            # 3+ columns: first is ID, second is wavelength, third is coefficient
            id_col = df.columns[0]
            wavelength_col = df.columns[1]
            coefficient_col = df.columns[2]

            df = df[[id_col, wavelength_col, coefficient_col]].dropna()
            df[wavelength_col] = pd.to_numeric(df[wavelength_col], errors="coerce")
            df[coefficient_col] = pd.to_numeric(df[coefficient_col], errors="coerce")
            df = df.dropna()
        else:
            # 2 columns: wavelength, coefficient — compound id comes from the filename
            wavelength_col = df.columns[0]
            coefficient_col = df.columns[1]

            df = df[[wavelength_col, coefficient_col]].dropna()
            df[wavelength_col] = pd.to_numeric(df[wavelength_col], errors="coerce")
            df[coefficient_col] = pd.to_numeric(df[coefficient_col], errors="coerce")
            df = df.dropna()

            filename_base = os.path.splitext(os.path.basename(file_path))[0]
            id_col = "compound_id"
            df[id_col] = filename_base
            df = df[[id_col, wavelength_col, coefficient_col]]

        return {
            "data": df,
            "id_column": id_col,
            "wavelength_column": wavelength_col,
            "coefficient_column": coefficient_col,
            "file_format": "3_column" if original_col_count >= 3 else "2_column",
        }

    except Exception as e:
        raise ValueError(f"Error parsing file: {e}") from e


@dataclass(eq=False)
class InterpolationJob:
    id: str
    filename: str
    source_path: Path
    output_dir: Path
    step_size: float
    status: str = "running"  # running | done | error | cancelled
    created_at: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    cancelled: bool = False
    excel_path: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # compound_id -> everything the charts and tables need
    compounds: dict[str, dict[str, Any]] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "filename": self.filename,
            "status": self.status,
            "created_at": self.created_at,
            "done": self.done,
            "cancelled": self.cancelled,
            "step_size": self.step_size,
            "has_excel": bool(self.excel_path and Path(self.excel_path).is_file()),
            "metadata": self.metadata,
            "compounds": [self.compound_summary(cid) for cid in self.order],
        }

    def compound_summary(self, compound_id: str) -> dict[str, Any]:
        entry = self.compounds[compound_id]
        return {
            "compound_id": compound_id,
            "original_points": entry["original_points"],
            "interpolated_points": entry["interpolated_points"],
            "additional_points": entry["interpolated_points"] - entry["original_points"],
            "summary": entry["summary"],
            "best_method": entry["best_method"],
        }


class InterpolationManager:
    def __init__(self) -> None:
        self.jobs: dict[str, InterpolationJob] = {}
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._tasks: set[asyncio.Task[None]] = set()

    def get(self, job_id: str) -> Optional[InterpolationJob]:
        return self.jobs.get(job_id)

    async def create(self, filename: str, data: bytes, step_size: float) -> InterpolationJob:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("File is larger than the 16 MB upload limit.")
        if not allowed_file(filename):
            raise ValueError("Invalid file type. Please upload a TXT, TSV, CSV, or Excel file.")
        if not (0.01 <= step_size <= 100):
            raise ValueError("Step size must be between 0.01 and 100.")

        safe_name = _sanitize_filename(filename)
        job_id = uuid.uuid4().hex
        job_dir = DATA_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        source_path = job_dir / safe_name
        source_path.write_bytes(data)

        job = InterpolationJob(
            id=job_id,
            filename=safe_name,
            source_path=source_path,
            output_dir=job_dir,
            step_size=step_size,
        )
        self.jobs[job_id] = job
        return job

    def start(self, job: InterpolationJob) -> None:
        # Strong reference kept: asyncio only holds weak refs to tasks.
        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def emit(self, job: InterpolationJob, event: dict[str, Any]) -> None:
        async with job.condition:
            job.events.append(event)
            if event.get("type") == "result":
                job.done = True
                job.status = "done"
            elif event.get("type") == "error":
                job.done = True
                job.status = "cancelled" if job.cancelled else "error"
            job.condition.notify_all()

    async def cancel(self, job: InterpolationJob) -> None:
        job.cancelled = True
        if not job.done:
            await self.emit(job, {"type": "error", "message": "Job was cancelled."})

    async def _run(self, job: InterpolationJob) -> None:
        try:
            if self._semaphore.locked():
                await self.emit(job, {
                    "type": "log",
                    "level": "info",
                    "message": "Waiting for a free worker…",
                })
            async with self._semaphore:
                if job.cancelled:
                    return
                await self._process(job)
        except Exception as exc:  # never let a job die silently
            if not job.done:
                await self.emit(job, {"type": "error", "message": f"Interpolation failed: {exc}"})

    async def _process(self, job: InterpolationJob) -> None:
        await self.emit(job, {
            "type": "stage", "stage": 1, "total": 3,
            "message": f"Parsing {job.filename}…",
        })

        try:
            parsed = await asyncio.to_thread(parse_interpolation_file, str(job.source_path))
        except ValueError as exc:
            await self.emit(job, {"type": "error", "message": str(exc)})
            return

        df = parsed["data"]
        id_col = parsed["id_column"]
        wavelength_col = parsed["wavelength_column"]
        coefficient_col = parsed["coefficient_column"]
        compound_ids = list(df[id_col].unique())

        if not compound_ids:
            await self.emit(job, {"type": "error", "message": "No valid data found for interpolation."})
            return

        job.metadata = {
            "filename": job.filename,
            "step_size": job.step_size,
            "id_column": str(id_col),
            "wavelength_column": str(wavelength_col),
            "coefficient_column": str(coefficient_col),
            "num_compounds": len(compound_ids),
            "file_format": parsed["file_format"],
        }

        await self.emit(job, {
            "type": "stage", "stage": 2, "total": 3,
            "message": f"Interpolating {len(compound_ids)} compound(s) with 5 methods…",
        })

        all_results: list[pd.DataFrame] = []

        for index, compound_id in enumerate(compound_ids, 1):
            if job.cancelled:
                return

            key = str(compound_id)
            compound_data = df[df[id_col] == compound_id].copy().sort_values(wavelength_col)
            x = compound_data[wavelength_col].values
            y = compound_data[coefficient_col].values

            try:
                interpolated = await asyncio.to_thread(
                    interpolation.interpolate_methods_fixed, x, y, key, job.step_size
                )
            except Exception as exc:
                await self.emit(job, {
                    "type": "log", "level": "error",
                    "message": f"Interpolation failed for {key}: {exc}",
                })
                continue

            all_results.append(interpolated)

            # Verification: predict the original points using ONLY the generated
            # ones (VERIFICATION_TECHNIQUE.md), then rank the methods by MSE.
            summary: list[dict[str, Any]] = []
            plot_data: dict[str, Any] = {}
            try:
                verification = await asyncio.to_thread(
                    interpolation.verify_interpolation_quality_generated_only,
                    x, y, interpolated, key,
                )
                summary_df = await asyncio.to_thread(
                    interpolation.create_verification_summary, verification
                )
                summary = summary_df.to_dict("records")
                plot_data = verification["plot_data"]
            except Exception as exc:
                await self.emit(job, {
                    "type": "log", "level": "warn",
                    "message": f"Verification failed for {key}: {exc}",
                })

            best_method = summary[0]["Method"] if summary else None

            job.compounds[key] = {
                "original": {
                    "x": [float(v) for v in x],
                    "y": [float(v) for v in y],
                },
                "wavelength": [float(v) for v in interpolated["wavelength"].values],
                "methods": {
                    m: [float(v) for v in interpolated[m].values]
                    for m in METHODS if m in interpolated.columns
                },
                "verification": plot_data,
                "summary": summary,
                "best_method": best_method,
                "original_points": int(len(x)),
                "interpolated_points": int(len(interpolated)),
            }
            job.order.append(key)

            await self.emit(job, {
                "type": "compound",
                "compound_id": key,
                "index": index,
                "total": len(compound_ids),
                "original_points": int(len(x)),
                "interpolated_points": int(len(interpolated)),
                "best_method": best_method,
            })

        if not all_results:
            await self.emit(job, {"type": "error", "message": "No valid data found for interpolation."})
            return

        await self.emit(job, {
            "type": "stage", "stage": 3, "total": 3,
            "message": "Writing the Excel workbook…",
        })

        combined = pd.concat(all_results, ignore_index=True)
        excel_path = job.output_dir / "interpolation_results.xlsx"
        await asyncio.to_thread(combined.to_excel, excel_path, index=False)
        job.excel_path = str(excel_path)

        await self.emit(job, {
            "type": "result",
            "success": True,
            "compoundCount": len(job.order),
            "pointCount": int(len(combined)),
        })

    async def stream(
        self, job: InterpolationJob, start_index: int = 0
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """Replay then follow this job's events. See JobManager.stream."""
        index = max(0, start_index)
        while True:
            event: Optional[dict[str, Any]] = None
            emitted_index = index
            async with job.condition:
                if index < len(job.events):
                    event = job.events[index]
                    emitted_index = index
                    index += 1
                elif job.done:
                    return
                else:
                    try:
                        await asyncio.wait_for(job.condition.wait(), timeout=15)
                        continue
                    except asyncio.TimeoutError:
                        event = {"type": "ping"}
            if event is None:
                continue
            yield emitted_index, event
            if event.get("type") in ("result", "error"):
                return
