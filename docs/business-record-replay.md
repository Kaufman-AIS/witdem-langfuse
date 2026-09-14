# Explicit business records alongside a Duckle backfill

Langfuse observations do not establish business truth. Historical contracts,
requirements, and outcomes must come from a trusted application outbox or export
of its original Witdem SDK records. No values are extracted from arbitrary
Langfuse metadata.

`src/witdem_langfuse/pipelines/replay.pipeline.json` runs the identity mapping in
Duckle. It reads `input.jsonl` and writes `output.jsonl` in the selected workspace.
Each input line has a `page_json` string containing this envelope:

```json
{
  "version": "1.0",
  "source": "application_records",
  "project_id": "your-langfuse-project",
  "source_execution_id": "original-witdem-execution",
  "source_trace_id": "original-langfuse-trace",
  "records": []
}
```

Populate `records` with 1–100 original `/sdk/v1/records` v1.0 wire envelopes.
The transformer rejects mismatched execution/trace bindings, duplicate event IDs,
unsupported versions, unknown envelope fields, and nonfinite JSON numbers.
It remaps execution, trace, span, and event IDs deterministically. Record kind,
name, value, and attributes remain unchanged, including explicit false values.
Both this pipeline and observation backfills use the same project-scoped trace
identity, so records join their imported execution without changing OSS.

Run the pipeline with the repository environment (do not resolve the Python
symlink out of the virtual environment):

```sh
export DUCKLE_PYTHON_BIN="$PWD/.venv/bin/python"
export DUCKLE_THREADS=2
export DUCKLE_MEMORY_LIMIT=256MB
.venv/bin/duckle \
  --pipeline "$PWD/src/witdem_langfuse/pipelines/replay.pipeline.json" \
  --workspace "$PWD/output/replay" \
  --log-dir "$PWD/output/replay/logs" \
  --name application-record-replay --manifest
```

Each output line contains `records_json`, a JSON array of mapped OSS wire
records. These are accepted by the existing `/sdk/v1/records` endpoint with its
normal authentication. The source label and binding are operator assertions,
not cryptographic proof of authenticity. Do not accept untrusted uploads as an
authoritative application outbox.

## Resumable delivery

Create a UTF-8 JSONL manifest with one replay envelope per line (the envelope
itself, without the `page_json` wrapper). The command wraps each page for Duckle:

```sh
export DUCKLE_EXECUTABLE="$PWD/.venv/bin/duckle"
.venv/bin/python -m witdem_langfuse.replay_delivery \
  --manifest output/replay/manifest.jsonl \
  --checkpoint output/replay/delivery.sqlite \
  --receiver https://YOUR_WITDEM_RECEIVER \
  --max-records 100 --follow
```

Installing this repository also provides `witdem-langfuse-replay`. Supply
`WITDEM_API_KEY` in the environment when required by OSS. Use `--allow-http`
only for an operator-approved local or self-hosted endpoint.

The entire manifest is validated and transformed through Duckle before the first
network write. Mapped records are persisted transactionally. Identical event IDs
across pages are deduplicated; differing payloads with the same event ID reject
the entire preflight. The checkpoint binds the manifest's SHA-256, receiver, and
replay version. Editing or appending to that manifest fails on resume.

Each matching acceptance receipt is committed separately. Lost acknowledgements
replay the same bytes and event identity; acknowledged records are not resent.
Delivery remains at least once, using the existing OSS event-ID deduplication.
HTTP 429/5xx and transport errors persist a retry deadline, honoring Retry-After.
`--follow` resumes after cooldown; without it, rerun the command later. Other
HTTP errors and mismatched acknowledgements fail visibly. Completed checkpoints
are no-ops. One process may operate a checkpoint at a time.

Conflict checks currently cover one manifest/checkpoint, not unrelated manifests
or producers. Cross-import ledgers, distributed scheduling, and deployment
hardening remain unfinished. Keep checkpoints and their frozen manifests together.

The original wire protocol has no top-level historical timestamp;
preserve existing timestamp attributes when present rather than inventing them.
Receiver acceptance time and original application event time are distinct.

## CUAD verification

The original CUAD run's 26 SDK records were read from its existing immutable
corpus without modifying OSS. Duckle remapped all 26; their business values and
attributes were compared exactly to the originals. Source export, Duckle output,
and verification files are in `output/cuad-acceptance/replay/` (ignored local
evidence, not a bundled customer-data fixture).

Those 26 mapped records were then accepted through the unmodified OSS 0.2.10
SDK-record endpoint in the isolated backfill fixture. Its imported execution
`0f2c72ba00e676fdbb7488738adce682` changed from trace-only/unknown to the original
explicit business result. The complete `outcomes` object matches the original
CUAD execution, including `approved_with_exceptions`, an achieved product goal,
and `decision_evidence_sufficient=false`. All 26 semantic records are present;
15,998 tokens and the original duration are preserved. Verification is saved in
`output/cuad-acceptance/replay/oss-verification.json`.

The trace-only verifier is expected to fail its no-business-facts assertion
after this explicit replay; that is the intended state transition. For a fresh
trace-only acceptance, use a separate empty fixture data volume.

The durable delivery CLI was subsequently tested against that same isolated
endpoint. It stopped at 10 acknowledged records, resumed in a new process to
20 and then 26, and completed its journal. OSS still contained exactly 26
semantic records and the same original business result. Evidence is saved in
`output/cuad-acceptance/replay/delivery-verification.json`. Five delivery tests
cover real Duckle transformation, full-manifest conflict rejection, identical
duplicate suppression, lost acknowledgements, immutable resume, and cooldown
recovery after an acknowledged prefix.
