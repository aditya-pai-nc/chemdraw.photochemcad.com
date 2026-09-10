"""
Curation for the compounds that did not match.

This is a much smaller job than the identification pass it replaces. Nothing
here tries to work out what a molecule is — ChemDraw already read the structure
and PubChem was already searched every way it can be. The only question left is
for the rows where those two disagreed, or where PubChem returned nothing:

    what should the researcher actually write down, and why did this not match?

So a small, cheap model is given the *relevant* fields from both sides — not the
full property sweep, which is mostly 3D descriptors and fingerprints that have no
bearing on whether two structures are the same compound — and asked to reconcile
them.

Two rules carry over from the pass this replaces, because they were the right
rules:

  * **A model is never trusted for an InChIKey.** It is a hash; it cannot be
    reasoned out, only copied. The model may only quote a key that is already in
    the evidence, and anything it returns is checked against that evidence before
    it reaches the workbook.
  * **Everything is labelled.** A curated value is written to its own sheet, never
    merged into the machine-derived columns, so no AI-authored value can ever be
    mistaken for something ChemDraw or PubChem said.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Optional

from inchi_tools import normalize_inchikey

# Small on purpose. The model is weighing evidence that is already in front of
# it, not doing research, so this is the cheap-and-fast end of the range.
CURATE_MODEL = os.environ.get("CHEMDRAW_CURATE_MODEL", "claude-haiku-4-5-20251001")
CURATE_EFFORT = os.environ.get("CHEMDRAW_CURATE_EFFORT", "medium")
CURATE_CONCURRENCY = max(1, int(os.environ.get("CHEMDRAW_CURATE_CONCURRENCY", "4")))

_ENABLED_SETTING = os.environ.get("CHEMDRAW_AI_ENABLED", "auto").lower()

_client = None
_client_error: Optional[str] = None
_client_lock = threading.Lock()


def is_enabled() -> bool:
    """Curation is opt-out, but it can only run when there is a key."""
    if _ENABLED_SETTING in ("0", "false", "no", "off"):
        return False
    if _ENABLED_SETTING in ("1", "true", "yes", "on"):
        return True
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def status() -> dict[str, Any]:
    """What the curation step is configured to do — surfaced at /api/ai."""
    enabled = is_enabled()
    client, error = _get_client() if enabled else (None, None)
    return {
        "enabled": enabled,
        "ready": bool(client),
        "curate_model": CURATE_MODEL,
        "concurrency": CURATE_CONCURRENCY,
        "has_credentials": bool(
            os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        ),
        "reason": error if not client else None,
    }


def _get_client():
    """Build the Anthropic client once, and remember why if it cannot be built."""
    global _client, _client_error
    if _client is not None or _client_error is not None:
        return _client, _client_error
    with _client_lock:
        if _client is not None or _client_error is not None:
            return _client, _client_error
        try:
            import anthropic
        except ImportError:
            _client_error = "The `anthropic` package is not installed (pip install anthropic)."
            return None, _client_error
        try:
            _client = anthropic.Anthropic()
        except Exception as exc:
            _client_error = f"Could not create the Anthropic client: {exc}"
            return None, _client_error
        return _client, None


# ---------------------------------------------------------------------------
# Talking to the model
# ---------------------------------------------------------------------------


def _extract_text(message) -> str:
    return "\n".join(b.text for b in message.content if getattr(b, "type", None) == "text").strip()


def _extract_json(text: str) -> Optional[dict]:
    """
    Pull the JSON object out of a reply.

    Structured outputs make the whole reply a JSON object, but the fallback path
    needs this: walk forward collecting top-level objects and keep the last one
    that parses, which is the answer rather than any JSON quoted while reasoning.
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    for candidate in reversed(fenced):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    found = None
    cursor = 0
    while True:
        start = text.find("{", cursor)
        if start == -1:
            return found
        depth, in_string, escaped, end = 0, False, False, -1
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            return found
        try:
            found = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
        cursor = end + 1


# Request shapes, richest first. Not every model accepts every one — Haiku 4.5
# rejects `effort` outright — and which are available also depends on the
# account, so the request degrades one rung at a time rather than failing the
# compound.
_VARIANTS = ("effort+schema", "schema", "plain")

# `effort` is only worth asking for when it was deliberately configured. The
# default model does not support it, so starting at the top rung would burn a
# rejected request per run for no benefit.
_EFFORT_CONFIGURED = "CHEMDRAW_CURATE_EFFORT" in os.environ
_START_VARIANT = "effort+schema" if _EFFORT_CONFIGURED else "schema"

# The rung known to work, remembered across calls. Curation runs several
# compounds at a time, and rediscovering the model's capabilities on every one
# would cost a rejected request each.
_variant: Optional[str] = None
_variant_lock = threading.Lock()


def _build_request(variant: str, system: str, content: list, schema: dict, max_tokens: int) -> dict:
    request: dict[str, Any] = {
        "model": CURATE_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    schema_format = {"type": "json_schema", "schema": schema}
    if variant == "effort+schema":
        request["output_config"] = {"effort": CURATE_EFFORT, "format": schema_format}
    elif variant == "schema":
        request["output_config"] = {"format": schema_format}
    else:
        # No structured output at all: ask in prose and lean on the tolerant parser.
        request["system"] = system + "\n\nReply with a single JSON object and nothing else."
    return request


def _ask_json(*, system: str, content: list, schema: dict, max_tokens: int = 4000):
    """
    One JSON-returning request. Returns (payload, error).

    Structured output is requested but never depended on. A `BadRequestError`
    means this model or account does not accept that request shape, so the next
    rung down is tried and the one that worked is remembered — an API-surface
    change degrades the answer instead of failing the row.
    """
    global _variant

    client, error = _get_client()
    if client is None:
        return None, error

    import anthropic

    def _run(payload: dict):
        with client.messages.stream(**payload) as stream:
            return stream.get_final_message()

    start = _variant or _START_VARIANT
    ladder = _VARIANTS[_VARIANTS.index(start):]

    message = None
    last_error: Optional[str] = None
    for variant in ladder:
        request = _build_request(variant, system, content, schema, max_tokens)
        try:
            message = _run(request)
        except anthropic.BadRequestError as exc:
            # The shape was refused, not the content — drop to the next rung.
            last_error = f"{variant}: {exc}"
            continue
        except Exception as exc:
            return None, f"{CURATE_MODEL} request failed: {exc}"

        if variant != _variant:
            with _variant_lock:
                _variant = variant
        break

    if message is None:
        return None, f"{CURATE_MODEL} rejected every request shape. Last: {last_error}"

    if getattr(message, "stop_reason", None) == "refusal":
        return None, f"{CURATE_MODEL} declined the request."

    payload = getattr(message, "parsed_output", None) or _extract_json(_extract_text(message))
    if payload is None:
        return None, f"{CURATE_MODEL} did not return parseable JSON."
    return payload, None


# ---------------------------------------------------------------------------
# The curation prompt
# ---------------------------------------------------------------------------

CURATE_SYSTEM = """You are curating one compound that a chemistry pipeline could not confirm automatically.

ChemDraw read a structure off a drawing and produced its InChIKey. PubChem was queried with that key. This compound reached you because the canonical SMILES from ChemDraw and the canonical SMILES from PubChem are NOT identical — or because PubChem had no record for that key at all.

Your job is not to identify the molecule from scratch. It is to reconcile what the two sources said and tell the researcher what to record.

How to weigh what you are given:
- CHEMDRAW is ground truth for *what is on the page*. It is not proof the chemist drew the right thing, but it is exactly what was drawn.
- The molecular formula is the cheapest hard check. If PubChem's formula differs from the drawn formula, PubChem has returned a different compound, however plausible its name looks.
- PubChem was found by exact InChIKey, so the record genuinely shares that structure hash. When the canonical SMILES still differ, the usual causes are a salt or charged form, a tautomer written differently, or one side defining a stereocentre the other left flat. Say which.
- One InChIKey can resolve to several deposited records. Where several CIDs are listed, say which one the researcher should trust.
- If PubChem returned nothing, the drawn structure may simply not be deposited — that is a finding, not an error. Do not invent a CID to fill the gap.

Never invent an InChIKey, a CAS number or a CID. You may only copy one that appears in the evidence you were given. If the right value is not there, return null.

`same_compound` is your verdict on whether the PubChem record and the drawing are the same substance:
  "yes"        same compound; the difference is a salt form, tautomer, or undrawn stereochemistry
  "no"         different compounds; PubChem's record should not be used for this row
  "uncertain"  the evidence does not settle it

Be concrete in `discrepancy` and `likely_cause`. "The SMILES differ" is useless; "PubChem's record is the magnesium complex while the drawing is the free base, so the formula carries an extra Mg" is what a researcher can act on."""

CURATE_SCHEMA = {
    "type": "object",
    "properties": {
        "same_compound": {"type": "string", "enum": ["yes", "no", "uncertain"]},
        "curated_name": {"type": ["string", "null"], "description": "Best name for this compound, or null."},
        "curated_formula": {"type": ["string", "null"], "description": "Hill notation."},
        "curated_smiles": {"type": ["string", "null"], "description": "Copied from the evidence, not invented."},
        "curated_inchikey": {"type": ["string", "null"], "description": "Only a key that appears in the evidence. Otherwise null."},
        "curated_cas": {"type": ["string", "null"], "description": "Only a CAS number that appears in the evidence."},
        "best_pubchem_cid": {"type": ["integer", "null"], "description": "Which candidate CID to trust, if any."},
        "discrepancy": {"type": "string", "description": "What specifically differs between the drawing and PubChem."},
        "likely_cause": {"type": "string", "description": "Why they differ — stereochemistry, salt form, wrong name hit, no record."},
        "recommended_action": {"type": "string", "description": "What the researcher should do with this row."},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["same_compound", "curated_name", "curated_formula", "curated_smiles",
                 "curated_inchikey", "curated_cas", "best_pubchem_cid", "discrepancy",
                 "likely_cause", "recommended_action", "confidence"],
    "additionalProperties": False,
}


def curate_compound(evidence: dict[str, Any]) -> dict[str, Any]:
    """
    Reconcile one unmatched compound. Never raises.

    `evidence` is the trimmed view built by `build_evidence` — enough to decide,
    and nothing that would only pad the prompt.
    """
    outcome: dict[str, Any] = {"ok": False, "model": CURATE_MODEL, "error": None}

    if not is_enabled():
        outcome["error"] = "Curation is disabled (set ANTHROPIC_API_KEY or CHEMDRAW_AI_ENABLED=1)."
        return outcome

    content = [{
        "type": "text",
        "text": (
            "One compound the pipeline could not confirm:\n\n"
            + json.dumps(evidence, indent=2, default=str)
            + "\n\nReconcile these sources and tell the researcher what to record."
        ),
    }]

    payload, error = _ask_json(system=CURATE_SYSTEM, content=content, schema=CURATE_SCHEMA)
    if error or payload is None:
        outcome["error"] = error or "No curation returned."
        return outcome

    outcome["ok"] = True
    outcome.update(payload)

    # A key is a hash, so the only acceptable provenance is "it was already in
    # the evidence". Anything else is discarded, however well-formed it looks.
    claimed = normalize_inchikey(payload.get("curated_inchikey"))
    allowed = {
        normalize_inchikey(k)
        for k in _keys_in(evidence)
        if normalize_inchikey(k)
    }
    if claimed and claimed not in allowed:
        outcome["curated_inchikey"] = None
        outcome["inchikey_rejected"] = claimed
    else:
        outcome["curated_inchikey"] = claimed

    return outcome


def _keys_in(evidence: dict[str, Any]) -> list[str]:
    """Every InChIKey anywhere in the evidence, so a returned key can be checked."""
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if "inchikey" in key.lower() and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(evidence)
    return found


def build_evidence(row: dict[str, Any], hit) -> dict[str, Any]:
    """
    The trimmed view of one compound that the model actually needs.

    Deliberately not the whole row: the two structures, the two formulas, the
    key that was searched, and the candidate CIDs. Nothing else bears on whether
    these are the same compound.
    """
    evidence: dict[str, Any] = {
        "caption_on_drawing": row.get("Compound Name"),
        "chemdraw": {
            "smiles": row.get("ChemDraw SMILES"),
            "canonical_smiles": row.get("Canonical SMILES"),
            "inchikey": row.get("ChemDraw InChIKey"),
            "molecular_formula": row.get("Formula"),
            "molecular_weight": row.get("Molecular Weight"),
        },
        "comparison": {
            "formula_and_weight_match": row.get("Match?"),
            "canonical_smiles_match": row.get("InChIKey Match?"),
        },
    }

    if hit is None:
        evidence["pubchem"] = {
            "found": False,
            "searched_inchikey": row.get("ChemDraw InChIKey"),
            "note": "PubChem holds no compound with this InChIKey.",
        }
        return evidence

    evidence["pubchem"] = {
        "found": True,
        "searched_inchikey": row.get("ChemDraw InChIKey"),
        "cid": hit.cid,
        "all_cids_sharing_this_inchikey": hit.candidate_cids[:10],
        "title": hit.title,
        "iupac_name": row.get("IUPAC Name") or hit.iupac_name,
        "molecular_formula": hit.formula,
        "molecular_weight": hit.weight,
        "canonical_smiles": row.get("PubChem Canonical SMILES"),
        "inchikey": hit.inchikey,
        "cas": row.get("CAS no(s)"),
        "fields_borrowed_from_other_candidates": hit.borrowed_summary(),
    }
    return evidence


def selftest() -> dict[str, Any]:
    """
    Curate a deliberately mismatched pair whose answer is known.

    Aspirin as drawn against salicylic acid from PubChem: same skeleton family,
    different formula, so a working curator must return `same_compound: "no"`
    and point at the formula. Needs no ChemDraw and no Windows.
    """
    evidence = {
        "caption_on_drawing": "compound 3b",
        "chemdraw": {
            "smiles": "CC(=O)Oc1ccccc1C(=O)O",
            "canonical_smiles": "CC(=O)Oc1ccccc1C(=O)O",
            "inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
            "molecular_formula": "C9H8O4",
            "molecular_weight": 180.159,
        },
        "comparison": {
            "formula_and_weight_match": "❌",
            "canonical_smiles_match": "❌",
        },
        "pubchem": {
            "found": True,
            "searched_inchikey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
            "cid": 338,
            "all_cids_sharing_this_inchikey": [338],
            "title": "Salicylic acid",
            "molecular_formula": "C7H6O3",
            "molecular_weight": 138.12,
            "canonical_smiles": "OC(=O)c1ccccc1O",
            "inchikey": "YGSDEFSMJLZEOE-UHFFFAOYSA-N",
        },
    }

    report: dict[str, Any] = {"status": status(), "evidence": evidence, "chemdraw_required": False}
    if not is_enabled():
        report["message"] = (
            "Curation is disabled. Set ANTHROPIC_API_KEY (or CHEMDRAW_AI_ENABLED=1) and try again."
        )
        report["ok"] = False
        return report

    result = curate_compound(evidence)
    report["curation"] = result
    report["ok"] = bool(result.get("ok")) and result.get("same_compound") == "no"
    report["message"] = (
        f"Curator returned same_compound={result.get('same_compound')!r} "
        f"(expected 'no' — the formulas differ)."
        if result.get("ok")
        else (result.get("error") or "Curation returned nothing.")
    )
    return report
