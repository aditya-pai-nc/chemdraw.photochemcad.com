"""
Orchestrates the 3-stage ChemDraw processing pipeline.
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
    excel_path = os.path.join(output_dir, cdx_name + "_compounds.xlsx")

    # ── Stage 1: CDX → CDXML ──────────────────────────────────────────────────
    emit({"type": "stage", "stage": 1, "total": 3, "message": "Converting CDX to CDXML via ChemDraw…"})
    from cdx_to_cdxml import automate_chemdraw_conversion_to_cdxml
    try:
        automate_chemdraw_conversion_to_cdxml(cdx_path, cdxml_path)
    except Exception as e:
        raise RuntimeError(f"Stage 1 failed (CDX → CDXML): {e}") from e

    if not os.path.isfile(cdxml_path):
        raise RuntimeError(f"Stage 1 produced no output — CDXML not found at: {cdxml_path}")

    # ── Stage 2: CDXML → individual molecule CDXML files ──────────────────────
    emit({"type": "stage", "stage": 2, "total": 3, "message": "Splitting CDXML into individual molecules…"})
    from cdxml_to_ind import split_cdxml
    try:
        mol_paths = split_cdxml(cdxml_path, split_dir)
    except Exception as e:
        raise RuntimeError(f"Stage 2 failed (split CDXML): {e}") from e

    if not mol_paths:
        raise RuntimeError("Stage 2 found no molecules in the CDXML file.")

    emit({
        "type": "stage", "stage": 2, "total": 3,
        "message": f"Found {len(mol_paths)} molecule(s) — starting enrichment…"
    })

    # ── Stage 3: Process each molecule → MOL + PubChem enrichment ─────────────
    emit({"type": "stage", "stage": 3, "total": 3, "message": f"Processing {len(mol_paths)} compound(s) with RDKit and PubChem…"})

    from processor import process_molecules
    rows = process_molecules(
        cdxml_paths=mol_paths,
        mol_dir=mol_dir,
        image_dir=image_dir,
        total=len(mol_paths),
        emit=emit
    )

    # Write Excel
    import pandas as pd
    from processor import PUBCHEM_PROPERTY_NAMES

    col_order = (
        ["Compound Name", "Compound ID", "Extracted SMILES", "Local Formula", "Local Weight",
         "PubChem Formula", "PubChem Weight", "PubChem CID", "PubChem SMILES", "PubChem Source", "Match?",
         "CAS no(s)", "Synonym", "IUPAC Name", "PubChem Link", "Wikipedia Link"]
        + PUBCHEM_PROPERTY_NAMES
    )
    df = pd.DataFrame(rows)
    df = df.reindex(columns=[c for c in col_order if c in df.columns], fill_value=None)
    df.to_excel(excel_path, index=False)

    emit({
        "type": "result",
        "success": True,
        "excelPath": excel_path,
        "compoundCount": len(rows),
        "outputDir": output_dir
    })
