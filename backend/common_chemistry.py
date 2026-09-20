"""Independent CAS Common Chemistry verification, after PubChem enrichment.

API reference: https://commonchemistry.cas.org/api-overview
Credentials belong to the backend; records are never merged across CAS RNs.
"""
from __future__ import annotations

import html
import math
import os
import re
import time
from typing import Any
from urllib.parse import urlencode

import requests
from rdkit import Chem

from inchi_tools import canonical_smiles, normalize_inchikey

BASE = "https://commonchemistry.cas.org"
CAS_COLUMNS = [
    "CAS Verification", "CAS Verification Detail",
    "CAS Common Chemistry RN", "CAS Common Chemistry Name",
    "CAS Common Chemistry Formula", "CAS Common Chemistry Molecular Weight",
    "CAS Common Chemistry SMILES", "CAS Common Chemistry InChIKey",
    "CAS Common Chemistry Link",
]


def status() -> dict[str, Any]:
    enabled = os.environ.get("CHEMDRAW_CAS_ENABLED", "1").lower() not in ("0", "false", "no", "off")
    configured = bool(os.environ.get("CAS_API_KEY", "").strip())
    return {
        "enabled": enabled, "configured": configured,
        "ready": enabled and configured,
        "reason": ("CAS verification is disabled." if not enabled else
                   "Set CAS_API_KEY in backend/.env to enable CAS Common Chemistry verification."
                   if not configured else None),
    }


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return html.unescape(re.sub(r"<[^>]*>", "", value)).strip() or None


def _cas_rns(value: str | None) -> list[str]:
    """Only well-formed CAS RNs with a valid check digit become candidates."""
    if not isinstance(value, str):
        return []
    out = []
    for rn in re.findall(r"\b\d{2,7}-\d{2}-\d\b", value or ""):
        digits = rn.replace("-", "")
        check = sum(i * int(d) for i, d in enumerate(reversed(digits[:-1]), 1)) % 10
        if check == int(digits[-1]) and rn not in out:
            out.append(rn)
    return out


def _record(data: dict) -> dict:
    rn = _text(data.get("rn"))
    if not rn or _cas_rns(rn) != [rn]:
        raise ValueError("CAS returned a record without a valid CAS RN.")
    # CAS's canonicalSmile omits stereochemistry. Use its stereo-preserving
    # smile, or reconstruct from InChI; never accept connectivity alone.
    raw_smiles = data.get("smile")
    smiles = raw_smiles.strip() or None if isinstance(raw_smiles, str) else None
    if not smiles and data.get("inchi"):
        try:
            mol = Chem.MolFromInchi(data["inchi"])
            if mol is not None:
                smiles = Chem.MolToSmiles(mol, isomericSmiles=True)
        except Exception:
            pass
    try:
        weight = float(data.get("molecularMass"))
        if not math.isfinite(weight):
            weight = None
    except (TypeError, ValueError):
        weight = None
    return {
        "CAS Common Chemistry RN": rn,
        "CAS Common Chemistry Name": _text(data.get("name")),
        "CAS Common Chemistry Formula": _text(data.get("molecularFormula")),
        "CAS Common Chemistry Molecular Weight": weight,
        "CAS Common Chemistry SMILES": smiles,
        "CAS Common Chemistry InChIKey": normalize_inchikey(data.get("inchiKey")),
        "CAS Common Chemistry Link": f"{BASE}/detail?{urlencode({'cas_rn': rn})}",
    }


class CommonChemistryError(Exception):
    pass


class CommonChemistryClient:
    """One client per job: paced requests, bounded lookups, cached responses.

    Access failures stop further requests for this job, so an expired key or an
    outage does not add a timeout for every remaining molecule.
    """

    def __init__(self):
        self.config = status()
        self._api_key = os.environ.get("CAS_API_KEY", "").strip()
        self._cache: dict[tuple[str, str, str], dict | None] = {}
        self._last_request = 0.0
        self._unavailable: str | None = None
        self.max_candidates = max(1, min(20, int(os.environ.get("CHEMDRAW_CAS_MAX_CANDIDATES", "5"))))

    def _get(self, endpoint: str, parameter: str, value: str) -> dict | None:
        key = (endpoint, parameter, value)
        if key in self._cache:
            return self._cache[key]
        if self._unavailable:
            raise CommonChemistryError(self._unavailable)
        time.sleep(max(0, 1.0 - (time.monotonic() - self._last_request)))
        self._last_request = time.monotonic()
        try:
            response = requests.get(
                f"{BASE}/api/{endpoint}", params={parameter: value},
                headers={"X-API-KEY": self._api_key, "Accept": "application/json"},
                timeout=(5, 15),
            )
            if response.status_code == 404:
                self._cache[key] = None
                return None
            if response.status_code in (401, 403):
                raise CommonChemistryError("CAS rejected API access; check CAS_API_KEY.")
            if response.status_code == 429:
                raise CommonChemistryError("CAS rate limit reached; verification is unavailable for this job.")
            if not response.ok:
                raise CommonChemistryError(f"CAS request failed (HTTP {response.status_code}).")
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
        except requests.RequestException:
            self._unavailable = "CAS Common Chemistry could not be reached."
            raise CommonChemistryError(self._unavailable) from None
        except ValueError:
            self._unavailable = "CAS Common Chemistry returned an invalid response."
            raise CommonChemistryError(self._unavailable) from None
        except CommonChemistryError as exc:
            self._unavailable = str(exc)
            raise
        self._cache[key] = data
        return data

    def lookup_rn(self, rn: str) -> dict:
        """Retrieve one registered substance for the bulk CAS extractor."""
        result = {"status": "Not configured" if self.config["enabled"] else "Disabled",
                  "note": self.config["reason"], "rn": rn, "name": None,
                  "formula": None, "weight": None, "smiles": None, "inchi": None,
                  "inchikey": None, "url": None}
        if not self.config["ready"]:
            return result
        if _cas_rns(rn) != [rn]:
            return {**result, "status": "Invalid CAS", "note": "Invalid CAS Registry Number."}
        try:
            data = self._get("detail", "cas_rn", rn)
            if data is None:
                return {**result, "status": "Not found", "note": "No record in CAS Common Chemistry."}
            record = _record(data)
            return {
                "status": "Found", "rn": record["CAS Common Chemistry RN"],
                "name": record["CAS Common Chemistry Name"],
                "formula": record["CAS Common Chemistry Formula"],
                "weight": record["CAS Common Chemistry Molecular Weight"],
                "smiles": record["CAS Common Chemistry SMILES"],
                "inchi": data.get("inchi") or None if isinstance(data.get("inchi"), str) else None,
                "inchikey": record["CAS Common Chemistry InChIKey"],
                "url": record["CAS Common Chemistry Link"],
                "note": (f"CAS resolved {rn} to current RN {record['CAS Common Chemistry RN']}."
                         if record["CAS Common Chemistry RN"] != rn else None),
            }
        except (CommonChemistryError, ValueError) as exc:
            self._unavailable = str(exc)
            return {**result, "status": "Unavailable", "note": str(exc)}

    def verify(self, row: dict, name: str | None = None) -> dict:
        result = {column: None for column in CAS_COLUMNS}
        result["CAS Verification"] = "Not configured" if self.config["enabled"] else "Disabled"
        result["CAS Verification Detail"] = self.config["reason"]
        if not self.config["ready"]:
            return result
        local = canonical_smiles(row.get("ChemDraw SMILES"))
        key = normalize_inchikey(row.get("ChemDraw InChIKey"))
        rns = _cas_rns(row.get("CAS no(s)"))
        queries = [("InChIKey", key), ("SMILES", row.get("ChemDraw SMILES")),
                   ("Name", name or row.get("Compound Name"))]
        best: dict | None = None
        best_rank = -1
        seen: set[str] = set()
        truncated = False

        def consider(rn: str, route: str) -> bool:
            nonlocal best, best_rank, truncated
            if rn in seen:
                return False
            if len(seen) >= self.max_candidates:
                truncated = True
                return False
            seen.add(rn)
            data = self._get("detail", "cas_rn", rn)
            if data is None:
                return False
            try:
                record = _record(data)
            except ValueError as exc:
                raise CommonChemistryError(str(exc)) from None
            remote = canonical_smiles(record["CAS Common Chemistry SMILES"])
            verdict = "Not comparable" if not local or not remote else "Verified" if local == remote else "Mismatch"
            detail = {"Verified": "Canonical isomeric SMILES agree with the drawing.",
                      "Mismatch": "Canonical isomeric SMILES differ from the drawing.",
                      "Not comparable": "A usable structure with stereochemistry is missing on one side."}[verdict]
            record.update({"CAS Verification": verdict, "CAS Verification Detail": f"Found by {route}. {detail}"})
            rank = {"Verified": 2, "Mismatch": 1, "Not comparable": 0}[verdict]
            if rank > best_rank:
                best, best_rank = record, rank
            return verdict == "Verified"

        try:
            # Exact structural query first. PubChem's CAS annotations are only
            # candidates; a name/RN hit still needs a structural comparison.
            for route, query in queries:
                if route == "SMILES":
                    for rn in rns:
                        if consider(rn, "PubChem CAS RN"):
                            return best
                if not query or len(seen) >= self.max_candidates:
                    truncated |= bool(query)
                    continue
                data = self._get("search", "q", query)
                if data is None:
                    continue
                items = data.get("results")
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    raise CommonChemistryError("CAS search returned an invalid response.")
                try:
                    truncated |= int(data.get("count", len(items))) > len(items)
                except (TypeError, ValueError):
                    raise CommonChemistryError("CAS search returned an invalid result count.") from None
                for item in items:
                    candidates = _cas_rns(item.get("rn"))
                    if len(candidates) != 1:
                        raise CommonChemistryError("CAS search returned an invalid CAS RN.")
                    if consider(candidates[0], route):
                        return best
            if best:
                if truncated:
                    best["CAS Verification Detail"] += " Candidate limit reached; additional records may exist."
                return best
            result["CAS Verification"] = "Not comparable" if truncated else "Not found"
            result["CAS Verification Detail"] = (
                "Candidate limit reached without a comparable CAS record." if truncated else
                "No CAS Common Chemistry record was found by InChIKey, CAS RN, SMILES or name."
            )
        except CommonChemistryError as exc:
            self._unavailable = str(exc)
            result.update(best or {})
            result["CAS Verification"] = "Unavailable"
            result["CAS Verification Detail"] = str(exc)
        return result


def selftest() -> dict:
    result = CommonChemistryClient().verify({
        "Compound Name": "aspirin", "CAS no(s)": "50-78-2",
        "ChemDraw SMILES": "CC(=O)Oc1ccccc1C(=O)O",
        "ChemDraw InChIKey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
    })
    return {"ok": result["CAS Verification"] == "Verified" and result["CAS Common Chemistry RN"] == "50-78-2",
            "chemdraw_required": False, "status": status(), "verification": result}
