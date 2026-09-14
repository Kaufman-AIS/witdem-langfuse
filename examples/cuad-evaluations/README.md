# Existing evaluations → understandable business requirements

[Integration home](../../README.md) · [Documentation index](../../docs/README.md) · [Witdem docs](https://docs.witdem.com/) · [Witdem OSS](https://github.com/ebrahimisoheil/witdem-oss)

This example uses the existing Haystack CUAD review and existing Witdem OSS UI.
The contract asks only whether the review meets two evidence-quality requirements.
It does not equate approval, legal correctness, or overall business success with
passing these evaluations.

## Prerequisites

New to Witdem? Follow the [official setup guide](https://docs.witdem.com/getting-started/). Read the [contract tutorial](https://docs.witdem.com/contract-tutorial/) for the schema reused here, and the [Haystack guide](https://docs.witdem.com/integrations/haystack/) for application instrumentation. This example adds a separate score-binding file to connect Langfuse evidence to those requirements.

## Run

Requires Python 3.12+, uv, an existing Langfuse project, and an existing Witdem OSS
receiver/dashboard. Install from the `witdem-langfuse` repository:

```sh
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -e '.[etl,telemetry,contracts]'
```

Supply `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`,
`LANGFUSE_PROJECT_ID`, `WITDEM_RECEIVER_URL`, and `WITDEM_DASHBOARD_URL` through your
environment. Set `WITDEM_API_KEY` if the receiver requires authentication.

First use the [existing CUAD acceptance recipe](../../docs/acceptance-cuad-results.md#reproduce)
to run the real application with Langfuse instrumentation and save its
`oss-detail.json`. It requires the CUAD application and its provider configuration;
this integration does not bundle or replace that application. For the recorded
local example we reuse a real, previously completed review, with no new model calls.

Then run this sequence, choosing a fixed interval covering both the original trace
and the time the example publishes its existing evaluations:

```sh
.venv/bin/python examples/cuad-evaluations/run.py \
  --detail output/cuad-acceptance/live/oss-detail.json \
  --source "$LANGFUSE_BASE_URL" --project "$LANGFUSE_PROJECT_ID" \
  --receiver "$WITDEM_RECEIVER_URL" \
  --from-time 2026-09-14T00:00:00Z --to-time 2026-09-15T00:00:00Z \
  --output output/contract-demo/live
```

For local HTTP endpoints, append `--allow-http`. Use an isolated OSS destination
for this example: its narrow contract is a separate assessment from the original
application's broader contract. The supplied fixture Compose file can start the
unchanged OSS release on receiver 24329 and dashboard 28512:

```sh
docker compose -p witdem-contract-demo -f tests/fixtures/oss-backfill.compose.yaml up -d
```

The runner copies two existing SDK evaluation values into Langfuse as API scores,
verifies their availability, backfills traces through Duckle, and imports only the
existing workflow definition/lifecycle alongside the new contract assessment.
It deliberately stops SDK delivery after two records, then resumes. Original
requirements and goal results are not replayed. Its generated `bindings.yaml`
selects exact score IDs, source API, and trace scope. If supplied an original
wire-record export with `--records`, observation bindings are preserved instead.

The score-reading interval is based on score timestamps, which may be later than
the trace. Source/receiver credentials never enter the YAML or checkpoint.

## The YAML bridge

[`contract.yaml`](contract.yaml) uses Witdem's existing v2 schema. A separate
integration-owned binding file declares which existing score supplies each check:

```yaml
version: 1
requirements:
  evidence_supported:
    evaluation: evidence_completeness
    name: evidence_completeness
    source: EVAL
    subject: observation
    config_id: YOUR_SCORE_CONFIG_ID
    observation_id: YOUR_OBSERVATION_ID
  extraction_trusted:
    evaluation: extraction_confidence
    name: extraction_confidence
    source: API
    subject: trace
    score_id: YOUR_SCORE_ID
```

Use exactly one `score_id` or `config_id` per binding. Config IDs describe the
score schema; they do not authenticate which evaluator produced it. Use exact
score IDs when evaluator identity cannot be established from your source setup.
Multiple matching scores are unknown; the adapter never silently chooses the
latest. Unknown, malformed, wrong-subject, or missing evidence cannot pass.
Thresholds and direction live in the contract; Boolean targets require actual
Boolean score values. Witdem-returned scores are excluded, including from the
assessment fingerprint.

To consume an already evaluated trace directly, without publishing example scores:

```sh
.venv/bin/python -m witdem_langfuse.evaluations \
  --contract examples/cuad-evaluations/contract.yaml \
  --bindings output/contract-demo/live/bindings.yaml \
  --source "$LANGFUSE_BASE_URL" --project "$LANGFUSE_PROJECT_ID" \
  --trace YOUR_TRACE_ID \
  --from-time 2026-09-14T00:00:00Z --to-time 2026-09-15T00:00:00Z \
  --workspace output/assessment --receiver "$WITDEM_RECEIVER_URL"
```

Supply `--application-result` only with the original reported disposition.
Without it, no application outcome is fabricated. Repeat identical commands to
resume. `--max-pages` bounds source reads; assessment waits until the score scan
is complete. A failed source request leaves prior pages checkpointed; rerun after
resolving the error or source rate limit. `--offline` requires a completed score
snapshot and makes no source requests. Omit `--receiver` for wholly offline
reassessment. Each workspace is immutable: changed source data or configuration
requires a new workspace. Assessment-version comparisons are outside this demo.

To verify a fresh live import without creating synthetic fixtures, place the
runner output in `output/live-usage/live` and run:

```sh
.venv/bin/python examples/cuad-evaluations/verify.py \
  --output output/live-usage --dashboard http://127.0.0.1:28512 --live-only
```

The verifier compares model calls, input/output/total tokens, providers, models,
reported cost, and cost coverage against the original application execution,
alongside the requirement and evidence checks. Witdem OSS performs normalization
and cost presentation using its existing adapters; the integration only restores
source telemetry attributes that were previously dropped.

Fresh provider run verified on 2026-09-14 with Duckle 0.7.2 and unchanged OSS 0.2.11:
19 model calls, 14,386 tokens, DeepSeek/Mistral/OpenAI/Voyage, and $0.04169092
reported cost. Cost coverage was 18/19 calls; the unmeasured call remains unknown.
Original trace: `edb4b038b31d4116928a8f9c8ed45549`; imported execution:
`910e41ecd8353136a8fb9d3bc0ba4b6e`. Both declared requirements passed. This fresh
run called the providers; importing and reassessing it made no additional AI calls.

## Failed and missing fixtures

These are deliberately synthetic saved evaluations, not real failed model runs.
They are restricted to a local receiver and are not published to Langfuse:

```sh
.venv/bin/python examples/cuad-evaluations/fixtures.py \
  --live output/contract-demo/live --receiver http://127.0.0.1:24329
.venv/bin/python examples/cuad-evaluations/verify.py \
  --output output/contract-demo --dashboard http://127.0.0.1:28512
```

The verifier reads the public OSS detail/evidence APIs, checks all requirement
values and source score objects, and writes `verification.json` plus a paced
terminal walkthrough, `walkthrough.cast` (play with `asciinema play`). This is an
API/terminal walkthrough, not a screen capture. Open its printed Witdem links
for the existing UI. Fixture root operations and evidence explicitly identify
synthetic data; their copied workflow diagram does not represent an actual run.

## Return to Langfuse

Use the [existing writeback command](../../docs/score-writeback.md), with the new
`oss-detail.json` and original Langfuse trace. Derived assessments emit
`contract_requirements_met` and `contract_assessment_status` (met/failed/unknown),
plus a separately reported business disposition when present. Unknown omits the
Boolean; it never becomes false. The score metadata/comment includes the existing
Witdem evidence export URL. Readers still need their own destination access.

## Verified locally, 2026-09-14

- Original run versions: Langfuse 4.35.0; unmodified Witdem OSS 0.2.11; SDK 0.2.3; Duckle 0.5.11.
- Real CUAD: evidence completeness 1.0; extraction confidence 0.9; requirements met.
- Application disposition retained as `approved_with_exceptions`.
- Historical trace: 66 operations, 19 model calls, 15,998 tokens retained.
- Synthetic failure: completeness 0.6 fails the 0.8 target.
- Synthetic missing evaluation: requirement and goal remain unknown.
- Exact source score IDs, values, comments, and subjects retained in exported evidence.
- Upgraded the demo from OSS 0.2.10 to 0.2.11 with its data volume preserved;
  all three execution/evidence checks passed again and the refreshed dashboard loaded.
  Pre-upgrade data backup: `output/contract-demo/backup-before-0.2.11/`.

Existing UI limitation: OSS 0.2.10 renders an unknown requirement as
“Reported / Unassessed” and the unknown goal badge as “Not reported”. The saved
assessment and evidence retain the explicit unknown result and missing-evaluation
reason. The goal-flow heading uses the application workflow name, “Contract
review”; the narrower declared goal is retained in contract metadata and the
execution-list goal filter. No UI changes were made to alter these labels.

The demo opts into fetching observation metadata to retain explicit provider, request-model, and reported-cost
attributes alongside three workflow identifiers: `haystack.component.name`, `haystack.component.type`, and
`witdem.workflow.id`. Arbitrary metadata and input/output are not forwarded.
The default generic trace backfill remains metadata-free; use `--telemetry-context`
to retain source provider/cost metadata. The example enables this metadata fetch.
Synthetic fixtures contain no model calls and therefore have no cost or token usage.
Use the real execution to inspect those measurements.

This is a bounded, operator-run integration. It does not implement automatic
scheduling, distributed quotas, a new UI, new evaluators, or an immutable external
reviewer portal. UI inspection verifies presentation, not a user study.
