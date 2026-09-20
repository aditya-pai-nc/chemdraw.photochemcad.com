# Project context

Working notes for `chemdraw.photochemcad.com` — what the system does, what was
changed and why, what was learned about ChemDraw's COM and menu surfaces on the
machine this runs on, and what is still open.

`README.md` documents how to run the thing. This file is the reasoning behind it
and the findings that are not recoverable from the code or the git history.

Last updated: 2026-09-14. Sections 1–11 describe the September 10 baseline;
section 12 records the CAS Common Chemistry addition. Consult git for current repository state.

---

## 1. What it is

Two independent pipelines behind one Next.js UI.

| | Input | Output | Needs |
|---|---|---|---|
| **ChemDraw pipeline** | `.cdx` | Excel workbook + ZIP of artifacts | Windows + ChemDraw, interactive desktop |
| **Spectral interpolation** | `.txt/.csv/.xlsx` | Interpolated curves + Excel | Nothing special |

They share transport and event machinery and deliberately nothing else.

```
browser ──▶ Next :3000 ──▶ FastAPI 127.0.0.1:8000 ──▶ worker.py ──▶ ChemDraw (COM + keystrokes)
             UI + /api proxy    (localhost only)      one subprocess per job
```

The browser never touches FastAPI. `frontend/app/api/[...path]/route.ts` proxies
every `/api/*` call server-side and re-streams both directions, so SSE arrives
live and large uploads/downloads never buffer in Node. FastAPI binds to
localhost; that binding is the entire security boundary — **there is no auth
anywhere**.

`--workers 1` is mandatory: job state and the ChemDraw FIFO queue live in
process memory (`backend/jobs.py`).

---

## 2. The ChemDraw pipeline as it stands

**Stage 0 — reset.** If ChemDraw holds any documents from earlier runs, quit it.
See [the document leak](#31-documentclose-is-a-no-op).

**Stage 1 — CDX → CDXML.** `cdx_to_cdxml.py` opens the upload and saves XML.

**Stage 2 — split.** `cdxml_to_ind.py` parses the CDXML geometrically: top-level
`<fragment>`/`<group>` nodes, noise dropped, each paired with the caption `<t>`
node below and horizontally aligned with it. Pure ElementTree — the only stage
that would run unchanged on a Mac.

**Stage 3 — gather and enrich.** Per molecule:

1. ChemDraw hands over **SMILES, InChI, InChIKey, MOL text**.
   Keystrokes first (`Edit > Copy As`), COM as fallback. See [section 4](#4-getting-data-out-of-chemdraw).
2. Every ChemDraw value is written to the row **immediately**, before anything
   that can fail is attempted.
3. RDKit reads ChemDraw's MOL text for molecular formula and weight. This step
   is allowed to fail without costing the row anything else.
4. RDKit canonicalises ChemDraw's SMILES.
5. PubChem is searched — exact InChIKey → skeleton → name → SMILES — and every
   candidate CID is swept so a field missing from the first record is filled
   from the next that has it.
6. `Ctrl+W` closes the document.

**Stage 4 — curate.** Only compounds where `InChIKey Match?` is not a tick go to
a small model (`claude-haiku-4-5`), which reconciles ChemDraw's data against
PubChem's and writes to its own worksheet. Skipped entirely without an API key.

### The two checks

Exactly two, by design. Their names are fixed.

| Column | Rule |
|---|---|
| `Match?` | PubChem's formula equals the drawn formula **and** the weights agree within 0.5 |
| `InChIKey Match?` | Canonical ChemDraw SMILES == canonical PubChem SMILES, character for character |

`InChIKey Match?` is named for the *lookup route*, not the comparison — PubChem
is reached via ChemDraw's InChIKey, then the SMILES are compared. This name was
chosen deliberately to match the column chemists already knew.

Verdicts are `✅` / `❌` / `—` only. No `🟡`, no skeleton tier, no stereo
tolerance, no "best of two comparisons".

**Widening the search does not widen the verdict.** A record found by name still
scores `❌` unless the canonical SMILES are identical.

### The workbook

Three sheets:

1. **Compounds** — 22 columns. Identity, ChemDraw's SMILES/InChIKey/canonical
   SMILES, formula, MW, PubChem CID + all CIDs + formula/MW/SMILES/canonical
   SMILES/InChIKey, the two verdicts, CAS, IUPAC name, synonyms, links,
   `Manual Match` (left empty for the researcher).
2. **Unmatched - AI curated** — one row per unmatched compound.
3. **Description** — how matching works, written for a chemist. Generated from
   `DESCRIPTION_ROWS` in `backend/pipeline.py`.

Was 87 columns before simplification. Extraction routes, round-trip
diagnostics, and PubChem's ~48 computed descriptors were all removed.

---

## 3. ChemDraw COM findings

All verified on this machine — ChemDraw Professional, ProgID `ChemDraw.Application`.
**These are the notes most worth keeping.** They cost the most to discover and
none of them are in any documentation.

### 3.1 `Document.Close()` is a no-op

It returns success and leaves the document open. Every variant was tried:

| Attempt | Result |
|---|---|
| `doc.Close()` | no error, `Documents.Count` unchanged |
| `doc.Close(False)` | no error, unchanged |
| `doc.Close(0)` | no error, unchanged |
| `doc.Close(SaveChanges=False)` | `TypeError` — no such keyword |
| `doc.Saved = True` first | `AttributeError: Property 'Item.Saved' can not be set` |
| `app.ActiveDocument.Close()` | no error, unchanged |

Consequence: a job that opens one document per molecule **leaked every one of
them**. Observed 12–14 open documents after an 11-compound run, accumulating
across jobs.

Two things fix it:

- **`Ctrl+W`** per molecule (`chemdraw_keys.close_document`) — this actually
  works. Document count now stays flat at 1 across a full run.
- **`app.Quit()`** at stage 0 — works cleanly, no dialog, process exits, and COM
  relaunches ChemDraw on the next Dispatch.

`Ctrl+W` on a dirty document can raise a "Save changes?" prompt. The code waits
up to 1.5s and *looks* for a real dialog (window class `#32770` owned by the
ChemDraw process) before answering `n`. Sending the key blind would type into
the drawing canvas.

### 3.2 `Documents.Open()` returns None instead of raising

When ChemDraw refuses a file it returns `None`. The caller then died on
`None.Activate()` — an `AttributeError` naming entirely the wrong problem. This
is what the leak surfaced as in practice.

`chemdraw_com.open_document()` checks for it and raises with the real cause and
the current document count.

### 3.3 MIME types

`Objects.Data(mime)` / `Objects.GetData(mime)` — both spellings are probed
because late binding gives no way to ask which exists.

| Format | MIME that works here | Note |
|---|---|---|
| SMILES | `chemical/x-daylight-smiles` | |
| InChI | `chemical/x-inchi` | |
| **InChIKey** | **`chemical/x-inchikey`** | the hyphenated `chemical/x-inchi-key` is **not supported** |
| MOL | `chemical/x-mdl-molfile` | returns **V2000** |

The clipboard (`Copy As`) MOL is **V3000**. Both parse in RDKit.

### 3.4 ProgID discovery is expensive

`_discover_progids()` enumerates every key under `HKEY_CLASSES_ROOT` — 50,000+.
It was being called on *every* `connect_chemdraw()`, i.e. once per molecule.
Now cached in a module global after first success.

### 3.5 COM apartment

uvicorn worker threads are not a COM STA. Without `pythoncom.CoInitialize()`,
`Dispatch` fails with an **empty** HRESULT and the UI reports "ChemDraw not
found". COM errors are frequently blank because ChemDraw does not populate
`IErrorInfo` — hence `str(exc) or repr(exc)` throughout.

### 3.6 Dispatch attaches, it does not isolate

ChemDraw registers as an out-of-process single-instance server, so repeated
`Dispatch` calls land in the **same** process sharing one document collection.
That is the whole reason `ChemDrawQueue` exists.

---

## 4. Getting data out of ChemDraw

Two routes. **Keystrokes take precedence** — that is what a chemist can verify
by hand, so it is treated as what the drawing means.

```
Ctrl+A          select all
Alt+E           Edit menu
o               Copy As submenu
s / n / k / m   SMILES / InChI / InChIKey / MOL text
read clipboard
```

Mnemonics are configurable (`CHEMDRAW_COPYAS_KEYS`) because they shift between
releases.

### Focus is the hard part

Windows blocks `SetForegroundWindow` from a process that does not already own
the foreground — exactly a uvicorn service. **The keystroke route silently never
worked until this was fixed.** `_focus()` now stacks:

1. `SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0)`
2. a synthetic ALT tap (marks the thread as having had recent input)
3. `AttachThreadInput` to the current foreground thread
4. `SetForegroundWindow` + `BringWindowToTop`, with a `HWND_TOPMOST` nudge fallback
5. up to 3 attempts

Confirmation accepts **any window of the same process**. ChemDraw's foreground
window after a raise is its main frame, not the document handle that was
located, so an exact-handle check reported failure on raises that had worked.

### Staleness guard

The clipboard is emptied before every copy. Without it, a wrong mnemonic leaves
the *previous* format sitting there and it reads back as a success.

### Do not strip a MOL block

`copy_as()` returns the clipboard **unstripped**. A MOL file's line 1 is its
title line and is usually blank; stripping it shifts every line of a
fixed-column format up by one and RDKit refuses the result. Per-format tidying
belongs in `chemdraw_com._clean`.

### Operational cost

ChemDraw runs **visible** and takes focus once per molecule. The machine cannot
be used for anything else while a job runs — any window brought forward will
receive the keystrokes.

### SLN

Dropped. COM never served it and the menu never produced it here. It cost ~4s
per compound in failed clipboard polling for a column nobody reads. One line to
restore in `chemdraw_com.FORMATS` plus the `_clean` branch.

---

## 5. PubChem

Lookup order, strongest first (`pubchem.lookup`):

1. **Exact InChIKey** — a hash; a hit cannot be coincidence
2. **InChIKey skeleton** (first 14 chars, connectivity only)
3. **Name** (the caption — weak, often a lab code)
4. **SMILES**

`GET /rest/pug/compound/inchikey/{key}/cids/JSON`, then a property fetch for
every CID returned.

### Candidate sweeping

One search term routinely resolves to several records — a parent, a salt, a
labelled analogue — and they are not equally complete. The first is the answer
(it decides the CID and therefore which compound the row is about); any empty
field is filled from the first later candidate that has it, and the borrow is
recorded in `field_sources`. `MAX_CANDIDATES` = 25; the reference sweep
(CAS/synonyms/Wikipedia, 3 pug_view calls each) is capped much tighter at 5.

Rate limit: 0.25s between calls (PubChem asks for ≤5/s).

---

## 6. Findings on `T_hydroporphyrins.cdx`

The reference file. 16 captions, 11 molecules split.

### Match rates (most recent verified run)

| | |
|---|---|
| PubChem records found | 6 / 11 |
| `Match?` ✅ | 5 |
| `InChIKey Match?` ✅ | 1 (Chlorin e6) |
| Curated | 10 |

### Exact-InChIKey hit rate is zero

All 6 keys ChemDraw produced return `PUGREST.NotFound`. Verified with raw curl,
outside the code. These drawings define stereochemistry differently from how the
compounds are deposited, and an InChIKey is a hash — there is no near-miss.

**This is why the skeleton/name fallbacks cannot be removed.** A strict
exact-key-only pipeline returns an empty sheet on this file.

### The Chlorin e6 case — worth understanding

| Source | InChIKey |
|---|---|
| ChemDraw MOL → RDKit | `VAJLRIOJDADNAT-HHGNVTQFSA-N` |
| ChemDraw InChI export | `VAJLRIOJDADNAT-HHGNVTQFSA-N` |
| **ChemDraw SMILES export** | `VAJLRIOJDADNAT-UWJYYQICSA-N` |
| PubChem CID 138978174 | `VAJLRIOJDADNAT-UWJYYQICSA-N` |

**ChemDraw's own SMILES and MOL exports disagree about stereochemistry for the
same document.** Same skeleton, different stereo block.

The current rule compares ChemDraw's *SMILES* against PubChem's SMILES, so this
scores `✅` — which is correct per the specification. Be aware the MOL-derived
key tells a different story.

Also: PubChem's *stored* key for CID 138978174 is `UWJYYQICSA`, but a key
*recomputed from PubChem's own 2D SDF* comes back `HHGNVTQFSA`. Two artefacts,
not one — likely 2D stereo perception from wedge bonds. Not currently acted on.

### The 5 metal-coordinated failures — open issue

Chlorophyll a, chlorophyll b, bacteriochlorophyll a, and the two generic
`M = H,H / Cu / Zn` substituent-table panels fail with:

```
Explicit valence for atom # 3 N, 4, is greater than permitted
```

RDKit rejects the nitrogen coordinating the Mg. **Pre-existing** — the run from
before any of this session's changes has the identical 5 failures. V3000 from
the clipboard did not help; it is the valence model, not the format.

Since the ChemDraw-data-first fix, this costs only the formula and weight. The
InChIKey, SMILES and canonical SMILES are still recorded and PubChem is still
searched. **This fix is compiled but has not yet been confirmed by a full run.**

A real fix needs `sanitize=False` plus manual handling for metal-coordinated
macrocycles.

---

## 7. Design decisions and why

**Two matches only, `Match?` and `InChIKey Match?`.** Directed by the user.
Earlier iterations had 3–4 verdict columns plus detail columns; a chemist reads
this sheet and the extra columns were noise.

**ChemDraw data over everything.** Directed by the user. ChemDraw is the
authority on what is on the page. Its values are written to the row before
anything that can fail runs. No downstream failure discards them.

**Keystrokes over COM.** Directed by the user. Same converters, but the menu is
what a person can reproduce by hand.

**Canonicalise before comparing.** `OC(=O)c1ccccc1` and `c1ccccc1C(O)=O` are the
same molecule and share almost no characters. Both sides go through RDKit's
canonical writer; comparing raw strings from two toolkits answers a question
nobody asked.

**AI never authors a machine column.** Curated values live on their own sheet.
The model may only quote an InChIKey that already appears in its evidence —
anything else is discarded and the attempt recorded in `Curation Error`. A key
is a hash; it cannot be reasoned out, only copied.

**Curation sees a trimmed view.** Not the full property sweep — 3D descriptors
and fingerprints have no bearing on whether two structures are the same
compound.

---

## 8. Bugs found and fixed this session

| Bug | Where | Status |
|---|---|---|
| `Documents.Open` returning None surfaced as `AttributeError` | `cdx_to_cdxml.py` | fixed — `open_document()` raises with the real cause |
| Stage 1 had no `try/finally`, leaking the parent document | `cdx_to_cdxml.py` | fixed |
| Document leak (12–14/run) from `Close()` being a no-op | `processor.py` | fixed — `Ctrl+W`, count now flat at 1 |
| ProgID sweep of all of `HKEY_CLASSES_ROOT` per molecule | `chemdraw_com.py` | fixed — cached |
| Keystroke route never worked (focus always failed) | `chemdraw_keys.py` | fixed — layered focus |
| `copy_as` stripping a MOL block's title line | `chemdraw_keys.py` | fixed — returns unstripped |
| `effort` param rejected by Haiku, retry kept it | `ai_curate.py` | fixed — capability ladder, cached |
| ChemDraw data discarded when RDKit MOL parse failed | `processor.py` | fixed, **unverified by a run** |
| `structural_agreement` taking best-of-two across *different* local structures | `inchi_tools.py` | removed in simplification |

### Two corrections to earlier claims

- I reported the keystroke fallback "found the window, took focus, sent keys"
  when SLN was failing. **Focus was failing.** The sequence never sent a key. I
  inferred from the error text instead of testing the path in isolation.
- I described the keystroke fallback as a working safety net for weeks of this
  work. It had never once executed successfully.

---

## 9. Configuration

Everything optional. `backend/.env` (gitignored) or real env vars — the latter win.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(unset)* | Enables curation |
| `CHEMDRAW_AI_ENABLED` | `auto` | `0` forces curation off |
| `CHEMDRAW_CURATE_MODEL` | `claude-haiku-4-5-20251001` | |
| `CHEMDRAW_CURATE_CONCURRENCY` | `4` | |
| `CHEMDRAW_CURATE_EFFORT` | *(unset)* | **Only set for a model that supports it** — Haiku 4.5 does not |
| `CHEMDRAW_KEYS_FALLBACK` | `1` | `0` disables the keystroke route |
| `CHEMDRAW_COPYAS_KEYS` | `smiles=s,sln=l,inchi=n,inchikey=k,molfile=m` | Menu mnemonics |
| `CHEMDRAW_KEYS_TIMEOUT` | `4.0` | Clipboard wait |
| `CHEMDRAW_KEYS_DELAY` | `0.12` | Between keystrokes |
| `CHEMDRAW_PUBCHEM_MAX_CANDIDATES` | `25` | |
| `CHEMDRAW_PUBCHEM_MAX_REFERENCE` | `5` | |
| `CHEMDRAW_MAX_QUEUE` | `20` | |
| `CHEMDRAW_API_URL` | `http://127.0.0.1:8000` | frontend → backend |

Neither `data/` directory is ever cleaned up.

---

## 10. Verification commands

```bash
# Services
cd backend && ./venv/Scripts/python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
cd frontend && npm run dev

# Health
curl localhost:8000/api/health
curl localhost:8000/api/chemdraw        # ChemDraw + ProgID
curl localhost:8000/api/ai              # curation config
curl localhost:8000/api/ai/selftest     # one small-model call, known answer
curl localhost:8000/api/inchikey/selftest

# PubChem by hand
B=https://pubchem.ncbi.nlm.nih.gov/rest/pug
curl -s "$B/compound/inchikey/VAJLRIOJDADNAT-HHGNVTQFSA-N/cids/JSON"   # 404 here
curl -s "$B/compound/inchikey/VAJLRIOJDADNAT/cids/JSON"                # skeleton hits

# Full run through the proxy
curl -F "file=@backend/data/jobs/157685b2316c47078bb204cda3b6e635/input/T_hydroporphyrins.cdx" \
     localhost:3000/api/jobs
curl -N localhost:3000/api/jobs/<JOB_ID>/events

# Is ChemDraw leaking documents?
cd backend && ./venv/Scripts/python.exe -c "import sys;sys.path.insert(0,'.');import config;from chemdraw_com import open_document_count;print(open_document_count())"
```

Two encoding traps when checking output by hand on Windows:

- Set `PYTHONIOENCODING=utf-8` or printing `✅`/`❌` raises `UnicodeEncodeError` on cp1252.
- Python does not resolve the bash `/tmp`; use a real Windows path.

---

## 11. Repository state

**Nothing in this session is committed.** Last commit is `a851ed5 inchl key`.

```
new:      backend/ai_curate.py, backend/chemdraw_keys.py
deleted:  backend/ai_identify.py
modified: README.md, backend/.env.example, backend/app.py, backend/cdx_to_cdxml.py,
          backend/chemdraw_com.py, backend/config.py, backend/inchi_tools.py,
          backend/jobs.py, backend/pipeline.py, backend/processor.py,
          backend/pubchem.py, backend/requirements.txt, backend/worker.py,
          frontend/app/page.tsx, frontend/components/ProcessingScreen.tsx,
          frontend/components/ResultsScreen.tsx, frontend/lib/types.ts
```

`pillow` was dropped from requirements — no image is sent to a model any more.

### Immediate next step

The **ChemDraw-data-first fix in `processor.py` has not been verified by a full
run.** It compiles. It should mean T-01 (Chlorophyll a) now shows its ChemDraw
InChIKey and SMILES with a blank formula/weight, instead of an empty row. Run
`T_hydroporphyrins.cdx` and check row 4.

### Known open

1. **5 metal-coordinated compounds** have no formula/weight (RDKit valence).
2. **Stage 1's parent document** is not closed by `Ctrl+W` — 1 document remains
   open after a run. Harmless; stage 0 clears it.
3. **No auth.** The localhost binding is the only boundary.
4. **`data/` grows forever.**
5. **PubChem stored vs recomputed InChIKey** can disagree (see Chlorin e6).
6. **Machine is unusable during a run** — ChemDraw takes focus per molecule.

## 12. CAS Common Chemistry verification (2026-09-14)

Requested as an additional verification source in the existing flow. The two
PubChem checks retain their names, comparison rules and curation gate. Stage 3
now also checks CAS after PubChem enrichment; it runs even if PubChem finds no
record. CAS results and reference values have their own columns in Compounds and
are copied as context to the curation sheet and evidence. SSE carries the verdict,
explanation, CAS RN and link into the results table.

`backend/common_chemistry.py` owns the client. It tries the full ChemDraw InChIKey,
PubChem CAS annotations, the drawing's SMILES and the caption, with at most five
candidate records by default. It never combines fields from separate CAS RNs.
Verification compares canonical isomeric SMILES. CAS's `canonicalSmile` can omit
stereochemistry; use `smile` or reconstruct from InChI. Missing structural data,
no record and API failures are separate outcomes. Neither a name hit nor matching
formula/weight can verify a compound. A mismatched candidate does not prove no
matching CAS record exists; bounded or incomplete lookups are disclosed.

CAS's public API returned HTTP 403 without credentials and explicitly required
`X-API-KEY`. The user subsequently supplied a key, saved only in the ignored
`backend/.env`. A live aspirin verification succeeded. `/api/cas` reports
configuration; `/api/cas/selftest` checks aspirin without driving ChemDraw. Public
API overview: https://commonchemistry.cas.org/api-overview .

Offline regression checks in `backend/test_common_chemistry.py` cover exact and
stereo comparisons, fallback routes, multiple candidates, missing data, access
failures, caching, curation evidence, unchanged PubChem verdicts, SSE, and workbook
export. A full ChemDraw desktop run remains unverified for this addition.

## 13. Bulk CAS extractor

Requested by Masahiko Taniguchi: upload roughly 100 CAS numbers in TXT/CSV/Excel,
then retrieve formula, weight, SMILES, InChI and both database links. Implemented
as `/bulk-cas`, a third tab alongside ChemDraw and interpolation. `bulk_cas.py`
parses files and queries CAS/Common Chemistry and PubChem; `bulk_cas_jobs.py`
owns background jobs, progress snapshots, cancellation and downloads. No desktop
automation or AI is involved.

Limits: 500 entries, 2 MB uploads, 10 queued/running batches, one batch at a time.
Legacy XLS uses `xlrd`; XLSX uses `openpyxl`. Input order and duplicates remain;
the extractor caches duplicate lookups. Invalid entries retain their own rows.
Primary values come from the single CAS record when available, else PubChem.
Independent source records and canonical isomeric SMILES agreement remain visible
in row details and the exported workbook. Never merge fields across candidate
substances. The CAS client now has `lookup_rn` for direct registry-number lookup.

Completed snapshots and Excel/CSV files live in `backend/data/bulk_cas/<id>/`.
Refresh restores the last batch, and completed snapshots survive server restarts.
Stop finishes the in-flight lookup and exports partial results. Existing ChemDraw
queue/state is untouched.

Validation: 30 offline regression tests, including 100-row API upload, both Excel
formats, checksum handling, duplicate caching, source selection, exports, saved
snapshots and cancellation. Live CAS and PubChem lookups agreed for aspirin,
caffeine and ethanol. Frontend type checking and production build passed.
Browser verification covered a live five-entry upload (three distinct compounds,
one invalid number, one duplicate), Excel download, both source detail panels,
refresh recovery, the review filter, and desktop/mobile layouts. No browser errors
were observed. A sample workbook is under
`backend/data/bulk-cas-qa/professor-extractor-example.xlsx` (ignored test output).

---

## 14. Deployment: Vercel + AWS Lightsail

Added 2026-09-20.

The production split is **Vercel (Next UI + `/api` proxy) → public internet →
AWS Lightsail Windows instance (FastAPI + ChemDraw + `data/jobs/`)**.

The backend cannot live on Vercel: it is Linux serverless, with no COM, no
ChemDraw, a read-only filesystem, and function durations measured in seconds
against a pipeline that runs for minutes per file. Only the frontend goes there.

### 14.1 Authentication is now required for that topology

The original security model was one sentence — *FastAPI binds to 127.0.0.1 and
is never reachable from the internet*. Proxying from Vercel breaks that
assumption and leaves every endpoint open to anyone who finds the address:
upload a `.cdx` and drive ChemDraw on the instance, download other jobs'
output, enumerate the queue, spend the Anthropic key through curation.

`CHEMDRAW_API_TOKEN` on **both** sides:

- Backend (`app.py`) — a middleware requiring `x-chemdraw-token` (or
  `Authorization: Bearer`), compared with `secrets.compare_digest`. Unset means
  disabled, so a localhost-only development setup is unchanged.
- Frontend (`app/api/[...path]/route.ts`) — injected **server-side**. Never
  `NEXT_PUBLIC_`; a token the browser can send is a token the browser can read.

`/api/health` is exempt and reports `auth_required`, so a deployment cannot be
open without saying so. A 401 through the proxy is rewritten to name the actual
cause — a missing or mismatched token — rather than surfacing a bare status.

### 14.2 Keystrokes cannot work on a cloud instance

`Edit > Copy As` needs a visible window, foreground focus, and an **active**
interactive session. On an RDP box the session goes to `Disc` the moment you
disconnect: `keybd_event` reports nothing, the clipboard simply never changes,
and the caller waits out its timeout for every format of every compound —
roughly **15 seconds per molecule** proving something knowable once.

`chemdraw_keys.can_send_input()` now calls `OpenInputDesktop()`, which succeeds
only for a process attached to the window station that owns user input. It fails
on a locked workstation, a disconnected RDP session, and in session 0. The
result is cached for `CHEMDRAW_KEYS_RECHECK` seconds (default 30) so that
reconnecting RDP restores the keystroke route without a restart.

Measured: with no input desktop, all four formats fall through to COM in
**1.3s** per molecule instead of ~15s. The same build therefore uses keystrokes
on an interactive desktop and COM-only on Lightsail, with no configuration.

`CHEMDRAW_KEYS_FALLBACK=0` still forces COM-only explicitly if wanted.

### 14.3 ChemDraw on the instance — unresolved

`/api/chemdraw` on the Lightsail box returns:

| ProgID | HRESULT | Meaning |
|---|---|---|
| `ChemDraw.Application`, `.40`, `.39`, `ChemOffice.ChemDrawApp` | `0x800401F3` `CO_E_CLASSSTRING` | not registered on that host |
| `ChemDraw_x64.Application` | `0x80080005` `CO_E_SERVER_EXEC_FAILURE` | registered, but the exe would not launch |

On the dev box (Windows 11, build 22631) **both** are registered in HKLM and
`ChemDraw.Application` connects. The half-state on Lightsail points at a partial
install, an unfinished activation, or a service running in session 0. Check
there, in the account uvicorn runs as:

```powershell
reg query "HKLM\SOFTWARE\Classes\ChemDraw_x64.Application\CLSID"
reg query "HKCR\CLSID\{guid}\LocalServer32"   # does that exe exist?
query session                                     # session 0 means no desktop
```

Also worth confirming the ChemDraw licence permits a cloud VM at all.

### 14.4 Expect Vercel to cut the SSE stream

Vercel serverless functions have a hard maximum duration, and
`/api/jobs/{id}/events` is a long-lived stream while a job runs for minutes. The
`proxyTimeout: 30min` in `next.config.mjs` applies to `next start`, not Vercel's
runtime. The EventSource `Last-Event-ID` resume logic will partly mask this;
long uploads through the same path are likelier to fail outright. Not yet
addressed — it only becomes visible once ChemDraw starts on the instance.

### 14.5 `deploy-windows.ps1`

One script at the repo root does the whole box: venv, dependencies, pywin32 COM
registration, API token, autostart, firewall, then a verification pass that
queries `/api/chemdraw` rather than assuming it works.

```powershell
.\deploy-windows.ps1                        # local, loopback, ChemDraw works
.\deploy-windows.ps1 -BindHost 0.0.0.0      # reachable from Vercel; prints a generated token
.\deploy-windows.ps1 -BindHost 0.0.0.0 -WithFrontend
.\deploy-windows.ps1 -Mode Service          # no ChemDraw; warns loudly
```

**It installs a Scheduled Task at logon, not a Windows Service, and that is the
whole point.** A service runs in session 0 with no desktop, so COM cannot launch
ChemDraw and the pipeline dies with `CO_E_SERVER_EXEC_FAILURE`. An earlier
NSSM-based install script was the direct cause of the failure in 14.3. `-Mode
Service` is still available for the interpolation and CAS halves, which need no
ChemDraw, and it sets `CHEMDRAW_KEYS_FALLBACK=0` since there is no desktop.

The machine must stay logged in. On a cloud instance, connect once over RDP and
leave the session connected — signing out ends the desktop the task runs in.

Other behaviour worth knowing:

- Removes any previous service *and* task before installing, and kills a stale
  listener on the port, so the two installation modes can never fight.
- Generates `CHEMDRAW_API_TOKEN` automatically whenever `-BindHost` is not
  loopback, reusing one already in `backend/.env` if present, and writes the
  matching value to `frontend/.env.local` under `-WithFrontend`.
- Refuses to run unelevated; fails on an `import app` error before installing
  autostart, so a broken deploy surfaces immediately rather than at next boot.
- Tested: parse-clean, admin guard fires, `Set-EnvValue` handles commented keys,
  absent keys, repeat runs, empty files and similar key names without
  corruption; the task builds with `LogonType=Interactive`,
  `ExecutionTimeLimit=PT0S`, `MultipleInstances=IgnoreNew`.
