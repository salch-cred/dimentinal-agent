"""The typed-extraction / plan / verify pipeline.

These three functions are the LLM-driven stages. Each one is kept tiny and
returns structured data that the deterministic core (guard.py) can reason
about. The LLM is never trusted with the final arithmetic.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List

from llm import llm_json
from schemas import ComputeStep, EvidenceTuple, Span


# --------------------------------------------------------------------------- #
#  Helpers for parsing LLM output into pydantic models                         #
# --------------------------------------------------------------------------- #
def _coerce_float(v) -> float:
    """Best-effort float coercion of UNTRUSTED LLM output.

    Never raises — a malformed value becomes 0.0 (the tuple is still dropped
    downstream if it lacks a real source_span). Catches the 'abc' / '1.2.3'
    cases that previously raised ValueError and 500'd the endpoint.
    """
    if isinstance(v, bool):  # bool is an int subclass; treat as junk
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace("$", "").replace(",", "").replace("%", "").strip()
        if s.lower() in ("", "-", "n/a", "—"):
            return 0.0
        try:
            return float(s)
        except ValueError:
            # take the first parseable number-looking token, else 0.0
            m = re.search(r"-?\d+(?:\.\d+)?", s)
            return float(m.group(0)) if m else 0.0
    return 0.0


def _period_from_context(text: str) -> str:
    """Best-effort period label from raw text/context."""
    m = re.search(r"\b(19|20)\d{2}\b", text)
    year = m.group(0) if m else "FY2024"
    if re.search(r"\bQ[1-4]\b", text, re.I):
        qm = re.search(r"\bQ([1-4])\b", text, re.I)
        return f"Q{qm.group(1)}-{year}"
    return f"FY{year}"


# --------------------------------------------------------------------------- #
#  Stage 3 — typed extraction                                                  #
# --------------------------------------------------------------------------- #
EXTRACT_PROMPT = """You extract financial figures as TYPED tuples. For each number \
relevant to the question, output a JSON object: {{"tuples": [ ... ]}} where each item \
matches this schema:
{{value, unit, scale, currency, entity, period, kind, metric, source_span, raw_text}}

Rules:
- value: the numeric value as printed (e.g. 1240).
- unit: one of "USD", "shares", "ratio", "percent", "count".
- scale: read headers/footnotes. "$ in thousands" => 1000; "$ in millions" => 1000000.
- currency: "USD" for dollar amounts, else null.
- kind: revenue/expense/cash-flow items = "flow"; balances/cash-on-hand/equity = \
"stock"; margins/rates/percentages = "rate".
- entity: "consolidated" unless a specific segment/subsidiary is named.
- period: exact fiscal label — "FY2024", "Q4-2024", or "as_of_YYYY-MM-DD".
- metric: a short human label, e.g. "total revenue".
- source_span: MUST be one of the span ids provided below.
- raw_text: the exact text of the figure as it appears in the span.
- If you cannot confidently determine unit/scale/period/entity for a number, \
DO NOT output that tuple.

Question: {q}

Spans (id: text  [context]):
{spans}
"""


def extract_ledger(question: str, spans: List[Span], feedback: str = None) -> Dict[str, EvidenceTuple]:
    """Ask the model to type every relevant figure; validate against schema."""
    valid_ids = {sp.id for sp in spans}
    spans_text = "\n".join(
        f"{sp.id}: {sp.text}  [context: {sp.context}]" for sp in spans
    )
    prompt = EXTRACT_PROMPT.format(q=question, spans=spans_text)
    if feedback:
        prompt += f"\n\nFeedback from previous attempt:\n{feedback}\nPlease correct the issues listed above."
    out = llm_json(prompt)
    tuples_raw = out.get("tuples", []) if isinstance(out, dict) else []

    ledger: Dict[str, EvidenceTuple] = {}
    for item in tuples_raw:
        if not isinstance(item, dict):
            continue
        src = item.get("source_span") or item.get("id")
        if src not in valid_ids:
            continue  # model hallucinated a span id — drop it
        # Backfill period from context if the model omitted it.
        period = (item.get("period") or "").strip()
        raw = item.get("raw_text", "")
        ctx = next((sp.context for sp in spans if sp.id == src), "")
        if not period:
            period = _period_from_context(f"{raw} {ctx}")
        try:
            t = EvidenceTuple(
                id=item.get("id") or f"ev_{len(ledger)}",
                value=_coerce_float(item.get("value")),
                unit=(item.get("unit") or "USD").strip(),
                scale=_coerce_float(item.get("scale")) if item.get("scale") not in (None, "") else 1.0,
                currency=(item.get("currency") or None),
                entity=(item.get("entity") or "consolidated").strip(),
                period=period,
                kind=item.get("kind") if item.get("kind") in ("flow", "stock", "rate") else "flow",
                metric=(item.get("metric") or raw or src),
                source_span=src,
                raw_text=raw,
            )
            ledger[t.id] = t
        except Exception:
            continue  # drop malformed tuple rather than crash
    return ledger


# --------------------------------------------------------------------------- #
#  Stage 4 — plan generation                                                   #
# --------------------------------------------------------------------------- #
PLAN_PROMPT = """Given the question and these typed tuple ids, output a JSON object: \
{{"steps": [ ... ]}} where each step is {{op, operands}}.
Use ONLY ops: add, sub, mul, div, ratio, pct_change, identity.

Rules:
- div, ratio, pct_change REQUIRE EXACTLY TWO operands. Never one. Never three.
- add/sub/mul may take 2+ operands.
- identity takes EXACTLY ONE operand (just return that value as-is).
- "growth", "change", "increase", "yoy", "year-over-year", "% change" ->
  use pct_change with the TWO same-metric tuples of different periods.
- "margin", "as a percentage of" -> ratio with two operands.
- A simple lookup ("what was X") -> identity on the single most-relevant tuple.
- Reference tuples ONLY by their id from the list below. Do NOT compute the
  number yourself — a calculator will.

Question: {q}

Tuples (id: metric = value [unit, period, entity, kind]):
{ledger_summary}
"""


def plan(question: str, ledger: Dict[str, EvidenceTuple], feedback: str = None) -> List[ComputeStep]:
    if not ledger:
        return []
    summary = "\n".join(
        f"{tid}: {t.metric} = {t.value} "
        f"[{t.unit}, {t.period}, {t.entity}, {t.kind}]"
        for tid, t in ledger.items()
    )
    prompt = PLAN_PROMPT.format(q=question, ledger_summary=summary)
    if feedback:
        prompt += f"\n\nFeedback from previous attempt:\n{feedback}\nPlease correct the issues listed above."
    out = llm_json(prompt)
    steps_raw = out.get("steps", []) if isinstance(out, dict) else []
    steps: List[ComputeStep] = []
    for s in steps_raw:
        if not isinstance(s, dict):
            continue
        op = s.get("op")
        operands = s.get("operands", [])
        if op not in ("add", "sub", "mul", "div", "ratio", "pct_change", "identity"):
            continue
        if not isinstance(operands, list) or not operands:
            continue
        operands = [str(o) for o in operands]
        # Keep only operands that reference real tuples; if none survive, skip.
        operands = [o for o in operands if o in ledger]
        if not operands:
            continue
        # Enforce operand count per op.
        if op in ("div", "ratio", "pct_change") and len(operands) != 2:
            continue  # malformed — will trigger the repair fallback below
        if op == "identity" and len(operands) != 1:
            operands = operands[:1]
        try:
            steps.append(ComputeStep(op=op, operands=operands))
        except Exception:
            continue

    # ---- Plan repair fallback ----
    # If the model produced no usable step (or only malformed ones) AND the
    # question is clearly a growth/change query, synthesize a pct_change over
    # the two same-metric tuples of different periods. This is deterministic
    # and rescues a live model that emitted a one-operand div.
    q_lower = question.lower()
    is_growth = any(k in q_lower for k in
                    ("growth", "change", "increase", "yoy", "year-over-year",
                     "grew", "decline", "decrease"))
    has_binary_op = any(s.op in ("pct_change", "div", "ratio", "sub") for s in steps)
    if is_growth and not has_binary_op:
        pair = _find_same_metric_period_pair(ledger)
        if pair:
            try:
                steps = [ComputeStep(op="pct_change", operands=pair)]
            except Exception:
                pass

    # ---- Plan repair: "as a percentage of" / "% of" queries ----
    # If the question asks for "X as a percentage of Y" and the plan is
    # malformed (identity, or ratio with same operand yielding 1.0), find
    # the two distinct metrics and build a proper ratio.
    is_pct_of = any(k in q_lower for k in
                    ("as a percentage of", "% of", "as a % of",
                     "spending as a percentage", "as a share of"))
    if is_pct_of and len(ledger) >= 2:
        # Check if current plan is degenerate (identity or same-operand ratio)
        needs_repair = not has_binary_op
        if not needs_repair:
            for s in steps:
                if s.op in ("ratio", "div") and len(set(s.operands)) < 2:
                    needs_repair = True
                    break
        if needs_repair:
            pair = _find_different_metric_pair(ledger, q_lower)
            if pair:
                try:
                    steps = [ComputeStep(op="ratio", operands=pair)]
                except Exception:
                    pass

    return steps


def _find_same_metric_period_pair(ledger: Dict[str, EvidenceTuple]) -> List[str]:
    """Find two tuples with the same metric but different periods (for pct_change)."""
    by_metric: Dict[str, List[str]] = {}
    for tid, t in ledger.items():
        key = (t.metric or "").strip().lower()
        by_metric.setdefault(key, []).append(tid)
    for key, ids in by_metric.items():
        if len(ids) >= 2:
            periods = {ledger[i].period for i in ids}
            if len(periods) >= 2:
                return ids[:2]
    return []


def _find_different_metric_pair(ledger: Dict[str, EvidenceTuple], q_lower: str) -> List[str]:
    """Find numerator and denominator tuples for 'X as a percentage of Y' queries.

    Heuristic: the denominator is the metric mentioned after 'of' in the question
    (e.g. 'total revenue' in 'R&D as a percentage of total revenue').
    The numerator is the other metric.
    """
    # Try to find 'of <denominator>' pattern
    import re as _re
    denom_match = _re.search(r"(?:percentage|%)\s+of\s+(.+?)(?:\?|$)", q_lower)
    denom_hint = denom_match.group(1).strip() if denom_match else ""

    # Score each tuple against the denominator hint
    tids = list(ledger.keys())
    if len(tids) < 2:
        return []

    # Find the best denominator match
    best_denom = None
    best_denom_score = -1
    best_numer = None
    best_numer_score = -1

    denom_tokens = set(denom_hint.split()) if denom_hint else set()

    for tid in tids:
        t = ledger[tid]
        metric_lower = (t.metric or "").lower()
        metric_tokens = set(metric_lower.split())

        if denom_tokens:
            overlap = len(denom_tokens & metric_tokens)
            if overlap > best_denom_score:
                best_denom_score = overlap
                best_denom = tid

    # The numerator is the tuple that is NOT the denominator
    if best_denom is not None:
        for tid in tids:
            if tid != best_denom:
                t = ledger[tid]
                # Same period as denominator preferred
                if ledger[tid].period == ledger[best_denom].period:
                    best_numer = tid
                    break
        if best_numer is None:
            best_numer = [t for t in tids if t != best_denom][0]
        return [best_numer, best_denom]

    # Fallback: just return first two different-metric tuples
    return tids[:2]


# --------------------------------------------------------------------------- #
#  Stage 3 & 4 combined — extraction and planning in a single call             #
# --------------------------------------------------------------------------- #
EXTRACT_AND_PLAN_PROMPT = """You extract financial figures as TYPED tuples and output a mathematical calculation plan to resolve the user's question.

Your output MUST be a JSON object matching this schema:
{{
  "reasoning": "A step-by-step reasoning trace of how to locate, type, and calculate the answer.",
  "tuples": [
    {{
      "id": "t1",
      "value": 1240.0,
      "unit": "USD",
      "scale": 1000.0,
      "currency": "USD",
      "entity": "consolidated",
      "period": "FY2024",
      "kind": "flow",
      "metric": "total revenue",
      "source_span": "span_id",
      "raw_text": "1,240"
    }}
  ],
  "steps": [
    {{
      "op": "pct_change",
      "operands": ["t1", "t2"]
    }}
  ]
}}

Tuple Rules:
- id: a unique identifier for the tuple (e.g. t1, t2, t3).
- value: the numeric value as printed (e.g. 1240).
- unit: one of "USD", "shares", "ratio", "percent", "count".
- scale: read headers/footnotes. "$ in thousands" => 1000; "$ in millions" => 1000000.
- currency: "USD" for dollar amounts, else null.
- kind: revenue/expense/cash-flow items = "flow"; balances/cash-on-hand/equity = "stock"; margins/rates/percentages = "rate".
- entity: "consolidated" unless a specific segment/subsidiary is named.
- period: exact fiscal label — "FY2024", "Q4-2024", or "as_of_YYYY-MM-DD".
- metric: a short human label, e.g. "total revenue".
- source_span: MUST be one of the span ids provided below.
- raw_text: the exact text of the figure as it appears in the span.
- If you cannot confidently determine unit/scale/period/entity for a number, DO NOT output that tuple.

Plan Rules:
- steps: a list of ComputeSteps representing the calculation. The last step must compute the final answer.
- Use ONLY ops: add, sub, mul, div, ratio, pct_change, identity.
- div, ratio, pct_change REQUIRE EXACTLY TWO operands.
- add/sub/mul may take 2+ operands.
- identity takes EXACTLY ONE operand (just returns that value as-is, e.g. for simple lookups).
- Reference tuples ONLY by their "id" from the "tuples" list. Do NOT compute the final arithmetic digits yourself.

Example 1 (Calculation query):
Question: What was the YoY growth rate of Cloud Segment revenue in FY2024?
Spans:
s1: Cloud Segment Revenue (FY2023): 100 [context: sheet=Segment scale=thousands]
s2: Cloud Segment Revenue (FY2024): 150 [context: sheet=Segment scale=thousands]
Output:
{{
  "reasoning": "Identify Cloud Segment revenue spans: s1 for FY2023 (100k) and s2 for FY2024 (150k). Both are segment flows with unit USD and scale 1000. We will calculate YoY growth rate using the pct_change operator over these two periods.",
  "tuples": [
    {{
      "id": "t1",
      "value": 100.0,
      "unit": "USD",
      "scale": 1000.0,
      "currency": "USD",
      "entity": "Cloud Segment",
      "period": "FY2023",
      "kind": "flow",
      "metric": "Cloud Segment Revenue",
      "source_span": "s1",
      "raw_text": "100"
    }},
    {{
      "id": "t2",
      "value": 150.0,
      "unit": "USD",
      "scale": 1000.0,
      "currency": "USD",
      "entity": "Cloud Segment",
      "period": "FY2024",
      "kind": "flow",
      "metric": "Cloud Segment Revenue",
      "source_span": "s2",
      "raw_text": "150"
    }}
  ],
  "steps": [
    {{
      "op": "pct_change",
      "operands": ["t1", "t2"]
    }}
  ]
}}

Example 2 (Lookup query):
Question: What was the Cash on hand in FY2024?
Spans:
s3: Cash and cash equivalents (as_of_2024-12-31): 8000 [context: sheet=Balance Sheet scale=thousands]
Output:
{{
  "reasoning": "Locate point-in-time cash balance for FY2024 on the Balance Sheet. Span s3 contains the cash value (8000k) as of 2024-12-31. This is a consolidated stock metric with unit USD and scale 1000. Perform a simple lookup via identity op.",
  "tuples": [
    {{
      "id": "t3",
      "value": 8000.0,
      "unit": "USD",
      "scale": 1000.0,
      "currency": "USD",
      "entity": "consolidated",
      "period": "as_of_2024-12-31",
      "kind": "stock",
      "metric": "Cash and cash equivalents",
      "source_span": "s3",
      "raw_text": "8,000"
    }}
  ],
  "steps": [
    {{
      "op": "identity",
      "operands": ["t3"]
    }}
  ]
}}

Question: {q}

Spans (id: text  [context]):
{spans}
"""


def extract_and_plan(question: str, spans: List[Span], feedback: str = None) -> tuple[Dict[str, EvidenceTuple], List[ComputeStep]]:
    valid_ids = {sp.id for sp in spans}
    spans_text = "\n".join(
        f"{sp.id}: {sp.text}  [context: {sp.context}]" for sp in spans
    )
    prompt = EXTRACT_AND_PLAN_PROMPT.format(q=question, spans=spans_text)
    if feedback:
        prompt += f"\n\nFeedback from previous attempt:\n{feedback}\nPlease correct the issues listed above."
    
    out = llm_json(prompt)
    if not isinstance(out, dict):
        out = {}
        
    tuples_raw = out.get("tuples", []) if isinstance(out, dict) else []
    
    ledger: Dict[str, EvidenceTuple] = {}
    for item in tuples_raw:
        if not isinstance(item, dict):
            continue
        src = item.get("source_span") or item.get("id")
        if src not in valid_ids:
            continue
        period = (item.get("period") or "").strip()
        raw = item.get("raw_text", "")
        ctx = next((sp.context for sp in spans if sp.id == src), "")
        if not period:
            period = _period_from_context(f"{raw} {ctx}")
        try:
            t = EvidenceTuple(
                id=item.get("id") or f"ev_{len(ledger)}",
                value=_coerce_float(item.get("value")),
                unit=(item.get("unit") or "USD").strip(),
                scale=_coerce_float(item.get("scale")) if item.get("scale") not in (None, "") else 1.0,
                currency=(item.get("currency") or None),
                entity=(item.get("entity") or "consolidated").strip(),
                period=period,
                kind=item.get("kind") if item.get("kind") in ("flow", "stock", "rate") else "flow",
                metric=(item.get("metric") or raw or src),
                source_span=src,
                raw_text=raw,
            )
            ledger[t.id] = t
        except Exception:
            continue
            
    steps_raw = out.get("steps", []) if isinstance(out, dict) else []
    steps: List[ComputeStep] = []
    for s in steps_raw:
        if not isinstance(s, dict):
            continue
        op = s.get("op")
        operands = s.get("operands", [])
        if op not in ("add", "sub", "mul", "div", "ratio", "pct_change", "identity"):
            continue
        if not isinstance(operands, list) or not operands:
            continue
        operands = [str(o) for o in operands]
        operands = [o for o in operands if o in ledger]
        if not operands:
            continue
        if op in ("div", "ratio", "pct_change") and len(operands) != 2:
            continue
        if op == "identity" and len(operands) != 1:
            operands = operands[:1]
        try:
            steps.append(ComputeStep(op=op, operands=operands))
        except Exception:
            continue
            
    # Apply standard fallbacks if steps are empty or degenerate
    q_lower = question.lower()
    is_growth = any(k in q_lower for k in
                    ("growth", "change", "increase", "yoy", "year-over-year",
                     "grew", "decline", "decrease"))
    has_binary_op = any(s.op in ("pct_change", "div", "ratio", "sub") for s in steps)
    if is_growth and not has_binary_op:
        pair = _find_same_metric_period_pair(ledger)
        if pair:
            try:
                steps = [ComputeStep(op="pct_change", operands=pair)]
            except Exception:
                pass

    is_pct_of = any(k in q_lower for k in
                    ("as a percentage of", "% of", "as a % of",
                     "spending as a percentage", "as a share of"))
    if is_pct_of and len(ledger) >= 2:
        needs_repair = not has_binary_op
        if not needs_repair:
            for s in steps:
                if s.op in ("ratio", "div") and len(set(s.operands)) < 2:
                    needs_repair = True
                    break
        if needs_repair:
            pair = _find_different_metric_pair(ledger, q_lower)
            if pair:
                try:
                    steps = [ComputeStep(op="ratio", operands=pair)]
                except Exception:
                    pass

    return ledger, steps


# --------------------------------------------------------------------------- #
#  Stage 7 — adversarial verifier                                              #
# --------------------------------------------------------------------------- #
# IMPORTANT: the verifier must NOT re-derive scale or redo arithmetic. Scale
# normalization and the actual math are done DETERMINISTICALLY by guard.py
# (normalize + execute_plan) and are guaranteed correct. A live LLM verifier
# that tries to redo that math hallucinates scale errors (a real bug seen with
# Mistral, which claimed a scaled number was "1000x too large"). So the
# verifier is given the ALREADY-NORMALIZED absolute values and is told the
# arithmetic is authoritative — it may only check citation support and
# dimensional consistency.
VERIFY_PROMPT = """You are a skeptical auditor checking CITATION SUPPORT and DIMENSIONAL CONSISTENCY.
Return JSON: {{"reason": "Audit justification checking constraints (a), (b), and (c).", "verdict": "survived" | "broken"}}.

The arithmetic and unit/scale normalization are ALREADY DONE by a deterministic
calculator and are AUTHORITATIVE — do NOT recompute them, do NOT flag the
magnitude of the answer, do NOT claim the scale is wrong. The absolute values
below are correct by construction.

You may ONLY flag these specific problems:
(a) The cited span does not actually contain the figure claimed (wrong cell,
    fabricated citation, span text doesn't match the metric).
(b) The WRONG metric/entity/period was chosen for the question (e.g. question
    asks for consolidated but a segment figure was used; question asks FY2024
    but a Q4 figure was used; question asks for revenue but cost was used).
(c) A relevant contradicting figure exists that the answer ignored AND that
    changes which number is correct.

If none of (a), (b), (c) apply, you MUST return "survived". Do not flag
formatting, decimal places, missing unit labels, or scale — those are handled.

CRITICAL INSTRUCTION FOR AUDITOR ACCURACY:
- Be EXTREMELY CONSERVATIVE in flagging an answer as "broken".
- ONLY return "broken" if you find an explicit, clear, and indisputable error under points (a), (b), or (c).
- If there is any doubt, or if the differences are due to rounding, formatting, or minor metric wording variations, you MUST return "survived".

Question: {q}
Answer being audited: {answer}

Tuples used (absolute normalized values, already scaled correctly):
{tuples}

Cited spans (id: text [context]):
{spans}
"""


def verify(
    question: str,
    answer: str,
    ledger: Dict[str, EvidenceTuple],
    spans: List[Span],
) -> str:
    if not ledger or answer in (None, "", "None"):
        return "flagged: no answer produced"
    # Only show spans that were actually cited, to keep the verifier focused.
    cited_ids = {t.source_span for t in ledger.values()}
    cited_spans = [sp for sp in spans if sp.id in cited_ids] or spans
    spans_text = "\n".join(f"{sp.id}: {sp.text} [{sp.context}]" for sp in cited_spans)
    tuples_text = "\n".join(
        f"- {t.metric}: {t.value} x{t.scale:g} = {t.value * t.scale:g} absolute "
        f"({t.unit}, {t.period}, {t.entity}, {t.kind}) "
        f"from {t.source_span}"
        for t in ledger.values()
    )
    out = llm_json(VERIFY_PROMPT.format(
        q=question, answer=answer, tuples=tuples_text, spans=spans_text,
    ))
    if not isinstance(out, dict):
        return "flagged: verifier returned no verdict"
    verdict = (out.get("verdict") or "").strip().lower()
    reason = (out.get("reason") or "").strip()
    if verdict == "survived":
        return "survived"
    return f"flagged: {reason}" if reason else "flagged"
