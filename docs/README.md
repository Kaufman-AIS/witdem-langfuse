# Integration documentation

[Repository home](../README.md) · [Witdem website](https://witdem.com/en) · [Official Witdem docs](https://docs.witdem.com/) · [Witdem OSS source](https://github.com/ebrahimisoheil/witdem-oss)

## Where to begin

1. Start Witdem using the [official installation guide](https://docs.witdem.com/getting-started/). The example also provides an isolated, version-pinned Compose deployment.
2. Read the [Witdem contract tutorial](https://docs.witdem.com/contract-tutorial/) to understand goals and named requirements.
3. Follow the [CUAD integration example](../examples/cuad-evaluations/README.md) to publish existing application evaluations to Langfuse, import them independently, and inspect the resulting assessment in Witdem.

Already have Langfuse evaluations? Use the example's direct assessment command and supply your own score bindings. Publishing CUAD evaluations is only a demonstration step.

## Responsibilities and interfaces

| Component | Responsibility | Interface used here |
| --- | --- | --- |
| Langfuse | Store application observations and evaluation scores | Public observations and scores APIs |
| This integration | Select evidence, apply declared targets, preserve provenance, checkpoint imports | Duckle pipelines and Python commands in this repository |
| Witdem OSS | Store and present executions, requirements, and evidence | Existing OTLP and SDK semantic-record ingestion, dashboard, evidence export |
| Application | Report its actual disposition and supply workflow context | Explicit application records; no disposition inferred from trace success |

The contract uses Witdem's existing schema. The score-binding YAML is owned by this integration. The adapter computes the assessment from saved evaluations and sends existing semantic record envelopes to Witdem; it does not add a new API or evaluator to either product.

## Implementation guides

| Guide | What it covers |
| --- | --- |
| [CUAD example](../examples/cuad-evaluations/README.md) | Setup, YAML binding, three demonstration cases, offline reassessment, and known UI limits |
| [Duckle backfills](backfills.md) | Observation mapping, bounded intervals, checkpoints, retries, and stable identities |
| [Business record replay](business-record-replay.md) | Explicit application context and existing SDK ingestion envelopes |
| [Score writeback](score-writeback.md) | Contract result names, evidence URLs, idempotent identities, and unknown values |
| [Original CUAD verification](acceptance-cuad-results.md) | Source application instrumentation and original live-run evidence; precedes the narrower evaluation-contract example |
| [Discussion draft](upstream.md) | Proposed community cookbook; not submitted or endorsed by Langfuse |

## Official Witdem references

- [Install and run](https://docs.witdem.com/getting-started/)
- [Define a YAML contract](https://docs.witdem.com/contract-tutorial/)
- [Haystack instrumentation](https://docs.witdem.com/integrations/haystack/)
- [Ingestion API](https://docs.witdem.com/api/ingestion/)
- [Workflow replay](https://docs.witdem.com/workflow-replay/)
- [Evidence bundles](https://docs.witdem.com/evidence-bundles/)

Witdem's official documentation describes the broader product. This repository documents the narrower, experimental Langfuse adapter and its verified compatibility; a feature in the product documentation is not automatically supported by this adapter.

## Repository map

- `src/witdem_langfuse/`: public API clients, score assessment, backfill, replay, and writeback.
- `src/witdem_langfuse/pipelines/`: Duckle pipeline definitions.
- `examples/cuad-evaluations/`: contract, runner, synthetic fixtures, and verification.
- `tests/`: contract semantics, source selection, resume behavior, and adapter checks.

For problems or feedback about this adapter, [open an issue here](https://github.com/Kaufman-AIS/witdem-langfuse/issues). For the Witdem application itself, use the [OSS project](https://github.com/ebrahimisoheil/witdem-oss).
