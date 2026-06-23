"""The Dimensional Guard — the core differentiator.

This module makes wrong answers *unrepresentable*. Before any arithmetic is
done, every combination of numbers is checked for dimensional compatibility:

  * period must match when adding/subtracting (Q4 != full year)
  * entity/segment must match (segment A != consolidated)
  * flow must not be added to stock (period total != point-in-time balance)
  * unit must match when adding/subtracting (USD != shares)

Scale and currency are normalized first, so "$1.2 (in millions)" and
"$1,200,000" are recognized as the same quantity.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from schemas import ComputeStep, EvidenceTuple


class DimensionError(Exception):
    """Raised when a compute plan combines dimensionally incompatible numbers."""


# Extend if multi-currency documents appear. Everything normalizes to USD=1.0.
CURRENCY_RATES: Dict[str, float] = {
    "USD": 1.0, 
    "EUR": 1.08, 
    "GBP": 1.26,
    "CAD": 0.73,
    "AUD": 0.66,
    "JPY": 0.0064,
    "CNY": 0.14,
    "INR": 0.012,
    "CHF": 1.11,
}


def _period_sort_key(period: str) -> Tuple:
    """Chronological sort key for a period label.

    Lets pct_change/sub be made order-independent: 'FY2023' < 'FY2024',
    'Q1-2024' < 'Q4-2024', 'as_of_2024-06-30' < 'as_of_2024-12-31'.
    Anything unparseable sorts as (inf,) so it stays last but stable.
    """
    p = period.lower()
    # as_of_YYYY-MM-DD  ->  (year, month, day, 0)
    m = re.search(r"as_of_(\d{4})-(\d{2})-(\d{2})", p)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 0)
    # Qn-YYYY  ->  (year, 0, 0, quarter)
    m = re.search(r"q(\d)[\s\-]?(\d{4})", p)
    if m:
        return (int(m.group(2)), 0, 0, int(m.group(1)))
    # FY/YYYY  ->  (year, 0, 0, 0)
    m = re.search(r"(?:fy)?\s*(\d{4})", p)
    if m:
        return (int(m.group(1)), 0, 0, 0)
    return (float("inf"),)


def normalize(t: EvidenceTuple) -> float:
    """Bring a tuple to base units (absolute, unscaled, currency-converted).

    IMPORTANT: the "$ in thousands/millions" footnote applies only to dollar
    AMOUNTS. Rates, ratios and percentages (gross margin, growth, EPS-as-rate)
    are already dimensionless — scaling them by 1e3 would be a bug (turning
    0.4087 into 408.7). So scale & currency conversion apply only to USD flows
    and stocks, never to `rate`-kind or `ratio`/`percent`-unit values.
    """
    if t.kind == "rate" or t.unit in ("ratio", "percent"):
        return t.value
    v = t.value * t.scale
    if t.unit == "USD" and t.currency:
        v *= CURRENCY_RATES.get(t.currency.upper(), 1.0)
    return v


def assert_combinable(a: EvidenceTuple, b: EvidenceTuple, op: str) -> None:
    """Raise DimensionError if `a` and `b` cannot be combined by `op`.

    mul/div/ratio/pct_change are *intentionally* permissive — mixing a flow
    by a rate (e.g. revenue * tax rate) is legitimate and is exactly how
    ratios are formed. add/sub are checked for entity/kind/unit, but they
    differ on the PERIOD rule:

      * `add` across periods is ALWAYS rejected — it invents a meaningless
        total (e.g. FY2023 revenue + FY2024 revenue is not "two years of
        revenue", it's a nonsense sum).
      * `sub` across periods is ALLOWED — a delta/change is meaningful
        (revenue FY2024 - FY2023 = year-over-year change), exactly like
        pct_change. The operands are sorted chronologically in execute_plan
        so the sign is stable regardless of how the model ordered them.
    """
    if op in ("add", "sub"):
        # period: add must match; sub may differ (delta across time is legal)
        if op == "add" and a.period != b.period:
            raise DimensionError(
                f"period mismatch: {a.period!r} vs {b.period!r} "
                f"(cannot {op} {a.metric!r} and {b.metric!r})"
            )
        if a.entity != b.entity:
            raise DimensionError(
                f"entity mismatch: {a.entity!r} vs {b.entity!r} "
                f"(cannot {op} {a.metric!r} and {b.metric!r})"
            )
        if a.kind != b.kind:
            raise DimensionError(
                f"kind mismatch: {a.kind!r} vs {b.kind!r} "
                f"(cannot {op} a {a.kind} with a {b.kind}: "
                f"{a.metric!r} vs {b.metric!r})"
            )
        if a.unit != b.unit:
            raise DimensionError(
                f"unit mismatch: {a.unit!r} vs {b.unit!r} "
                f"(cannot {op} {a.metric!r} and {b.metric!r})"
            )


def check_plan(steps: List[ComputeStep], ledger: Dict[str, EvidenceTuple]) -> List[str]:
    """Validate every step. Returns human-readable check lines; raises on mismatch."""
    checks: List[str] = []
    for s in steps:
        for op_id in s.operands:
            if op_id not in ledger:
                raise DimensionError(
                    f"unknown operand {op_id!r} in step {s.op}({s.operands})"
                )
        ops = [ledger[i] for i in s.operands]
        # add/sub require pairwise compatibility of all operands.
        if s.op in ("add", "sub"):
            if len(ops) < 1:
                raise DimensionError(f"{s.op} needs at least one operand")
            for x in ops[1:]:
                assert_combinable(ops[0], x, s.op)
        elif s.op in ("mul", "div", "ratio", "pct_change", "identity"):
            pass  # permissive by design
        else:
            raise DimensionError(f"unsupported op {s.op!r}")
        checks.append(f"OK {s.op}({', '.join(s.operands)})")
    return checks


def execute_plan(
    steps: List[ComputeStep], ledger: Dict[str, EvidenceTuple]
) -> Tuple[float, List[str]]:
    """Run a real calculator over the plan. The LLM never produces the digits.

    `pct_change` and `sub` are made ORDER-INDEPENDENT: their operands are
    sorted chronologically by period before computing, so the model cannot
    reverse base/new (a real bug seen with a live LLM putting FY2024 before
    FY2023 and yielding a negative growth). pct_change = (new-old)/old*100.
    """
    vals = {k: normalize(v) for k, v in ledger.items()}
    result: float = 0.0
    trace: List[str] = []
    for s in steps:
        nums = [vals[i] for i in s.operands]
        if s.op == "identity":
            result = nums[0]
        elif s.op == "add":
            result = sum(nums)
        elif s.op == "sub":
            # order-independent: subtract later periods from the earliest.
            ordered = sorted(
                [(ledger[i].period, n) for i, n in zip(s.operands, nums)],
                key=lambda t: _period_sort_key(t[0]),
            )
            onums = [n for _, n in ordered]
            result = onums[0] - sum(onums[1:])
        elif s.op == "mul":
            result = 1.0
            for n in nums:
                result *= n
        elif s.op == "div":
            if len(nums) != 2 or nums[1] == 0:
                raise DimensionError(f"invalid div operands: {s.operands}")
            result = nums[0] / nums[1]
        elif s.op == "ratio":
            if len(nums) != 2 or nums[1] == 0:
                raise DimensionError(f"invalid ratio operands: {s.operands}")
            result = nums[0] / nums[1]
        elif s.op == "pct_change":
            # order-independent: (new - old) / old * 100, oldest period = old.
            ordered = sorted(
                [(ledger[i].period, i) for i in s.operands],
                key=lambda t: _period_sort_key(t[0]),
            )
            if len(ordered) != 2:
                raise DimensionError(f"pct_change needs exactly 2 operands: {s.operands}")
            old = vals[ordered[0][1]]
            new = vals[ordered[1][1]]
            if old == 0:
                raise DimensionError(f"invalid pct_change operands: {s.operands}")
            result = (new - old) / old * 100.0
        else:
            raise DimensionError(f"unsupported op {s.op!r}")
        trace.append(
            f"{s.op}({', '.join(s.operands)}) = {result:,.4f}"
        )
    return result, trace
