"""
Orchestrates the ChemDraw processing pipeline.
Calls emit() for every progress event so the web UI can update in real time.
"""
import os
from typing import Callable

# Written to the workbook as its own tab. A chemist reading the two match
# columns should not have to ask anyone how they were decided, and a method
# buried in a commit message is a method nobody reads.
DESCRIPTION_ROWS: list[tuple[str, str]] = [
    ("HOW MATCHING WORKS", ""),
    ("", ""),
    ("Sheet 1 - Compounds",
     "One row per structure found in the ChemDraw file, with two independent checks."),
    ("", ""),

    ("THE TWO CHECKS", ""),
    ("Match?",
     "Compares molecular formula and molecular weight. A tick means PubChem's formula is "
     "identical to the formula calculated from the drawing, and the two weights agree to "
     "within 0.5. Two different molecules can share a formula and a weight, so this is a "
     "cross-check rather than proof of identity."),
    ("InChIKey Match?",
     "Compares the structures themselves. ChemDraw's SMILES and PubChem's SMILES are both "
     "rewritten in RDKit's canonical form and compared. A tick means they are identical "
     "character for character, i.e. the same structure including stereochemistry."),
    ("", ""),

    ("HOW A COMPOUND IS FOUND IN PUBCHEM", "Tried in this order, stopping at the first hit:"),
    ("1. Exact InChIKey",
     "ChemDraw's own InChIKey, taken from ChemDraw via COM or its Edit > Copy As menu. An "
     "InChIKey is a hash of the structure, so a hit here cannot be a coincidence. Strongest "
     "route."),
    ("2. InChIKey skeleton",
     "The first 14 characters of that key, which encode connectivity only. Used when the "
     "exact key misses - most often because the drawing defines stereochemistry differently "
     "from the deposited record. Finds the right skeleton, not necessarily the right "
     "stereoisomer."),
    ("3. Compound name",
     "The caption printed under the structure. Weak: captions are often lab codes, and two "
     "different compounds can share a name."),
    ("4. SMILES",
     "ChemDraw's SMILES string. Structural, but sensitive to how the SMILES was written."),
    ("Important",
     "Widening the search does not weaken the verdict. However a record is found, "
     "InChIKey Match? is still decided only by whether the canonical SMILES are identical - "
     "so a loose hit that is not the drawn compound still scores a cross."),
    ("", ""),

    ("WHY THE SMILES ARE CANONICALISED",
     "A SMILES string is one of many ways to write the same structure: OC(=O)c1ccccc1 and "
     "c1ccccc1C(O)=O are the same molecule and share almost no characters. ChemDraw and "
     "PubChem each write their own, so both are rewritten by RDKit in one standard form "
     "before they are compared."),
    ("", ""),

    ("MULTIPLE PUBCHEM RECORDS",
     "One InChIKey can resolve to several deposited records - a parent compound, a salt, a "
     "labelled analogue. All of them are listed in 'All PubChem CIDs'. The first is used as "
     "the answer, and any field it leaves empty is filled from the next record that has it."),
    ("", ""),

    ("SYMBOLS", ""),
    ("✅", "The two sources agree."),
    ("❌", "The two sources disagree."),
    ("—", "One side is missing, so there was nothing to compare."),
    ("", ""),

    ("Manual Match", "Left empty on purpose - your own verdict."),
    ("", ""),

    ("Sheet 2 - Unmatched - AI curated",
     "Every compound whose InChIKey Match? is not a tick. A small AI model is given the "
     "ChemDraw data and the PubChem data and asked to reconcile them: what differs, why, and "
     "what to record. It may only quote an InChIKey that already appears in the evidence; "
     "anything else it proposes is discarded and noted in 'Curation Error'. Nothing on that "
     "sheet is used to change sheet 1."),
]


def _write_description(writer, sheet_name: str = "Description") -> None:
    """Lay the method out as a readable page rather than a wall of cells."""
    import pandas as pd

    pd.DataFrame(DESCRIPTION_ROWS).to_excel(
        writer, sheet_name=sheet_name, index=False, header=False
    )
    try:
        from openpyxl.styles import Alignment, Font

        ws = writer.sheets[sheet_name]
        ws.column_dimensions["A"].width = 34
        ws.column_dimensions["B"].width = 105
        for row in ws.iter_rows(min_col=1, max_col=2):
            label, detail = row[0], row[1]
            label.alignment = Alignment(vertical="top", wrap_text=True)
            detail.alignment = Alignment(vertical="top", wrap_text=True)
            # Headings are the rows that carry a label and no detail.
            if label.value and not detail.value:
                label.font = Font(bold=True)
    except Exception:
        # Styling is a nicety; the text is the point.
        pass


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
    excel_path = os.path.join(output_dir, cdx_name + "_compounds.xlsx")

    import ai_curate
    # The curation pass only exists when it is configured, so the stage count the
    # UI shows has to reflect that.
    stage_total = 4 if ai_curate.is_enabled() else 3

    # ── Stage 0: start from a clean ChemDraw ─────────────────────────────────
    # `Document.Close()` is a no-op on some ChemDraw builds — it returns without
    # error and leaves the document open — so a job that opens one document per
    # molecule leaks every one of them. They accumulate across jobs until
    # `Documents.Open` starts returning None and the next run dies in stage 1.
    # Quitting first is the only reliable reset, and it costs a few seconds:
    # jobs are serialised through the ChemDraw queue, so nothing else can be
    # mid-document while this happens.
    from chemdraw_com import open_document_count, reset_chemdraw

    leftover = open_document_count()
    if leftover != 0:
        emit({"type": "log", "level": "info",
              "message": f"ChemDraw has {leftover} document(s) open from earlier runs — restarting it."})
        report = reset_chemdraw()
        if report.get("error"):
            emit({"type": "log", "level": "warn",
                  "message": f"ChemDraw restart reported: {report['error']}"})

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

    # ── Stage 3: ChemDraw formats + RDKit + PubChem (stage 4 is inside) ───────
    emit({
        "type": "stage", "stage": 3, "total": stage_total,
        "message": (
            f"Processing {len(mol_paths)} compound(s): ChemDraw structures, "
            "then PubChem by InChIKey…"
        ),
    })

    from processor import CURATED_COLUMNS, ROW_COLUMNS, process_molecules
    rows, curated_rows = process_molecules(
        cdxml_paths=mol_paths,
        mol_dir=mol_dir,
        image_dir=image_dir,
        total=len(mol_paths),
        emit=emit,
        structure_dir=structure_dir,
    )

    # ── Write the workbook ────────────────────────────────────────────────────
    import pandas as pd

    def frame(records: list[dict], columns: list[str]):
        df = pd.DataFrame(records)
        # The column lists are the single source of truth for layout, so adding
        # a column in processor.py places it in the workbook without touching
        # this file.
        ordered = [c for c in columns if c in df.columns]
        return df.reindex(
            columns=ordered + [c for c in df.columns if c not in ordered], fill_value=None
        )

    df = frame(rows, ROW_COLUMNS)

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Compounds", index=False)
        # Second sheet, always written — an empty one is a meaningful result
        # ("everything matched"), and a sheet that appears and disappears would
        # break anything reading the workbook by position.
        if curated_rows:
            frame(curated_rows, CURATED_COLUMNS).to_excel(
                writer, sheet_name="Unmatched - AI curated", index=False
            )
        else:
            pd.DataFrame(columns=CURATED_COLUMNS).to_excel(
                writer, sheet_name="Unmatched - AI curated", index=False
            )
        _write_description(writer)

    def tally(column: str, value: str) -> int:
        return sum(1 for r in rows if r.get(column) == value)

    emit({
        "type": "result",
        "success": True,
        "excelPath": excel_path,
        "compoundCount": len(rows),
        "outputDir": output_dir,
        # The two verdicts, reported separately. How often the structural check
        # succeeds where formula and weight alone fail is the whole point of
        # keeping both.
        "matchCount": tally("Match?", "✅"),
        "inchikeyMatchCount": tally("InChIKey Match?", "✅"),
        "curatedCount": len(curated_rows),
    })
