"""
Job manager: save uploads, queue ChemDraw access, spawn the pipeline worker,
fan out JSON-line events over SSE.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

BACKEND_DIR = Path(__file__).resolve().parent
WORKER_PATH = BACKEND_DIR / "worker.py"
DATA_DIR = Path(os.environ.get("CHEMDRAW_DATA_DIR", BACKEND_DIR / "data" / "jobs"))
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# How many jobs may sit in the system at once (the running one plus the line
# behind it). Beyond this uploads are refused rather than queued forever.
MAX_QUEUE_DEPTH = int(os.environ.get("CHEMDRAW_MAX_QUEUE", "20"))


class QueueFullError(RuntimeError):
    """Raised when the ChemDraw line is already at MAX_QUEUE_DEPTH."""


def _python_executable() -> str:
    venv_unix = BACKEND_DIR / "venv" / "bin" / "python"
    venv_win = BACKEND_DIR / "venv" / "Scripts" / "python.exe"
    if venv_win.exists():
        return str(venv_win)
    if venv_unix.exists():
        return str(venv_unix)
    return sys.executable


def _sanitize_filename(name: str) -> str:
    base = Path(name).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    if not base.lower().endswith(".cdx"):
        base = (base or "upload") + ".cdx"
    return base[:120]


def _queue_message(position: int, depth: int) -> str:
    if position <= 0:
        return "ChemDraw is free — starting now…"
    ahead = "1 job" if position == 1 else f"{position} jobs"
    return f"Waiting for ChemDraw — {ahead} ahead of you ({depth} in the queue)."


# eq=False so the queue's identity checks (deque.index / deque.remove) compare
# by object identity rather than by field values.
@dataclass(eq=False)
class Job:
    id: str
    filename: str
    cdx_path: Path
    output_dir: Path
    status: str = "queued"  # queued | running | done | error | cancelled
    created_at: float = field(default_factory=time.time)
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    cancelled: bool = False
    excel_path: Optional[str] = None
    compound_count: int = 0
    proc: Optional[asyncio.subprocess.Process] = None
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    # Last queue position sent to the client, so the same one is not repeated.
    announced_position: int = -2

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "filename": self.filename,
            "status": self.status,
            "created_at": self.created_at,
            "done": self.done,
            "cancelled": self.cancelled,
            "excel_path": self.excel_path,
            "compound_count": self.compound_count,
            "has_excel": bool(self.excel_path and Path(self.excel_path).is_file()),
            "has_output": self.output_dir.is_dir(),
        }


class ChemDrawQueue:
    """
    A FIFO line for ChemDraw.

    ChemDraw is a single desktop application driven over COM — two pipelines
    running at once would fight over the same instance, opening and closing each
    other's documents. So every job waits its turn here and one drain task runs
    them strictly one at a time. Whenever the line moves, the jobs still waiting
    are told their new position.
    """

    def __init__(
        self,
        runner: Callable[[Job], Awaitable[None]],
        on_position: Callable[[Job, int, int], Awaitable[None]],
    ) -> None:
        self._runner = runner
        self._on_position = on_position
        self._pending: deque[Job] = deque()
        self._current: Optional[Job] = None
        self._wakeup = asyncio.Event()
        # Held on the instance so the drain task is never garbage-collected.
        self._drainer: Optional[asyncio.Task[None]] = None

    @property
    def current(self) -> Optional[Job]:
        return self._current

    def depth(self) -> int:
        """Jobs in the system: the one holding ChemDraw plus everyone waiting."""
        return len(self._pending) + (1 if self._current is not None else 0)

    def position_of(self, job: Job) -> int:
        """
        How many jobs must finish before this one starts.
        0 = it holds ChemDraw now, -1 = it is not in the line at all.
        """
        if self._current is job:
            return 0
        try:
            ahead = self._pending.index(job)
        except ValueError:
            return -1
        return ahead + (1 if self._current is not None else 0)

    def waiting(self) -> list[Job]:
        return list(self._pending)

    def submit(self, job: Job) -> int:
        """Join the back of the line. Returns the job's position."""
        if self.depth() >= MAX_QUEUE_DEPTH:
            raise QueueFullError(
                f"The ChemDraw queue is full ({MAX_QUEUE_DEPTH} jobs). Try again shortly."
            )

        self._pending.append(job)

        # Lazily started: there is no running event loop when JobManager is
        # constructed at import time, only once a request is being handled.
        if self._drainer is None or self._drainer.done():
            self._drainer = asyncio.create_task(self._drain())
        self._wakeup.set()

        return self.position_of(job)

    def remove(self, job: Job) -> bool:
        """Drop a still-waiting job out of the line. False if it already started."""
        try:
            self._pending.remove(job)
            return True
        except ValueError:
            return False

    async def announce(self) -> None:
        """Tell everyone still waiting where they now stand."""
        depth = self.depth()
        for job in list(self._pending):
            await self._on_position(job, self.position_of(job), depth)

    async def _drain(self) -> None:
        while True:
            if not self._pending:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue

            job = self._pending.popleft()
            self._current = job
            # The line just moved up by one.
            await self.announce()
            try:
                await self._runner(job)
            except asyncio.CancelledError:
                self._current = None
                raise
            except Exception:
                # A crash must never kill the drain task, or every queued job
                # would wait forever. The runner reports its own failures.
                traceback.print_exc(file=sys.stderr)
            finally:
                self._current = None


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.queue = ChemDrawQueue(runner=self._run_job, on_position=self._emit_position)

    def get(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def snapshot(self, job: Job) -> dict[str, Any]:
        data = job.snapshot()
        data["queue_position"] = self.queue.position_of(job)
        data["queue_depth"] = self.queue.depth()
        return data

    def queue_snapshot(self) -> dict[str, Any]:
        current = self.queue.current
        return {
            "depth": self.queue.depth(),
            "max_depth": MAX_QUEUE_DEPTH,
            "running": (
                {"job_id": current.id, "filename": current.filename} if current else None
            ),
            "waiting": [
                {
                    "job_id": job.id,
                    "filename": job.filename,
                    "position": self.queue.position_of(job),
                }
                for job in self.queue.waiting()
            ],
        }

    async def create(self, filename: str, data: bytes) -> Job:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("File is larger than the 50 MB upload limit.")

        safe_name = _sanitize_filename(filename)
        job_id = uuid.uuid4().hex
        job_dir = DATA_DIR / job_id
        input_dir = job_dir / "input"
        output_dir = job_dir / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        cdx_path = input_dir / safe_name
        cdx_path.write_bytes(data)

        job = Job(
            id=job_id,
            filename=safe_name,
            cdx_path=cdx_path,
            output_dir=output_dir,
        )
        self.jobs[job_id] = job
        return job

    async def submit(self, job: Job) -> int:
        """Queue a job for ChemDraw. Raises QueueFullError if the line is full."""
        position = self.queue.submit(job)
        if position > 0:
            await self._emit_position(job, position, self.queue.depth())
        return position

    async def emit(self, job: Job, event: dict[str, Any]) -> None:
        async with job.condition:
            job.events.append(event)
            if event.get("type") == "result":
                job.excel_path = event.get("excelPath")
                job.compound_count = int(event.get("compoundCount") or 0)
                job.done = True
                job.status = "done"
            elif event.get("type") == "error":
                job.done = True
                job.status = "cancelled" if job.cancelled else "error"
            job.condition.notify_all()

    async def _emit_position(self, job: Job, position: int, depth: int) -> None:
        if job.announced_position == position:
            return
        job.announced_position = position
        await self.emit(job, {
            "type": "queue",
            "position": position,
            "depth": depth,
            "message": _queue_message(position, depth),
        })

    async def _run_job(self, job: Job) -> None:
        """Called by the queue when this job's turn comes up."""
        if job.cancelled:
            await self.emit(job, {"type": "error", "message": "Job was cancelled."})
            return
        job.status = "running"
        await self._run_worker(job)

    async def _run_worker(self, job: Job) -> None:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        try:
            proc = await asyncio.create_subprocess_exec(
                _python_executable(),
                str(WORKER_PATH),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(BACKEND_DIR),
                env=env,
            )
        except Exception as exc:
            await self.emit(job, {"type": "error", "message": f"Failed to start pipeline worker: {exc}"})
            return

        job.proc = proc
        cmd = json.dumps({
            "cmd": "process",
            "cdx_path": str(job.cdx_path),
            "output_dir": str(job.output_dir),
        })

        assert proc.stdin is not None
        proc.stdin.write((cmd + "\n").encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()

        async def read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    await self.emit(job, {"type": "log", "level": "warn", "message": text})

        stderr_task = asyncio.create_task(read_stderr())

        assert proc.stdout is not None
        buffer = ""
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            lines = buffer.split("\n")
            buffer = lines.pop() or ""
            for line in lines:
                trimmed = line.strip()
                if not trimmed:
                    continue
                try:
                    event = json.loads(trimmed)
                    await self.emit(job, event)
                except json.JSONDecodeError:
                    await self.emit(job, {"type": "log", "level": "info", "message": trimmed})

        if buffer.strip():
            trimmed = buffer.strip()
            try:
                await self.emit(job, json.loads(trimmed))
            except json.JSONDecodeError:
                await self.emit(job, {"type": "log", "level": "info", "message": trimmed})

        await stderr_task
        code = await proc.wait()
        job.proc = None

        if not job.done:
            if job.cancelled:
                await self.emit(job, {"type": "error", "message": "Job was cancelled."})
            elif code not in (0, None):
                await self.emit(job, {"type": "error", "message": f"Pipeline worker exited with code {code}."})
            else:
                await self.emit(job, {"type": "error", "message": "Pipeline ended without a result."})

    async def cancel(self, job: Job) -> None:
        job.cancelled = True

        # Still waiting its turn: leave the line without ever touching ChemDraw,
        # then shuffle everyone behind it up one place.
        if self.queue.remove(job):
            await self.emit(job, {
                "type": "error",
                "message": "Job was cancelled while waiting in the queue.",
            })
            await self.queue.announce()
            return

        if job.proc and job.proc.returncode is None:
            job.proc.kill()
            try:
                await job.proc.wait()
            except Exception:
                pass

    async def stream(
        self, job: Job, start_index: int = 0
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """
        Replay this job's events, then follow it live.

        Yields (index, event). The index becomes the SSE event id, so a browser
        that silently reconnects sends it back as Last-Event-ID and resumes from
        there instead of replaying the whole job — otherwise every compound and
        log line would arrive a second time.
        """
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


async def check_chemdraw() -> dict[str, Any]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        proc = await asyncio.create_subprocess_exec(
            _python_executable(),
            str(WORKER_PATH),
            "--check-chemdraw",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BACKEND_DIR),
            env=env,
        )
    except Exception as exc:
        return {"available": False, "reason": str(exc)}

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"available": False, "reason": "Timed out while checking ChemDraw availability."}

    output = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace").strip()
    lines = [line.strip() for line in output.split("\n") if line.strip()]
    for line in reversed(lines):
        try:
            result = json.loads(line)
            return {
                "available": bool(result.get("available")),
                "version": result.get("version"),
                "progid": result.get("progid"),
                "reason": result.get("reason"),
            }
        except json.JSONDecodeError:
            continue

    return {
        "available": False,
        "reason": err or output.strip() or "Could not parse ChemDraw check output from Python backend.",
    }
