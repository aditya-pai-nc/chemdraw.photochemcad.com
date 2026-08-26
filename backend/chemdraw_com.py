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
