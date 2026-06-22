"""Pydantic schemas for proof-carrying financial QA.

The whole point of this project: every number that enters the pipeline is a
*TYPED* EvidenceTuple. Numbers are never combined unless their dimensions
(unit / scale / period / entity / flow-vs-stock) are compatible. That makes
the most common financial-QA mistakes (thousands vs millions, Q4 vs full
year, segment vs consolidated, flow vs stock) *impossible to represent*,
rather than merely unlikely.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# What a single extracted financial figure looks like once it has been typed.
class EvidenceTuple(BaseModel):
    id: str
    value: float
    unit: str            # "USD", "shares", "ratio", "percent", "count"
    scale: float = 1.0   # 1, 1e3, 1e6 — resolved from headers like "$ in thousands"
    currency: Optional[str] = None
    entity: str          # "consolidated", "Cloud Segment", company name, ...
    period: str          # "FY2024", "Q4-2024", "as_of_2024-12-31"
    kind: Literal["flow", "stock", "rate"]  # flow=over a period, stock=point-in-time
    metric: str = ""     # human label, e.g. "total revenue", "cash and equivalents"
    source_span: str     # id back into the chunk store (page/cell) for citation
    raw_text: str        # exact text as it appeared in the document

    def base_value(self) -> float:
        """Value normalized to base units (absolute, unscaled)."""
        return self.value * self.scale


# One step of a deterministic compute plan. The LLM only emits these steps;
# it never emits the final digits.
class ComputeStep(BaseModel):
    op: Literal["add", "sub", "mul", "div", "ratio", "pct_change", "identity"]
    operands: List[str] = Field(..., description="ids of EvidenceTuples")


# The receipt that ships with every answer.
class ProofObject(BaseModel):
    answer: Optional[str] = None
    normalized_value: Optional[float] = None
    unit: Optional[str] = None
    citations: List[str] = Field(default_factory=list)
    trace: List[str] = Field(default_factory=list)
    dimension_checks: List[str] = Field(default_factory=list)
    verifier_verdict: str = "not_run"
    rejected: bool = False
    reason: Optional[str] = None


# A retrieved chunk of the document, with coordinates preserved for citation.
class Span(BaseModel):
    id: str
    text: str
    context: str = ""   # table title / column header / footnote ("$ in thousands")
    source: str = ""    # e.g. "page=3 table=1 row=2 col=Revenue" or "Sheet1!B4"
