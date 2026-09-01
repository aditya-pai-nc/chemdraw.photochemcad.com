"""
Pipeline worker process.
Reads one JSON command from stdin, runs the pipeline, writes JSON-lines to stdout.
Spawned by the FastAPI app — same protocol as the desktop PythonBridge.
"""
import sys
import json
import os
import traceback

# The worker inherits the API server's environment, but it is also launched on
# its own (--check-chemdraw, or by hand), so it loads the .env itself. This must
# precede any import that reads configuration — hence the local imports further
# down rather than module-level ones.
import config  # noqa: F401  (imported for its side effect)


def emit(obj: dict) -> None:
    """Write a JSON event line to stdout and flush immediately."""
    print(json.dumps(obj), flush=True)


def check_chemdraw() -> dict:
    try:
        from chemdraw_com import connect_chemdraw
        app, progid = connect_chemdraw()
        version = getattr(app, "Version", "unknown")
        try:
            app.Quit()
        except Exception:
            pass
        return {"type": "chemdraw_status", "available": True, "version": str(version), "progid": progid}
    except Exception as e:
        return {"type": "chemdraw_status", "available": False, "reason": str(e)}


def run_pipeline(cdx_path: str, output_dir: str) -> None:
    from pipeline import run_full_pipeline
    run_full_pipeline(cdx_path, output_dir, emit)


def main() -> None:
    if "--check-chemdraw" in sys.argv:
        result = check_chemdraw()
        emit(result)
        return

    raw = sys.stdin.readline().strip()
    if not raw:
        emit({"type": "error", "message": "No command received on stdin."})
        return

    try:
        cmd = json.loads(raw)
    except json.JSONDecodeError as e:
        emit({"type": "error", "message": f"Invalid JSON command: {e}"})
        return

    if cmd.get("cmd") == "process":
        cdx_path = cmd.get("cdx_path", "")
        output_dir = cmd.get("output_dir", "")
        if not cdx_path or not output_dir:
            emit({"type": "error", "message": "cdx_path and output_dir are required."})
            return
        if not os.path.isfile(cdx_path):
            emit({"type": "error", "message": f"CDX file not found: {cdx_path}"})
            return
        try:
            run_pipeline(cdx_path, output_dir)
        except Exception as e:
            emit({"type": "error", "message": f"Pipeline error: {e}\n{traceback.format_exc()}"})
    else:
        emit({"type": "error", "message": f"Unknown command: {cmd.get('cmd')}"})


if __name__ == "__main__":
    main()
