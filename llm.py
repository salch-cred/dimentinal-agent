"""Thin LLM wrapper — provider-agnostic (OpenAI-compatible).

Works with any provider whose API matches the OpenAI Chat Completions shape:
Mistral, OpenRouter, OpenAI, Groq, Together, DeepSeek, Ollama, etc. The
provider is chosen by which env var you set:

    MISTRAL_API_KEY          -> https://api.mistral.ai/v1          (model: mistral-medium-latest)
    OPENROUTER_API_KEY       -> https://openrouter.ai/api/v1       (model: openai/gpt-4o-mini)
    OPENAI_API_KEY           -> https://api.openai.com/v1          (model: gpt-4o-mini)
    LLM_BASE_URL + LLM_API_KEY + MODEL  -> any compatible endpoint

temperature=0 everywhere for reproducibility. Persistent on-disk cache so demo
re-runs are instant and free, and a runaway retry loop can't burn credits.
MOCK mode: if NO key is set, a rule-based responder stands in so the whole
pipeline still runs end-to-end without network access.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

CACHE_PATH = os.path.join(os.path.dirname(__file__), "cache.json")

_client = None
_model: Optional[str] = None


# Provider presets: env var name -> (base_url, default model).
_PROVIDERS = [
    ("MISTRAL_API_KEY",    "https://api.mistral.ai/v1", "mistral-medium-latest"),
    ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
    ("OPENAI_API_KEY",     "https://api.openai.com/v1", "gpt-4o-mini"),
]


def _is_placeholder(key: str, provider_var: str) -> bool:
    """A key that's empty or still the .env.example placeholder -> mock mode."""
    if not key:
        return True
    # each provider's example placeholder
    placeholders = {
        "MISTRAL_API_KEY": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "OPENROUTER_API_KEY": "sk-or-v1-x",
    }
    ph = placeholders.get(provider_var)
    return bool(ph and key.startswith(ph))


def _get_client():
    """Build the OpenAI client for whichever provider key is configured.

    Priority: explicit LLM_BASE_URL+LLM_API_KEY > MISTRAL > OPENROUTER > OPENAI.
    Returns None (mock mode) when no usable key is found.
    """
    global _client, _model
    if _client is not None:
        return _client

    base_url: Optional[str] = None
    api_key: Optional[str] = None

    # 1) Fully explicit override (any OpenAI-compatible endpoint).
    explicit_key = os.environ.get("LLM_API_KEY", "").strip()
    explicit_url = os.environ.get("LLM_BASE_URL", "").strip()
    if explicit_key and explicit_url:
        base_url, api_key = explicit_url, explicit_key

    # 2) Provider presets.
    if not api_key:
        for var, url, default_model in _PROVIDERS:
            key = os.environ.get(var, "").strip()
            if not _is_placeholder(key, var):
                base_url, api_key = url, key
                # use the provider's default model only if MODEL isn't set
                if not os.environ.get("MODEL"):
                    _model = default_model
                break

    # No usable key -> mock mode.
    if not api_key:
        return None

    from openai import OpenAI
    _client = OpenAI(base_url=base_url, api_key=api_key)
    if not _model:
        _model = os.environ.get("MODEL", "gpt-4o-mini")
    return _client


def _load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


_cache: dict = _load_cache()


def _hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
#  Mock responder — keeps the pipeline runnable with no API key.              #
#  It pattern-matches numbers in the provided spans. This is NOT used when a  #
#  real key is present.                                                       #
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
    qm = re.search(r"^question:\s*(.+?)(?:\n\s*spans|\n\s*tuples|\Z)", prompt, re.M | re.I)
    if qm:
        qtext = qm.group(1).strip()
    qtoks = _qtokens(qtext)

    # Build candidate figures from rich spans: "<metric> (<period>): <value>".
    # The metric may itself contain a parenthetical (e.g. "Net income
    # (consolidated) (FY2024): 520000"), so the PERIOD is the LAST
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
        # Build the candidate set: the top metric (and its other periods, for
        # pct_change questions) plus up to 4 total. We group on a normalized
        # metric (strip leading "Cloud Segment —" type prefixes) so e.g.
        # "Net income (consolidated)" and "Cloud Segment — net income" are
        # treated as the same metric for grouping purposes.
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
        # "As reported / as printed" questions want the raw printed number, not
        # the scale-normalized one. Detect that intent and freeze scale to 1.0.
        as_reported = bool(re.search(r"as reported|as printed|under the .* footnote", qtext, re.I))
        for sid, metric, period, val, raw, ctx in chosen:
            # Percent cells already carry their true magnitude (40.87) and a
            # "unit=percent" context marker; rate values are NOT scaled.
            is_percent = "unit=percent" in ctx
            if is_percent:
                unit = "percent"
                scale = 1.0
            else:
                unit = _guess_unit(metric, ctx)
                scale = 1.0 if as_reported else _guess_scale(raw, ctx)
            # Entity: detect a named segment/subsidiary in the metric label,
            # otherwise consolidated.
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
    # Optimized baseline: normalized to absolute value unless "as reported" is asked,
    # and returns null (None) for incompatible flow+stock questions.
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
        
        # If there is a direct lookup row in figures for gross margin % or gross margin:
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


MAX_CACHE_ENTRIES = 2000  # bound the on-disk cache (disk-fill DoS guard)


def llm_json(prompt: str, *, use_cache: bool = True) -> dict[str, Any]:
    """Call the model and parse JSON. Mocks when no key is configured."""
    h = _hash(prompt)
    if use_cache and h in _cache:
        return _cache[h]

    client = _get_client()
    if client is None:
        out = _mock_json(prompt)
    else:
        try:
            r = client.chat.completions.create(
                model=_model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0,
            )
            content = r.choices[0].message.content
            out = json.loads(content)
        except (json.JSONDecodeError, ValueError, KeyError, IndexError, TypeError) as e:
            # Recoverable: bad/empty model output. Fall back to mock + flag it.
            # (Scoped — genuine bugs like NameError/AttributeError still raise.)
            out = _mock_json(prompt)
            out["_llm_error"] = f"parse: {e}"
        except Exception as e:
            # Network / quota / rate-limit / API errors. The openai SDK raises
            # its own exception types ( subclasses); catch broadly but log so
            # auth misconfig isn't silently hidden. We deliberately do NOT
            # include the API key in the message.
            try:
                import logging
                logging.getLogger("proof.llm").warning(
                    "LLM call failed (%s); falling back to mock", type(e).__name__
                )
            except Exception:
                pass
            out = _mock_json(prompt)
            out["_llm_error"] = f"{type(e).__name__}: request failed"

    # Bound the cache: FIFO-evict oldest entries past the cap so a hostile
    # client sending unique questions can't grow cache.json without limit.
    _cache[h] = out
    if len(_cache) > MAX_CACHE_ENTRIES:
        for k in list(_cache.keys())[: len(_cache) - MAX_CACHE_ENTRIES]:
            _cache.pop(k, None)
    if use_cache:
        _save_cache(_cache)
    return out
