"""
Environment loading, done before anything else imports.

The backend reads its configuration from environment variables, which is fine on
a developer's shell but awkward where this actually runs: the README has uvicorn
started from Windows Task Scheduler "At log on", and a scheduled task inherits
almost nothing. A `.env` file beside the code is the practical way to give the
server an API key there.

Import this module **first**, before `ai_identify` or anything else that reads
configuration at import time — several of those values become module constants
the moment they are read, so a `.env` loaded afterwards would arrive too late.
Real environment variables always win over the file, so a shell export or a
CI secret still overrides it.
"""
from __future__ import annotations

import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
ENV_PATH = Path(os.environ.get("CHEMDRAW_ENV_FILE", BACKEND_DIR / ".env"))

_loaded = False


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    if "=" not in line:
        return None
    key, _, value = line.partition("=")
    key, value = key.strip(), value.strip()
    if not key:
        return None
    # Strip one layer of matching quotes, which is how a value with spaces or a
    # trailing comment is usually written.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def load_env(path: Path | None = None) -> bool:
    """
    Load `backend/.env` into the process environment. Returns whether a file was read.

    Uses python-dotenv when it is installed and falls back to a small parser
    otherwise, so a missing optional dependency cannot stop the server from
    starting — it would only mean the API key has to come from the shell.
    """
    global _loaded
    target = Path(path) if path else ENV_PATH
    if _loaded and path is None:
        return True
    if not target.is_file():
        return False

    try:
        from dotenv import load_dotenv
        # override=False: a real environment variable beats the file.
        load_dotenv(target, override=False)
    except ImportError:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parsed = _parse_line(line)
                if parsed and parsed[0] not in os.environ:
                    os.environ[parsed[0]] = parsed[1]

    if path is None:
        _loaded = True
    return True


# Loading on import is the point: every entry point gets the same configuration
# just by importing this module before it reads any setting.
load_env()
