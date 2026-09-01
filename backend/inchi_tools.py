"""
InChI / InChIKey helpers.

Everything in this module is pure RDKit and string work — no ChemDraw, no COM,
no Windows. That is deliberate: the InChIKey half of the pipeline has to be
developable and testable on a Mac, where ChemDraw cannot run at all.

An InChIKey is three blocks, `AAAAAAAAAAAAAA-BBBBBBBBFV-P`:

    block 1 (14 chars)  skeleton — connectivity only
    block 2 (10 chars)  stereochemistry + isotopes, then version/flag chars
    block 3 (1 char)    protonation state

So two keys can disagree in a way that still matters to a chemist: identical
skeletons with different second blocks means "same molecule drawn with different
(or missing) stereochemistry", which is a very different finding from "not the
same molecule at all". Every comparison here reports that middle ground rather
than collapsing it into a boolean.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from rdkit import Chem, RDLogger

# RDKit's InChI backend is chatty about perfectly ordinary drawings (unusual
# valences, undefined stereo). Those warnings are captured per call instead.
RDLogger.DisableLog("rdApp.warning")

try:
    from rdkit.Chem import inchi as _rd_inchi
    INCHI_AVAILABLE = bool(_rd_inchi.INCHI_AVAILABLE)
except Exception:  # pragma: no cover - RDKit built without InChI support
    _rd_inchi = None
    INCHI_AVAILABLE = False

# Symbols shared by every match column in the workbook.
MATCH_YES = "✅"      # exact agreement
MATCH_PARTIAL = "🟡"  # same skeleton, different stereo/protonation
MATCH_NO = "❌"       # genuine disagreement
MATCH_NA = "—"        # one side is missing, so there is nothing to compare

_INCHIKEY_RE = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
_SKELETON_RE = re.compile(r"^[A-Z]{14}$")

AGREEMENT_SYMBOLS = {
    "exact": MATCH_YES,
    "skeleton": MATCH_PARTIAL,
    "mismatch": MATCH_NO,
    "unknown": MATCH_NA,
}

AGREEMENT_LABELS = {
    "exact": "Exact InChIKey match",
    "skeleton": "Same skeleton, different stereochemistry/protonation",
    "mismatch": "Different structures",
    "unknown": "Not enough data to compare",
}


def normalize_inchikey(value) -> Optional[str]:
    """Uppercase and strip a candidate key; None unless it is well-formed."""
    if not value:
        return None
    key = str(value).strip().upper()
    # Tolerate the "InChIKey=" prefix that some tools (and users) paste in.
    if key.startswith("INCHIKEY="):
        key = key[len("INCHIKEY="):]
    return key if _INCHIKEY_RE.match(key) else None


def is_inchikey(value) -> bool:
    return normalize_inchikey(value) is not None


def inchikey_skeleton(value) -> Optional[str]:
    """The connectivity block — the first 14 characters of a key."""
    key = normalize_inchikey(value)
    if key:
        return key.split("-")[0]
    if value:
        candidate = str(value).strip().upper().split("-")[0]
        if _SKELETON_RE.match(candidate):
            return candidate
    return None


def inchikey_agreement(left, right) -> str:
    """
    Compare two InChIKeys.

    Returns "exact", "skeleton" (same connectivity only), "mismatch", or
    "unknown" when either side is missing or malformed.
    """
    a, b = normalize_inchikey(left), normalize_inchikey(right)
    if not a or not b:
        return "unknown"
    if a == b:
        return "exact"
    return "skeleton" if a.split("-")[0] == b.split("-")[0] else "mismatch"


def agreement_symbol(agreement: str) -> str:
    return AGREEMENT_SYMBOLS.get(agreement, MATCH_NA)


def agreement_label(agreement: str) -> str:
    return AGREEMENT_LABELS.get(agreement, AGREEMENT_LABELS["unknown"])


def best_agreement(*agreements: str) -> str:
    """The most favourable of several comparisons, e.g. ChemDraw's key or RDKit's."""
    for level in ("exact", "skeleton", "mismatch"):
        if level in agreements:
            return level
    return "unknown"


def inchi_from_mol(mol) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    (inchi, inchikey, error) for an RDKit mol.

    Never raises: a molecule InChI cannot describe (exotic valences, polymer
    brackets) has to leave the rest of the row intact.
    """
    if mol is None:
        return None, None, "No molecule to convert."
    if not INCHI_AVAILABLE:
        return None, None, "This RDKit build has no InChI support."
    try:
        # The low-level binding returns the warning text alongside the InChI;
        # the high-level MolToInchi only logs it, and those warnings ("undefined
        # stereo", "unusual valence") are exactly what a researcher wants to see.
        inchi, retcode, message, _log, _aux = _rd_inchi.rdinchi.MolToInchi(mol)
    except Exception as exc:
        return None, None, f"InChI generation failed: {exc}"

    if not inchi:
        return None, None, (message or "InChI generation returned nothing.").strip() or None

    try:
        key = _rd_inchi.InchiToInchiKey(inchi)
    except Exception as exc:
        return inchi, None, f"InChIKey generation failed: {exc}"

    warning = (message or "").strip() if retcode not in (0, None) else None
    return inchi, normalize_inchikey(key) or key, warning


def inchi_from_molfile(mol_path: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(inchi, inchikey, error) read straight from a MOL/SDF file on disk."""
    try:
        mol = Chem.MolFromMolFile(mol_path)
    except Exception as exc:
        return None, None, f"Could not read MOL file: {exc}"
    if mol is None:
        return None, None, f"RDKit could not parse MOL: {mol_path}"
    return inchi_from_mol(mol)


def inchi_from_molblock(molblock: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(inchi, inchikey, error) for a MOL block held in memory."""
    if not molblock:
        return None, None, "Empty MOL block."
    try:
        mol = Chem.MolFromMolBlock(molblock)
    except Exception as exc:
        return None, None, f"Could not parse MOL block: {exc}"
    if mol is None:
        return None, None, "RDKit could not parse the MOL block."
    return inchi_from_mol(mol)


def inchi_from_smiles(smiles: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(inchi, inchikey, error) for a SMILES string."""
    if not smiles:
        return None, None, "Empty SMILES."
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None, None, f"RDKit could not parse SMILES: {smiles}"
    return inchi_from_mol(mol)


def inchikey_from_inchi(inchi: str) -> Optional[str]:
    """Derive the key from an InChI string — how ChemDraw's InChI becomes a key."""
    if not inchi or not INCHI_AVAILABLE:
        return None
    text = inchi.strip()
    if not text.startswith("InChI="):
        return None
    try:
        return normalize_inchikey(_rd_inchi.InchiToInchiKey(text))
    except Exception:
        return None


def parse_inchi_text(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Pull an InChI string and/or an InChIKey out of arbitrary text.

    ChemDraw's InChI export is not one fixed shape — depending on version and
    export path it can be a bare `InChI=1S/...` line, that line followed by an
    `InChIKey=...` line, or either wrapped in surrounding chatter. This finds
    whatever is actually in there.
    """
    if not text:
        return None, None

    inchi = None
    key = None
    for raw in str(text).replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("InChI=") and inchi is None:
            inchi = line.split()[0]
        elif line.upper().startswith("INCHIKEY=") and key is None:
            key = normalize_inchikey(line.split("=", 1)[1])
        elif key is None and _INCHIKEY_RE.match(line.upper()):
            key = line.upper()

    if inchi and not key:
        key = inchikey_from_inchi(inchi)
    return inchi, key
