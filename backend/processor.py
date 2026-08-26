"""
Stage 3: Process individual CDXML files.
Adapted from ind_cdxml_processing.py — all paths are parameters, no globals.
Progress events are emitted via the emit() callback.
"""
import os
import time
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
import requests
from typing import Callable

from rdkit import Chem
from rdkit.Chem import Descriptors
from chemdraw_com import connect_chemdraw

PUBCHEM_DELAY = 0.25

PUBCHEM_PROPERTY_NAMES = [
    "MolecularFormula", "MolecularWeight", "SMILES", "ConnectivitySMILES",
    "InChI", "InChIKey", "IUPACName", "Title", "XLogP", "ExactMass", "MonoisotopicMass",
    "TPSA", "Complexity", "Charge", "HBondDonorCount", "HBondAcceptorCount",
    "RotatableBondCount", "HeavyAtomCount", "IsotopeAtomCount",
    "AtomStereoCount", "DefinedAtomStereoCount", "UndefinedAtomStereoCount",
    "BondStereoCount", "DefinedBondStereoCount", "UndefinedBondStereoCount",
    "CovalentUnitCount", "PatentCount", "PatentFamilyCount",
    "AnnotationTypes", "AnnotationTypeCount", "SourceCategories", "LiteratureCount",
    "Volume3D", "XStericQuadrupole3D", "YStericQuadrupole3D", "ZStericQuadrupole3D",
    "FeatureCount3D", "FeatureAcceptorCount3D", "FeatureDonorCount3D",
    "FeatureAnionCount3D", "FeatureCationCount3D", "FeatureRingCount3D", "FeatureHydrophobeCount3D",
    "ConformerModelRMSD3D", "EffectiveRotorCount3D", "ConformerCount3D",
    "Fingerprint2D",
]


def _rate_limit() -> None:
    time.sleep(PUBCHEM_DELAY)


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


def open_and_save_as_mol_and_tif(cdxml_path: str, mol_path: str, tif_path: str) -> None:
    chemdraw, _ = connect_chemdraw()
    chemdraw.Visible = False
    doc = chemdraw.Documents.Open(os.path.abspath(cdxml_path))
    doc.Activate()
    time.sleep(1)
    doc.SaveAs(os.path.abspath(mol_path))
    doc.SaveAs(os.path.abspath(tif_path))
    doc.Close()


def mol_to_smiles_and_props(mol_path: str):
    mol = Chem.MolFromMolFile(mol_path)
    if mol is None:
        raise ValueError(f"RDKit could not parse MOL: {mol_path}")
    smiles  = Chem.MolToSmiles(mol)
    formula = Chem.rdMolDescriptors.CalcMolFormula(mol)
    weight  = round(Descriptors.MolWt(mol), 3)
    return mol, smiles, formula, weight


def query_pubchem_by_name(name: str):
    safe = urllib.parse.quote(clean_name(name), safe='')
    url = (f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{safe}"
           f"/property/MolecularFormula,MolecularWeight,CanonicalSMILES/JSON")
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        p = r.json()['PropertyTable']['Properties'][0]
        return p.get('MolecularFormula'), round(float(p.get('MolecularWeight', 0)), 3), p.get('CID'), p.get('CanonicalSMILES'), "Name"
    except Exception:
        return None, None, None, None, None


def query_pubchem_by_smiles(smiles: str):
    safe = urllib.parse.quote(smiles, safe='')
    url = (f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/smiles/{safe}"
           f"/property/MolecularFormula,MolecularWeight/JSON")
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        p = r.json()['PropertyTable']['Properties'][0]
        return p.get('MolecularFormula'), round(float(p.get('MolecularWeight', 0)), 3), p.get('CID'), smiles, "SMILES"
    except Exception:
        return None, None, None, None, None


def fetch_properties_by_cid(cid) -> dict | None:
    if cid is None:
        return None
    props_str = ",".join(PUBCHEM_PROPERTY_NAMES)
    url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{cid}/property/{props_str}/JSON"
    try:
        _rate_limit()
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        plist = r.json().get("PropertyTable", {}).get("Properties", [])
        if not plist:
            return None
        out = dict(plist[0])
        for k, v in list(out.items()):
            if isinstance(v, (list, dict)):
                out[k] = json.dumps(v) if v else None
        return out
    except Exception:
        return None


def _find_section(obj, heading: str):
    if not isinstance(obj, dict):
        return None
    if obj.get("TOCHeading") == heading:
        return obj
    for key in ("Section", "section"):
        for item in obj.get(key) or []:
            found = _find_section(item, heading)
            if found is not None:
                return found
    return None


def fetch_cas_by_cid(cid) -> str | None:
    if cid is None:
        return None
    url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON?heading=CAS"
    try:
        _rate_limit()
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        record = r.json().get("Record") or {}
        section = _find_section(record, "CAS")
        if section is None:
            return None
        cas_set = set()
        for info in section.get("Information") or []:
            for swm in (info.get("Value") or {}).get("StringWithMarkup") or []:
                s = (swm.get("String") or "").strip()
                if s and re.match(r"^\d+-\d+-\d+$", s):
                    cas_set.add(s)
        return ", ".join(sorted(cas_set)) if cas_set else None
    except Exception:
        return None


def fetch_synonyms_by_cid(cid) -> str | None:
    if cid is None:
        return None
    url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/{cid}/synonyms/JSON"
    try:
        _rate_limit()
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        info_list = (r.json().get("InformationList") or {}).get("Information") or []
        if not info_list:
            return None
        syns = info_list[0].get("Synonym") or []
        return "; ".join(str(s) for s in syns[:500]) if syns else None
    except Exception:
        return None


def fetch_wikipedia_url_by_cid(cid) -> str | None:
    if cid is None:
        return None
    for heading in ("Wikipedia", "WIkipedia"):
        url = f"https://pubchem.ncbi.nlm.nih.gov/rest/pug_view/data/compound/{cid}/JSON?heading={heading}"
        try:
            _rate_limit()
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            record = r.json().get("Record") or {}
            section = _find_section(record, heading)
            if section is None:
                continue
            for info in section.get("Information") or []:
                u = info.get("URL")
                if u:
                    return u
        except Exception:
            continue
    return None


def _empty_row(name: str, suffix_tag: str) -> dict:
    row: dict = {
        "Compound Name": name, "Compound ID": suffix_tag,
        "Extracted SMILES": "Error", "Local Formula": "Error", "Local Weight": "Error",
        "PubChem Formula": None, "PubChem Weight": None, "PubChem CID": None,
        "PubChem SMILES": None, "PubChem Source": "None", "Match?": "❌",
        "CAS no(s)": None, "Synonym": None, "IUPAC Name": None,
        "PubChem Link": None, "Wikipedia Link": None,
    }
    for p in PUBCHEM_PROPERTY_NAMES:
        row[p] = None
    return row


def process_molecules(
    cdxml_paths: list[str],
    mol_dir: str,
    image_dir: str,
    total: int,
    emit: Callable
) -> list[dict]:
    os.makedirs(mol_dir, exist_ok=True)
    os.makedirs(image_dir, exist_ok=True)

    rows = []
    for idx, cdxml_path in enumerate(cdxml_paths, 1):
        filename = os.path.basename(cdxml_path)
        base     = os.path.splitext(filename)[0]
        name     = extract_name_from_cdxml(cdxml_path)
        suffix   = extract_suffix_tag(name)

        mol_path = os.path.join(mol_dir, base + ".mol")
        tif_path = os.path.join(image_dir, base + ".tif")

        try:
            open_and_save_as_mol_and_tif(cdxml_path, mol_path, tif_path)
            _mol, smiles, formula, weight = mol_to_smiles_and_props(mol_path)

            pub_formula, pub_weight, pub_cid, pub_smiles, source = query_pubchem_by_name(name)
            if not pub_formula:
                pub_formula, pub_weight, pub_cid, pub_smiles, source = query_pubchem_by_smiles(smiles)

            is_match = "✅" if (
                pub_formula and pub_formula == formula and
                pub_weight is not None and abs(pub_weight - weight) < 0.5
            ) else "❌"

            row: dict = {
                "Compound Name": name, "Compound ID": suffix,
                "Extracted SMILES": smiles, "Local Formula": formula, "Local Weight": weight,
                "PubChem Formula": pub_formula, "PubChem Weight": pub_weight,
                "PubChem CID": pub_cid, "PubChem SMILES": pub_smiles,
                "PubChem Source": source or "None", "Match?": is_match,
            }

            prop_dict = None
            if pub_cid is not None:
                prop_dict      = fetch_properties_by_cid(pub_cid)
                row["CAS no(s)"]     = fetch_cas_by_cid(pub_cid)
                row["Synonym"]       = fetch_synonyms_by_cid(pub_cid)
                row["Wikipedia Link"]= fetch_wikipedia_url_by_cid(pub_cid)
                row["PubChem Link"]  = f"https://pubchem.ncbi.nlm.nih.gov/compound/{pub_cid}"
                row["IUPAC Name"]    = (prop_dict.get("IUPACName") if prop_dict else None)
            else:
                row["CAS no(s)"] = row["Synonym"] = row["Wikipedia Link"] = row["PubChem Link"] = row["IUPAC Name"] = None

            for p in PUBCHEM_PROPERTY_NAMES:
                row[p] = (prop_dict.get(p) if prop_dict else None)

        except Exception as e:
            emit({"type": "log", "level": "error", "message": f"Failed {filename}: {e}"})
            row = _empty_row(name, suffix)
            is_match = "❌"

        rows.append(row)
        emit({"type": "compound", "name": name, "match": row["Match?"], "index": idx, "total": total})

    return rows
