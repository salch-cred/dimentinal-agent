"""Evaluation & ablation harness.

Loads eval.jsonl rows of the form {"q", "doc", "gold"} and runs BOTH the
proof-carrying pipeline (/answer) and the plain-RAG baseline (/baseline)
on each. Prints accuracy for both plus how many dimensional traps the
Guard caught — that delta is the headline result.
"""
from __future__ import annotations

import json
import os
from typing import Any

from app import AskRequest, answer, baseline
from guard import DimensionError, check_plan, execute_plan
from pipeline import extract_ledger, plan


def load_eval(path: str = "eval.jsonl") -> list[dict]:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _to_float(s: Any) -> float | None:
    if isinstance(s, (int, float)):
        return float(s)
    if not isinstance(s, str):
        return None
    cleaned = s.replace("$", "").replace(",", "").replace("%", "").strip()
    # take the first number-looking token
    for tok in cleaned.split():
        try:
            return float(tok)
        except ValueError:
            continue
    return None


def match(pred: Any, gold: Any, tol: float = 0.02) -> bool:
    """Numeric match within tol (default 2%), else case-insensitive string match."""
    pf = _to_float(pred)
    gf = _to_float(gold)
    if pf is not None and gf is not None:
        if gf == 0:
            return abs(pf) < 1e-9
        return abs(pf - gf) <= tol * abs(gf)
    return str(pred).strip().lower() == str(gold).strip().lower()


def run_eval(path: str = "eval.jsonl") -> dict:
    rows = load_eval(path)
    n = len(rows)
    ours_correct = base_correct = traps_caught = 0
    details: list[dict] = []

    for r in rows:
        req = AskRequest(question=r["q"], doc_path=r["doc"])
        ours = answer(req)
        try:
            base = baseline(req)
        except Exception as e:
            base = {"answer": None, "citation": None, "_error": str(e)}

        gold = r.get("gold")
        ours_ans = ours.get("answer")
        base_ans = base.get("answer") if isinstance(base, dict) else None

        is_trap = bool(r.get("trap", False))

        if ours.get("rejected"):
            # A rejected trap counts as a success (we caught the impossibility).
            if is_trap:
                traps_caught += 1
                ours_correct += 1
            else:
                # rejecting a non-trap question is a miss
                ours_ok = False
        else:
            ours_ok = match(ours_ans, gold)
            if ours_ok:
                ours_correct += 1

        if ours.get("rejected"):
            ours_ok = is_trap  # handled above, keep var sane
        base_ok = match(base_ans, gold)

        if base_ok:
            base_correct += 1

        details.append({
            "q": r["q"],
            "gold": gold,
            "ours": ours_ans,
            "baseline": base_ans,
            "ours_rejected": bool(ours.get("rejected")),
            "reject_reason": ours.get("reason"),
            "ours_ok": ours_ok if not ours.get("rejected") else (is_trap),
            "base_ok": base_ok,
            "trap": is_trap,
            "verifier": ours.get("verifier_verdict"),
        })

    summary = {
        "n": n,
        "ours_correct": ours_correct,
        "baseline_correct": base_correct,
        "ours_accuracy": ours_correct / n if n else 0.0,
        "baseline_accuracy": base_correct / n if n else 0.0,
        "traps_caught": traps_caught,
        "delta_pp": (ours_correct - base_correct) / n * 100 if n else 0.0,
        "details": details,
    }
    return summary


def print_summary(s: dict) -> None:
    print("=" * 64)
    print("ABLATION — Proof-Carrying vs. Plain RAG Baseline")
    print("=" * 64)
    print(f"Questions:                         {s['n']}")
    print(f"Ours (proof-carrying) correct:     {s['ours_correct']}/{s['n']} "
          f"= {s['ours_accuracy']:.0%}")
    print(f"Baseline (plain RAG) correct:      {s['baseline_correct']}/{s['n']} "
          f"= {s['baseline_accuracy']:.0%}")
    print(f"Dimensional traps caught by Guard: {s['traps_caught']}")
    print(f"Accuracy delta:                    {s['delta_pp']:+.1f} percentage points")
    print("-" * 64)
    print("Per-question detail:")
    for d in s["details"]:
        flag = "TRAP" if d["trap"] else "    "
        rej = " [REJECTED: " + (d["reject_reason"] or "") + "]" if d["ours_rejected"] else ""
        print(f"  {flag} Q: {d['q'][:70]}")
        print(f"        gold={d['gold']}  ours={d['ours']}{rej}  base={d['baseline']}")
        print(f"        ours_ok={d['ours_ok']}  base_ok={d['base_ok']}  "
              f"verifier={d['verifier']}")
    print("=" * 64)


def sys_eval_path() -> str:
    """Path to eval.jsonl relative to this file."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval.jsonl")


if __name__ == "__main__":
    path = sys_eval_path()
    s = run_eval(path)
    print_summary(s)
