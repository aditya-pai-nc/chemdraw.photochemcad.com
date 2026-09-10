"""
Keystroke fallback for pulling representations out of ChemDraw.

ChemDraw's `Edit > Copy As >` submenu can emit every representation the pipeline
wants — SMILES, SLN, InChI, InChIKey and MOL text — and that menu is the route a
chemist can verify by hand. The COM surface exposes the *same* converters as
`Objects.Data(mime)`, but whether a given install answers to that call varies by
version and by whether the InChI plug-in shipped with it, so this module exists
as the fallback for whatever COM would not give up.

The sequence per format, exactly as driven by hand:

    Ctrl+A          select every object on the page
    Alt+E           open the Edit menu
    o               open the "Copy As" submenu
    <letter>        pick the format (s/l/n/k/m)
    read clipboard

Three things make this fragile, and each one is defended against here:

  * **Focus.** Keystrokes go to whatever window is foreground, so ChemDraw has
    to be raised first — and Windows blocks naive focus stealing, hence the
    AttachThreadInput dance in `_focus`.
  * **Staleness.** If a menu path is wrong the clipboard simply keeps its old
    contents, and the previous format would be read back as though this one had
    succeeded. So the clipboard is emptied before every copy and the result is
    only accepted once it has actually changed.
  * **Timing.** The menu is asynchronous. Every step polls rather than assuming
    a fixed delay has been long enough.

Nothing here raises: a format that cannot be copied comes back as None, and the
row is still written with everything that did work.
"""
from __future__ import annotations

import os
import sys
import time
from typing import Optional

# Edit > Copy As > <letter>. Overridable without touching code, because the
# mnemonics can shift between ChemDraw releases and a wrong letter silently
# picks the neighbouring menu entry.
DEFAULT_COPY_AS_KEYS = {
    "smiles": "s",
    "sln": "l",
    "inchi": "n",
    "inchikey": "k",
    "molfile": "m",
}

# Mnemonics for the menu itself, same reasoning.
EDIT_MENU_KEY = os.environ.get("CHEMDRAW_EDIT_MENU_KEY", "e")
COPY_AS_KEY = os.environ.get("CHEMDRAW_COPY_AS_KEY", "o")

# How long to wait for the clipboard to change after the final keystroke.
CLIPBOARD_TIMEOUT = float(os.environ.get("CHEMDRAW_KEYS_TIMEOUT", "4.0"))
# Pause between keystrokes in a menu sequence. The menu animates; too fast and
# the submenu has not opened when the format letter arrives.
KEY_DELAY = float(os.environ.get("CHEMDRAW_KEYS_DELAY", "0.12"))

ENABLED = os.environ.get("CHEMDRAW_KEYS_FALLBACK", "1").lower() not in ("0", "false", "no", "off")

_VK_CONTROL = 0x11
_VK_MENU = 0x12  # ALT
_VK_ESCAPE = 0x1B
_KEYEVENTF_KEYUP = 0x0002


def copy_as_keys() -> dict[str, str]:
    """The format → menu-letter map, with `CHEMDRAW_COPYAS_KEYS` applied over it."""
    keys = dict(DEFAULT_COPY_AS_KEYS)
    override = os.environ.get("CHEMDRAW_COPYAS_KEYS", "").strip()
    if override:
        for pair in override.split(","):
            name, _, letter = pair.partition("=")
            name, letter = name.strip().lower(), letter.strip().lower()
            if name and len(letter) == 1:
                keys[name] = letter
    return keys


def availability() -> tuple[bool, Optional[str]]:
    """(usable, reason it is not) for the keystroke route."""
    if not ENABLED:
        return False, "Keystroke fallback is disabled (CHEMDRAW_KEYS_FALLBACK=0)."
    if sys.platform != "win32":
        return False, f"Keystroke automation needs Windows; this is {sys.platform}."
    try:
        import win32api  # noqa: F401
        import win32clipboard  # noqa: F401
        import win32gui  # noqa: F401
        import win32process  # noqa: F401
    except ImportError as exc:
        return False, f"pywin32 is required for the keystroke fallback ({exc})."
    return True, None


# ---------------------------------------------------------------------------
# Window focus
# ---------------------------------------------------------------------------


def _find_window() -> Optional[int]:
    """The most likely ChemDraw top-level window, or None."""
    import win32gui

    matches: list[tuple[int, str]] = []

    def visit(hwnd: int, _arg) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        try:
            title = win32gui.GetWindowText(hwnd) or ""
            cls = win32gui.GetClassName(hwnd) or ""
        except Exception:
            return
        haystack = f"{title} {cls}".lower()
        if "chemdraw" in haystack or "chemoffice" in haystack:
            matches.append((hwnd, title))

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        return None

    if not matches:
        return None
    # Prefer a window with a document title (it carries the filename) over a
    # bare splash or palette window.
    matches.sort(key=lambda m: (len(m[1]) == 0, -len(m[1])))
    return matches[0][0]


def _unlock_foreground() -> None:
    """
    Lift Windows' foreground lock for this process.

    Windows refuses SetForegroundWindow from a process that does not already own
    the foreground — which is exactly a uvicorn service. Two documented levers
    move it: zeroing SPI_SETFOREGROUNDLOCKTIMEOUT, and the fact that the lock is
    released for a process that has just received keyboard input, which a
    synthetic ALT tap satisfies.
    """
    import ctypes

    SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
    SPIF_SENDCHANGE = 0x0002
    try:
        ctypes.windll.user32.SystemParametersInfoW(
            SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(0), SPIF_SENDCHANGE
        )
    except Exception:
        pass

    # An ALT press/release marks this thread as having had recent input, which
    # is one of the conditions under which the foreground lock is waived.
    try:
        import win32api
        win32api.keybd_event(_VK_MENU, 0, 0, 0)
        time.sleep(0.01)
        win32api.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
    except Exception:
        pass


def _same_process(a: int, b: int) -> bool:
    """Do two windows belong to the same process?"""
    import win32process
    try:
        _, pa = win32process.GetWindowThreadProcessId(a)
        _, pb = win32process.GetWindowThreadProcessId(b)
        return pa == pb and pa != 0
    except Exception:
        return False


def _focus(hwnd: int) -> bool:
    """
    Raise a window to the foreground, and confirm it actually came forward.

    Several mechanisms are stacked because no single one is reliable from a
    background service: unlocking the foreground timeout, attaching to the
    current foreground thread's input queue, then the raise itself.

    The confirmation accepts any window of the *same process*: ChemDraw's
    foreground window after a raise is often its main frame rather than the
    document window that was located, and requiring an exact handle match
    reported failure on a raise that had in fact worked.
    """
    import win32api
    import win32con
    import win32gui
    import win32process

    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        else:
            win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    except Exception:
        pass

    _unlock_foreground()

    current_thread = win32api.GetCurrentThreadId()
    attached = False
    fg_thread = None
    try:
        foreground = win32gui.GetForegroundWindow()
        if foreground and (foreground == hwnd or _same_process(foreground, hwnd)):
            return True
        if foreground:
            fg_thread, _ = win32process.GetWindowThreadProcessId(foreground)
            if fg_thread and fg_thread != current_thread:
                attached = bool(win32process.AttachThreadInput(current_thread, fg_thread, True))
    except Exception:
        pass

    for attempt in range(3):
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            # SetForegroundWindow raises when the lock is still held; the
            # topmost nudge below often shakes it loose.
            try:
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
                )
            except Exception:
                pass

        deadline = time.time() + 1.0
        while time.time() < deadline:
            try:
                fg = win32gui.GetForegroundWindow()
                if fg and (fg == hwnd or _same_process(fg, hwnd)):
                    if attached and fg_thread:
                        try:
                            win32process.AttachThreadInput(current_thread, fg_thread, False)
                        except Exception:
                            pass
                    return True
            except Exception:
                break
            time.sleep(0.05)
        _unlock_foreground()

    if attached and fg_thread:
        try:
            win32process.AttachThreadInput(current_thread, fg_thread, False)
        except Exception:
            pass
    return False


# ---------------------------------------------------------------------------
# Keystrokes
# ---------------------------------------------------------------------------


def _tap(vk: int, hold: int | None = None) -> None:
    """Press and release a virtual key, optionally with a modifier held down."""
    import win32api

    if hold is not None:
        win32api.keybd_event(hold, 0, 0, 0)
        time.sleep(0.02)
    win32api.keybd_event(vk, 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
    if hold is not None:
        time.sleep(0.02)
        win32api.keybd_event(hold, 0, _KEYEVENTF_KEYUP, 0)


def _tap_letter(letter: str, hold: int | None = None) -> None:
    _tap(ord(letter.upper()), hold=hold)


def _dismiss_menus() -> None:
    """Escape twice, so a half-open menu cannot swallow the next sequence."""
    for _ in range(2):
        _tap(_VK_ESCAPE)
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------


def _clipboard_text() -> Optional[str]:
    """Clipboard as text, or None. Retries: another app may hold it open."""
    import win32clipboard

    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
        except Exception:
            time.sleep(0.05)
            continue
        try:
            for fmt in (win32clipboard.CF_UNICODETEXT, win32clipboard.CF_TEXT):
                try:
                    if not win32clipboard.IsClipboardFormatAvailable(fmt):
                        continue
                    value = win32clipboard.GetClipboardData(fmt)
                except Exception:
                    continue
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                if value:
                    return str(value)
            return None
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
    return None


def _empty_clipboard() -> bool:
    """
    Empty the clipboard so a stale value cannot be mistaken for a fresh copy.

    This is the difference between "the menu path was wrong" and "the menu path
    silently returned the previous format", which is the failure this whole
    module is most likely to hit.
    """
    import win32clipboard

    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
        except Exception:
            time.sleep(0.05)
            continue
        try:
            win32clipboard.EmptyClipboard()
            return True
        except Exception:
            return False
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
    return False


# ---------------------------------------------------------------------------
# The public call
# ---------------------------------------------------------------------------


def copy_as(fmt: str, hwnd: int | None = None) -> tuple[Optional[str], Optional[str]]:
    """
    Drive Edit > Copy As > <fmt> and return (text, error).

    `fmt` is one of the keys in `DEFAULT_COPY_AS_KEYS`. Returns (None, reason)
    rather than raising, because a format ChemDraw will not copy still has to
    leave the rest of the compound's row intact.
    """
    usable, reason = availability()
    if not usable:
        return None, reason

    letter = copy_as_keys().get(fmt.lower())
    if not letter:
        return None, f"No Copy As mnemonic is configured for '{fmt}'."

    window = hwnd if hwnd is not None else _find_window()
    if window is None:
        return None, "Could not find a ChemDraw window to send keystrokes to."
    if not _focus(window):
        return None, "Could not bring the ChemDraw window to the foreground."

    if not _empty_clipboard():
        return None, "Could not clear the clipboard before copying."

    try:
        _dismiss_menus()
        _tap_letter("a", hold=_VK_CONTROL)      # Ctrl+A — select all
        time.sleep(KEY_DELAY)
        _tap_letter(EDIT_MENU_KEY, hold=_VK_MENU)  # Alt+E — Edit menu
        time.sleep(KEY_DELAY)
        _tap_letter(COPY_AS_KEY)                # o — Copy As submenu
        time.sleep(KEY_DELAY)
        _tap_letter(letter)                     # the format itself
    except Exception as exc:
        _dismiss_menus()
        return None, f"Keystroke sequence failed: {exc}"

    deadline = time.time() + CLIPBOARD_TIMEOUT
    while time.time() < deadline:
        text = _clipboard_text()
        if text and text.strip():
            # Returned exactly as it came. A MOL block's first line is its title
            # line and is usually blank; stripping it shifts every subsequent
            # line of a fixed-column format up by one and RDKit can no longer
            # read it. Per-format tidying belongs in chemdraw_com._clean.
            return text, None
        time.sleep(0.08)

    # Nothing arrived: the mnemonic is probably wrong for this ChemDraw build,
    # or the format is greyed out for this selection.
    _dismiss_menus()
    return None, (
        f"Clipboard stayed empty after Edit > Copy As > '{letter}' for {fmt}. "
        f"Check the mnemonic (CHEMDRAW_COPYAS_KEYS) for this ChemDraw version."
    )


def _find_dialog(reference_hwnd: int) -> Optional[int]:
    """
    A modal dialog belonging to the same process as `reference_hwnd`, if one is up.

    Closing a modified document raises "Save changes?", and the reply has to go
    to that dialog. Sending the keystroke blind is not an option: with no dialog
    present the same key lands in the drawing canvas and edits the structure.
    """
    import win32gui

    found: list[int] = []

    def visit(hwnd: int, _arg) -> None:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            if win32gui.GetClassName(hwnd) != "#32770":  # the Windows dialog class
                return
        except Exception:
            return
        if _same_process(hwnd, reference_hwnd):
            found.append(hwnd)

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        return None
    return found[0] if found else None


def close_document(hwnd: int | None = None, discard_changes: bool = True) -> tuple[bool, Optional[str]]:
    """
    Close the active ChemDraw document with Ctrl+W.

    This exists because `Document.Close()` over COM is a no-op on some ChemDraw
    builds — it returns without error and leaves the document open, so a job
    that opens one document per molecule leaks every one of them until
    `Documents.Open` starts refusing.

    The pipeline never wants the edits kept: it has already written the MOL and
    the TIFF to disk, and the document is only dirty because exporting marked it
    so. A "Save changes?" dialog is therefore answered "No" — but only once it
    has actually been seen on screen.
    """
    usable, reason = availability()
    if not usable:
        return False, reason

    window = hwnd if hwnd is not None else _find_window()
    if window is None:
        return False, "No ChemDraw window to close."
    if not _focus(window):
        return False, "Could not bring the ChemDraw window to the foreground."

    try:
        _tap_letter("w", hold=_VK_CONTROL)
    except Exception as exc:
        return False, f"Ctrl+W failed: {exc}"

    # Give the dialog a chance to appear before deciding there isn't one.
    dialog = None
    deadline = time.time() + 1.5
    while time.time() < deadline:
        dialog = _find_dialog(window)
        if dialog:
            break
        time.sleep(0.05)

    if dialog:
        if not discard_changes:
            return False, "A save-changes dialog is open and discarding was not permitted."
        try:
            import win32gui
            win32gui.SetForegroundWindow(dialog)
        except Exception:
            pass
        time.sleep(0.1)
        # "No" — the mnemonic on ChemDraw's save prompt. Escape as the backstop
        # cancels the close rather than saving, which is the safe wrong answer.
        _tap_letter("n")
        time.sleep(0.2)
        if _find_dialog(window):
            _tap(_VK_ESCAPE)
            time.sleep(0.15)
            if _find_dialog(window):
                return False, "A dialog stayed open after Ctrl+W."

    return True, None


def copy_many(formats: list[str], hwnd: int | None = None) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """
    Copy several formats in one focused session.

    The window is located and raised once rather than per format — each raise
    steals focus from whatever the user is doing, so doing it five times per
    molecule would make the machine unusable for the length of a run.
    """
    usable, reason = availability()
    if not usable:
        return {fmt: (None, reason) for fmt in formats}

    window = hwnd if hwnd is not None else _find_window()
    if window is None:
        return {fmt: (None, "Could not find a ChemDraw window.") for fmt in formats}

    return {fmt: copy_as(fmt, hwnd=window) for fmt in formats}
