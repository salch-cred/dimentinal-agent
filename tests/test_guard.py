"""Unit tests for the Dimensional Guard and deterministic compute.

Run with:  python -m pytest tests/test_guard.py   (or)   python tests/test_guard.py

These tests pin down the core guarantee of the project: numbers with mismatched
dimensions (period / entity / flow-vs-stock / unit) CANNOT be combined, while
legitimate operations (identity, ratio, pct_change) always succeed. Scale and
currency are normalized first; rates/percentages are never scaled.
"""
import os
import sys

# Make the package importable when running this file directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import ComputeStep, EvidenceTuple  # noqa: E402
from guard import (  # noqa: E402
    DimensionError, check_plan, execute_plan, normalize,
)


def _t(tid, value, unit="USD", scale=1.0, currency="USD",
       entity="consolidated", period="FY2024", kind="flow",
       metric="revenue", span="s1") -> EvidenceTuple:
    return EvidenceTuple(
        id=tid, value=value, unit=unit, scale=scale, currency=currency,
        entity=entity, period=period, kind=kind, metric=metric,
        source_span=span, raw_text=metric,
    )


def _run(ledger, ops):
    steps = [ComputeStep(op=o, operands=opnd) for o, opnd in ops]
    checks = check_plan(steps, ledger)
    val, trace = execute_plan(steps, ledger)
    return val, checks


def _expect_reject(ledger, ops):
    steps = [ComputeStep(op=o, operands=opnd) for o, opnd in ops]
    try:
        check_plan(steps, ledger)
    except DimensionError:
        return  # expected
    raise AssertionError(f"expected DimensionError for {ops}")


def _expect_ok(ledger, ops):
    steps = [ComputeStep(op=o, operands=opnd) for o, opnd in ops]
    check_plan(steps, ledger)  # raises if not ok


# --------------------------------------------------------------------------- #
#  Normalize                                                                  #
# --------------------------------------------------------------------------- #
def test_normalize_usd_flow_scales():
    t = _t("a", 4820000, scale=1000.0)
    assert normalize(t) == 4_820_000_000.0


def test_normalize_rate_never_scaled():
    """A gross margin stored as 40.87 percent must NOT be scaled, even when the
    sheet footnote says '$ in thousands'. This was a real bug — scaling a rate
    turned 0.4087 into 408.7."""
    t = _t("m", 40.87, unit="percent", scale=1000.0, kind="rate", metric="margin")
    assert normalize(t) == 40.87


def test_normalize_shares():
    t = _t("sh", 418, unit="shares", scale=1e6, currency=None, kind="stock", metric="shares")
    assert normalize(t) == 418_000_000.0


# --------------------------------------------------------------------------- #
#  Dimensional mismatches that MUST be rejected                               #
# --------------------------------------------------------------------------- #
def test_reject_period_mismatch_add():
    led = {"a": _t("a", 100, period="FY2024"), "b": _t("b", 90, period="FY2023")}
    _expect_reject(led, [("add", ["a", "b"])])


def test_reject_period_mismatch_fy_vs_q():
    led = {"a": _t("a", 100, period="FY2024"), "b": _t("b", 25, period="Q4-2024")}
    _expect_reject(led, [("add", ["a", "b"])])


def test_reject_entity_mismatch():
    led = {"a": _t("a", 100, entity="consolidated"),
           "b": _t("b", 90, entity="Cloud Segment", metric="seg")}
    _expect_reject(led, [("add", ["a", "b"])])


def test_reject_flow_plus_stock():
    led = {"a": _t("a", 100, kind="flow", metric="revenue"),
           "b": _t("b", 50, kind="stock", metric="cash")}
    _expect_reject(led, [("add", ["a", "b"])])


def test_reject_unit_mismatch():
    led = {"a": _t("a", 100, unit="USD", metric="revenue"),
           "b": _t("b", 418, unit="shares", kind="stock", metric="shares")}
    _expect_reject(led, [("add", ["a", "b"])])


# --------------------------------------------------------------------------- #
#  Legitimate operations that MUST succeed                                    #
# --------------------------------------------------------------------------- #
def test_ok_identity():
    led = {"a": _t("a", 4820000, scale=1000.0)}
    val, _ = _run(led, [("identity", ["a"])])
    assert val == 4_820_000_000.0


def test_ok_pct_change_across_periods():
    led = {
        "a": _t("a", 4510000, scale=1000.0, period="FY2023"),
        "b": _t("b", 4820000, scale=1000.0, period="FY2024"),
    }
    val, _ = _run(led, [("pct_change", ["a", "b"])])
    assert abs(val - 6.8736) < 1e-3


def test_pct_change_order_independent():
    """Bug 1 regression: the LLM may emit pct_change operands in either order
    (new,old) or (old,new). The result must be the SAME positive growth either
    way — operands are sorted chronologically before computing. Without this
    fix, a live model that put FY2024 first returned -6.43% instead of +6.87%.
    """
    led = {
        "old": _t("old", 4510000, scale=1000.0, period="FY2023"),
        "new": _t("new", 4820000, scale=1000.0, period="FY2024"),
    }
    val_fwd, _ = _run(led, [("pct_change", ["old", "new"])])
    val_rev, _ = _run(led, [("pct_change", ["new", "old"])])
    assert abs(val_fwd - 6.8736) < 1e-3, f"forward order wrong: {val_fwd}"
    assert abs(val_rev - 6.8736) < 1e-3, f"reversed order wrong: {val_rev}"
    assert val_fwd == val_rev, "pct_change must be order-independent"


def test_sub_order_independent():
    """sub is allowed ACROSS PERIODS (a delta/change is meaningful, unlike add
    which invents a fake total), and is order-independent: operands are sorted
    chronologically so the model can't flip the sign. FY2024(4.51B) - FY2023
    (4.82B) = -0.31B either way."""
    led = {
        "old": _t("old", 4820000, scale=1000.0, period="FY2023", metric="rev"),
        "new": _t("new", 4510000, scale=1000.0, period="FY2024", metric="rev"),
    }
    val_fwd, _ = _run(led, [("sub", ["old", "new"])])
    val_rev, _ = _run(led, [("sub", ["new", "old"])])
    # earliest (FY2023: 4.82B) minus later (FY2024: 4.51B) = +0.31B, either order
    assert abs(val_fwd - val_rev) < 1e-6, "cross-period sub must be order-independent"
    assert abs(val_fwd - 310_000_000.0) < 1.0, f"sub value wrong: {val_fwd}"


def test_add_still_rejects_cross_period():
    """add across periods stays REJECTED — that's the difference from sub.
    Summing FY2023 + FY2024 revenue invents a meaningless total."""
    led = {
        "a": _t("a", 4820000, scale=1000.0, period="FY2023", metric="rev"),
        "b": _t("b", 4510000, scale=1000.0, period="FY2024", metric="rev"),
    }
    _expect_reject(led, [("add", ["a", "b"])])


def test_ok_ratio_mixed_dimensions():
    """ratio (revenue / shares) mixes a USD flow with shares — legitimate,
    because ratios are how you form per-share metrics. Must NOT be rejected."""
    led = {
        "a": _t("a", 4820000, scale=1000.0, kind="flow", metric="revenue"),
        "b": _t("b", 418, unit="shares", scale=1e6, currency=None,
                kind="stock", metric="shares"),
    }
    _expect_ok(led, [("ratio", ["a", "b"])])
    val, _ = _run(led, [("ratio", ["a", "b"])])
    assert abs(val - 11.5311) < 1e-3


def test_ok_sub_same_period_entity():
    led = {
        "a": _t("a", 4820000, scale=1000.0, metric="revenue"),
        "b": _t("b", 2850000, scale=1000.0, metric="cost"),
    }
    val, _ = _run(led, [("sub", ["a", "b"])])
    assert val == 1_970_000_000.0


def test_reject_unknown_operand():
    led = {"a": _t("a", 100)}
    _expect_reject(led, [("identity", ["nope"])])


# --------------------------------------------------------------------------- #
#  Runner so `python tests/test_guard.py` works without pytest                #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {fn.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(fns)} total")
    sys.exit(1 if failed else 0)
