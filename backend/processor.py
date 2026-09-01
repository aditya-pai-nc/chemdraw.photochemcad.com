"""
Stage 3: turn each split CDXML into one workbook row.

Four sources look at the same molecule and the row records what each of them
said, rather than collapsing them into a single verdict:

  1. ChemDraw   opens the drawing, writes a MOL and a TIFF, and exports its own
                InChI.
  2. RDKit      reads that MOL and computes SMILES, formula, weight and a second,
                independent InChIKey.
  3. PubChem    is searched — by InChIKey first, because that is a hash of the
                structure and cannot match by coincidence, and only then by name
                or SMILES. When it hits, its structure is downloaded and rebuilt
                locally so the match can be verified rather than assumed.
  4. Claude     identifies the molecule from the caption, the picture and the
                formula alone, then a second, cheaper model reconciles all of
                the above into one answer.

Which produces three separate match columns — `Match?` (formula and weight),
`InChIKey Match?` (structure hash) and `AI Match?` — plus an empty
`Manual Match` for the researcher. Reporting them separately is the point: how
often the InChIKey route succeeds where the formula route fails is exactly the
question the extra column exists to answer.
"""
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from rdkit import Chem
from rdkit.Chem import Descriptors

import ai_identify
import pubchem
from chemdraw_com import connect_chemdraw, export_inchi_from_document
from inchi_tools import (
    MATCH_NA,
    MATCH_NO,
    MATCH_YES,
    agreement_label,
    agreement_symbol,
    best_agreement,
    inchi_from_molfile,
    inchikey_agreement,
    normalize_inchikey,
)
from pubchem import PUBCHEM_PROPERTY_NAMES  # re-exported: pipeline.py imports it from here

# The AI pass is network-bound and slow, and unlike ChemDraw it has no
# single-instance constraint, so compounds are enriched in parallel. A 40-
# compound scheme would otherwise spend most of an hour waiting on one request
# at a time.
AI_CONCURRENCY = max(1, int(os.environ.get("CHEMDRAW_AI_CONCURRENCY", "4")))

# Weight agreement tolerance for the classic formula/weight match, unchanged.
WEIGHT_TOLERANCE = 0.5


# ---------------------------------------------------------------------------
# Column layout — pipeline.py orders the workbook from this
# ---------------------------------------------------------------------------

IDENTITY_COLUMNS = ["Compound Name", "Compound ID"]

LOCAL_COLUMNS = [
    "Extracted SMILES", "Local Formula", "Local Weight",
    "RDKit InChI", "RDKit InChIKey",
    "ChemDraw InChI", "ChemDraw InChIKey", "ChemDraw InChI Source",
    "ChemDraw vs RDKit InChIKey",
]

PUBCHEM_COLUMNS = [
    "PubChem Formula", "PubChem Weight", "PubChem CID", "PubChem SMILES",
    "PubChem InChIKey", "PubChem Source", "PubChem Match Notes",
    "PubChem Structure File", "PubChem Structure Round Trip",
]

# The three verdicts plus the researcher's own column, kept adjacent so they can
# be read across at a glance.
MATCH_COLUMNS = [
    "Match?", "InChIKey Match?", "InChIKey Match Detail",
    "AI Match?", "AI Match Detail", "Manual Match",
]

AI_COLUMNS = [
    "AI Name", "AI IUPAC Name", "AI Formula", "AI SMILES", "AI InChIKey",
    "AI Reported InChIKey", "AI CAS", "AI Compound Class", "AI Confidence",
    "AI Reasoning", "AI Notes", "AI Sources", "AI Web Searches", "AI Model", "AI Error",
]

CONSENSUS_COLUMNS = [
    "Consensus Name", "Consensus Formula", "Consensus SMILES", "Consensus InChIKey",
    "Consensus CAS", "Consensus Winning Source", "Consensus Agreement",
    "Consensus Confidence", "Consensus Needs Review", "Consensus Votes",
    "Consensus Rationale", "Consensus Model", "Consensus Error",
]

REFERENCE_COLUMNS = [
    "CAS no(s)", "Synonym", "IUPAC Name", "PubChem Link", "Wikipedia Link",
]

ROW_COLUMNS = (
    IDENTITY_COLUMNS + LOCAL_COLUMNS + PUBCHEM_COLUMNS + MATCH_COLUMNS
    + AI_COLUMNS + CONSENSUS_COLUMNS + REFERENCE_COLUMNS + PUBCHEM_PROPERTY_NAMES
)


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


def open_and_export(cdxml_path: str, mol_path: str, tif_path: str,
                    inchi_path: str | None = None) -> dict:
    """
    One ChemDraw session per molecule: MOL, TIFF, and ChemDraw's own InChI.

    The InChI is pulled here rather than in a second pass because reopening the
    document would mean a second round trip through COM for every compound, and
    ChemDraw is the slowest and least reliable link in the chain.
    """
    chemdraw, _ = connect_chemdraw()
    chemdraw.Visible = False
    doc = chemdraw.Documents.Open(os.path.abspath(cdxml_path))
    try:
        doc.Activate()
        time.sleep(1)
        doc.SaveAs(os.path.abspath(mol_path))
        doc.SaveAs(os.path.abspath(tif_path))
        # After the InChI attempt the document may have been re-saved in another
        # format, so this must come last — the MOL and TIFF are already on disk.
        return export_inchi_from_document(doc, inchi_path)
    finally:
        try:
            doc.Close()
        except Exception:
            pass


def mol_to_smiles_and_props(mol_path: str):
    mol = Chem.MolFromMolFile(mol_path)
    if mol is None:
        raise ValueError(f"RDKit could not parse MOL: {mol_path}")
    smiles = Chem.MolToSmiles(mol)
    formula = Chem.rdMolDescriptors.CalcMolFormula(mol)
    weight = round(Descriptors.MolWt(mol), 3)
    return mol, smiles, formula, weight


# ---------------------------------------------------------------------------
# Backwards-compatible PubChem wrappers
# ---------------------------------------------------------------------------
# These keep the old tuple-returning signatures working for anything still
# importing them from this module; new code should call pubchem.lookup().

def query_pubchem_by_name(name: str):
    hit = pubchem.query_by_name(clean_name(name))
    if not hit:
        return None, None, None, None, None
    return hit.formula, hit.weight, hit.cid, hit.smiles, hit.source


def query_pubchem_by_smiles(smiles: str):
    hit = pubchem.query_by_smiles(smiles)
    if not hit:
        return None, None, None, None, None
    return hit.formula, hit.weight, hit.cid, hit.smiles, hit.source


fetch_properties_by_cid = pubchem.fetch_properties_by_cid
fetch_cas_by_cid = pubchem.fetch_cas_by_cid
fetch_synonyms_by_cid = pubchem.fetch_synonyms_by_cid
fetch_wikipedia_url_by_cid = pubchem.fetch_wikipedia_url_by_cid


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def formula_weight_match(local_formula, local_weight, hit) -> str:
    """
    The original `Match?`: same formula and a weight within half a unit.

    Kept exactly as it was so match rates stay comparable with workbooks
    produced before the InChIKey columns existed.
    """
    if hit is None or not hit.formula or not local_formula:
        return MATCH_NO
    if hit.formula != local_formula:
        return MATCH_NO
    if hit.weight is None or local_weight is None:
        return MATCH_NO
    return MATCH_YES if abs(hit.weight - local_weight) < WEIGHT_TOLERANCE else MATCH_NO


def inchikey_match(local_keys: list[str], pubchem_key: Optional[str]) -> tuple[str, str]:
    """
    (symbol, explanation) for `InChIKey Match?`.

    A formula match says two molecules weigh the same. An InChIKey match says
    they *are* the same, because the key is a hash of the structure itself —
    which is why this column is reported next to `Match?` rather than folded
    into it: the gap between the two is the measure of what the InChIKey route
    is worth.
    """
    usable = [k for k in local_keys if k]
    if not usable:
        return MATCH_NA, "No InChIKey could be computed from the drawing."
    if not pubchem_key:
        return MATCH_NA, "PubChem returned no InChIKey to compare against."

    agreement = best_agreement(*(inchikey_agreement(k, pubchem_key) for k in usable))
    return agreement_symbol(agreement), agreement_label(agreement)


# ---------------------------------------------------------------------------
# One compound
# ---------------------------------------------------------------------------


def _blank_row(name: str, suffix_tag: str) -> dict:
    row = {col: None for col in ROW_COLUMNS}
    row["Compound Name"] = name
    row["Compound ID"] = suffix_tag
    row["Match?"] = MATCH_NO
    row["InChIKey Match?"] = MATCH_NA
    row["AI Match?"] = MATCH_NA
    # Left empty on purpose: the researcher's own verdict, and the reason the
    # other three columns are machine-written rather than final.
    row["Manual Match"] = None
    return row


def _extract_structure(cdxml_path: str, mol_dir: str, image_dir: str,
                       scratch_dir: str, row: dict) -> dict:
    """ChemDraw + RDKit. Fills the local columns and returns what the rest needs."""
    base = os.path.splitext(os.path.basename(cdxml_path))[0]
    mol_path = os.path.join(mol_dir, base + ".mol")
    tif_path = os.path.join(image_dir, base + ".tif")
    inchi_path = os.path.join(scratch_dir, base + ".inchi")

    chemdraw_inchi = open_and_export(cdxml_path, mol_path, tif_path, inchi_path)
    _mol, smiles, formula, weight = mol_to_smiles_and_props(mol_path)
    rdkit_inchi, rdkit_key, rdkit_warning = inchi_from_molfile(mol_path)

    chemdraw_key = normalize_inchikey(chemdraw_inchi.get("inchikey"))

    row["Extracted SMILES"] = smiles
    row["Local Formula"] = formula
    row["Local Weight"] = weight
    row["RDKit InChI"] = rdkit_inchi
    row["RDKit InChIKey"] = rdkit_key
    row["ChemDraw InChI"] = chemdraw_inchi.get("inchi")
    row["ChemDraw InChIKey"] = chemdraw_key
    row["ChemDraw InChI Source"] = chemdraw_inchi.get("source") or "unavailable"
    row["ChemDraw vs RDKit InChIKey"] = agreement_symbol(
        inchikey_agreement(chemdraw_key, rdkit_key)
    )

    return {
        "base": base, "mol_path": mol_path, "tif_path": tif_path,
        "smiles": smiles, "formula": formula, "weight": weight,
        "rdkit_key": rdkit_key, "chemdraw_key": chemdraw_key,
        "chemdraw_inchi": chemdraw_inchi, "rdkit_warning": rdkit_warning,
    }


def _enrich_from_pubchem(local: dict, name: str, structure_dir: str, row: dict) -> Optional[Any]:
    """
    Search PubChem, strongest evidence first, then pull the winning structure
    back down and rebuild it locally.

    Downloading the SDF is not decoration. Until the record is parsed by RDKit
    and its InChIKey recomputed, "PubChem returned CID 2244" is only a claim
    about a database row; afterwards it is a structure the backend holds and has
    checked against the key it searched with.
    """
    # Either engine's key is worth trying — they disagree exactly when one of
    # them read something the other did not.
    keys = [k for k in (local["rdkit_key"], local["chemdraw_key"]) if k]
    hit = pubchem.lookup(inchikeys=keys, name=clean_name(name), smiles=local["smiles"])

    if hit is None:
        row["PubChem Source"] = "None"
        row["PubChem Match Notes"] = "No PubChem compound matched by InChIKey, name or SMILES."
        return None

    row["PubChem Formula"] = hit.formula
    row["PubChem Weight"] = hit.weight
    row["PubChem CID"] = hit.cid
    row["PubChem SMILES"] = hit.smiles
    row["PubChem InChIKey"] = hit.inchikey
    row["PubChem Source"] = hit.source
    row["PubChem Match Notes"] = hit.notes

    if hit.cid is not None:
        save_path = os.path.join(structure_dir, f"{local['base']}_pubchem_cid{hit.cid}.mol")
        structure = pubchem.fetch_structure(hit.cid, save_path=save_path)
        row["PubChem Structure File"] = (
            os.path.relpath(structure["saved_path"], os.path.dirname(structure_dir))
            if structure.get("saved_path") else None
        )
        # Did the structure we downloaded survive the trip? Compare against the
        # key we searched with, or PubChem's own key when we searched by name.
        reference = next((k for k in keys if k), None) or hit.inchikey
        agreement = inchikey_agreement(reference, structure.get("inchikey"))
        row["PubChem Structure Round Trip"] = (
            f"{agreement_symbol(agreement)} {agreement_label(agreement)}"
            if structure.get("parsed")
            else f"{MATCH_NO} {structure.get('error') or 'Structure could not be rebuilt.'}"
        )
        # PubChem's stored InChIKey can be absent from the property table; the
        # one recomputed from its own structure is just as good and is verified.
        if not row["PubChem InChIKey"]:
            row["PubChem InChIKey"] = structure.get("inchikey")

    return hit


def _fill_reference_fields(cid, row: dict) -> None:
    prop_dict = pubchem.fetch_properties_by_cid(cid) if cid is not None else None
    if cid is not None:
        row["CAS no(s)"] = pubchem.fetch_cas_by_cid(cid)
        row["Synonym"] = pubchem.fetch_synonyms_by_cid(cid)
        row["Wikipedia Link"] = pubchem.fetch_wikipedia_url_by_cid(cid)
        row["PubChem Link"] = f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}"
        row["IUPAC Name"] = prop_dict.get("IUPACName") if prop_dict else None
    for p in PUBCHEM_PROPERTY_NAMES:
        row[p] = prop_dict.get(p) if prop_dict else None


def _run_ai_pass(name: str, local: dict, hit, row: dict) -> None:
    """
    Identification, then consensus, then the deterministic `AI Match?` score.

    The identifier is given the caption, the picture and the formula and nothing
    else — deliberately, so that when it agrees with PubChem the agreement means
    something. The referee is then given everything, including how PubChem was
    found, and decides.
    """
    ai = ai_identify.identify_compound(
        name=name,
        formula=local.get("formula"),
        weight=local.get("weight"),
        image_path=local.get("tif_path"),
        source_file=local.get("base"),
    )

    row["AI Name"] = ai.name
    row["AI IUPAC Name"] = ai.iupac_name
    row["AI Formula"] = ai.formula
    row["AI SMILES"] = ai.smiles
    row["AI InChIKey"] = ai.inchikey
    row["AI Reported InChIKey"] = ai.reported_inchikey
    row["AI CAS"] = ai.cas
    row["AI Compound Class"] = ai.compound_class
    row["AI Confidence"] = ai.confidence
    row["AI Reasoning"] = ai.reasoning
    row["AI Notes"] = ai.notes
    row["AI Sources"] = "; ".join(ai.sources) if ai.sources else None
    row["AI Web Searches"] = ai.web_searches
    row["AI Model"] = ai.model
    row["AI Error"] = ai.error

    if not ai.attempted:
        row["AI Match?"] = MATCH_NA
        row["AI Match Detail"] = ai.error
        row["Consensus Error"] = ai.error
        return

    evidence = {
        "caption_on_drawing": name,
        "chemdraw_rdkit": {
            "smiles": local.get("smiles"),
            "molecular_formula": local.get("formula"),
            "molecular_weight": local.get("weight"),
            "rdkit_inchikey": local.get("rdkit_key"),
            "chemdraw_inchikey": local.get("chemdraw_key"),
            "chemdraw_inchi_source": (local.get("chemdraw_inchi") or {}).get("source"),
            "chemdraw_vs_rdkit": row.get("ChemDraw vs RDKit InChIKey"),
        },
        "pubchem": (
            {
                "found": True,
                "found_by": hit.source,
                "search_term": hit.query,
                "cid": hit.cid,
                "title": hit.title,
                "iupac_name": hit.iupac_name,
                "molecular_formula": hit.formula,
                "molecular_weight": hit.weight,
                "smiles": hit.smiles,
                "inchikey": hit.inchikey,
                "skeleton_candidates": hit.candidates,
                "notes": hit.notes,
                "structure_round_trip": row.get("PubChem Structure Round Trip"),
                "cas": row.get("CAS no(s)"),
            }
            if hit is not None
            else {"found": False, "notes": row.get("PubChem Match Notes")}
        ),
        "ai_identification": (
            {
                "name": ai.name,
                "iupac_name": ai.iupac_name,
                "molecular_formula": ai.formula,
                "smiles": ai.smiles,
                "inchikey_computed_from_its_smiles": ai.inchikey,
                "cas": ai.cas,
                "compound_class": ai.compound_class,
                "confidence": ai.confidence,
                "reasoning": ai.reasoning,
                "notes": ai.notes,
                "saw_structure_image": ai.had_image,
                "web_searches": ai.web_searches,
            }
            if ai.ok
            else {"available": False, "error": ai.error}
        ),
        "precomputed_comparisons": {
            "formula_weight_match": row.get("Match?"),
            "inchikey_match": row.get("InChIKey Match?"),
            "inchikey_match_detail": row.get("InChIKey Match Detail"),
        },
    }

    consensus = ai_identify.build_consensus(evidence)
    row["Consensus Name"] = consensus.get("final_name")
    row["Consensus Formula"] = consensus.get("final_formula")
    row["Consensus SMILES"] = consensus.get("final_smiles")
    row["Consensus InChIKey"] = consensus.get("final_inchikey")
    row["Consensus CAS"] = consensus.get("final_cas")
    row["Consensus Winning Source"] = consensus.get("winning_source")
    row["Consensus Agreement"] = consensus.get("agreement")
    row["Consensus Confidence"] = consensus.get("confidence")
    row["Consensus Rationale"] = consensus.get("rationale")
    row["Consensus Model"] = consensus.get("model")
    row["Consensus Error"] = consensus.get("error")
    needs_review = consensus.get("needs_review")
    row["Consensus Needs Review"] = None if needs_review is None else ("Yes" if needs_review else "No")

    votes = consensus.get("votes")
    if isinstance(votes, list) and votes:
        row["Consensus Votes"] = " | ".join(
            f"{v.get('source')}: {v.get('weight')} — "
            f"{'agrees' if v.get('agrees_with_final') else 'differs'}"
            + (f" ({v.get('claim')})" if v.get("claim") else "")
            for v in votes
            if isinstance(v, dict)
        )

    symbol, detail = ai_identify.score_ai_match(
        ai,
        [local.get("rdkit_key"), local.get("chemdraw_key")],
        local.get("formula"),
        consensus,
    )
    row["AI Match?"] = symbol
    row["AI Match Detail"] = detail


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
    scratch_dir: str | None = None,
) -> list[dict]:
    """
    Two passes over the molecules.

    Pass 1 is strictly sequential because it drives ChemDraw over COM, and two
    pipelines touching one ChemDraw instance would open and close each other's
    documents. Pass 2 is network-bound with no such constraint, so the AI work
    runs several compounds at a time.
    """
    os.makedirs(mol_dir, exist_ok=True)
    os.makedirs(image_dir, exist_ok=True)
    structure_dir = structure_dir or os.path.join(os.path.dirname(mol_dir), "pubchem_structures")
    scratch_dir = scratch_dir or os.path.join(os.path.dirname(mol_dir), "chemdraw_inchi")
    os.makedirs(structure_dir, exist_ok=True)
    os.makedirs(scratch_dir, exist_ok=True)

    rows: list[dict] = []
    pending: list[tuple[int, str, dict, dict, Any]] = []

    # ── Pass 1: ChemDraw, RDKit, PubChem ──────────────────────────────────────
    for idx, cdxml_path in enumerate(cdxml_paths, 1):
        filename = os.path.basename(cdxml_path)
        name = extract_name_from_cdxml(cdxml_path)
        suffix = extract_suffix_tag(name)
        row = _blank_row(name, suffix)

        try:
            local = _extract_structure(cdxml_path, mol_dir, image_dir, scratch_dir, row)
            if local.get("rdkit_warning"):
                emit({"type": "log", "level": "warn",
                      "message": f"{filename}: {local['rdkit_warning']}"})

            hit = _enrich_from_pubchem(local, name, structure_dir, row)
            row["Match?"] = formula_weight_match(local["formula"], local["weight"], hit)
            symbol, detail = inchikey_match(
                [local["rdkit_key"], local["chemdraw_key"]],
                hit.inchikey if hit else None,
            )
            row["InChIKey Match?"] = symbol
            row["InChIKey Match Detail"] = detail

            _fill_reference_fields(hit.cid if hit else None, row)
            pending.append((idx, name, row, local, hit))

        except Exception as exc:
            emit({"type": "log", "level": "error", "message": f"Failed {filename}: {exc}"})
            row["Extracted SMILES"] = row["Local Formula"] = row["Local Weight"] = "Error"
            row["InChIKey Match Detail"] = f"Structure extraction failed: {exc}"
            row["AI Match Detail"] = "Skipped — the structure could not be extracted."

        rows.append(row)
        emit({
            "type": "compound", "name": name, "match": row["Match?"],
            "inchikeyMatch": row["InChIKey Match?"], "aiMatch": row["AI Match?"],
            "index": idx, "total": total, "stage": "structure",
        })

    # ── Pass 2: identification and consensus ──────────────────────────────────
    if not pending:
        return rows

    if not ai_identify.is_enabled():
        reason = "AI identification is off (no ANTHROPIC_API_KEY, or CHEMDRAW_AI_ENABLED=0)."
        emit({"type": "log", "level": "info", "message": reason})
        for _idx, _name, row, _local, _hit in pending:
            row["AI Match Detail"] = reason
        return rows

    emit({
        "type": "stage", "stage": 4, "total": 4,
        "message": (
            f"Identifying {len(pending)} compound(s) with {ai_identify.IDENTIFY_MODEL} "
            f"and reconciling with {ai_identify.CONSENSUS_MODEL}…"
        ),
    })

    def work(item):
        idx, name, row, local, hit = item
        try:
            _run_ai_pass(name, local, hit, row)
        except Exception as exc:
            # A failed identification must cost the row its AI columns, nothing more.
            row["AI Match?"] = MATCH_NA
            row["AI Match Detail"] = f"AI pass failed: {exc}"
            row["AI Error"] = str(exc)
        return item

    done = 0
    with ThreadPoolExecutor(max_workers=min(AI_CONCURRENCY, len(pending))) as pool:
        for idx, name, row, _local, _hit in pool.map(work, pending):
            done += 1
            emit({
                "type": "compound", "name": name, "match": row["Match?"],
                "inchikeyMatch": row["InChIKey Match?"], "aiMatch": row["AI Match?"],
                "index": idx, "total": total, "stage": "ai",
                "aiProgress": done, "aiTotal": len(pending),
            })

    return rows
