# Duckle backfills

Everything here lives in `witdem-langfuse`. No Witdem OSS source changes are
required. The destination is its existing `POST /v1/traces` OTLP endpoint.

The data path is Langfuse Observations v2 → bounded daily page → Duckle JSONL
source → normalization node → JSONL sink → durable pending OTLP page → OSS.
The Duckle pipeline is `src/witdem_langfuse/pipelines/backfill.pipeline.json`.
There is no production fallback around Duckle. The Python command coordinates
public API pagination and acknowledgements; Duckle transforms each page.

Install and run from this repository:

```sh
uv pip install --python .venv/bin/python -e '.[etl,telemetry]'
export DUCKLE_EXECUTABLE="$PWD/.venv/bin/duckle"
# Supply LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, and optional WITDEM_API_KEY
# through your environment or secret manager.
.venv/bin/witdem-langfuse-backfill \
  --source https://cloud.langfuse.com \
  --project YOUR_LANGFUSE_PROJECT_ID \
  --receiver https://YOUR_WITDEM_RECEIVER \
  --from-time 2026-09-01T00:00:00Z \
  --to-time 2026-09-14T00:00:00Z \
  --checkpoint output/backfills/september.sqlite \
  --max-pages 10
```

Repeat the identical command to resume. `complete: false` means the page budget
was reached, not that the range is complete. Use `--allow-http` only for an
operator-approved local/self-hosted HTTP destination or source.

Each checkpoint binds the source, project, destination, frozen range, page size,
and normalization version. A different configuration is rejected. Only one
process may write a checkpoint. Completed ranges are not silently rescheduled;
use a new checkpoint for a deliberate reconciliation sweep.

Each transformed page is committed locally before delivery. The cursor moves
only after full acknowledgement. A timeout or process crash replays identical
bytes before fetching another page. Delivery is **at least once**, not exactly
once; repeated pages retain the same operation identities. Receiver analytics
deduplication must be verified before production adoption. Partial OTLP success
does not advance the checkpoint.

Imported identities are deterministically scoped by Langfuse project and trace,
so imports cannot overwrite a directly instrumented execution. Original project,
trace, and observation IDs remain as correlation attributes. Imports do not
fabricate explicit outcome events, contracts, workflow definitions, or business
success from trace metadata. Historical business records use the separate
[explicit replay/binding path](business-record-replay.md).

Pages and Duckle output are bounded to 32 MiB, with a 60-second transform timeout,
two threads, and a 256 MB Duckle memory setting. Only engineering fields are
requested. Inputs, outputs, and arbitrary metadata are excluded from export.
Secrets are not placed in the checkpoint or command arguments.

Current scope: one operator-controlled local backfill. Source/receiver HTTP 429,
5xx, and transport failures preserve progress and save a durable retry deadline.
Numeric and HTTP-date Retry-After values are honored; otherwise exponential
backoff is used. Re-running before the deadline makes no network requests.
Authentication and other non-retryable errors fail visibly.

Add `--follow` to continue automatically until complete. It processes one page
at a time, with at least two seconds between iterations and durable cooldowns.
Ctrl-C leaves the checkpoint resumable. Without this flag, the command returns
its progress and `retry_at` so an operator or scheduler can resume it later.

A 400 response while using a cursor restarts the same daily window once. This
may replay acknowledged rows with stable identities; row counts represent
acknowledged deliveries, not unique observations. A second cursor failure in
that window stops visibly. First-page 400 responses are never reset blindly.
CLI backfills now share a host-local SQLite request budget by default. The
database is `~/.local/state/witdem-langfuse/quota.sqlite`; override it with
`--quota-db`. The default group is the source URL, conservatively sharing across
projects at that URL. Use the same `--quota-group` for connections that consume
the same organization allowance. Groups must use a consistent configured rate.
The default is 30 requests per minute, spaced evenly; configure
`--requests-per-minute` to fit your actual allowance. This is a local pacing
policy, not a claim about your Langfuse subscription's limits.

A source rate-limit deadline is shared with other checkpoints in that group.
Reservations survive crashes and cannot be reclaimed early. Concurrent writers
reserve atomically. Pending receiver delivery does not spend a source request.
Separate machines or different quota databases do not coordinate, and requests
from other applications are outside this budget. Distributed quota coordination
and integration with the durable service scheduler remain unfinished.

## Verification on 2026-09-14

The full HTTP path is now verified against an isolated, unmodified Witdem OSS
0.2.10 image. Start that fixture from this repository with:

```sh
docker compose -f tests/fixtures/oss-backfill.compose.yaml up -d
```

Its receiver is at port 24329 and dashboard at 28512. It has separate data from
the user's existing OSS deployment. The real Langfuse → Duckle → OSS run imported
107 observations across two executions. For the live execution, verification
against the original instrumented CUAD report found 66 operations, 65 parent
links, 19 model calls, and exactly 15,998 tokens. All original observation IDs
remained available as correlation attributes. No business outcome was inferred.
The existing receiver also converged to the corrected usage projection after
reimporting the same stable identities without increasing operation counts.

Recheck the live run with:

```sh
.venv/bin/python scripts/verify_backfill_oss.py \
  --project witdem-cuad \
  --original output/cuad-acceptance/live/acceptance.json \
  --observations output/cuad-acceptance/live/langfuse-observations.json \
  --output output/cuad-acceptance/oss-backfill/verification.json
```

This exposed and fixed a normalization error: Langfuse aggregate usage includes
arbitrary units, such as OCR bytes/pages and embedding counts. Version 2 maps
only token buckets from `usageDetails`, adding exclusive cache/reasoning token
buckets to their respective direction. See Langfuse's
[usage bucket semantics](https://langfuse.com/docs/observability/features/token-and-cost-tracking).
It does not relabel bytes, pages, items, or vectors as tokens. Unknown usage
shapes remain unmeasured. Version 1 checkpoints are rejected by the new normalizer;
start a new checkpoint to reconcile their range deliberately. Imported costs and
explicit historical business-event replay remain separate unfinished work.

The real local Langfuse public API returned 107 CUAD observations. A one-page
budget processed 100 through Duckle and stopped with `complete: false`; resuming
the same checkpoint processed the remaining seven and completed the range.
That initial test wrote normalized OTLP to local verification files, not Witdem OSS.
Evidence: `output/cuad-acceptance/duckle-backfill-verification/result.json`.

The test suite ran 71 tests: 54 passed and 17 environment-dependent tests were
skipped. Backfill tests exercise the real Duckle executable, stable identities,
excluded business metadata, ambiguous-delivery replay, completed-range no-op,
configuration mismatch, daily boundaries, and page-budget resumption. No OSS
tracked files were modified; its pre-existing untracked `output/` remains.
