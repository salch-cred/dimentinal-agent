# dimentinal-agent — Sentient Arena Challenge 0 (OfficeQA)

A harness-based grounded-reasoning agent for the OfficeQA benchmark (grounded
reasoning over the U.S. Treasury Bulletin corpus, 1939–2025, full-corpus mode).

The agent is customized **only** through the mechanisms the challenge allows:

- `arena.yaml` — selects the harness (`goose`) and points to the prompt template.
- `prompts/officeqa_prompt.j2` — the system prompt that drives corpus retrieval,
  dense-table reading, deterministic computation, and strict numeric output.

There is **no** custom pipeline, no external LLM/API calls, no web-app code, no
cached responses, no bundled data files, and no hard-coded answers. The agent
reasons over each task from the mounted corpus at run time, in the offline
evaluation sandbox, using the competition's own model.

## Structure
- `arena.yaml` — agent configuration
- `prompts/officeqa_prompt.j2` — Jinja2 prompt template
