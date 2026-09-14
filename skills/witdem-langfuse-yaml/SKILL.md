---
name: witdem-langfuse-yaml
description: Author and validate Witdem YAML outcome contracts and their Langfuse evaluation bindings. Use when defining named requirements, numeric or Boolean targets, or mapping existing scores to those requirements in witdem-langfuse.
---

# Witdem contracts and Langfuse bindings

Turn the user's stated requirements into two files: a Witdem `contract.yaml`
and an integration-owned `bindings.yaml`. Work in a clone of witdem-langfuse with
its Python package and `contracts` extra installed. Locate the clone explicitly
if the current directory is a different project; paths below are repository-relative.

## Start from the actual format

Read `examples/cuad-evaluations/contract.yaml`, the YAML bridge section of
`examples/cuad-evaluations/README.md`, and `validate`, `Binding`, and `assess` in
`src/witdem_langfuse/evaluations.py`. These are the supported schema and semantics;
do not invent another contract format or new evaluator fields.

Keep contract schema `version: 2` separate from binding schema `version: 1`.
The contract has `id`, `name`, `description`, `result`, `goal`, and `evaluations`.
`goal.requirements` maps stable requirement IDs to names, descriptions, and failure
explanations. `evaluations` maps evaluation keys to targets. Bindings connect the
two; evaluation keys are not necessarily Langfuse score names.

## Establish the meaning before filling the YAML

Use a narrowly stated goal justified by existing evaluations. Ask for genuinely
missing business targets and source identities. Draft unresolved portions with
explicit TODO comments when useful, but do not call those bindings ready to run.
Do not invent score IDs, config IDs, observation IDs, units, score meanings, or
business thresholds. An example's 0.80/0.70 thresholds are not universal defaults.

Keep the application's approval/rejection/escalation in `result.values` separate
from the assessed goal. Confidence or evidence-quality checks do not establish
legal approval or complete business success.

## Bind exact evidence

Bind every requirement exactly once, using its requirement ID as the mapping key.
Each binding has:

- `evaluation`: a key in the contract's `evaluations`.
- `name`: the exact Langfuse score name.
- `source`: the actual `API`, `EVAL`, or `ANNOTATION` source.
- Exactly one of `score_id` or `config_id`.
- `subject`: `trace` or `observation`. Observation bindings also need the exact
  `observation_id`; trace bindings must not include it.

Use IDs and scope from supplied score records or an authorized read of Langfuse.
Names alone are insufficient. A config ID can support reuse across traces, but
multiple matching scores remain ambiguous: do not silently pick the newest or
average them. Do not use Witdem writeback scores as input.

Numeric targets need `direction: higher_is_better` (value >= target) or
`direction: lower_is_better` (value <= target), with compatible units. Boolean
targets use YAML `true`/`false`, not quoted strings. The adapter expects BOOLEAN
source values to be JSON booleans; numeric 0/1 is not automatically coerced.

A missing, ambiguous, or invalid-value evaluation is unknown. An explicit failed
threshold is failed. Any failure makes the goal false; otherwise unknown evidence
makes it unknown. Preserve these distinctions in the explanation.

## Validate and deliver

Run the helper from this skill's actual installation path:

```sh
.venv/bin/python <skill-directory>/scripts/validate_yaml.py \
  --contract <contract.yaml> --bindings <bindings.yaml>
```

It rejects duplicate YAML keys and invokes the integration's existing validator.
Validation checks structure and linkage, not whether source IDs exist or a target
is appropriate. If a saved `assessment.jsonl` from the integration is available,
add `--snapshot <assessment.jsonl>` for an offline preview with the proposed YAML.
The helper performs no network calls, ingestion, writeback, agent or judge calls.
It does not overwrite the saved assessment.

Deliver the YAML files, explain requirement-to-score mappings and target choices,
report validation/preview results, and identify unresolved inputs. Use a fresh
assessment workspace for changed YAML. Authoring YAML does not require importing
production data or publishing scores; follow the user's separately authorized
scope for those actions. Keep credentials out of the YAML.
