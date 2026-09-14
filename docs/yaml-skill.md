# Write YAML with an AI assistant

[Repository home](../README.md) · [Skill instructions](../skills/witdem-langfuse-yaml/SKILL.md)

Use **witdem-langfuse-yaml** to turn your workflow requirements and existing
Langfuse evaluation metadata into `contract.yaml` and `bindings.yaml`. It explains
numeric and Boolean targets, exact score selection, and missing or ambiguous
evidence. It reuses this integration's validators and the existing Witdem schema.

## Use it from your clone

Ask your coding assistant:

> Read skills/witdem-langfuse-yaml/SKILL.md and use it to help me create a contract
> and score bindings for my application workflow using its existing evaluations in
> Langfuse. Ask me for missing targets or source
> identities, then validate both YAML files locally.

Provide the goal, requirement targets, evaluation names and actual source
identities, and the intended trace or observation scope. A saved evaluation
snapshot is useful for an offline preview. Credentials are not needed to draft
or validate the files. The assistant should not invent production score IDs or
assume that the CUAD thresholds apply to your workflow.

The directory uses the standard `SKILL.md` structure. You can copy it into your
assistant's supported skills directory. For a personal Codex installation:

```sh
mkdir -p ~/.codex/skills
cp -R skills/witdem-langfuse-yaml ~/.codex/skills/
```

If that skill already exists, review the local version before replacing it.
In a session where it is available, invoke `$witdem-langfuse-yaml`. Keep the
integration clone available: the skill reads its example and schema implementation.
Other assistants can follow the file directly or use their own skill installation
mechanism; the repository does not install itself into an assistant.

## Validate without an assistant

After following the repository's Python installation instructions:

```sh
.venv/bin/python skills/witdem-langfuse-yaml/scripts/validate_yaml.py \
  --contract path/to/contract.yaml --bindings path/to/bindings.yaml
```

Optionally preview the proposed YAML against an existing integration snapshot:

```sh
.venv/bin/python skills/witdem-langfuse-yaml/scripts/validate_yaml.py \
  --contract path/to/contract.yaml --bindings path/to/bindings.yaml \
  --snapshot path/to/assessment.jsonl
```

The helper returns JSON and exits 0 for valid input, or exits 1 for invalid input.
A valid preview can still report failed or unknown requirements; those are
assessment outcomes, not schema errors. Duplicate YAML keys are rejected.
A preview uses the supplied contract and bindings in memory and does not modify
the saved snapshot, deliver records, or contact Langfuse. Structural validation
cannot establish whether IDs exist or whether a business threshold is appropriate.

Use the [CUAD walkthrough](../examples/cuad-evaluations/README.md) when you are
ready to import. Changed contracts or bindings require a fresh assessment workspace.
