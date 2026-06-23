# Dimensional Proof-Carrying Answers

> **Proof-carrying financial QA. We don't make the model smarter — we make wrong answers unrepresentable.**

**Result (on 10 OfficeQA-style questions over a trap-laden financial workbook):**

| | Accuracy | Dimensional traps caught |
| --- | --- | --- |
| **Ours (proof-carrying)** | **10 / 10 (100%)** | **1 / 1 rejected** |
| Baseline (plain RAG) | 3 / 10 (30%) | 0 / 1 |
| **Delta** | **+70 percentage points** | — |

Every answer ships with a **receipt**: the typed evidence tuples used, a deterministic computation trace, the dimension-check log, and a verdict from an adversarial verifier. When numbers are dimensionally incompatible, the system **refuses to answer** rather than guess — and that refusal is the win.

Built for the Sentient Arena **Grounded Reasoning** challenge.

---

## The four guarantees

Every output satisfies all four, or it is rejected:

1. **Backed by a real source span** — every number traces to an exact cell (`Income Statement!C5`), so there's no missing evidence.
2. **Dimensionally consistent** — numbers are never combined across mismatched *scale* (thousands vs millions), *period* (Q4 vs full year), *entity* (segment vs consolidated), or *flow vs stock*.
3. **Computed deterministically** — a real calculator does the arithmetic over an op whitelist (`add`, `sub`, `mul`, `div`, `ratio`, `pct_change`, `identity`). The LLM never emits the final digits.
4. **Survived an attempt to disprove it** — a skeptical "auditor" LLM tries to break the answer before it's final.

## Why this works (the core insight)

Financial QA fails not because the model lacks knowledge, but because it combines numbers with mismatched *meaning*. A "$ in thousands" footnote, a Q4 column next to an FY column, a Cloud-Segment row next to a Consolidated row, a period flow next to a point-in-time balance — each is an invitation to silently invent a meaningless number. We label every number with its dimensions up front and **refuse to combine incompatible ones**, turning the most common failure modes into impossible ones.

## A caught trap (the money shot)

> **Q:** _What is ACME's FY2024 total revenue plus cash on hand at year-end FY2024?_

```
Ours:    ✕ REJECTED — Dimensional Guard:
         period mismatch: 'FY2024' vs 'as_of_2024-12-31'
         (cannot add 'Total revenue' and 'Cash and cash equivalents')
Baseline: confidently returns a summed revenue and cash figure (a flow + a stock, summed)
```

Adding a period total (revenue, a *flow*) to a point-in-time balance (cash, a *stock*) is meaningless. The Dimensional Guard rejects it on the `kind` dimension before any arithmetic happens. The baseline happily adds them.

---

## How to run

```bash
# 1. Install
python -m pip install -r requirements.txt

# 2. (Optional) add your key — without it, the pipeline runs in MOCK mode
cp .env.example .env
#   then edit .env: OPENROUTER_API_KEY=sk-or-v1-...

# 3. Generate the sample financial document
python make_sample_data.py

# 4. Run the ablation
python eval.py

# 5. Run the Dimensional Guard unit tests + the security audit
python tests/test_guard.py
python tests/test_security.py

# 6. Serve the demo
python -m uvicorn app:app --reload
#   open http://127.0.0.1:8000  →  the Grounding Diff page
```

### No API key? No problem.

With no `OPENROUTER_API_KEY` set, the pipeline runs end-to-end in **mock mode** — a small rule-based responder stands in for the LLM so extraction, planning, the Dimensional Guard, deterministic compute, and the verifier all execute. The ablation numbers above were produced in mock mode and are fully reproducible. Drop in a real key and the same pipeline drives a real model (`openai/gpt-4o-mini` by default; override with `MODEL=`).

## Endpoints

| Method | Path | Returns |
| --- | --- | --- |
| `POST` | `/answer` | A `ProofObject` — `{answer, normalized_value, citations, trace, dimension_checks, verifier_verdict}` — or `{answer: null, rejected: true, reason}` when the Guard catches a mismatch. |
| `POST` | `/baseline` | Plain RAG: `{answer, citation}`. This is the control and the "confidently wrong" side of the demo. |
| `POST` | `/spans` | The retrieved spans for a question (for debugging / the UI). |
| `GET`  | `/` | The Grounding Diff demo page. |
| `GET`  | `/health` | `{status: ok}`. |

**Request body** for the POST endpoints: `{"question": "...", "doc_path": "sample_data/acme_financials.xlsx"}`.

## Architecture

```
Question + Document
   │
   ├─ 1. ingest ─────────── table-aware parsing (xlsx/csv/pdf), keeps cell
   │                        coords + context (table title, column header,
   │                        footnotes like "$ in thousands")
   ├─ 2. retrieve ───────── keyword + magnitude-aware ranking → top spans
   ├─ 3. extract_ledger ─── LLM types every figure → EvidenceTuple
   │                        {value, unit, scale, currency, entity, period,
   │                         kind(flow|stock|rate), metric, source_span}
   ├─ 4. plan ───────────── LLM emits a JSON compute plan (ops + tuple ids);
   │                        it never emits the final number
   ├─ 5. check_plan ─────── Dimensional Guard (deterministic) — raises
   │                        DimensionError on any mismatch
   ├─ 6. execute_plan ───── real calculator over the op whitelist
   ├─ 7. verify ─────────── adversarial auditor LLM tries to break it
   └─ 8. ProofObject ────── answer + citations + trace + checks + verdict
```

The differentiator is steps 5–6: the LLM proposes *what* to compute; a deterministic core decides *whether it's legal* and does the actual math.

## Project layout

```
.
├── app.py                 # FastAPI: /answer, /baseline, /spans, demo page
├── schemas.py             # EvidenceTuple, ComputeStep, ProofObject, Span
├── ingest.py              # xlsx/csv/pdf parsing + table-aware retrieval
├── pipeline.py            # extract_ledger, plan, verify (LLM stages)
├── guard.py               # Dimensional Guard + deterministic execute_plan
├── llm.py                 # OpenRouter client + on-disk cache + mock mode
├── eval.py                # ablation: ours vs baseline, prints the table
├── eval.jsonl             # 10 questions (6 clean + 4 traps) with gold answers
├── make_sample_data.py    # generates sample_data/acme_financials.xlsx
├── tests/test_guard.py    # 13 unit tests for the Guard + compute
├── tests/test_security.py # 39-check security/vulnerability audit
├── static/index.html      # Grounding Diff demo (two-column, trap buttons)
├── requirements.txt
└── sample_data/acme_financials.xlsx
```

## The trap questions (`eval.jsonl`)

The eval deliberately includes four dimensional traps so the demo always has a catch:

| Trap | Question | What the baseline does wrong |
| --- | --- | --- |
| **Period** | total revenue for Q4-2024 | picks the full-year column or ignores the quarter |
| **Entity** | consolidated net income (not the Cloud Segment) | returns the segment number (215,000 vs 520,000) |
| **Flow vs stock** | total revenue **plus** cash on hand | adds a period flow to a point-in-time balance — meaningless |
| **Scale** | revenue "as reported under the 'in thousands' footnote" | reports the wrong magnitude (raw vs scaled) |

## Security

The service takes a user-supplied `doc_path` and a `question`, so it has the usual attack surface of a document-QA endpoint. A defensive audit (`tests/test_security.py`, **39 checks**) passes clean. Controls in place:

| Surface | Control |
| --- | --- |
| **Path traversal / local-file read** | `ingest.resolve_doc_path()` confines `doc_path` to `DOCS_ROOTS` (default `./sample_data`; override via `DOCS_ROOTS` env, `os.pathsep`-separated) after `..`/symlink normalization, AND restricts extensions to `.xlsx/.xlsm/.csv/.pdf`. Disallowed paths → HTTP **400**, not 500. |
| **Request-size DoS** | `AskRequest` caps `question` to 2000 chars and `doc_path` to 512 via pydantic `Field(max_length=...)`. Oversized → HTTP **422**. |
| **Arithmetic DoS** | The deterministic calculator raises `DimensionError` on divide-by-zero / ratio-by-zero / pct_change-from-zero; overflow → `inf` (caught, no crash). No `eval`/`exec` — a fixed op whitelist. |
| **Cache disk-fill** | `llm.MAX_CACHE_ENTRIES = 2000` with FIFO eviction; a hostile client sending unique prompts can't grow `cache.json` without bound. |
| **Secrets** | `OPENROUTER_API_KEY` lives only in `.env` (gitignored). No key is hardcoded or logged; the API-error path logs only `type(e).__name__`, never the key. `cache.json` is also gitignored. |
| **LLM output trust** | All model output is schema-validated; `source_span` ids must exist in the retrieved-span set (hallucinated ids are dropped); `_coerce_float` never raises on junk values. |
| **Prompt injection** | Inherent to RAG — the user question reaches the model. Mitigations: `response_format=json_object`, post-call schema validation, and the deterministic Guard/verifier that can still reject a manipulated plan. |

Run the audit:

```bash
python tests/test_security.py     # 39 checks
```

Verified at the HTTP layer: `../etc/passwd` → 400, `.env` → 400, 5MB question → 422, a flow+stock trap → 200 with `rejected: true`, and a prompt-injection attempt → 200 with the correct answer (no system prompt leaked).

## Limitations (honest scope)

- **One failure class, deeply.** This targets *dimensional* errors — scale/period/entity/flow-stock. It will not catch a model that picks the right number for the wrong semantic reason beyond those axes.
- **Small eval set.** 10 hand-authored questions on one synthetic workbook. The point is to demonstrate the mechanism and the delta, not to claim a benchmark.
- **Retrieval is keyword-based.** No vector DB; fine for small financial docs, would need upgrading for large corpora.
- **Mock mode** stands in for the LLM when no key is present. With a real key the same pipeline runs against `openai/gpt-4o-mini` (or any OpenRouter model).
- **Single currency** assumed; `CURRENCY_RATES` is a stub.

## Pitch

> **"We don't make the AI smarter — we make wrong answers unrepresentable."**
