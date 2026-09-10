import os
import platform
import subprocess
import sys
import threading
import time
from typing import Optional, Tuple

try:
    import winreg
except Exception:  # pragma: no cover - Windows-only import
    winreg = None

# The ProgID that last worked. Discovery enumerates every key under
# HKEY_CLASSES_ROOT — tens of thousands of them — and stage 3 connects once per
# molecule, so without this a 40-compound scheme pays for 40 identical registry
# sweeps before the first Dispatch is even attempted.
_cached_progid: str | None = None
_progid_lock = threading.Lock()


def _discover_progids() -> list[str]:
    candidates: list[str] = []

    # Preferred aliases seen across ChemDraw installs.
    candidates.extend(
        [
            "ChemDraw.Application",
            "ChemOffice.ChemDrawApp",
            "ChemDraw_x64.Application",
        ]
    )

    # Add versioned ProgIDs commonly registered by installers.
    for major in range(40, 9, -1):
        candidates.append(f"ChemDraw.Application.{major}")
        candidates.append(f"ChemDraw_x64.Application.{major}")

    # Pull additional COM classes directly from registry if available.
    if winreg is not None:
        for key_path in (
            "ChemDraw.Application",
            "ChemDraw_x64.Application",
            r"Wow6432Node\ChemDraw.Application",
            r"Wow6432Node\ChemDraw_x64.Application",
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key_path) as key:
                    cur_ver, _ = winreg.QueryValueEx(key, "CurVer")
                    if isinstance(cur_ver, str) and cur_ver.strip():
                        candidates.insert(0, cur_ver.strip())
            except Exception:
                pass

        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "") as root:
                i = 0
                while True:
                    subkey = winreg.EnumKey(root, i)
                    if subkey.lower().startswith("chemdraw") and ".application" in subkey.lower():
                        candidates.append(subkey)
                    i += 1
        except Exception:
            pass

    # De-duplicate while preserving order.
    seen = set()
    ordered: list[str] = []
    for p in candidates:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def connect_chemdraw() -> Tuple[object, str]:
    if sys.platform != "win32":
        raise RuntimeError(
            "ChemDraw COM automation requires Windows with ChemDraw installed. "
            f"This server is running {sys.platform}."
        )

    try:
        import win32com.client
    except ImportError as exc:
        raise RuntimeError("pywin32 is required to connect to ChemDraw COM.") from exc

    # asyncio/uvicorn threads are not a COM STA by default; without this,
    # Dispatch can fail with an empty error and the UI reports "not installed".
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:
        pass

    global _cached_progid

    # The ProgID that worked a moment ago is overwhelmingly likely to work now,
    # and Dispatch on an already-running ChemDraw is cheap. Only fall back to a
    # full discovery sweep if it stops working (ChemDraw was closed, upgraded).
    cached = _cached_progid
    if cached:
        try:
            return win32com.client.Dispatch(cached), cached
        except Exception:
            with _progid_lock:
                if _cached_progid == cached:
                    _cached_progid = None

    errors: list[str] = []
    for progid in _discover_progids():
        try:
            app = win32com.client.Dispatch(progid)
        except Exception as exc:
            errors.append(f"{progid}: {exc}")
            continue
        with _progid_lock:
            _cached_progid = progid
        return app, progid

    arch = platform.architecture()[0]
    detail = "; ".join(errors[:6]) if errors else "No ProgIDs tried."
    raise RuntimeError(
        f"Could not connect to ChemDraw COM. Python arch={arch}. "
        f"Tried ProgIDs: {detail}"
    )


def chemdraw_is_running() -> bool:
    """Whether a ChemDraw process exists, without starting one."""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ChemDraw.exe", "/NH"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return False
    return "chemdraw.exe" in out.lower()


def open_document_count() -> int:
    """How many documents ChemDraw currently holds open. -1 if it cannot be asked."""
    if not chemdraw_is_running():
        return 0
    try:
        app, _ = connect_chemdraw()
        return int(app.Documents.Count)
    except Exception:
        return -1


def reset_chemdraw(force: bool = False) -> dict:
    """
    Quit ChemDraw so the next Dispatch gets a clean instance.

    This exists because `Document.Close()` does not work on every ChemDraw
    build: it returns without error and leaves the document open, so a pipeline
    that opens one document per molecule leaks all of them. Once enough have
    piled up, `Documents.Open()` starts returning None and the next job dies in
    stage 1 with an AttributeError that says nothing about the real cause.

    Quitting is the only reliable reset. It is cheap — COM relaunches ChemDraw
    on the next Dispatch — and jobs are serialised through the ChemDraw queue,
    so no other job can be mid-document when this runs.
    """
    report = {"was_running": chemdraw_is_running(), "documents": 0,
              "quit": False, "killed": False, "error": None}
    if not report["was_running"]:
        return report

    try:
        app, _ = connect_chemdraw()
        try:
            report["documents"] = int(app.Documents.Count)
        except Exception:
            pass
        app.Quit()
        report["quit"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"

    # Give it a moment to go away, then insist if it did not.
    for _ in range(20):
        if not chemdraw_is_running():
            break
        time.sleep(0.25)
    else:
        if force or report["quit"]:
            try:
                subprocess.run(["taskkill", "/IM", "ChemDraw.exe", "/F"],
                               capture_output=True, timeout=20)
                report["killed"] = not chemdraw_is_running()
            except Exception as exc:
                report["error"] = report["error"] or f"kill failed: {exc}"

    global _cached_progid
    with _progid_lock:
        _cached_progid = None
    return report


def open_document(app, path: str):
    """
    `Documents.Open` that reports its own failure.

    ChemDraw returns None rather than raising when it will not open a file, and
    the caller then dies on `None.Activate()` — an AttributeError that names the
    wrong problem entirely.
    """
    target = os.path.abspath(path)
    if not os.path.isfile(target):
        raise RuntimeError(f"File not found: {target}")
    doc = app.Documents.Open(target)
    if doc is None:
        count = -1
        try:
            count = int(app.Documents.Count)
        except Exception:
            pass
        raise RuntimeError(
            f"ChemDraw refused to open {os.path.basename(target)} (Documents.Open returned "
            f"nothing). It currently holds {count} document(s) open; a stale instance is the "
            "usual cause. Restarting ChemDraw clears it."
        )
    return doc


# ---------------------------------------------------------------------------
# Representations straight out of ChemDraw
# ---------------------------------------------------------------------------

# The five representations gathered per molecule. ChemDraw is the authority on
# what is actually drawn, so every one of these is taken from ChemDraw itself
# rather than derived downstream — RDKit's opinion is recorded separately, and
# the pipeline is worth more when the two are independent.
FORMATS = ("smiles", "inchi", "inchikey", "molfile")

# ChemDraw exposes its clipboard/export formats by MIME type on the document's
# object collection — the same converters the Edit > Copy As menu drives. Which
# of these a given install answers to varies by version and by whether the InChI
# plug-in shipped with it, so every spelling is tried before the keystroke
# fallback is reached for.
FORMAT_MIME_TYPES = {
    "smiles": ("chemical/x-daylight-smiles", "chemical/daylight-smiles", "chemical/x-smiles"),
    "sln": ("chemical/x-sln", "chemical/x-sybyl-sln"),
    "inchi": ("chemical/x-inchi",),
    "inchikey": ("chemical/x-inchi-key", "chemical/x-inchikey"),
    "molfile": ("chemical/x-mdl-molfile", "chemical/x-mdl-molfile-v3000"),
}

FORMAT_LABELS = {
    "smiles": "SMILES",
    "sln": "SLN",
    "inchi": "InChI",
    "inchikey": "InChIKey",
    "molfile": "MOL text",
}


def _objects_data(doc, mime: str):
    """
    Read one export format off a document.

    ChemDraw's COM surface is not consistent across versions: some expose
    `Objects.Data(mime)` as a parameterised property, others only
    `Objects.GetData(mime)`, and pywin32 surfaces the difference as a plain
    AttributeError or TypeError. Both spellings are tried before giving up.
    """
    objects = doc.Objects
    for attempt in ("Data", "GetData"):
        try:
            accessor = getattr(objects, attempt)
        except Exception:
            continue
        try:
            value = accessor(mime)
        except Exception:
            continue
        if value:
            return str(value)
    return None


def _clean(fmt: str, raw: str | None) -> Optional[str]:
    """
    Tidy one raw representation into the form the rest of the pipeline expects.

    ChemDraw's exports are not uniform: a SMILES can arrive with a trailing name
    field, an InChI can be wrapped in surrounding chatter, and the clipboard adds
    its own line endings. MOL text is left exactly as it came, because it is a
    fixed-column format and stripping inner whitespace would corrupt it.
    """
    from inchi_tools import normalize_inchikey, parse_inchi_text

    if raw is None:
        return None
    text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return None

    if fmt == "molfile":
        return text.rstrip() + "\n"

    if fmt == "inchi":
        inchi, _key = parse_inchi_text(text)
        return inchi

    if fmt == "inchikey":
        _inchi, key = parse_inchi_text(text)
        if key:
            return key
        return normalize_inchikey(text.strip().split("\n")[0].strip())

    # SMILES and SLN are single-line notations. Some exports append a tab or
    # space separated title after the notation itself.
    first = next((line.strip() for line in text.split("\n") if line.strip()), "")
    if not first:
        return None
    if fmt == "smiles":
        return first.split()[0]
    return first


def export_formats_from_document(doc, app=None, allow_keys: bool = True) -> dict:
    """
    Gather every representation ChemDraw can give for an open document.

    Two routes, and **keystrokes are tried first**:

      "keys"  Edit > Copy As > <format>, driven by keystrokes and read off the
              clipboard. This is the route a chemist can verify by hand, so it
              is what the drawing is taken to mean. It needs the ChemDraw window
              raised, which is why the document is made visible for it.
      "com"   `Objects.Data(mime)` / `Objects.GetData(mime)`. Used for whatever
              the menu did not produce — a format greyed out for this selection,
              a failed window raise, a machine with no interactive desktop.

    The two hit the same internal converters, so they should agree; where they
    do not, the menu wins because that is what a chemist sees when they do it
    themselves. `route` records which one produced each value.

    Never raises. A format neither route produces comes back as None with its
    reason, and the compound still gets a row.
    """
    report: dict[str, dict] = {
        fmt: {"value": None, "route": None, "error": None, "attempts": []} for fmt in FORMATS
    }

    # ── Route 1: the Copy As menu ────────────────────────────────────────────
    if allow_keys:
        import chemdraw_keys

        usable, reason = chemdraw_keys.availability()
        if not usable:
            for fmt in FORMATS:
                report[fmt]["attempts"].append(f"keys: unavailable ({reason})")
        else:
            # Keystrokes go to whichever window is foreground, so ChemDraw has
            # to be on screen and focused before any are sent.
            try:
                if app is not None:
                    app.Visible = True
                doc.Activate()
            except Exception:
                pass

            results = chemdraw_keys.copy_many(list(FORMATS))
            for fmt in FORMATS:
                entry = report[fmt]
                raw, error = results.get(fmt, (None, "Keystroke route did not run."))
                if raw:
                    value = _clean(fmt, raw)
                    if value:
                        entry.update({"value": value, "route": "keys"})
                        entry["attempts"].append("keys: ok")
                        continue
                    entry["attempts"].append("keys: clipboard held nothing usable")
                else:
                    entry["attempts"].append(f"keys: {error}")
    else:
        for fmt in FORMATS:
            report[fmt]["attempts"].append("keys: disabled")

    missing = [fmt for fmt in FORMATS if not report[fmt]["value"]]
    if not missing:
        return report

    # ── Route 2: COM, for whatever the menu would not give up ────────────────
    for fmt in missing:
        entry = report[fmt]
        for mime in FORMAT_MIME_TYPES.get(fmt, ()):
            try:
                raw = _objects_data(doc, mime)
            except Exception as exc:
                entry["attempts"].append(f"com {mime}: error ({exc})")
                continue
            if not raw:
                entry["attempts"].append(f"com {mime}: not supported")
                continue
            value = _clean(fmt, raw)
            if value:
                entry.update({"value": value, "route": "com"})
                entry["attempts"].append(f"com {mime}: ok")
                break
            entry["attempts"].append(f"com {mime}: returned nothing usable")

        if not entry["value"] and not entry["error"]:
            entry["error"] = (
                f"Neither the Copy As menu nor COM produced a usable "
                f"{FORMAT_LABELS.get(fmt, fmt)}."
            )

    return report


def format_route_summary(formats: dict) -> str:
    """One cell describing which route produced what, e.g. `SMILES=com; InChI=keys`."""
    parts = []
    for fmt in FORMATS:
        entry = formats.get(fmt) or {}
        parts.append(f"{FORMAT_LABELS[fmt]}={entry.get('route') or 'none'}")
    return "; ".join(parts)
