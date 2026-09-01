"""
Every PubChem call the pipeline makes, in one place.

Lookups are ordered by how much they can be trusted. An InChIKey is a hash of
the structure itself, so a key hit is an identification; a name hit is only ever
a guess that two people spelled a compound the same way. The pipeline therefore
tries the key first and records which route actually produced the answer, so a
researcher reading the workbook can weigh the row accordingly.

Nothing here needs ChemDraw or Windows — it is plain HTTP plus RDKit, which is
what makes the InChIKey flow testable on any machine.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Any, Optional

import requests

from inchi_tools import (
    inchi_from_molblock,
    inchikey_agreement,
    inchikey_skeleton,
    normalize_inchikey,
)

BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
VIEW = "https://pubchem.ncbi.nlm.nih.gov/rest/pug_view"

# PubChem asks for no more than 5 requests/second from one client.
PUBCHEM_DELAY = 0.25

# Properties fetched on the first, cheap lookup — enough to identify the
# compound and compare it against what came out of ChemDraw.
LOOKUP_PROPS = (
    "MolecularFormula,MolecularWeight,SMILES,ConnectivitySMILES,"
    "InChI,InChIKey,IUPACName,Title"
)

# The full property sweep, fetched once a CID is known.
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

# Reference compounds for the round-trip self-test. These are stable, famous
# CIDs, so a failure here means the network or PubChem is the problem — not the
# molecule being looked up.
KNOWN_INCHIKEYS = {
    "aspirin": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
    "caffeine": "RYYVLZVUVIJVGH-UHFFFAOYSA-N",
    "paracetamol": "RZVAJINKPMORJF-UHFFFAOYSA-N",
    "glucose": "WQZGKKKJIJFFOK-GASJEMHNSA-N",
    "benzene": "UHOVQNZJYSORNB-UHFFFAOYSA-N",
}
DEFAULT_SELFTEST_KEY = KNOWN_INCHIKEYS["aspirin"]


def _rate_limit() -> None:
    time.sleep(PUBCHEM_DELAY)


def _as_weight(value) -> Optional[float]:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


@dataclass
class PubChemHit:
    """One PubChem compound plus the route that found it."""
    cid: Optional[int] = None
    formula: Optional[str] = None
    weight: Optional[float] = None
    smiles: Optional[str] = None
    connectivity_smiles: Optional[str] = None
    inchi: Optional[str] = None
    inchikey: Optional[str] = None
    iupac_name: Optional[str] = None
    title: Optional[str] = None
    # How this hit was found: "InChIKey", "InChIKey skeleton", "Name", "SMILES".
    source: str = "None"
    # What was actually sent to PubChem, so a surprising row can be re-run by hand.
    query: Optional[str] = None
    # For a skeleton search: how many CIDs shared the connectivity block.
    candidates: int = 0
    notes: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_properties(cls, props: dict, source: str, query: str) -> "PubChemHit":
        return cls(
            cid=props.get("CID"),
            formula=props.get("MolecularFormula"),
            weight=_as_weight(props.get("MolecularWeight")),
            smiles=props.get("SMILES") or props.get("CanonicalSMILES"),
            connectivity_smiles=props.get("ConnectivitySMILES"),
            inchi=props.get("InChI"),
            inchikey=normalize_inchikey(props.get("InChIKey")),
            iupac_name=props.get("IUPACName"),
            title=props.get("Title"),
            source=source,
            query=query,
        )


def _get_properties(url: str, timeout: int = 15) -> list[dict]:
    try:
        _rate_limit()
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json().get("PropertyTable", {}).get("Properties", []) or []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Lookups, most trustworthy first
# ---------------------------------------------------------------------------


def query_by_inchikey(inchikey: str) -> Optional[PubChemHit]:
    """
    Exact InChIKey lookup — the strongest identification PubChem can give.

    All three blocks must agree, so a hit means PubChem holds this exact
    structure including stereochemistry and protonation state.
    """
    key = normalize_inchikey(inchikey)
    if not key:
        return None
    props = _get_properties(f"{BASE}/compound/inchikey/{key}/property/{LOOKUP_PROPS}/JSON")
    if not props:
        return None
    return PubChemHit.from_properties(props[0], "InChIKey", key)


def query_by_inchikey_skeleton(inchikey: str, max_candidates: int = 25) -> Optional[PubChemHit]:
    """
    Fall back to the 14-character connectivity block.

    A drawing with undefined stereochemistry — extremely common in a scheme
    where the stereocentre is drawn flat — produces a different full key from
    the same compound in PubChem while sharing the skeleton. Searching the
    skeleton recovers those, at the cost of a weaker claim: the result is the
    right connectivity, not necessarily the right stereoisomer. The row is
    marked accordingly rather than being passed off as an exact match.
    """
    skeleton = inchikey_skeleton(inchikey)
    if not skeleton:
        return None

    try:
        _rate_limit()
        r = requests.get(f"{BASE}/compound/inchikey/{skeleton}/cids/JSON", timeout=15)
        r.raise_for_status()
        cids = (r.json().get("IdentifierList") or {}).get("CID") or []
    except Exception:
        return None

    if not cids:
        return None

    # Lowest CIDs are the oldest, best-curated records — usually the parent
    # compound rather than a salt, isotopologue or later-deposited variant.
    shortlist = sorted(int(c) for c in cids)[:max_candidates]
    joined = ",".join(str(c) for c in shortlist)
    props = _get_properties(f"{BASE}/compound/cid/{joined}/property/{LOOKUP_PROPS}/JSON", timeout=30)
    if not props:
        return None

    wanted = normalize_inchikey(inchikey)

    def score(entry: dict) -> tuple:
        candidate = normalize_inchikey(entry.get("InChIKey"))
        agreement = inchikey_agreement(wanted, candidate)
        # Prefer an exact key, then a neutral parent (protonation block "N"),
        # then the lowest CID.
        return (
            0 if agreement == "exact" else 1,
            0 if (candidate or "").endswith("-N") else 1,
            int(entry.get("CID") or 10**12),
        )

    best = sorted(props, key=score)[0]
    hit = PubChemHit.from_properties(best, "InChIKey skeleton", skeleton)
    hit.candidates = len(cids)
    hit.notes = (
        f"Matched on connectivity only ({skeleton}); {len(cids)} PubChem "
        "compound(s) share this skeleton."
    )
    return hit


def query_by_name(name: str) -> Optional[PubChemHit]:
    """Name lookup — convenient, but only as good as the label on the drawing."""
    if not name:
        return None
    safe = urllib.parse.quote(name, safe="")
    props = _get_properties(f"{BASE}/compound/name/{safe}/property/{LOOKUP_PROPS}/JSON")
    if not props:
        return None
    return PubChemHit.from_properties(props[0], "Name", name)


def query_by_smiles(smiles: str) -> Optional[PubChemHit]:
    """SMILES lookup — structural, but sensitive to how the SMILES was written."""
    if not smiles:
        return None
    safe = urllib.parse.quote(smiles, safe="")
    props = _get_properties(f"{BASE}/compound/smiles/{safe}/property/{LOOKUP_PROPS}/JSON")
    if not props:
        return None
    hit = PubChemHit.from_properties(props[0], "SMILES", smiles)
    if not hit.smiles:
        hit.smiles = smiles
    return hit


def lookup(
    inchikeys: list[str] | None = None,
    name: str | None = None,
    smiles: str | None = None,
    allow_skeleton: bool = True,
) -> Optional[PubChemHit]:
    """
    Find a compound, strongest evidence first.

    Order: exact InChIKey → InChIKey skeleton → name → SMILES. `inchikeys` takes
    several candidate keys because ChemDraw and RDKit can each produce one and
    they do not always agree; either is worth trying before falling back to the
    label on the drawing.
    """
    tried: list[str] = []
    for key in inchikeys or []:
        normalized = normalize_inchikey(key)
        if not normalized or normalized in tried:
            continue
        tried.append(normalized)
        hit = query_by_inchikey(normalized)
        if hit:
            return hit

    if allow_skeleton:
        for key in tried:
            hit = query_by_inchikey_skeleton(key)
            if hit:
                return hit

    if name:
        hit = query_by_name(name)
        if hit:
            return hit

    if smiles:
        hit = query_by_smiles(smiles)
        if hit:
            return hit

    return None


# ---------------------------------------------------------------------------
# Pulling a structure back out of PubChem
# ---------------------------------------------------------------------------


def fetch_sdf(cid) -> Optional[str]:
    """The 2D SDF record for a CID — a real connection table, not just a string."""
    if cid is None:
        return None
    try:
        _rate_limit()
        r = requests.get(f"{BASE}/compound/cid/{cid}/record/SDF", timeout=30)
        r.raise_for_status()
        text = r.text
        return text if text and "$$$$" in text else (text or None)
    except Exception:
        return None


def fetch_structure(cid, save_path: str | None = None) -> dict[str, Any]:
    """
    Download a CID's structure and rebuild it locally in RDKit.

    This is the step that turns a PubChem answer into something the rest of the
    backend can work with: the SDF is parsed into a real molecule, its
    properties are recomputed from the connection table rather than trusted from
    the JSON, and — if `save_path` is given — it is written to disk alongside the
    MOL that ChemDraw produced, so the two structures can be diffed later.
    """
    result: dict[str, Any] = {
        "cid": cid, "sdf_downloaded": False, "parsed": False, "saved_path": None,
        "smiles": None, "formula": None, "weight": None,
        "inchi": None, "inchikey": None, "error": None,
    }

    sdf = fetch_sdf(cid)
    if not sdf:
        result["error"] = f"PubChem returned no SDF record for CID {cid}."
        return result
    result["sdf_downloaded"] = True

    # RDKit wants a single MOL block; an SDF record ends with the $$$$ delimiter.
    molblock = sdf.split("$$$$")[0]

    from rdkit import Chem
    from rdkit.Chem import Descriptors, rdMolDescriptors

    mol = Chem.MolFromMolBlock(molblock)
    if mol is None:
        result["error"] = "RDKit could not parse the SDF record PubChem returned."
        return result
    result["parsed"] = True

    try:
        result["smiles"] = Chem.MolToSmiles(mol)
        result["formula"] = rdMolDescriptors.CalcMolFormula(mol)
        result["weight"] = round(Descriptors.MolWt(mol), 3)
    except Exception as exc:
        result["error"] = f"Could not compute properties from the PubChem structure: {exc}"

    inchi, key, warning = inchi_from_molblock(molblock)
    result["inchi"], result["inchikey"] = inchi, key
    if warning and not result["error"]:
        result["error"] = warning

    if save_path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
            with open(save_path, "w", encoding="utf-8") as fh:
                fh.write(molblock)
            result["saved_path"] = save_path
        except Exception as exc:
            result["error"] = result["error"] or f"Could not save the structure: {exc}"

    return result


def round_trip_inchikey(inchikey: str, save_path: str | None = None) -> dict[str, Any]:
    """
    Push an InChIKey into PubChem, pull the structure back, and check it survived.

    Every step is reported separately so a failure says *which* link broke:

      1. the key is well-formed
      2. PubChem resolves it to a CID
      3. the SDF for that CID downloads
      4. RDKit parses the SDF into a molecule
      5. recomputing the InChIKey from that molecule returns the key we started with

    Step 5 is the real test. PubChem's stored InChIKey and a key recomputed from
    PubChem's own connection table are two different artefacts, and if they
    disagree then the structure the backend is about to use is not the structure
    that was asked for.
    """
    started = time.time()
    report: dict[str, Any] = {
        "requested_inchikey": inchikey,
        "normalized_inchikey": None,
        "valid_inchikey": False,
        "found": False,
        "matched_on": None,
        "cid": None,
        "pubchem": None,
        "structure": None,
        "recomputed_inchikey": None,
        "round_trip": "unknown",
        "round_trip_ok": False,
        "message": None,
        "elapsed_seconds": None,
    }

    key = normalize_inchikey(inchikey)
    report["normalized_inchikey"] = key
    if not key:
        report["message"] = (
            "Not a valid InChIKey. Expected 14 letters, a hyphen, 10 letters, "
            "a hyphen, then 1 letter — e.g. BSYNRYMUTXBXSQ-UHFFFAOYSA-N."
        )
        report["elapsed_seconds"] = round(time.time() - started, 3)
        return report
    report["valid_inchikey"] = True

    hit = query_by_inchikey(key) or query_by_inchikey_skeleton(key)
    if hit is None:
        report["message"] = f"PubChem has no compound for InChIKey {key}."
        report["elapsed_seconds"] = round(time.time() - started, 3)
        return report

    report["found"] = True
    report["matched_on"] = hit.source
    report["cid"] = hit.cid
    report["pubchem"] = hit.to_dict()

    structure = fetch_structure(hit.cid, save_path=save_path)
    report["structure"] = structure
    report["recomputed_inchikey"] = structure.get("inchikey")

    agreement = inchikey_agreement(key, structure.get("inchikey"))
    report["round_trip"] = agreement
    report["round_trip_ok"] = agreement == "exact"

    if agreement == "exact":
        report["message"] = (
            f"Round trip clean: {key} → CID {hit.cid} → SDF → RDKit → {key}."
        )
    elif agreement == "skeleton":
        report["message"] = (
            f"Round trip returned the right skeleton but a different stereochemistry "
            f"or protonation state: asked for {key}, rebuilt "
            f"{structure.get('inchikey')}."
        )
    elif agreement == "mismatch":
        report["message"] = (
            f"Round trip returned a different structure: asked for {key}, rebuilt "
            f"{structure.get('inchikey')}."
        )
    else:
        report["message"] = (
            structure.get("error")
            or "Could not recompute an InChIKey from the structure PubChem returned."
        )

    report["elapsed_seconds"] = round(time.time() - started, 3)
    return report


def selftest(inchikey: str | None = None, save_path: str | None = None) -> dict[str, Any]:
    """
    The InChIKey flow end to end against a compound with a known answer.

    Runs on any machine — no ChemDraw, no Windows — so the PubChem and RDKit
    half of the pipeline can be verified on a Mac while ChemDraw itself cannot
    even start.
    """
    key = inchikey or DEFAULT_SELFTEST_KEY
    report = round_trip_inchikey(key, save_path=save_path)
    report["known_compounds"] = KNOWN_INCHIKEYS
    report["is_reference_compound"] = normalize_inchikey(key) in {
        normalize_inchikey(v) for v in KNOWN_INCHIKEYS.values()
    }
    return report


# ---------------------------------------------------------------------------
# Deep enrichment once a CID is known
# ---------------------------------------------------------------------------


def fetch_properties_by_cid(cid) -> Optional[dict]:
    if cid is None:
        return None
    props_str = ",".join(PUBCHEM_PROPERTY_NAMES)
    props = _get_properties(f"{BASE}/compound/cid/{cid}/property/{props_str}/JSON", timeout=30)
    if not props:
        return None
    out = dict(props[0])
    for k, v in list(out.items()):
        if isinstance(v, (list, dict)):
            out[k] = json.dumps(v) if v else None
    return out


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


def fetch_cas_by_cid(cid) -> Optional[str]:
    if cid is None:
        return None
    url = f"{VIEW}/data/compound/{cid}/JSON?heading=CAS"
    try:
        _rate_limit()
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        section = _find_section(r.json().get("Record") or {}, "CAS")
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


def fetch_synonyms_by_cid(cid) -> Optional[str]:
    if cid is None:
        return None
    try:
        _rate_limit()
        r = requests.get(f"{BASE}/compound/cid/{cid}/synonyms/JSON", timeout=15)
        r.raise_for_status()
        info_list = (r.json().get("InformationList") or {}).get("Information") or []
        if not info_list:
            return None
        syns = info_list[0].get("Synonym") or []
        return "; ".join(str(s) for s in syns[:500]) if syns else None
    except Exception:
        return None


def fetch_wikipedia_url_by_cid(cid) -> Optional[str]:
    if cid is None:
        return None
    for heading in ("Wikipedia", "WIkipedia"):
        try:
            _rate_limit()
            r = requests.get(f"{VIEW}/data/compound/{cid}/JSON?heading={heading}", timeout=15)
            r.raise_for_status()
            section = _find_section(r.json().get("Record") or {}, heading)
            if section is None:
                continue
            for info in section.get("Information") or []:
                if info.get("URL"):
                    return info["URL"]
        except Exception:
            continue
    return None
