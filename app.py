"""Dimensional Proof-Carrying Answers — FastAPI app.

POST /answer   -> proof-carrying answer (typed -> guarded -> computed -> verified)
                 or {answer: null, rejected: true, reason} on a DimensionError.
POST /baseline -> plain RAG control (the "confidently wrong" side of the demo).
GET  /         -> the Grounding Diff demo page.

Run:  uvicorn app:app --reload
"""
from __future__ import annotations

import os
from typing import List

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from guard import DimensionError, check_plan, execute_plan
from ingest import UnsafePathError, ingest, retrieve
from llm import llm_json
from pipeline import extract_ledger, plan, verify
from schemas import ProofObject, Span

MAX_RETRIES = 2  # cap so a loop can't burn credits

# Request-size caps: stop a hostile client from sending pathological payloads
# that would be embedded into LLM prompts or expanded into huge spans.
MAX_QUESTION_LEN = 2000
MAX_DOC_PATH_LEN = 512

app = FastAPI(title="Dimensional Proof-Carrying Answers")
app.mount("/static", StaticFiles(directory="static"), name="static")


class AskRequest(BaseModel):
    question: str = Field(..., max_length=MAX_QUESTION_LEN)
    doc_path: str = Field(..., max_length=MAX_DOC_PATH_LEN)


class StressTestRequest(BaseModel):
    doc_path: str = Field(..., max_length=MAX_DOC_PATH_LEN)


@app.exception_handler(UnsafePathError)
async def _unsafe_path_handler(_req, exc: UnsafePathError):
    # 400, not 500 — this is a client error (disallowed path).
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# --------------------------------------------------------------------------- #
#  The proof-carrying pipeline                                                 #
# --------------------------------------------------------------------------- #
def run_proof_pipeline(question: str, doc_path: str) -> ProofObject:
    """Full pipeline with up to MAX_RETRIES re-retrievals on guard/verify failure."""
    last_failure_reason = None
    for attempt in range(MAX_RETRIES + 1):
        all_spans: List[Span] = ingest(doc_path)
        spans = retrieve(question, all_spans)

        ledger = extract_ledger(question, spans)
        if not ledger:
            last_failure_reason = "no typed evidence could be extracted"
            continue  # re-retrieve (broader) on next attempt

        steps = plan(question, ledger)
        if not steps:
            last_failure_reason = "no compute plan produced"
            continue

        try:
            checks = check_plan(steps, ledger)
            value, trace = execute_plan(steps, ledger)
        except DimensionError as e:
            # A dimensional mismatch is a HARD reject — do not retry into the
            # same trap. Surface it; rejecting wrong answers is the point.
            return ProofObject(
                answer=None, rejected=True, reason=str(e),
                citations=[t.source_span for t in ledger.values()],
                trace=[], dimension_checks=[],
                verifier_verdict="not_run",
            )

        # Build a human answer string.
        answer_str = _format_answer(value, ledger, steps)
        verdict = verify(question, answer_str, ledger, spans)

        if verdict == "survived" or attempt == MAX_RETRIES:
            return ProofObject(
                answer=answer_str,
                normalized_value=value,
                unit=_dominant_unit(ledger),
                citations=[t.source_span for t in ledger.values()],
                trace=trace,
                dimension_checks=checks,
                verifier_verdict=verdict,
            )
        # verifier flagged — record and try a broader retrieve
        last_failure_reason = verdict

    # Exhausted retries without a clean answer.
    return ProofObject(
        answer=None,
        rejected=True,
        reason=last_failure_reason or "could not produce a verifiable answer",
        verifier_verdict="flagged" if last_failure_reason else "not_run",
    )


def _dominant_unit(ledger) -> str:
    units = [t.unit for t in ledger.values()]
    return units[0] if units else ""


def _format_answer(value: float, ledger, steps) -> str:
    if not steps:
        return f"{value:,.2f}"
    op = steps[-1].op
    if op == "pct_change":
        return f"{value:,.2f}%"
    if op in ("ratio",):
        return f"{value:,.4f}"
    unit = _dominant_unit(ledger)
    prefix = "$" if unit == "USD" else ""
    return f"{prefix}{value:,.2f}"


@app.post("/answer")
def answer(req: AskRequest):
    return run_proof_pipeline(req.question, req.doc_path).model_dump()


# --------------------------------------------------------------------------- #
#  Baseline: plain RAG — the control / "confidently wrong" side                #
# --------------------------------------------------------------------------- #
@app.post("/baseline")
def baseline(req: AskRequest):
    spans = retrieve(req.question, ingest(req.doc_path))
    spans_text = "\n".join(f"{sp.id}: {sp.text} [context: {sp.context}]" for sp in spans)
    out = llm_json(
        'Answer the question with a single JSON object: '
        '{"answer": <number as it appears in the doc, no normalization>, '
        '"citation": <one span id>}. Use the raw printed value.\n\n'
        f'Question: {req.question}\n\nSpans:\n{spans_text}'
    )
    return out if isinstance(out, dict) else {"answer": None, "citation": None}


@app.post("/spans")
def list_spans(req: AskRequest):
    """Convenience endpoint: show the retrieved spans (for the demo UI)."""
    return [sp.model_dump() for sp in retrieve(req.question, ingest(req.doc_path))]


@app.post("/all_spans")
def all_spans(req: AskRequest):
    """Return all spans in the document (the entire spreadsheet)."""
    return [sp.model_dump() for sp in ingest(req.doc_path)]


@app.post("/upload")
async def upload_file(request: Request, filename: str):
    """Securely upload a file to sample_data/ directory."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".xlsx", ".xlsm", ".csv", ".pdf"):
        return JSONResponse(
            status_code=400,
            content={"detail": f"Unsupported extension: {ext}"}
        )
    
    # Root confinement & directory creation
    if "VERCEL" in os.environ:
        target_dir = "/tmp"
    else:
        target_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_data")
    os.makedirs(target_dir, exist_ok=True)
    
    # Safe basename to prevent directory traversal
    safe_name = os.path.basename(filename)
    target_path = os.path.join(target_dir, safe_name)
    
    try:
        body = await request.body()
        with open(target_path, "wb") as f:
            f.write(body)
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"detail": f"Failed to save file: {str(e)}"}
        )
        
    res_path = f"/tmp/{safe_name}" if "VERCEL" in os.environ else f"sample_data/{safe_name}"
    return {"status": "ok", "filename": res_path}


@app.post("/stress_test")
def stress_test(req: StressTestRequest):
    traps = [
        {
            "id": "scale",
            "name": "Scale Trap",
            "description": "Baseline RAG ignores '$ in thousands' footnote; calculates raw number. Prover scales it correctly.",
            "question": "What was ACME's total revenue for FY2024 as reported under the 'in thousands' footnote?",
        },
        {
            "id": "period",
            "name": "Period Mismatch",
            "description": "Baseline RAG picks Q4-2024 instead of FY2024. Prover enforces strict period checking.",
            "question": "What was ACME's total revenue for Q4-2024?",
        },
        {
            "id": "entity",
            "name": "Entity Mismatch",
            "description": "Baseline RAG confuses Cloud Segment net income with Consolidated. Prover enforces entity checks.",
            "question": "What was ACME consolidated net income for FY2024 (not the Cloud Segment)?",
        },
        {
            "id": "flowstock",
            "name": "Flow + Stock Trap",
            "description": "LLM tries to sum Flow (Revenue) and Stock (Cash). Prover's Guard rejects the operation.",
            "question": "What is ACME's FY2024 total revenue plus cash on hand at year-end FY2024?",
        },
        {
            "id": "clean",
            "name": "Clean Margins",
            "description": "A clean ratio calculation. Both models should successfully resolve this.",
            "question": "What was ACME's FY2024 gross margin as a percentage?",
        }
    ]
    
    results = []
    for t in traps:
        req_ask = AskRequest(question=t["question"], doc_path=req.doc_path)
        
        # Run baseline
        try:
            base_res = baseline(req_ask)
        except Exception as e:
            base_res = {"answer": f"Error: {str(e)}", "citation": None}
            
        # Run proof
        try:
            proof_res = run_proof_pipeline(t["question"], req.doc_path)
            proof_dump = proof_res.model_dump()
        except Exception as e:
            proof_dump = {
                "answer": None,
                "rejected": True,
                "reason": str(e),
                "citations": [],
                "trace": [],
                "dimension_checks": [],
                "verifier_verdict": "error",
            }
            
        results.append({
            "id": t["id"],
            "name": t["name"],
            "description": t["description"],
            "question": t["question"],
            "baseline": base_res,
            "proof": proof_dump
        })
        
    return {"results": results}


# --------------------------------------------------------------------------- #
#  Demo page                                                                   #
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index():
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>static/index.html not found</h1>", status_code=404)


@app.get("/health")
def health():
    """Report status + which LLM provider/mode is active."""
    import llm as llmmod
    client = llmmod._get_client()
    if client is None:
        mode = "mock"
        provider = "none"
        model = "rule-based"
    else:
        # infer provider from base_url
        base = str(getattr(client, "base_url", "")).rstrip("/")
        if "mistral" in base:
            provider = "mistral"
        elif "openrouter" in base:
            provider = "openrouter"
        elif "openai.com" in base:
            provider = "openai"
        else:
            provider = base
        mode = "live"
        model = llmmod._model or "unknown"
    return {"status": "ok", "mode": mode, "provider": provider, "model": model}
