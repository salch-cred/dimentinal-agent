"""Proof-Carrying QA CLI.

Standalone entry point for the proof-carrying pipeline. Usable inside the
Arena codex harness sandbox without any web framework or external API deps.
"""
import argparse
import sys
import json
from typing import List

from guard import DimensionError, check_plan, execute_plan
from ingest import ingest, retrieve
from llm import llm_json
from pipeline import extract_and_plan, verify
from schemas import ProofObject, Span

MAX_RETRIES = 2


def _dominant_unit(ledger) -> str:
    units = [t.unit for t in ledger.values()]
    return units[0] if units else ""


def _format_answer(value: float, ledger, steps, question: str = "") -> str:
    if not steps:
        return f"{value:,.2f}"

    q_lower = question.lower()
    if "as reported" in q_lower or "as printed" in q_lower or "reported under" in q_lower:
        tups = list(ledger.values())
        if tups:
            raw_val = value / tups[0].scale
            return f"{raw_val:,.0f}"

    op = steps[-1].op
    if op == "pct_change":
        return f"{value:,.2f}%"
    if op in ("ratio",):
        q_lower = question.lower()
        is_pct_query = any(k in q_lower for k in
                          ("as a percentage", "% of", "as a %", "percentage of"))
        if is_pct_query:
            return f"{value * 100:,.2f}"
        return f"{value:,.4f}"
    unit = _dominant_unit(ledger)
    prefix = "$" if unit == "USD" else ""
    return f"{prefix}{value:,.2f}"


def run_proof_pipeline(question: str, doc_path: str) -> ProofObject:
    """Full pipeline with up to MAX_RETRIES extract-and-plan loops."""
    last_failure_reason = None
    for attempt in range(MAX_RETRIES + 1):
        all_spans: List[Span] = ingest(doc_path)
        spans = retrieve(question, all_spans)

        ledger, steps = extract_and_plan(question, spans, feedback=last_failure_reason)
        if not ledger:
            last_failure_reason = "no typed evidence could be extracted"
            continue

        if not steps:
            last_failure_reason = "no compute plan produced"
            continue

        # --- Flow+stock pre-check ---
        q_lower = question.lower()
        asks_combine = any(w in q_lower for w in ("plus", "combined", "add", "sum of", "together"))
        if asks_combine and ledger:
            kinds_in_ledger = {t.kind for t in ledger.values()}
            if "flow" in kinds_in_ledger and "stock" in kinds_in_ledger:
                flow_t = next(t for t in ledger.values() if t.kind == "flow")
                stock_t = next(t for t in ledger.values() if t.kind == "stock")
                return ProofObject(
                    answer=None, rejected=True,
                    reason=(
                        f"period mismatch: {flow_t.period!r} vs {stock_t.period!r} "
                        f"(cannot add {flow_t.metric!r} and {stock_t.metric!r})"
                    ),
                    citations=[t.source_span for t in ledger.values()],
                    trace=[], dimension_checks=[],
                    verifier_verdict="not_run",
                )
            _FLOW_KEYWORDS = {"revenue", "income", "profit", "expense", "operating",
                              "cash flow", "earnings", "sales", "cost"}
            _STOCK_KEYWORDS = {"assets", "liabilities", "equity", "cash on hand",
                               "debt", "balance", "goodwill", "receivable"}
            has_flow_word = any(w in q_lower for w in _FLOW_KEYWORDS)
            has_stock_word = any(w in q_lower for w in _STOCK_KEYWORDS)
            if has_flow_word and has_stock_word:
                return ProofObject(
                    answer=None, rejected=True,
                    reason=(
                        "cannot combine a period flow metric with a point-in-time "
                        "balance metric (flow + stock is dimensionally invalid)"
                    ),
                    citations=[t.source_span for t in ledger.values()],
                    trace=[], dimension_checks=[],
                    verifier_verdict="not_run",
                )

        try:
            checks = check_plan(steps, ledger)
            value, trace = execute_plan(steps, ledger)
        except DimensionError as e:
            last_failure_reason = f"DimensionError: {str(e)} during checking/execution of the plan. Please select correct cells or verify period/scale/entity labels."
            if attempt == MAX_RETRIES:
                return ProofObject(
                    answer=None, rejected=True, reason=str(e),
                    citations=[t.source_span for t in ledger.values()],
                    trace=[], dimension_checks=[],
                    verifier_verdict="not_run",
                )
            continue

        answer_str = _format_answer(value, ledger, steps, question)
        is_simple = len(steps) == 1 and steps[0].op == "identity"
        if is_simple:
            verdict = "survived"
        else:
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
        last_failure_reason = f"Auditor check failed: {verdict}"

    return ProofObject(
        answer=None,
        rejected=True,
        reason=last_failure_reason or "could not produce a verifiable answer",
        verifier_verdict="flagged" if last_failure_reason else "not_run",
    )


def main():
    parser = argparse.ArgumentParser(description="Proof-Carrying QA CLI")
    parser.add_argument("--question", required=True, type=str, help="Question to answer")
    parser.add_argument("--doc_path", required=True, type=str, help="Path to document")
    args = parser.parse_args()

    try:
        proof = run_proof_pipeline(args.question, args.doc_path)
        if proof.rejected:
            print(json.dumps({"rejected": True, "reason": proof.reason}))
            sys.exit(0)
        else:
            print(json.dumps({
                "rejected": False,
                "answer": proof.answer,
                "value": proof.normalized_value,
                "unit": proof.unit,
                "citations": proof.citations
            }))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)

if __name__ == "__main__":
    main()
