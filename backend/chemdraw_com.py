import platform
import sys
from typing import Tuple

try:
    import winreg
except Exception:  # pragma: no cover - Windows-only import
    winreg = None


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

    errors: list[str] = []
    for progid in _discover_progids():
        try:
            app = win32com.client.Dispatch(progid)
            return app, progid
        except Exception as exc:
            errors.append(f"{progid}: {exc}")

    arch = platform.architecture()[0]
    detail = "; ".join(errors[:6]) if errors else "No ProgIDs tried."
    raise RuntimeError(
        f"Could not connect to ChemDraw COM. Python arch={arch}. "
        f"Tried ProgIDs: {detail}"
    )


# ---------------------------------------------------------------------------
# InChI straight out of ChemDraw
# ---------------------------------------------------------------------------

# ChemDraw exposes its clipboard/export formats by MIME type on the document's
# object collection. Which of these a given install answers to varies by version
# and by whether the InChI plug-in shipped with it, so every one is tried.
CHEMDRAW_INCHI_MIME_TYPES = ("chemical/x-inchi", "chemical/x-inchi-key")
CHEMDRAW_MOLFILE_MIME_TYPES = ("chemical/x-mdl-molfile", "chemical/x-mdl-molfile-v3000")


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


def export_inchi_from_document(doc, scratch_inchi_path: str | None = None) -> dict:
    """
    Get ChemDraw's own InChI for an open document.

    This exists so the workbook can carry two *independently produced* InChIKeys
    for the same drawing — ChemDraw's and RDKit's. When they agree, the structure
    survived the ChemDraw → MOL → RDKit handoff intact. When they disagree, the
    handoff lost or changed something, and that is worth knowing before any of
    the downstream matching is believed.

    Three routes are tried, strongest first, and the one that worked is recorded
    in `source` — because the third route is not really an independent opinion
    and a researcher must be able to tell:

      "chemdraw-inchi"   ChemDraw's InChI export. Genuinely independent.
      "chemdraw-file"    ChemDraw's Save As .inchi. Also independent.
      "chemdraw-molfile" ChemDraw's MOL, converted by RDKit. NOT independent —
                         it shares RDKit's InChI generator, so agreement with
                         the RDKit column proves nothing about ChemDraw.

    Never raises: a document ChemDraw will not export an InChI for still has to
    produce a usable row.
    """
    import os

    from inchi_tools import inchi_from_molblock, inchikey_from_inchi, parse_inchi_text

    report = {"inchi": None, "inchikey": None, "source": None, "attempts": [], "error": None}

    def record(route: str, outcome: str) -> None:
        report["attempts"].append(f"{route}: {outcome}")

    # 1. ChemDraw's InChI export.
    for mime in CHEMDRAW_INCHI_MIME_TYPES:
        try:
            raw = _objects_data(doc, mime)
        except Exception as exc:
            record(mime, f"error ({exc})")
            continue
        if not raw:
            record(mime, "not supported")
            continue
        inchi, key = parse_inchi_text(raw)
        if inchi or key:
            report.update({"inchi": inchi, "inchikey": key or inchikey_from_inchi(inchi),
                           "source": "chemdraw-inchi"})
            record(mime, "ok")
            return report
        record(mime, "returned nothing usable")

    # 2. Save As .inchi, then read the file back.
    if scratch_inchi_path:
        try:
            doc.SaveAs(os.path.abspath(scratch_inchi_path))
            if os.path.isfile(scratch_inchi_path):
                with open(scratch_inchi_path, "r", encoding="utf-8", errors="replace") as fh:
                    inchi, key = parse_inchi_text(fh.read())
                if inchi or key:
                    report.update({"inchi": inchi, "inchikey": key or inchikey_from_inchi(inchi),
                                   "source": "chemdraw-file"})
                    record("SaveAs .inchi", "ok")
                    return report
                record("SaveAs .inchi", "file held no InChI")
            else:
                record("SaveAs .inchi", "no file written")
        except Exception as exc:
            record("SaveAs .inchi", f"error ({exc})")

    # 3. ChemDraw's MOL block through RDKit. Weakest route — see the docstring.
    for mime in CHEMDRAW_MOLFILE_MIME_TYPES:
        try:
            molblock = _objects_data(doc, mime)
        except Exception as exc:
            record(mime, f"error ({exc})")
            continue
        if not molblock:
            record(mime, "not supported")
            continue
        inchi, key, warning = inchi_from_molblock(molblock)
        if inchi:
            report.update({"inchi": inchi, "inchikey": key, "source": "chemdraw-molfile",
                           "error": warning})
            record(mime, "ok (via RDKit — not an independent InChI)")
            return report
        record(mime, f"RDKit could not convert it ({warning})")

    report["error"] = "ChemDraw produced no InChI. Tried: " + "; ".join(report["attempts"])
    return report
