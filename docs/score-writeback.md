For assessments derived from Langfuse evaluations, the Boolean is named
`contract_requirements_met`; a separate `contract_assessment_status` retains
met/failed/unknown. This avoids claiming overall business success from the narrow
CUAD evidence-quality contract. See the [current example](../examples/cuad-evaluations/README.md).

# Explicit outcome scores and external evidence links

Witdem remains external. `witdem_langfuse.writeback` uses Langfuse's supported
score API and existing presentation; it adds no Langfuse UI, schema, or backend
changes. The source is an explicit Witdem assessment, not trace metadata.

Prepare a reviewable score document without sending it:

```sh
.venv/bin/python -m witdem_langfuse.writeback \
  --detail output/cuad-acceptance/live/oss-detail.json \
  --project YOUR_LANGFUSE_PROJECT \
  --trace ORIGINAL_LANGFUSE_TRACE_ID \
  --evidence-url https://YOUR_WITDEM/api/v1/runs/EXECUTION_ID/evidence-bundle \
  --output output/writeback/scores.json
```

Add `--publish` to send using `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, and
`LANGFUSE_SECRET_KEY`. Local HTTP testing additionally requires `--allow-http`.
Before any POST, the command verifies the target trace in the credential-bound
Langfuse project. Redirects are disabled. Supply authenticated Witdem evidence
URLs without embedded credentials; this does not grant access to the evidence.

Mapping:

| Explicit Witdem assessment | Langfuse score |
| --- | --- |
| product_goal_achieved | business_outcome_achieved, BOOLEAN |
| decision_evidence_sufficient | evidence_sufficient, BOOLEAN |
| reported business disposition | business_result, CATEGORICAL |

False becomes numeric zero on creation and is returned as Boolean false by the
v3 read API. Unknown/null Boolean values are omitted, never converted to false.
Each score carries source, contract hash/version/reference, execution ID,
assessment fingerprint, requirement failure/unknown IDs, and an evidence link.
The human-readable comment also includes the link and contract version.
Langfuse may normalize numeric-looking metadata strings, so `contract_reference`
also retains a textual name/version reference such as `contract_review@2.0`.

Score IDs are deterministic for each assessment snapshot and score name. Retrying
the same snapshot uses the same IDs; it does not append duplicate scores. Changed
assessments receive new IDs, preserving prior snapshots. These are historical
assessment scores: do not aggregate them as one current result per execution
without selecting the intended assessment fingerprint. Automatic latest-only
publication, conflict/revision ordering, and durable scheduled writeback remain
unfinished. Never claim an HTTP acknowledgement means the asynchronous read
projection is already available.

Evidence links currently point to the existing execution export. They are not
claims that the URL serves immutable bytes or a publicly accessible reviewer
bundle. Snapshot freezing and reviewer access remain Witdem responsibilities.

The input detail file is trusted operator-supplied assessment data; its `sdk`
source marker is not a cryptographic authenticity proof.

## Local CUAD verification

The original live CUAD assessment was posted twice to Langfuse 4.35.0. Reading
the trace through `/api/public/v3/scores` returned exactly three distinct scores:
business outcome achieved **true**, evidence sufficient **false**, and business
result **approved_with_exceptions**. All retained the evidence URL and contract
hash. Prepared requests, acknowledgements and readback are in
`output/cuad-acceptance/writeback/`. No upstream changes were made.

Official references: [score creation](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk)
and [Scores API](https://langfuse.com/docs/api-and-data-platform/features/scores-api).
