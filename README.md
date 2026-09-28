# SVC-C2-085 — BroadcastStandardsReviewAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: SVC

## Overview

BroadcastStandardsReviewAgent reads a programme outline, a shooting script or a sponsor-integration
proposal and drafts the review points a broadcast standards desk should work through — against the
broadcaster's own criteria, pharmaceutical-advertising rules and undisclosed-endorsement
disclosure requirements — with the evidence for each point and the desk's own past rulings where
comparable ones exist.

**It does not clear anything for broadcast.** The output is a working draft for a reviewer, and the
template is built so it cannot be mistaken for a ruling:

- **Assertive phrasing is blocked.** The draft raises points to check; it does not declare a script
  compliant or non-compliant.
- **Every point carries its criterion and its evidence.** `evidence_map` and `cited_source_ids` tie
  each issue back to the criterion record and the past ruling it came from, and a cited id that is
  not among the records actually retrieved does not survive the output gate.
- **Past rulings are recalled within one desk only** — the same broadcaster, the same review desk,
  the same rule axis. A ruling made by one broadcaster is not precedent for another.
- **Sponsor and product names are tokenised on the way in** and stay tokenised through the graph,
  the checkpoint and the store. They are resolved back to real names only in the response copy,
  and only for a caller who is verified, in the matching scope, *and* holds the
  `view_sponsor_names` permission. A caller without it gets a working draft with reference ids
  instead — not an error.
- **A reviewer's decision comes back into the loop.** `/resume` returns approve / correct / reject
  on a suspended review, and `/feedback` records a confirmed ruling into the desk's own scoped,
  redacted ruling store, where it becomes precedent for later reviews.

Rules are configuration, not code: `config/review_criteria.yaml` holds the criteria the desk
applies, versioned, so a criterion change is a config change with an auditable version rather than
a code change.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime;
- your own `config/review_criteria.yaml`. The shipped file is an example of the shape, not a legal
  rule set — the criteria a desk applies are the desk's own, and their correctness is its
  responsibility;
- authentication middleware that establishes the caller identity, stamps the verified
  `broadcaster_id` and `review_desk_id`, and carries the caller's permissions. Those scope values
  are the tenant boundary and are never read from the request body;
- a review store. `wire_services()` falls back to SQLite at `:memory:`, which means rulings are
  lost when the process exits; pass `database_path`, or inject a `repository`, for a durable one;
- a `BaseLLM` implementation through `config["llm"]` for the drafting stage. None is bundled and no
  model is named;
- durable checkpointing. The standalone server compiles with an in-process saver, so a review
  suspended for a human does not survive a restart.

Runtime parameters in `config/config.yaml`: `memory_enabled` (must stay true — the checkpointer is
what makes the human-review pause resumable), `hitl.enabled` / `hitl.max_hitl`,
`payload_ttl_seconds` (how long the raw submission is retrievable, 1–86400),
`review_criteria_path`, `program_types` and `program_type_vocabulary_version`, `max_retry`,
`timeout_s`, and `retention_years` — whose default of `null` means rulings are kept indefinitely.
Set it from your own retention policy before recording anything real.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is built to be served by the platform. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

There is no degraded mode and no bundled framework. Without the AgentCore wheel from the platform
registry, importing the agent fails at the first `from framework...` import.

With the framework present, the standalone server in `src/api/server.py` starts and wires its
services **eagerly at import**, on purpose: a missing service seam fails at start-up rather than
surfacing as an opaque 500 on someone's first real submission. What it will not do on its own is
produce model-written prose (no LLM is injected), keep rulings (SQLite `:memory:`) or survive a
restart mid-review (in-process checkpointer). The test suite runs with no platform connection.

## Input

| Route | Purpose | Who may call it |
|---|---|---|
| `POST /invoke` | Draft the review points for a submission | Authenticated caller in scope |
| `POST /ingest` | Load past rulings — csv, xlsx or docx, or structured records | Verified write identity |
| `POST /feedback` | Record a confirmed ruling into the desk's store | Verified write identity |
| `POST /resume` | Return a reviewer's decision to a suspended review | The same caller and session that opened it |

`broadcaster_id` and `review_desk_id` come from the authenticated request, never from the body.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "<programme outline, script or sponsor-integration text>",
    "session_id": "example-session",
    "broadcast_date": "2026-04-01",
    "program_type": "variety"
  }'
```

`input` is bounded at 200,000 characters and must be non-empty. `program_type` is drawn from the
configured vocabulary. Ingest accepts csv, xlsx and docx; PDF, video and audio are out of scope.

`/resume` takes `thread_id`, the originating `session_id`, an `action` of approve / correct /
reject, and up to fifty corrections each naming an `issue_id` and a `criteria_id`. Any other action
is **422**. Ownership is enforced strictly: a resume whose broadcaster, desk, caller and session do
not all match the ones that opened the review is **403** — an omitted `session_id` is a valid
request that simply cannot match, and is refused the same way. Resumes on one thread are
serialised, so two callers cannot race the same suspended review.

## Output

The review draft comes back through the delivery gate described above:

```json
{
  "review_issues": [
    {"issue_id": "...", "criteria_id": "...", "criteria_version": "1.0",
     "axis": "stealth_marketing", "cited_source_ids": ["ruling-001"]}
  ],
  "disclosure_map": {"...": "..."},
  "escalation_flags": ["..."],
  "evidence_map": [{"claim_id": "0", "source_ids": ["ruling-001"]}],
  "cited_source_ids": ["ruling-001"],
  "criteria_records": [{"criteria_id": "...", "criteria_version": "1.0"}],
  "mode": "invoke",
  "gate_kind": "draft"
}
```

`review_issues` are the points to check, each bound to the criterion and version it came from —
so a draft produced under one revision of the rules stays interpretable after the rules change.
`disclosure_map` covers what a sponsor integration would have to disclose; `escalation_flags` marks
what should go to a human beyond the normal desk. A submission needing a reviewer's decision comes
back as `awaiting_human` with a `thread_id` for `/resume`.

Sponsor and product values appear as `sponsor_ref_…` / `product_ref_…` unless the caller is
entitled to real names, as described in *Overview*. If resolution fails for any reason, the
reference is returned as-is rather than the response failing.

Values above are illustrative; real output depends on the submission and the configured criteria.

## Security and Limitations

Every node requires verified-external trust, and read, write and resume are separately
authorised — a caller who may request a draft is not thereby able to write into the ruling store.
Raw submission text is held in a scope-bound, expiring store and deleted when the request ends, so
it does not persist in the graph state; checkpoint content is scrubbed on the ingest path. Names
are tokenised before anything is stored and resolved only in the response.

Known limits worth stating plainly:

- **No compliance guarantee.** Point-raising, schema constraints and blocked assertive phrasing are
  product-safety measures. They do not establish conformance with pharmaceutical-advertising law,
  representations law, broadcast standards or anything else. The criteria's validity and the final
  decision belong to the organisation's review, legal and compliance staff.
- **Recall is bounded by the desk's own history.** A desk with no recorded rulings gets points
  without precedent, correctly uncited.
- **Text only.** No PDF, video or audio.
- **A criterion match is a pointer, not a finding.** That a point cites a criterion does not mean
  the criterion is breached — that is the reviewer's judgement.
- **It never clears a programme for broadcast.**

Output is machine-generated and assistive. It does not substitute for expert review, and any
decision that matters should be made against the primary material by a person.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2)
- Generation mode: `llm`, driven entirely through the `config["llm"]` injection seam
- HITL: **enabled** — a review can suspend for a human decision and resume on the same thread
- Persistence: a scoped ruling store supplied by the deployment; a checkpointer is required
  (`memory_enabled: true`)
- Secrets: none required by the manifest; store, model and entry credentials are
  deployment-supplied
- External API clients: none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, without a model and without external storage.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the scope and name-tokenisation
model, the criteria-versioning contract and the output gates. `docs/03_test_spec.md` records the
test specification and the verification results, including a run against a live model.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
