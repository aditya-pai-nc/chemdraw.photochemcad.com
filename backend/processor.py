"""
Stage 3: turn each split CDXML into one workbook row.

The flow is deliberately linear, and there is exactly one path through it:

  1. ChemDraw   opens the drawing and hands over its SMILES, its InChI, its
                InChIKey and its MOL text.
  2. RDKit      reads ChemDraw's MOL text for the molecular formula and weight,
                and canonicalises ChemDraw's SMILES.
  3. PubChem    is queried by ChemDraw's InChIKey first, then connectivity,
                name and SMILES. Missing fields are filled across candidates.
  4. Two PubChem checks:

        Match?           PubChem's formula equals the drawn formula, and its
                         weight agrees to within 0.5.
        InChIKey Match?  The canonical SMILES from ChemDraw and the canonical
                         SMILES from PubChem are identical.
  5. CAS Common Chemistry independently verifies the drawing against a CAS
                record, with its result and reference values kept separate.

Both SMILES go through RDKit's canonical writer before being compared, because a
SMILES string is one of many ways to write a structure — ChemDraw's and
PubChem's spellings differ for the same molecule, so comparing the raw strings
would answer a question nobody asked.

There is no skeleton tier, no stereo tolerance, no second opinion consulted when
the first is inconvenient. Either the two structures write the same way or they
do not. Compounds where `InChIKey Match?` is not a tick go to the curation pass
in `ai_curate`, which writes to a separate worksheet.
"""
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from rdkit import Chem
from rdkit.Chem import Descriptors

import ai_curate
import common_chemistry
import pubchem
from chemdraw_com import connect_chemdraw, export_formats_from_document, open_document
from inchi_tools import (
    MATCH_NA,
    MATCH_NO,
    MATCH_YES,
    canonical_smiles,
    canonical_smiles_match,
    normalize_inchikey,
)

# Curation is network-bound and touches only the compounds that failed, so it
# runs several at a time. Unlike ChemDraw it has no single-instance constraint.
CURATE_CONCURRENCY = ai_curate.CURATE_CONCURRENCY

# Weight agreement tolerance for the formula/weight match, unchanged since the
# first version of this pipeline so match rates stay comparable.
WEIGHT_TOLERANCE = 0.5


# ---------------------------------------------------------------------------
# Column layout — pipeline.py orders the workbook from this
# ---------------------------------------------------------------------------
# A chemist reads this sheet. Every column here is either the compound's
# identity, a number they would check by hand, or one of the two verdicts.
# Extraction routes, round-trip diagnostics and PubChem's 48 computed
# descriptors are not on it.

ROW_COLUMNS = [
    "Compound Name", "Compound ID",

    # What ChemDraw says is on the page.
    "ChemDraw SMILES", "ChemDraw InChIKey", "Canonical SMILES",
    "Formula", "Molecular Weight",

    # What PubChem returned for that InChIKey.
    "PubChem CID", "All PubChem CIDs",
    "PubChem Formula", "PubChem Molecular Weight",
    "PubChem SMILES", "PubChem Canonical SMILES", "PubChem InChIKey",

    # The two verdicts.
    "Match?", "InChIKey Match?",

    # Reference data worth having beside them.
    "CAS no(s)", "IUPAC Name", "Synonym", "PubChem Link", "Wikipedia Link",

    # Independent verification against CAS; the PubChem verdicts stay separate.
    *common_chemistry.CAS_COLUMNS,

    # The researcher's own column, left empty on purpose.
    "Manual Match",
]

# The second sheet: only the compounds that did not match.
CURATED_COLUMNS = [
    "Compound Name", "Compound ID", "Why Unmatched",
    "Same Compound?", "Curated Name", "Curated Formula", "Curated SMILES",
    "Curated InChIKey", "Curated CAS", "Best PubChem CID",
    "Discrepancy", "Likely Cause", "Recommended Action", "Curation Confidence",
    # Context, so the sheet reads on its own without cross-referencing sheet 1.
    "ChemDraw SMILES", "Formula", "PubChem CID", "PubChem Formula",
    *common_chemistry.CAS_COLUMNS,
    "Curation Model", "Curation Error",
]


# ---------------------------------------------------------------------------
# Reading the drawing
# ---------------------------------------------------------------------------


def extract_name_from_cdxml(cdxml_path: str) -> str:
    try:
        root = ET.parse(cdxml_path).getroot()
        ns = {'cdx': root.tag.split('}')[0].strip('{')}
        tags = root.findall(".//cdx:t", ns)
        if tags:
            return tags[0].text.strip()
    except Exception:
        pass
    return os.path.splitext(os.path.basename(cdxml_path))[0]


def clean_name(name: str) -> str:
    name = re.sub(r'[_\s]*\([A-Za-z0-9\-]+\)$', '', name).strip()
    name = name.replace('_', ' ')
    name = re.sub(r'\s*-\s*', '-', name)
    return re.sub(r'\s+', ' ', name).strip()


def extract_suffix_tag(name: str) -> str:
    m = re.search(r'\(([A-Za-z])[-\s]?(\d+)\)$', name)
    if m:
        letter = m.group(1).upper()
        num = int(m.group(2))
        return f"{letter}{num:02d}" if num < 10 else f"{letter}{num}"
    return ""


def open_and_export(cdxml_path: str, mol_path: str, tif_path: str) -> dict:
    """
    One ChemDraw session per molecule: the TIFF, then the representations.

    The MOL text gathered here is what gets written to `mol_path` and what RDKit
    reads, so the file in `mol_files/` is exactly the structure the row was
    computed from. `SaveAs` is only the fallback for when neither COM nor the
    Copy As menu produced MOL text.
    """
    chemdraw, _ = connect_chemdraw()
    # Visible, because the Copy As menu is now the primary extraction route and
    # keystrokes only reach a window that is on screen. This does mean the
    # machine cannot be used for anything else while a job runs.
    chemdraw.Visible = True
    doc = open_document(chemdraw, cdxml_path)
    try:
        doc.Activate()
        time.sleep(1)
        doc.SaveAs(os.path.abspath(tif_path))

        formats = export_formats_from_document(doc, app=chemdraw)

        molblock = (formats.get("molfile") or {}).get("value")
        if molblock:
            with open(mol_path, "w", encoding="utf-8") as fh:
                fh.write(molblock)
        else:
            # Neither route gave MOL text, so fall back to writing the file
            # directly. This must come last: SaveAs changes the document's
            # format and the representations are already gathered.
            doc.SaveAs(os.path.abspath(mol_path))

        return formats
    finally:
        # `doc.Close()` is a no-op on some ChemDraw builds — it returns without
        # error and leaves the document open, which is how earlier runs leaked a
        # document per molecule until ChemDraw refused to open any more. Ctrl+W
        # is what actually closes it, so both are attempted and the COM call is
        # only the fallback for a machine with no interactive desktop.
        import chemdraw_keys
        closed, close_error = chemdraw_keys.close_document()
        if not closed:
            try:
                doc.Close()
            except Exception:
                pass


def mol_to_formula_and_weight(mol_path: str):
    mol = Chem.MolFromMolFile(mol_path)
    if mol is None:
        raise ValueError(f"RDKit could not parse MOL: {mol_path}")
    formula = Chem.rdMolDescriptors.CalcMolFormula(mol)
    weight = round(Descriptors.MolWt(mol), 3)
    return mol, formula, weight


# ---------------------------------------------------------------------------
# The two checks
# ---------------------------------------------------------------------------


def formula_weight_match(local_formula, local_weight, hit) -> str:
    """`Match?` — same formula, and a weight within half a unit."""
    if hit is None or not hit.formula or not local_formula:
        return MATCH_NO
    if hit.formula != local_formula:
        return MATCH_NO
    if hit.weight is None or local_weight is None:
        return MATCH_NO
    return MATCH_YES if abs(hit.weight - local_weight) < WEIGHT_TOLERANCE else MATCH_NO


# ---------------------------------------------------------------------------
# One compound
# ---------------------------------------------------------------------------


def _blank_row(name: str, suffix_tag: str) -> dict:
    row = {col: None for col in ROW_COLUMNS}
    row["Compound Name"] = name
    row["Compound ID"] = suffix_tag
    row["Match?"] = MATCH_NO
    row["InChIKey Match?"] = MATCH_NA
    row["CAS Verification"] = "Skipped"
    row["CAS Verification Detail"] = "No structure was extracted for verification."
    row["Manual Match"] = None
    return row


def _extract_structure(cdxml_path: str, mol_dir: str, image_dir: str, row: dict) -> dict:
    """
    ChemDraw first, and unconditionally.

    Everything ChemDraw hands over is written to the row the moment it arrives,
    before anything that can fail is attempted. ChemDraw is the authority on
    what is drawn on the page; no later step failing is a reason to discard what
    it already said.

    RDKit is then asked for the molecular formula and weight from ChemDraw's MOL
    text. That step is allowed to fail — a metal-coordinated macrocycle such as
    chlorophyll has a nitrogen valence RDKit rejects — and when it does, only
    the formula and weight are missing. The InChIKey, the SMILES and the
    canonical SMILES are already recorded, PubChem is still searched with them,
    and `InChIKey Match?` is still decided.
    """
    base = os.path.splitext(os.path.basename(cdxml_path))[0]
    mol_path = os.path.join(mol_dir, base + ".mol")
    tif_path = os.path.join(image_dir, base + ".tif")

    formats = open_and_export(cdxml_path, mol_path, tif_path)

    def value(fmt: str):
        return (formats.get(fmt) or {}).get("value")

    chemdraw_smiles = value("smiles")
    chemdraw_key = normalize_inchikey(value("inchikey"))

    # ── Recorded before anything else runs ───────────────────────────────────
    row["ChemDraw SMILES"] = chemdraw_smiles
    row["ChemDraw InChIKey"] = chemdraw_key
    row["Canonical SMILES"] = canonical_smiles(chemdraw_smiles)

    # ── Optional: formula and weight, from ChemDraw's MOL via RDKit ──────────
    formula = weight = None
    mol_error = None
    try:
        _mol, formula, weight = mol_to_formula_and_weight(mol_path)
    except Exception as exc:
        mol_error = str(exc)

    row["Formula"] = formula
    row["Molecular Weight"] = weight

    return {
        "base": base,
        "chemdraw_smiles": chemdraw_smiles,
        "chemdraw_key": chemdraw_key,
        "formula": formula,
        "weight": weight,
        "formats": formats,
        "mol_error": mol_error,
    }


def _enrich_from_pubchem(local: dict, name: str, structure_dir: str, row: dict) -> Optional[Any]:
    """
    Find the compound in PubChem, strongest evidence first.

    ChemDraw's InChIKey is tried exactly first, because a key is a hash of the
    structure and cannot match by coincidence. When that misses — and it misses
    often, because a drawing that defines a stereocentre differently from the
    deposited record produces a different key — the search widens rather than
    giving up: the 14-character connectivity block, then the caption, then the
    SMILES.

    Widening the *search* does not weaken the *verdict*. Whatever route finds a
    record, `InChIKey Match?` is still decided solely by whether the canonical
    SMILES are identical, so a loose hit that is not the drawn compound still
    scores a cross.

    One key can resolve to several deposited records, and they are not equally
    complete, so every CID is collected and a field the first one leaves empty
    is filled from the next that has it.
    """
    hit = pubchem.lookup(
        inchikeys=[local.get("chemdraw_key")],
        name=clean_name(name),
        smiles=local.get("chemdraw_smiles"),
    )
    if hit is None:
        return None

    row["PubChem CID"] = hit.cid
    row["All PubChem CIDs"] = (
        ", ".join(str(c) for c in hit.candidate_cids) if hit.candidate_cids else None
    )
    row["PubChem Formula"] = hit.formula
    row["PubChem Molecular Weight"] = hit.weight
    row["PubChem SMILES"] = hit.smiles
    row["PubChem Canonical SMILES"] = canonical_smiles(hit.smiles)
    row["PubChem InChIKey"] = hit.inchikey

    if hit.cid is not None:
        # Keep PubChem's own structure beside ChemDraw's MOL so the two can be
        # compared after the fact.
        save_path = os.path.join(structure_dir, f"{local['base']}_pubchem_cid{hit.cid}.mol")
        structure = pubchem.fetch_structure(hit.cid, save_path=save_path)
        if not row["PubChem Canonical SMILES"]:
            row["PubChem Canonical SMILES"] = canonical_smiles(structure.get("smiles"))

    return hit


def _fill_reference_fields(hit, row: dict) -> None:
    """CAS, IUPAC name, synonyms and Wikipedia, filled across the candidates."""
    cids = list(hit.candidate_cids) if hit and hit.candidate_cids else ([hit.cid] if hit and hit.cid else [])
    if not cids:
        return

    fields = pubchem.fetch_reference_fields(cids)
    row["CAS no(s)"] = fields.get("cas")
    row["IUPAC Name"] = fields.get("iupac_name")
    row["Synonym"] = fields.get("synonyms")
    row["Wikipedia Link"] = fields.get("wikipedia")
    row["PubChem Link"] = f"https://pubchem.ncbi.nlm.nih.gov/compound/{cids[0]}"


# ---------------------------------------------------------------------------
# Curation of the compounds that did not match
# ---------------------------------------------------------------------------


def _why_unmatched(row: dict, hit) -> str:
    """A one-line reason, written locally — not by the model."""
    if not row.get("ChemDraw InChIKey") and not row.get("Canonical SMILES"):
        return "ChemDraw produced neither an InChIKey nor a usable SMILES for this structure."
    if hit is None:
        return ("PubChem returned nothing for this compound, by InChIKey, connectivity, "
                "name or SMILES.")
    if row.get("InChIKey Match?") == MATCH_NA:
        return "There was no usable SMILES on one side, so the structures could not be compared."
    return "The canonical SMILES from ChemDraw and from PubChem differ."


def _curated_row(row: dict, result: dict, reason: str) -> dict:
    curated = {col: None for col in CURATED_COLUMNS}
    for col in ("Compound Name", "Compound ID", "ChemDraw SMILES", "Formula",
                "PubChem CID", "PubChem Formula", *common_chemistry.CAS_COLUMNS):
        curated[col] = row.get(col)
    curated["Why Unmatched"] = reason
    curated["Curation Model"] = result.get("model")
    curated["Curation Error"] = result.get("error")

    if not result.get("ok"):
        return curated

    curated["Same Compound?"] = result.get("same_compound")
    curated["Curated Name"] = result.get("curated_name")
    curated["Curated Formula"] = result.get("curated_formula")
    curated["Curated SMILES"] = result.get("curated_smiles")
    curated["Curated InChIKey"] = result.get("curated_inchikey")
    curated["Curated CAS"] = result.get("curated_cas")
    curated["Best PubChem CID"] = result.get("best_pubchem_cid")
    curated["Discrepancy"] = result.get("discrepancy")
    curated["Likely Cause"] = result.get("likely_cause")
    curated["Recommended Action"] = result.get("recommended_action")
    curated["Curation Confidence"] = result.get("confidence")

    # The model tried to supply a key that was not in the evidence. Keeping the
    # rejection visible is the point — it is the one field it cannot reason out.
    if result.get("inchikey_rejected"):
        note = (f"(model proposed InChIKey {result['inchikey_rejected']}, "
                "which was not in the evidence — discarded)")
        curated["Curation Error"] = " ".join(filter(None, [curated["Curation Error"], note]))

    return curated


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def process_molecules(
    cdxml_paths: list[str],
    mol_dir: str,
    image_dir: str,
    total: int,
    emit: Callable,
    structure_dir: str | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Two passes, returning (rows, curated_rows).

    Pass 1 is strictly sequential because it drives ChemDraw over COM, and two
    pipelines touching one ChemDraw instance would open and close each other's
    documents. Pass 2 curates only the compounds pass 1 could not confirm, and
    is network-bound with no such constraint, so it runs several at a time.
    """
    os.makedirs(mol_dir, exist_ok=True)
    os.makedirs(image_dir, exist_ok=True)
    structure_dir = structure_dir or os.path.join(os.path.dirname(mol_dir), "pubchem_structures")
    os.makedirs(structure_dir, exist_ok=True)

    rows: list[dict] = []
    unmatched: list[tuple[int, dict, Any]] = []
    cas_client = common_chemistry.CommonChemistryClient()
    if not cas_client.config["ready"]:
        emit({"type": "log", "level": "info", "message": cas_client.config["reason"]})

    # ── Pass 1: ChemDraw, RDKit, PubChem ──────────────────────────────────────
    for idx, cdxml_path in enumerate(cdxml_paths, 1):
        filename = os.path.basename(cdxml_path)
        name = extract_name_from_cdxml(cdxml_path)
        row = _blank_row(name, extract_suffix_tag(name))
        hit = None

        try:
            local = _extract_structure(cdxml_path, mol_dir, image_dir, row)
            if local.get("mol_error"):
                emit({"type": "log", "level": "warn", "message": (
                    f"{filename}: RDKit could not read ChemDraw's MOL, so the formula and "
                    f"weight are blank. ChemDraw's InChIKey and SMILES are still used. "
                    f"({local['mol_error'][:120]})")})

            hit = _enrich_from_pubchem(local, name, structure_dir, row)

            row["Match?"] = formula_weight_match(local["formula"], local["weight"], hit)
            symbol, _reason = canonical_smiles_match(
                row.get("Canonical SMILES"), row.get("PubChem Canonical SMILES")
            )
            row["InChIKey Match?"] = symbol

            _fill_reference_fields(hit, row)

        except Exception as exc:
            # Reached only when ChemDraw itself could not be driven for this
            # molecule. Anything ChemDraw already returned is left in the row.
            emit({"type": "log", "level": "error",
                  "message": f"Failed {filename}: {exc}"})

        # Verification also runs when PubChem found nothing or failed. All
        # extracted ChemDraw values survive a CAS/network failure.
        if row.get("ChemDraw SMILES") or row.get("ChemDraw InChIKey"):
            try:
                row.update(cas_client.verify(row, clean_name(name)))
            except Exception:
                row["CAS Verification"] = "Unavailable"
                row["CAS Verification Detail"] = "CAS verification could not be completed."

        if row["InChIKey Match?"] != MATCH_YES:
            unmatched.append((idx, row, hit))

        rows.append(row)
        emit({
            "type": "compound", "name": name,
            "match": row["Match?"], "inchikeyMatch": row["InChIKey Match?"],
            "casVerification": row["CAS Verification"],
            "casDetail": row["CAS Verification Detail"],
            "casRn": row["CAS Common Chemistry RN"],
            "casLink": row["CAS Common Chemistry Link"],
            "index": idx, "total": total,
        })

    # ── Pass 2: curate only what did not match ────────────────────────────────
    if not unmatched:
        emit({"type": "log", "level": "info",
              "message": "Every compound matched — nothing to curate."})
        return rows, []

    if not ai_curate.is_enabled():
        reason = "Curation is off (no ANTHROPIC_API_KEY, or CHEMDRAW_AI_ENABLED=0)."
        emit({"type": "log", "level": "info", "message": reason})
        return rows, [
            _curated_row(row, {"error": reason}, _why_unmatched(row, hit))
            for _idx, row, hit in unmatched
        ]

    emit({
        "type": "stage", "stage": 4, "total": 4,
        "message": f"Curating {len(unmatched)} unmatched compound(s) with {ai_curate.CURATE_MODEL}…",
    })

    def work(item):
        idx, row, hit = item
        reason = _why_unmatched(row, hit)
        try:
            result = ai_curate.curate_compound(ai_curate.build_evidence(row, hit))
        except Exception as exc:
            result = {"ok": False, "model": ai_curate.CURATE_MODEL,
                      "error": f"Curation failed: {exc}"}
        return idx, row, result, reason

    curated_rows: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=min(CURATE_CONCURRENCY, len(unmatched))) as pool:
        for idx, row, result, reason in pool.map(work, unmatched):
            done += 1
            curated_rows.append(_curated_row(row, result, reason))
            emit({
                "type": "curated", "name": row.get("Compound Name"), "index": idx,
                "verdict": result.get("same_compound"),
                "progress": done, "total": len(unmatched),
            })

    return rows, curated_rows
