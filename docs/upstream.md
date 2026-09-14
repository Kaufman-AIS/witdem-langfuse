# Discussion draft — not submitted

## Witdem integration: business-readable outcomes from existing Langfuse evaluations

I'm the creator of Witdem OSS. I'd like to offer a small integration that lets
product and domain teams understand whether an AI execution met declared business
requirements, using the evaluations their team already stores in Langfuse.

In [roadmap discussion #11391](https://github.com/orgs/langfuse/discussions/11391),
a customer described a team of more than 20 nontechnical reviewers needing a
clearer way to work with agent executions. I also read your current
[product-team guide](https://langfuse.com/resources/engineering/langfuse-for-product-teams)
and the subsequent improvements discussed in that thread. This is an additional
contract-based workflow, not a claim those historical UI issues remain unfixed.

Our CUAD example uses a Witdem YAML contract to declare two evidence-quality
requirements, binds them to existing Langfuse evaluations, and shows met, failed,
or unknown results in the existing Witdem OSS UI. Each requirement preserves its
source score and original trace reference in the evidence export. Approval or
escalation remains a separate application-reported disposition.

The working example includes a real contract-review run, two labeled synthetic
failure/missing-evidence fixtures, resumable Duckle backfill, and reassessment of
saved evaluations without rerunning agents or judges. Results link back through
Langfuse's existing score API. There are no Langfuse application changes or new
Langfuse dependencies.

We recognize that Langfuse already supports business-rule evaluators, product-team
workflows, and evidence-oriented deployment gates. Our offer is a reusable YAML
contract-to-evaluation mapping and business-readable presentation of that mapping.
The adapter lives in a separate repository and reuses Witdem OSS unchanged.

I would appreciate feedback from teams reviewing executions against named
requirements: does this mapping make the result easier to understand, and what
evidence would you need to trust it? The runnable example is linked below.

---

Local material to review before posting:

- [Runnable example and setup](../examples/cuad-evaluations/README.md)
- [Actual YAML contract](../examples/cuad-evaluations/contract.yaml)
- Local proof: `output/contract-demo/verification.json`, per-case evidence exports,
  score API readback, and `output/contract-demo/walkthrough.cast`.

Runnable repository: https://github.com/Kaufman-AIS/witdem-langfuse.
The verifier generates a terminal walkthrough locally; no public recording is hosted. No issue/Discussion/PR
has been submitted and no CLA accepted. Verified against local Langfuse 4.35.0;
no broad production compatibility or user-study claims.
