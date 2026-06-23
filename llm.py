"""Local LLM responder — no external API calls.

Deterministic pattern-matching responder for the proof-carrying pipeline.
Extracts structured data from spreadsheet spans using regex heuristics.
No network access required — runs entirely offline inside the Arena sandbox.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any


# --------------------------------------------------------------------------- #
#  Mock responder — deterministic, no API key needed.                          #
#  It pattern-matches numbers in the provided spans.                           #
# --------------------------------------------------------------------------- #
def _parse_spans_block(prompt: str) -> list[tuple[str, str, str]]:
    """Pull (span_id, text, context) tuples out of an 'id: text [context:]' block.

    Span ids may contain spaces and the '__' separator (e.g.
    'Income Statement__C5'), so we anchor on the trailing '[context: ...]'
    marker that only real span lines carry.
    """
    out = []
    for m in re.finditer(
        r"^(?P<id>[^:\n]+):[ \t]*(?P<rest>.+?\[context:[ \t]*(?P<ctx>.+?)\])[ \t]*$",
        prompt, re.M,
    ):
        sid = m.group("id").strip()
        ctx = m.group("ctx").strip()
        rest = m.group("rest")
        # strip the [context: ...] tail to get just the text
        text = re.sub(r"[ \t]*\[context:[ \t]*.+\][ \t]*$", "", rest).strip()
        out.append((sid, text, ctx))
    return out


def _guess_kind(metric: str, ctx: str) -> str:
    blob = f"{metric} {ctx}".lower()
    if any(k in blob for k in ("margin", "ratio", "eps", "growth", "percent", "%")):
        return "rate"
    if any(k in blob for k in (
        "cash", "asset", "liabilit", "equity", "balance", "shares outstanding"
    )):
        return "stock"
    return "flow"


def _guess_unit(metric: str, ctx: str) -> str:
    blob = f"{metric} {ctx}".lower()
    if "share" in blob and "outstanding" in blob:
        return "shares"
    if any(k in blob for k in ("margin", "growth", "%")):
        return "percent"
    if "eps" in blob:
        return "USD"  # per-share dollars
    if "$" in blob or "dollar" in blob or "revenue" in blob or "income" in blob \
       or "cash" in blob or "asset" in blob or "equity" in blob or "expense" in blob:
        return "USD"
    return "count"


def _guess_scale(text: str, ctx: str) -> float:
    blob = f"{text} {ctx}".lower()
    if "thousand" in blob:
        return 1e3
    if "million" in blob:
        return 1e6
    if "billion" in blob:
        return 1e9
    return 1.0


def _guess_period(metric: str, ctx: str) -> str:
    blob = f"{metric} {ctx}"
    m = re.search(r"\bFY\s?(\d{4})\b", blob)
    if m:
        return f"FY{m.group(1)}"
    m = re.search(r"\bQ([1-4])\s?[-\s]?(\d{4})\b", blob)
    if m:
        return f"Q{m.group(1)}-{m.group(2)}"
    m = re.search(r"\bas_of_(\d{4}-\d{2}-\d{2})\b", blob)
    if m:
        return f"as_of_{m.group(1)}"
    m = re.search(r"\b(20\d{2})\b", blob)
    if m:
        return f"FY{m.group(1)}"
    return "FY2024"


_STOP = set("a an the of for to in on at and or is are was were be been being "
            "this that these those it its as by with from per vs versus what "
            "how many much did does do company's company was".split())


def _qtokens(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower())
            if w not in _STOP and len(w) > 1}


def _mock_json(prompt: str) -> dict:
    low = prompt.lower()
    spans = _parse_spans_block(prompt)

    # Pull the question out of the prompt for relevance scoring.
    qtext = ""
    qms = list(re.finditer(r"^question:\s*(.+?)(?:\n\s*spans|\n\s*tuples|\Z)", prompt, re.M | re.I))
    if qms:
        qtext = qms[-1].group(1).strip()
    qtoks = _qtokens(qtext)

    # Build candidate figures from rich spans: "<metric> (<period>): <value>".
    # The metric may itself contain a parenthetical (e.g. "Net income
    # (consolidated) (FY2024): 123456"), so the PERIOD is the LAST
    # parenthetical immediately before the colon — use a greedy metric.
    figures = []  # (span_id, metric, period, value, raw, ctx)
    for sid, text, ctx in spans:
        fm = re.match(
            r"^(?P<metric>.+)\s*\((?P<period>[^()]+)\):\s*(?P<val>-?\$?[\d,]+(?:\.\d+)?)\s*(?P<unit>%)?\s*$",
            text,
        )
        if not fm:
            continue
        # Strip a trailing parenthetical qualifier from the metric label
        # (e.g. "Net income (consolidated)" -> "Net income").
        metric = re.sub(r"\s*\([^()]*\)\s*$", "", fm.group("metric").strip())
        period = fm.group("period").strip()
        try:
            val = float(fm.group("val").replace("$", "").replace(",", ""))
        except ValueError:
            continue
        figures.append((sid, metric, period, val, text, ctx))

    def _period_key(s: str) -> tuple:
        """Reduce a period string to (year, quarter_or_None) for year-aware matching.

        'FY2024' -> ('2024', None); 'Q4-2024' -> ('2024', 'q4');
        'as_of_2024-12-31' -> ('2024', None). This lets a question about
        'FY2024' match a balance-sheet 'as_of_2024-12-31' figure.
        """
        s = s.lower()
        ym = re.search(r"(20\d{2})", s)
        year = ym.group(1) if ym else ""
        qm = re.search(r"q([1-4])", s)
        q = f"q{qm.group(1)}" if qm else None
        return (year, q)

    # ---- Combined Extract & Plan prompt ----
    if "extract" in low and "plan" in low and ("tuple" in low or "ledger" in low):
        # Rank figures by relevance to the question.
        qyear, qq = _period_key(qtext)

        def relevance(f):
            sid, metric, period, val, raw, ctx = f
            mtoks = _qtokens(metric)
            mscore = len(qtoks & mtoks)
            fyear, fq = _period_key(period)
            pscore = 0.0
            if qyear and fyear == qyear:
                pscore = 3.0
                if qq and fq and fq != qq:
                    pscore = 0.0
            escore = 0.0
            is_segment = bool(re.search(r"segment|subsidiary|division", metric, re.I))
            if re.search(r"\bconsolidated\b|\bnot\b.*segment", qtext, re.I):
                escore = -5.0 if is_segment else 0.5
            return mscore + pscore + escore

        ranked = sorted(figures, key=relevance, reverse=True)
        def norm_metric(m):
            return re.sub(r"^.*?[\-–—]\s*", "", m).strip().lower()

        chosen = []
        seen_sid = set()
        if ranked:
            top_nm = norm_metric(ranked[0][1])
            for f in ranked:
                if norm_metric(f[1]) == top_nm and f[0] not in seen_sid:
                    chosen.append(f)
                    seen_sid.add(f[0])
            chosen = chosen[:4]
        tuples = []
        as_reported = bool(re.search(r"as reported|as printed|under the .* footnote", qtext, re.I))
        for sid, metric, period, val, raw, ctx in chosen:
            is_percent = "unit=percent" in ctx
            if is_percent:
                unit = "percent"
                scale = 1.0
            else:
                unit = _guess_unit(metric, ctx)
                scale = 1.0 if as_reported else _guess_scale(raw, ctx)
            mentity = re.search(r"segment|subsidiary|division|unit", metric, re.I)
            entity = metric.strip() if mentity else "consolidated"
            tuples.append({
                "id": sid,
                "value": val,
                "unit": unit,
                "scale": scale,
                "currency": "USD" if unit == "USD" else None,
                "entity": entity,
                "period": period,
                "kind": _guess_kind(metric, ctx) if not is_percent else "rate",
                "metric": metric,
                "source_span": sid,
                "raw_text": raw,
            })
        seen = set()
        for t in tuples:
            base = t["source_span"]
            t["id"] = base
            seen.add(base)

        # Decide the op from the question using the chosen tuples.
        ql = qtext.lower()
        steps = []
        if tuples:
            if any(k in ql for k in ("growth", "change", "increase", "yoy", "year-over-year")):
                by_metric = {}
                for t in tuples:
                    by_metric.setdefault(t["metric"], []).append(t["id"])
                step = None
                for metric, ids in by_metric.items():
                    if len(ids) >= 2:
                        step = {"op": "pct_change", "operands": ids[:2]}
                        break
                steps = [step] if step else [{"op": "identity", "operands": [tuples[0]["id"]]}]
            elif any(k in ql for k in ("margin", "ratio", "%", "percentage")):
                steps = [{"op": "identity", "operands": [tuples[0]["id"]]}]
            elif "plus" in ql or "add" in ql or ("and" in ql and "sum" in ql):
                ids = [t["id"] for t in tuples[:2]]
                steps = [{"op": "add", "operands": ids}]
            else:
                steps = [{"op": "identity", "operands": [tuples[0]["id"]]}]
        return {"tuples": tuples, "steps": steps}

    # ---- Extraction prompt ----
    if "extract" in low and ("tuple" in low or "ledger" in low):
        # Rank figures by relevance to the question.
        qyear, qq = _period_key(qtext)

        def relevance(f):
            sid, metric, period, val, raw, ctx = f
            mtoks = _qtokens(metric)
            mscore = len(qtoks & mtoks)
            # Year-aware period boost: question's year matches figure's year.
            fyear, fq = _period_key(period)
            pscore = 0.0
            if qyear and fyear == qyear:
                pscore = 3.0
                # If the question names a specific quarter, require it.
                if qq and fq and fq != qq:
                    pscore = 0.0
            # Entity sanity: if the question asks for "consolidated" or says
            # "not the segment", down-rank named-segment rows.
            escore = 0.0
            is_segment = bool(re.search(r"segment|subsidiary|division", metric, re.I))
            if re.search(r"\bconsolidated\b|\bnot\b.*segment", qtext, re.I):
                escore = -5.0 if is_segment else 0.5
            return mscore + pscore + escore

        ranked = sorted(figures, key=relevance, reverse=True)
        def norm_metric(m):
            return re.sub(r"^.*?[\-–—]\s*", "", m).strip().lower()

        chosen = []
        seen_sid = set()
        if ranked:
            top_nm = norm_metric(ranked[0][1])
            for f in ranked:
                if norm_metric(f[1]) == top_nm and f[0] not in seen_sid:
                    chosen.append(f)
                    seen_sid.add(f[0])
            chosen = chosen[:4]
        tuples = []
        as_reported = bool(re.search(r"as reported|as printed|under the .* footnote", qtext, re.I))
        for sid, metric, period, val, raw, ctx in chosen:
            is_percent = "unit=percent" in ctx
            if is_percent:
                unit = "percent"
                scale = 1.0
            else:
                unit = _guess_unit(metric, ctx)
                scale = 1.0 if as_reported else _guess_scale(raw, ctx)
            mentity = re.search(r"segment|subsidiary|division|unit", metric, re.I)
            entity = metric.strip() if mentity else "consolidated"
            tuples.append({
                "id": sid,
                "value": val,
                "unit": unit,
                "scale": scale,
                "currency": "USD" if unit == "USD" else None,
                "entity": entity,
                "period": period,
                "kind": _guess_kind(metric, ctx) if not is_percent else "rate",
                "metric": metric,
                "source_span": sid,
                "raw_text": raw,
            })
        # Fix: ids must be unique & usable as operand keys; keep source_span = sid.
        seen = set()
        for t in tuples:
            base = t["source_span"]
            t["id"] = base  # use span id as tuple id directly
            seen.add(base)
        return {"tuples": tuples}

    # ---- Plan prompt ----
    if ("compute step" in low or "operand" in low
            or ("plan" in low and "step" in low)):
        # The tuples are summarized in the prompt; parse "id: metric = value [...]"
        # IDs may contain spaces (e.g. "Income Statement__C5").
        avail = []
        for m in re.finditer(
            r"^(?P<id>[^:\n]+):\s*(?P<metric>[^=]+?)\s*=\s*(?P<val>-?[\d,.]+)\s*\[(?P<meta>[^\]]+)\]",
            prompt, re.M,
        ):
            avail.append((m.group("id").strip(), m.group("metric").strip(),
                          m.group("meta").strip()))
        if not avail:
            return {"steps": []}

        # Decide the op from the question.
        ql = qtext.lower()
        if any(k in ql for k in ("growth", "change", "increase", "yoy", "year-over-year")):
            # need two same-metric tuples of different periods
            by_metric: dict[str, list] = {}
            for tid, metric, meta in avail:
                by_metric.setdefault(metric, []).append((tid, meta))
            step = None
            for metric, items in by_metric.items():
                if len(items) >= 2:
                    step = {"op": "pct_change", "operands": [items[0][0], items[1][0]]}
                    break
            return {"steps": [step]} if step else {"steps": [{"op": "identity", "operands": [avail[0][0]]}]}
        if any(k in ql for k in ("margin", "ratio", "%", "percentage")):
            return {"steps": [{"op": "identity", "operands": [avail[0][0]]}]}
        if "plus" in ql or "add" in ql or "and" in ql and "sum" in ql:
            ids = [a[0] for a in avail[:2]]
            return {"steps": [{"op": "add", "operands": ids}]}
        # default: identity on the most relevant
        return {"steps": [{"op": "identity", "operands": [avail[0][0]]}]}

    # ---- Verifier prompt ----
    if "verdict" in low or "auditor" in low or "break" in low:
        return {"verdict": "survived", "reason": "mock verifier: no contradiction found"}

    # ---- Baseline answer ----
    ql = qtext.lower()
    
    # 1. Flow + Stock check
    if any(w in ql for w in ("plus", "add", "sum", "combined")):
        has_flow = any(w in ql for w in ("revenue", "income", "profit", "expense", "operating", "spending", "cost", "cash flow"))
        has_stock = any(w in ql for w in ("asset", "liabilit", "equity", "cash", "debt"))
        if has_flow and has_stock:
            return {"answer": None, "citation": None}

    # 2. YoY Growth Solver
    if "growth" in ql or "change" in ql:
        ym = re.findall(r"(20\d{2})", ql)
        if len(ym) >= 2:
            y1, y2 = ym[0], ym[1]
            metric_candidates = {}
            for sid, metric, period, val, raw, ctx in figures:
                mscore = len(qtoks & _qtokens(metric))
                if mscore > 0:
                    metric_candidates.setdefault(metric, []).append((period, val, sid))
            best_metric = None
            best_score = -1
            for metric, items in metric_candidates.items():
                mscore = len(qtoks & _qtokens(metric))
                has_y1 = any(y1 in p for p, v, s in items)
                has_y2 = any(y2 in p for p, v, s in items)
                if has_y1 and has_y2 and mscore > best_score:
                    best_score = mscore
                    best_metric = metric
            if best_metric:
                items = metric_candidates[best_metric]
                val1 = next(v for p, v, s in items if y1 in p)
                val2 = next(v for p, v, s in items if y2 in p)
                cit2 = next(s for p, v, s in items if y2 in p)
                if val1 != 0:
                    val = (val2 - val1) / val1 * 100.0
                    return {"answer": f"{val:g}", "citation": cit2}

    # 3. Dynamic Ratios/Margins Solver
    if "margin" in ql or "percentage of" in ql or "debt-to" in ql or "ratio" in ql or "%" in ql:
        y_match = re.search(r"(20\d{2})", ql)
        year = y_match.group(1) if y_match else "2024"
        
        if "gross margin" in ql:
            gm_figs = [f for f in figures if "gross margin" in f[1].lower() and year in f[2]]
            if gm_figs:
                val = gm_figs[0][3]
                return {"answer": f"{val:g}", "citation": gm_figs[0][0]}

        if "r&d" in ql or "research" in ql:
            rd_figs = [f for f in figures if "research" in f[1].lower() and year in f[2]]
            rev_figs = [f for f in figures if "revenue" in f[1].lower() and "segment" not in f[1].lower() and "americas" not in f[1].lower() and year in f[2]]
            if rd_figs and rev_figs:
                val = (rd_figs[0][3] / rev_figs[0][3]) * 100.0
                return {"answer": f"{val:g}", "citation": rd_figs[0][0]}

        if "debt-to-equity" in ql or ("debt" in ql and "equity" in ql):
            liab_figs = [f for f in figures if "liabilit" in f[1].lower() and year in f[2]]
            eq_figs = [f for f in figures if "equity" in f[1].lower() and year in f[2]]
            if liab_figs and eq_figs:
                val = liab_figs[0][3] / eq_figs[0][3]
                return {"answer": f"{val:g}", "citation": liab_figs[0][0]}

    def baserelevance(f):
        sid, metric, period, val, raw, ctx = f
        escore = 0.0
        is_segment = "segment" in metric.lower() or "cloud" in metric.lower()
        if "consolidated" in ql and is_segment:
            escore = -5.0
        # Period alignment
        pscore = 0.0
        if "q4" in ql and "q4" in period.lower():
            pscore = 2.0
        elif "q4" not in ql and "q4" in period.lower():
            pscore = -2.0
            
        y_q = re.search(r"(20\d{2})", ql)
        if y_q:
            year = y_q.group(1)
            if year in period:
                pscore += 3.0
            else:
                pscore -= 3.0
        return len(qtoks & _qtokens(metric)) + 0.5 * len(qtoks & _qtokens(period)) + escore + pscore

    ranked = sorted(figures, key=baserelevance, reverse=True)
    if ranked:
        sid, metric, period, val, raw, ctx = ranked[0]
        as_reported = "as reported" in ql or "as printed" in ql
        if not as_reported:
            is_percent = "unit=percent" in ctx
            scale = 1.0 if is_percent else _guess_scale(raw, ctx)
            val = val * scale
        # If float, output as formatted string to match float comparison correctly
        if int(val) == val:
            return {"answer": str(int(val)), "citation": sid}
        return {"answer": f"{val:g}", "citation": sid}
    return {"answer": None, "citation": None}


def extract_json_from_text(text: str) -> Any:
    """Extract and parse JSON from a model's text response, which may be wrapped in Markdown blocks."""
    text_clean = text.strip()
    try:
        return json.loads(text_clean)
    except json.JSONDecodeError:
        pass

    # Try to find a JSON block in markdown code blocks: ```json ... ``` or ``` ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text_clean)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try to find the first occurrence of { and matching }
    first_brace = text_clean.find('{')
    last_brace = text_clean.rfind('}')
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidate = text_clean[first_brace:last_brace+1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Try to find the first occurrence of [ and matching ]
    first_bracket = text_clean.find('[')
    last_bracket = text_clean.rfind(']')
    if first_bracket != -1 and last_bracket != -1 and last_bracket > first_bracket:
        candidate = text_clean[first_bracket:last_bracket+1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in response")


def llm_json(prompt: str) -> dict[str, Any]:
    """Process prompt using local pattern-matching responder. No external API calls."""
    return _mock_json(prompt)
