"""
The AI half of the identification: one model that identifies the molecule, and a
second, cheaper one that reconciles every source into a single answer.

Two agents, deliberately asymmetric:

  identify_compound()  Claude Opus 5, with web search. Sees only what a chemist
                       reading the page would see — the caption, the drawing,
                       and the molecular formula. It does NOT get the SMILES
                       that ChemDraw and RDKit derived, because an opinion that
                       has already been shown the answer is not evidence. Its
                       whole value in the vote is that it arrives independently.

  build_consensus()    Claude Sonnet 5. Sees everything — the ChemDraw/RDKit
                       extraction, what PubChem returned and how it was found,
                       and the identification above — and decides which source
                       to believe, where they agree, and what needs a human.

One rule holds throughout: **a language model is never trusted for an InChIKey.**
An InChIKey is a SHA-256 hash of the InChI string; it cannot be reasoned out,
only computed or recalled, and a model asked for one will happily produce
something that looks exactly right and is not. So the identifier is asked for a
SMILES, and the key is computed from that SMILES locally with RDKit. Any key the
model volunteers is kept beside it, clearly labelled, and never used for matching.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from inchi_tools import (
    MATCH_NA,
    agreement_symbol,
    best_agreement,
    inchi_from_smiles,
    inchikey_agreement,
    normalize_inchikey,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The identifier may have to do real research — read a paper's supporting
# information, disambiguate a lab's in-house code name — so it gets the strong
# model. The referee only weighs evidence that is already in front of it, so it
# gets the cheap one, exactly as asked.
IDENTIFY_MODEL = os.environ.get("CHEMDRAW_AI_MODEL", "claude-opus-5")
CONSENSUS_MODEL = os.environ.get("CHEMDRAW_AI_CONSENSUS_MODEL", "claude-sonnet-5")

IDENTIFY_EFFORT = os.environ.get("CHEMDRAW_AI_EFFORT", "high")
CONSENSUS_EFFORT = os.environ.get("CHEMDRAW_AI_CONSENSUS_EFFORT", "medium")

WEB_SEARCH_ENABLED = os.environ.get("CHEMDRAW_AI_WEB_SEARCH", "1").lower() not in ("0", "false", "no")
MAX_WEB_SEARCHES = int(os.environ.get("CHEMDRAW_AI_MAX_SEARCHES", "6"))

# Anthropic's vision guidance: nothing is gained above ~1568px on the long edge,
# and a full-page ChemDraw TIFF is far larger than that.
MAX_IMAGE_EDGE = int(os.environ.get("CHEMDRAW_AI_MAX_IMAGE_PX", "1568"))

_ENABLED_SETTING = os.environ.get("CHEMDRAW_AI_ENABLED", "auto").lower()

_client = None
_client_error: Optional[str] = None
# The AI pass runs several compounds at once, and the first burst would otherwise
# race to build a client each.
_client_lock = threading.Lock()


def is_enabled() -> bool:
    """AI enrichment is opt-out, but it can only run when there is a key."""
    if _ENABLED_SETTING in ("0", "false", "no", "off"):
        return False
    if _ENABLED_SETTING in ("1", "true", "yes", "on"):
        return True
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def status() -> dict[str, Any]:
    """What the AI subsystem is configured to do — surfaced at /api/ai."""
    enabled = is_enabled()
    client, error = _get_client() if enabled else (None, None)
    return {
        "enabled": enabled,
        "ready": bool(client),
        "identify_model": IDENTIFY_MODEL,
        "consensus_model": CONSENSUS_MODEL,
        "web_search": WEB_SEARCH_ENABLED,
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
# Image handling
# ---------------------------------------------------------------------------


def encode_image(image_path: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    (media_type, base64_data, error) for a structure drawing.

    ChemDraw exports TIFF, which the Messages API does not accept, so everything
    is normalised to PNG and scaled down to the point where more pixels stop
    buying more accuracy.
    """
    if not image_path or not os.path.isfile(image_path):
        return None, None, "No structure image was produced for this compound."

    try:
        from PIL import Image
    except ImportError:
        return None, None, "Pillow is not installed, so the TIFF could not be converted (pip install pillow)."

    try:
        with Image.open(image_path) as img:
            img.load()
            # TIFFs from ChemDraw are often 1-bit or palettised; flatten onto
            # white so the structure does not come through on a black field.
            if img.mode not in ("RGB", "L"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                converted = img.convert("RGBA")
                background.paste(converted, mask=converted.split()[-1])
                img = background
            else:
                img = img.convert("RGB")

            longest = max(img.size)
            if longest > MAX_IMAGE_EDGE:
                scale = MAX_IMAGE_EDGE / longest
                img = img.resize(
                    (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                    Image.LANCZOS,
                )

            buffer = io.BytesIO()
            img.save(buffer, format="PNG", optimize=True)
    except Exception as exc:
        return None, None, f"Could not convert the structure image: {exc}"

    return "image/png", base64.standard_b64encode(buffer.getvalue()).decode("ascii"), None


# ---------------------------------------------------------------------------
# Talking to the model
# ---------------------------------------------------------------------------


def _extract_text(message) -> str:
    return "\n".join(b.text for b in message.content if getattr(b, "type", None) == "text").strip()


def _extract_json(text: str) -> Optional[dict]:
    """
    Pull the JSON object out of a reply.

    Structured outputs make the whole reply a JSON object, but the fallback path
    (and any model that decides to add a sentence first) needs this: take the
    last balanced `{...}` in the text, which is the answer rather than any JSON
    quoted while reasoning.
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

    # Walk forward collecting *top-level* objects, skipping past each one that
    # parses. Scanning backwards instead would find the innermost `{` first and
    # return a nested fragment of the answer rather than the answer.
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


def _count_searches(message) -> int:
    # Count invocations only. Each search also yields a web_search_tool_result
    # block, and counting both would report double the searches actually run.
    return sum(
        1 for b in message.content
        if getattr(b, "type", None) == "server_tool_use"
        and getattr(b, "name", None) == "web_search"
    )


def _ask_json(
    *,
    model: str,
    system: str,
    content: list,
    schema: dict,
    effort: str,
    max_tokens: int = 16000,
    tools: list | None = None,
) -> tuple[Optional[dict], Optional[str], dict]:
    """
    One JSON-returning request. Returns (payload, error, meta).

    Streaming, because Opus 5 with web search can spend minutes on a hard
    identification and a non-streaming request would hit the HTTP timeout first.
    Structured output is requested but not depended on: if the account or model
    rejects `output_config` the same call is retried plainly and the reply is
    parsed out of the text, so an API-surface change degrades the answer instead
    of failing the compound.
    """
    client, error = _get_client()
    if client is None:
        return None, error, {}

    import anthropic

    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
    }
    if tools:
        request["tools"] = tools

    def _run(payload: dict):
        with client.messages.stream(**payload) as stream:
            return stream.get_final_message()

    meta: dict[str, Any] = {"model": model}
    try:
        message = _run(request)
    except anthropic.BadRequestError as exc:
        # Most likely: structured output is not accepted alongside this tool set.
        # Ask for JSON in prose instead and lean on the tolerant parser.
        retry = dict(request)
        retry["output_config"] = {"effort": effort}
        retry["system"] = system + "\n\nReply with a single JSON object and nothing else."
        meta["structured_output"] = f"unavailable ({exc.__class__.__name__}), fell back to prose JSON"
        try:
            message = _run(retry)
        except Exception as inner:
            return None, f"{model} request failed: {inner}", meta
    except Exception as exc:
        return None, f"{model} request failed: {exc}", meta

    if getattr(message, "stop_reason", None) == "refusal":
        details = getattr(message, "stop_details", None)
        return None, f"{model} declined the request ({getattr(details, 'category', 'unknown')}).", meta

    meta["web_searches"] = _count_searches(message)
    usage = getattr(message, "usage", None)
    if usage is not None:
        meta["input_tokens"] = getattr(usage, "input_tokens", None)
        meta["output_tokens"] = getattr(usage, "output_tokens", None)

    payload = getattr(message, "parsed_output", None) or _extract_json(_extract_text(message))
    if payload is None:
        return None, f"{model} did not return parseable JSON.", meta
    return payload, None, meta


# ---------------------------------------------------------------------------
# Agent 1 — identification
# ---------------------------------------------------------------------------

IDENTIFY_SYSTEM = """You are a synthetic chemist identifying a compound from one panel of a ChemDraw scheme.

You are given the caption printed under the structure, the molecular formula computed from the drawing, and an image of the structure itself. Work out which specific compound this is.

How to read the evidence:
- The image is the primary evidence. Read the skeleton, the substituents, the stereo wedges, and any R-groups or abbreviations (Me, Ph, Bn, Boc, TBS, Ar).
- The caption is often a lab code ("12b", "S-4", "compound 7") rather than a chemical name. A code is a hint about provenance, not an identity — do not let it override what is drawn.
- The molecular formula is a hard constraint. Your answer's formula must equal it. If it cannot, say so in `notes` and lower your confidence rather than forcing a fit.

Use web search when the drawing is a known natural product, drug, reagent or literature compound and a name would let a researcher find it. Do not search for generic fragments.

Report a SMILES for exactly what is drawn — including stereochemistry when it is drawn, and omitting it when it is not. Do not add stereocentres the drawing leaves flat.

Never guess an InChIKey. It is a hash, not something that can be reasoned out. Leave `reported_inchikey` null unless you actually read the key from a source you retrieved; the pipeline computes the real key from your SMILES.

Set `confidence`:
- "high"   — the structure is unambiguous and you can name the compound
- "medium" — the skeleton is clear but a substituent, stereocentre or the exact identity is not
- "low"    — the drawing is ambiguous, truncated, or a generic scaffold

Be honest about doubt. A "low" that says why is far more useful to a researcher than a confident wrong name."""

IDENTIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"], "description": "Common or literature name of the compound, or null if it has none."},
        "iupac_name": {"type": ["string", "null"]},
        "molecular_formula": {"type": ["string", "null"], "description": "Hill notation, e.g. C9H8O4."},
        "smiles": {"type": ["string", "null"], "description": "SMILES for exactly what is drawn."},
        "reported_inchikey": {"type": ["string", "null"], "description": "Only if read from a retrieved source; otherwise null."},
        "cas": {"type": ["string", "null"]},
        "compound_class": {"type": ["string", "null"], "description": "e.g. flavonoid, beta-lactam, BODIPY dye."},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "reasoning": {"type": "string", "description": "2-4 sentences on what in the drawing led to this identification."},
        "notes": {"type": ["string", "null"], "description": "Caveats: ambiguity, formula disagreement, undrawn stereochemistry."},
        "sources": {"type": "array", "items": {"type": "string"}, "description": "URLs consulted, if any."},
    },
    "required": ["name", "iupac_name", "molecular_formula", "smiles", "reported_inchikey",
                 "cas", "compound_class", "confidence", "reasoning", "notes", "sources"],
    "additionalProperties": False,
}


@dataclass
class AIIdentification:
    attempted: bool = False
    ok: bool = False
    name: Optional[str] = None
    iupac_name: Optional[str] = None
    formula: Optional[str] = None
    smiles: Optional[str] = None
    # Computed locally from `smiles` with RDKit — never taken from the model.
    inchikey: Optional[str] = None
    inchi: Optional[str] = None
    # What the model claimed, kept only so a human can see it disagreed.
    reported_inchikey: Optional[str] = None
    cas: Optional[str] = None
    compound_class: Optional[str] = None
    confidence: Optional[str] = None
    reasoning: Optional[str] = None
    notes: Optional[str] = None
    sources: list[str] = field(default_factory=list)
    model: Optional[str] = None
    web_searches: int = 0
    had_image: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def identify_compound(
    *,
    name: str,
    formula: Optional[str],
    weight: Optional[float],
    image_path: Optional[str] = None,
    source_file: Optional[str] = None,
) -> AIIdentification:
    """
    Ask the strong model what this molecule is, from the caption, the picture and
    the formula alone.

    The derived SMILES is deliberately withheld — see the module docstring.
    """
    result = AIIdentification(attempted=True, model=IDENTIFY_MODEL)

    if not is_enabled():
        result.attempted = False
        result.error = "AI identification is disabled (set ANTHROPIC_API_KEY or CHEMDRAW_AI_ENABLED=1)."
        return result

    content: list[dict[str, Any]] = []
    media_type, data, image_error = encode_image(image_path) if image_path else (None, None, "No image path.")
    if data:
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
        result.had_image = True

    facts = [f"Caption printed under the structure: {name or '(none)'}"]
    if formula:
        facts.append(f"Molecular formula computed from the drawing: {formula}")
    if weight is not None:
        facts.append(f"Molecular weight computed from the drawing: {weight}")
    if source_file:
        facts.append(f"Source file: {source_file}")
    if not data:
        facts.append(
            f"No structure image is available ({image_error}). Identify from the caption "
            "and formula alone, and lower your confidence accordingly."
        )

    content.append({"type": "text", "text": "\n".join(facts) + "\n\nIdentify this compound."})

    tools = None
    if WEB_SEARCH_ENABLED:
        tools = [{
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": MAX_WEB_SEARCHES,
        }]

    payload, error, meta = _ask_json(
        model=IDENTIFY_MODEL,
        system=IDENTIFY_SYSTEM,
        content=content,
        schema=IDENTIFY_SCHEMA,
        effort=IDENTIFY_EFFORT,
        tools=tools,
    )

    result.web_searches = int(meta.get("web_searches") or 0)
    if error or payload is None:
        result.error = error or "No identification returned."
        return result

    result.ok = True
    result.name = payload.get("name")
    result.iupac_name = payload.get("iupac_name")
    result.formula = payload.get("molecular_formula")
    result.smiles = payload.get("smiles")
    result.reported_inchikey = normalize_inchikey(payload.get("reported_inchikey"))
    result.cas = payload.get("cas")
    result.compound_class = payload.get("compound_class")
    result.confidence = payload.get("confidence")
    result.reasoning = payload.get("reasoning")
    result.notes = payload.get("notes")
    sources = payload.get("sources")
    result.sources = [str(s) for s in sources] if isinstance(sources, list) else []

    # The one number the model is not allowed to supply: compute it from its
    # SMILES so the AI column is comparable to every other InChIKey in the row.
    if result.smiles:
        inchi, key, warning = inchi_from_smiles(result.smiles)
        result.inchi, result.inchikey = inchi, key
        if warning and not key:
            result.notes = "; ".join(filter(None, [result.notes, f"SMILES from the model was unusable: {warning}"]))

    return result


# ---------------------------------------------------------------------------
# Agent 2 — consensus
# ---------------------------------------------------------------------------

CONSENSUS_SYSTEM = """You are the referee for a compound-identification pipeline. Three independent sources have looked at the same molecule and you decide what the pipeline should report.

The sources:
1. CHEMDRAW/RDKIT — the structure as actually drawn, converted to MOL by ChemDraw and read by RDKit. This is ground truth for *what is on the page*. It is not proof the chemist drew the right thing, and it carries no name.
2. PUBCHEM — a database record. How much it is worth depends entirely on how it was found:
     - "InChIKey"           exact structure hash matched. Very strong.
     - "InChIKey skeleton"  same connectivity, different stereochemistry or protonation. Strong for identity, weak for the exact isomer.
     - "Name"               only the caption text matched. Weak — captions are lab codes and homonyms.
     - "SMILES"             structural but sensitive to how the SMILES was written. Moderate.
3. AI — a chemist model reading the drawing, the caption and the formula, with web access. It supplies a name and a class where the other two cannot, and is the only source that can be confidently wrong about everything.

Weigh them like a chemist, not by majority:
- Molecular formula is the cheapest hard check. Sources whose formula disagrees with ChemDraw's are describing a different compound, however confident they sound.
- Exact InChIKey agreement between two independent sources is near-conclusive.
- Skeleton-only agreement means the same compound drawn with stereochemistry left undefined. That is usually a drawing convention, not an error — say so rather than calling it a mismatch.
- A name-only PubChem hit that disagrees with the drawn formula is the classic failure mode of this pipeline. Call it out.
- Two sources agreeing because one was derived from the other is not corroboration. Only ChemDraw, PubChem and the AI are independent here.

Never invent an InChIKey, a CAS number or a CID. Copy them from a source or leave them null.

`needs_review` is true whenever a human would want to look: sources conflict, the formula does not reconcile, PubChem matched on name only, or nothing was found at all."""

CONSENSUS_SCHEMA = {
    "type": "object",
    "properties": {
        "final_name": {"type": ["string", "null"]},
        "final_formula": {"type": ["string", "null"]},
        "final_smiles": {"type": ["string", "null"]},
        "final_inchikey": {"type": ["string", "null"], "description": "Copied from a source, never invented."},
        "final_cas": {"type": ["string", "null"]},
        "winning_source": {
            "type": "string",
            "enum": ["chemdraw", "pubchem", "ai", "chemdraw+pubchem", "chemdraw+ai",
                     "pubchem+ai", "all", "none"],
            "description": "Which source(s) the final answer rests on.",
        },
        "agreement": {
            "type": "string",
            "enum": ["unanimous", "majority", "split", "conflict", "insufficient"],
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "ai_matches_drawing": {
            "type": "string",
            "enum": ["yes", "same_skeleton", "no", "unknown"],
            "description": "Does the AI's structure match what ChemDraw read off the page?",
        },
        "pubchem_matches_drawing": {
            "type": "string",
            "enum": ["yes", "same_skeleton", "no", "unknown"],
        },
        "votes": {
            "type": "array",
            "description": "One entry per source, saying what it claimed and how much it counted.",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": ["chemdraw", "pubchem", "ai"]},
                    "claim": {"type": ["string", "null"]},
                    "weight": {"type": "string", "enum": ["strong", "moderate", "weak", "none"]},
                    "agrees_with_final": {"type": "boolean"},
                },
                "required": ["source", "claim", "weight", "agrees_with_final"],
                "additionalProperties": False,
            },
        },
        "needs_review": {"type": "boolean"},
        "rationale": {"type": "string", "description": "2-4 sentences a researcher can act on."},
    },
    "required": ["final_name", "final_formula", "final_smiles", "final_inchikey", "final_cas",
                 "winning_source", "agreement", "confidence", "ai_matches_drawing",
                 "pubchem_matches_drawing", "votes", "needs_review", "rationale"],
    "additionalProperties": False,
}


def build_consensus(evidence: dict[str, Any]) -> dict[str, Any]:
    """
    Hand every source to the referee model and let it decide.

    `evidence` is passed through as JSON rather than prose so nothing is lost in
    summarising — the model sees exactly the fields the workbook will contain,
    including how PubChem was found, which is the single most important signal
    for how far to trust it.
    """
    outcome: dict[str, Any] = {
        "attempted": True, "ok": False, "model": CONSENSUS_MODEL, "error": None,
    }

    if not is_enabled():
        outcome["attempted"] = False
        outcome["error"] = "AI consensus is disabled (set ANTHROPIC_API_KEY or CHEMDRAW_AI_ENABLED=1)."
        return outcome

    content = [{
        "type": "text",
        "text": (
            "Here is everything the pipeline gathered for one compound.\n\n"
            + json.dumps(evidence, indent=2, default=str)
            + "\n\nDecide what the pipeline should report."
        ),
    }]

    payload, error, meta = _ask_json(
        model=CONSENSUS_MODEL,
        system=CONSENSUS_SYSTEM,
        content=content,
        schema=CONSENSUS_SCHEMA,
        effort=CONSENSUS_EFFORT,
        max_tokens=8000,
    )

    if error or payload is None:
        outcome["error"] = error or "No consensus returned."
        return outcome

    outcome["ok"] = True
    outcome.update(payload)
    # A referee that invents a key is worse than one that returns none.
    outcome["final_inchikey"] = normalize_inchikey(payload.get("final_inchikey"))
    return outcome


# ---------------------------------------------------------------------------
# Deterministic scoring of the AI's answer
# ---------------------------------------------------------------------------

_VERDICT_TO_AGREEMENT = {
    "yes": "exact",
    "same_skeleton": "skeleton",
    "no": "mismatch",
    "unknown": "unknown",
}


def score_ai_match(
    ai: AIIdentification,
    local_inchikeys: list[str],
    local_formula: Optional[str],
    consensus: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """
    (symbol, explanation) for the "AI Match?" column.

    Structure comes first: if the AI's SMILES and the drawn structure produce
    comparable InChIKeys, that comparison is arithmetic and settles the question.
    Only when the AI could not give a usable structure does this fall back to the
    formula, and then to the referee's judgement — each one weaker than the last,
    and each one said out loud in the explanation so the column is never a bare
    tick a researcher has to take on faith.
    """
    if not ai.attempted:
        return MATCH_NA, ai.error or "AI identification was not run."
    if not ai.ok:
        return MATCH_NA, ai.error or "AI identification failed."

    if ai.inchikey and local_inchikeys:
        agreement = best_agreement(*(inchikey_agreement(ai.inchikey, k) for k in local_inchikeys if k))
        if agreement != "unknown":
            detail = {
                "exact": "AI structure gives the same InChIKey as the drawing.",
                "skeleton": "AI structure has the same skeleton as the drawing but different stereochemistry or protonation.",
                "mismatch": "AI structure has a different InChIKey from the drawing.",
            }[agreement]
            return agreement_symbol(agreement), detail

    if ai.formula and local_formula:
        if ai.formula.replace(" ", "") == local_formula.replace(" ", ""):
            return agreement_symbol("skeleton"), (
                "Formula agrees with the drawing, but the AI gave no usable structure "
                "to compare InChIKeys."
            )
        return agreement_symbol("mismatch"), (
            f"AI formula {ai.formula} disagrees with the drawn formula {local_formula}."
        )

    verdict = (consensus or {}).get("ai_matches_drawing")
    if verdict in _VERDICT_TO_AGREEMENT:
        agreement = _VERDICT_TO_AGREEMENT[verdict]
        if agreement != "unknown":
            return agreement_symbol(agreement), (
                f"No structure or formula to compare; consensus model judged the AI "
                f"identification as '{verdict}'."
            )

    return MATCH_NA, "AI returned no structure or formula that could be compared."


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def selftest(smiles: str = "CC(=O)Oc1ccccc1C(=O)O", caption: str = "compound 1a") -> dict[str, Any]:
    """
    Exercise both models on a compound with a known answer, with no ChemDraw.

    A structure is rendered from SMILES with RDKit — standing in for the TIFF
    ChemDraw would have exported — and put through the real identifier and the
    real referee. Because the true InChIKey is known up front, the result is
    checkable rather than merely plausible, which is what makes this worth
    running: it proves the credentials, the vision path, the JSON contract and
    the consensus step all work, on a machine where the pipeline itself cannot.

    The caption defaults to a meaningless lab code on purpose, so a pass means
    the model read the drawing rather than looked up the name.
    """
    import tempfile

    from rdkit import Chem
    from rdkit.Chem import AllChem, Draw, Descriptors, rdMolDescriptors

    report: dict[str, Any] = {
        "smiles": smiles, "caption": caption, "status": status(),
        "expected_inchikey": None, "identification": None, "consensus": None,
        "structure_agreement": "unknown", "ok": False, "message": None,
        "chemdraw_required": False,
    }

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        report["message"] = f"Not a valid SMILES: {smiles}"
        return report

    expected_inchi, expected_key, _ = inchi_from_smiles(smiles)
    report["expected_inchikey"] = expected_key
    formula = rdMolDescriptors.CalcMolFormula(mol)
    weight = round(Descriptors.MolWt(mol), 3)

    if not is_enabled():
        report["message"] = (
            "AI identification is disabled. Set ANTHROPIC_API_KEY (or "
            "CHEMDRAW_AI_ENABLED=1) and try again."
        )
        return report

    with tempfile.TemporaryDirectory(prefix="chemdraw_ai_selftest_") as tmp:
        image_path = os.path.join(tmp, "structure.png")
        try:
            AllChem.Compute2DCoords(mol)
            Draw.MolToFile(mol, image_path, size=(900, 700))
        except Exception as exc:
            report["message"] = f"Could not render a test structure: {exc}"
            return report

        ai = identify_compound(
            name=caption, formula=formula, weight=weight,
            image_path=image_path, source_file="selftest",
        )

    report["identification"] = ai.to_dict()
    if not ai.ok:
        report["message"] = ai.error or "The identifier returned nothing."
        return report

    report["structure_agreement"] = inchikey_agreement(expected_key, ai.inchikey)

    consensus = build_consensus({
        "caption_on_drawing": caption,
        "chemdraw_rdkit": {
            "smiles": smiles, "molecular_formula": formula, "molecular_weight": weight,
            "rdkit_inchikey": expected_key, "chemdraw_inchikey": None,
            "chemdraw_inchi_source": "selftest (no ChemDraw on this machine)",
        },
        "pubchem": {"found": False, "notes": "Skipped for the self-test."},
        "ai_identification": ai.to_dict(),
    })
    report["consensus"] = consensus

    symbol, detail = score_ai_match(ai, [expected_key], formula, consensus)
    report["ai_match"] = symbol
    report["ai_match_detail"] = detail
    report["ok"] = report["structure_agreement"] in ("exact", "skeleton") and consensus.get("ok", False)
    report["message"] = (
        f"Identifier returned {ai.inchikey or 'no structure'} for a compound whose key is "
        f"{expected_key} ({report['structure_agreement']}); consensus "
        f"{'succeeded' if consensus.get('ok') else 'failed'}."
    )
    return report
