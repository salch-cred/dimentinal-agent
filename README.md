# Dimensional Proof-Carrying Answers

> **Proof-carrying financial QA. We don't make the model smarter — we make wrong answers unrepresentable.**

This is a grounded reasoning agent designed for the Sentient Arena **OfficeQA** challenge. 

Every answer produced by this agent goes through a deterministic verification pipeline:
1. **Source Span Verification**: Every number traces back to an exact cell location inside the Excel sheets or PDF documents.
2. **Dimensional Consistency**: Mismatched operations (e.g. adding flows and stocks, scale mismatch, period mismatch) are caught and rejected.
3. **Deterministic Computation**: Calculations are performed by a rule-based deterministic parser instead of relying on the LLM to do math.

## Project Structure

- `arena.yaml`: Agent configuration file defining the harness.
- `pipeline_cli.py`: The CLI entry point that runs the pipeline on questions.
- `pipeline.py`: Coordinates data retrieval, parsing, planning, and evaluation.
- `ingest.py`: Parses spreadsheets and PDFs into clean structural segments.
- `guard.py`: Implements the Dimensional Guard to prevent dimensional math errors.
- `llm.py`: Offline deterministic mock LLM helper that resolves queries cleanly.
- `schemas.py`: Common Pydantic data schemas.
- `requirements.txt`: Python package dependencies.
