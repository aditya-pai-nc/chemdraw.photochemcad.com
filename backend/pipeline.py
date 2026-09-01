"""
Orchestrates the ChemDraw processing pipeline.
Calls emit() for every progress event so the web UI can update in real time.
"""
import os
from typing import Callable


def run_full_pipeline(cdx_path: str, output_dir: str, emit: Callable) -> None:
    os.makedirs(output_dir, exist_ok=True)

    cdx_name = os.path.splitext(os.path.basename(cdx_path))[0]
    cdxml_path = os.path.join(output_dir, cdx_name + ".cdxml")
    split_dir = os.path.join(output_dir, "split_molecules")
    mol_dir = os.path.join(output_dir, "mol_files")
    image_dir = os.path.join(output_dir, "images")
    # Structures pulled back down from PubChem, kept beside ChemDraw's own MOL
    # files so the two can be compared after the fact.
    structure_dir = os.path.join(output_dir, "pubchem_structures")
    inchi_dir = os.path.join(output_dir, "chemdraw_inchi")
    excel_path = os.path.join(output_dir, cdx_name + "_compounds.xlsx")

    import ai_identify
    # The AI identification and consensus pass only exists when it is configured,
    # so the stage count the UI shows has to reflect that.
    stage_total = 4 if ai_identify.is_enabled() else 3

    # ── Stage 1: CDX → CDXML ──────────────────────────────────────────────────
    emit({"type": "stage", "stage": 1, "total": stage_total, "message": "Converting CDX to CDXML via ChemDraw…"})
    from cdx_to_cdxml import automate_chemdraw_conversion_to_cdxml
    try:
        automate_chemdraw_conversion_to_cdxml(cdx_path, cdxml_path)
    except Exception as e:
        raise RuntimeError(f"Stage 1 failed (CDX → CDXML): {e}") from e

    if not os.path.isfile(cdxml_path):
        raise RuntimeError(f"Stage 1 produced no output — CDXML not found at: {cdxml_path}")

    # ── Stage 2: CDXML → individual molecule CDXML files ──────────────────────
    emit({"type": "stage", "stage": 2, "total": stage_total, "message": "Splitting CDXML into individual molecules…"})
    from cdxml_to_ind import split_cdxml
    try:
        mol_paths = split_cdxml(cdxml_path, split_dir)
    except Exception as e:
        raise RuntimeError(f"Stage 2 failed (split CDXML): {e}") from e

    if not mol_paths:
        raise RuntimeError("Stage 2 found no molecules in the CDXML file.")

    emit({
        "type": "stage", "stage": 2, "total": stage_total,
        "message": f"Found {len(mol_paths)} molecule(s) — starting enrichment…"
    })

    # ── Stage 3: MOL + InChIKey + PubChem (stage 4, the AI pass, is inside) ───
    emit({
        "type": "stage", "stage": 3, "total": stage_total,
        "message": (
            f"Processing {len(mol_paths)} compound(s): ChemDraw and RDKit InChIKeys, "
            "then PubChem…"
        ),
    })

    from processor import ROW_COLUMNS, process_molecules
    rows = process_molecules(
        cdxml_paths=mol_paths,
        mol_dir=mol_dir,
        image_dir=image_dir,
        total=len(mol_paths),
        emit=emit,
        structure_dir=structure_dir,
        scratch_dir=inchi_dir,
    )

    # Write Excel
    import pandas as pd

    df = pd.DataFrame(rows)
    # ROW_COLUMNS is the single source of truth for the layout, so adding a
    # column in processor.py places it in the workbook without touching this file.
    ordered = [c for c in ROW_COLUMNS if c in df.columns]
    df = df.reindex(columns=ordered + [c for c in df.columns if c not in ordered], fill_value=None)
    df.to_excel(excel_path, index=False)

    def tally(column: str, symbol: str) -> int:
        return sum(1 for r in rows if r.get(column) == symbol)

    emit({
        "type": "result",
        "success": True,
        "excelPath": excel_path,
        "compoundCount": len(rows),
        "outputDir": output_dir,
        # Three independent verdicts, reported separately — the point of the
        # extra columns is that a researcher can see where they disagree.
        "matchCount": tally("Match?", "✅"),
        "inchikeyMatchCount": tally("InChIKey Match?", "✅"),
        "inchikeyPartialCount": tally("InChIKey Match?", "🟡"),
        "aiMatchCount": tally("AI Match?", "✅"),
        "needsReviewCount": sum(1 for r in rows if r.get("Consensus Needs Review") == "Yes"),
    })
