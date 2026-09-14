# CUAD → Langfuse + Witdem OSS acceptance

Verified on 2026-09-14 using the existing application and OSS dashboard.
Langfuse: local self-hosted 4.35.0, public OTLP ingestion and Observations v2 API.
Witdem SDK: 0.2.3. No Langfuse internal database reads or changes.

| Check | Deterministic | Live providers |
| --- | --- | --- |
| Trace ID | `a0269959fd593d798ba013d4cdd5dcd7` | `9ad191db17d222cc0e949e72122732f5` |
| Witdem execution | `67e7b95f8de841dcb559d66a5f98d46a` | `5ecb9bc0a1f046b888b30d6f706b8b9c` |
| Exported / Langfuse observations | 41 / 41 | 66 / 66 |
| Application disposition | Human review required | Approved with exceptions |
| Explicit goal reported | Yes | Yes |
| Contract | contract_review | contract_review |
| OSS evidence export | Available | Available |

The live trace contains 18 Langfuse GENERATION observations and 1 EMBEDDING,
plus 47 SPAN observations. Models include DeepSeek Flash, GPT-5.4, Mistral OCR,
and Voyage 4 Large. Witdem records 19 model calls, 15,998 tokens, and approximately
$0.0491 measured cost. All four declared business requirements are present.

Witdem's existing OSS UI was inspected for both runs. Its Goal flow shows the
observed disposition and achieved goal, and its evaluation panel shows the
four business requirements plus evidence completeness, extraction confidence,
and result validity. No new UI was used or required.

The live application separately reported `decision_evidence_sufficient=false`
while the goal requirements passed. This flag was preserved in OSS's result
and evidence data. The integration does not turn technical success into
business or evidence success. This test is not validation of contract advice.

## Reproduce

Use the separately installed [Haystack CUAD application](https://github.com/ebrahimisoheil/haystack-cuad-contract-review). In its environment, install this sibling repository with its `telemetry,contracts`
extras. Supply `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`,
and `LANGFUSE_PROJECT_ID` for your Langfuse project. Then run:

```sh
.venv/bin/python ../witdem-langfuse/examples/verify_cuad_oss.py \
  --app "$PWD" \
  --source output/showcase/scanned-vendor-saas.pdf \
  --mode live \
  --receiver http://127.0.0.1:24319 \
  --dashboard http://127.0.0.1:28502 \
  --output ../witdem-langfuse/output/cuad-acceptance/live
```

The acceptance runner overrides receiver configuration temporarily and matches
the actual trace ID, never whichever run happens to be newest. It retrieves
the canonical evidence export and checks every locally exported span against
the remote Langfuse observations. Live mode also checks the existing app's
provider/token/cost verification criteria. Authentication and transport errors
fail visibly; delayed successful ingestion is polled for up to three minutes.

Local evidence is under `output/cuad-acceptance/{deterministic,live}/`, including
`acceptance.json`, `oss-detail.json`, and `oss-evidence-bundle.json`. The live
folder also contains a public API observation export without input/output.
Local Langfuse Compose and private bootstrap credentials are in the ignored
`output/cuad-acceptance/` directory; its `.env` has owner-only permissions.
The isolated Compose project is `witdem-cuad-langfuse`, web port 18503.

## Scope and remaining work

This proves shared OpenTelemetry instrumentation and explicit business reporting
into existing Witdem OSS. It does not prove historical Langfuse-only imports
into OSS, deployment against Langfuse Cloud, or production hardening. The older
standalone service does not substitute for those remaining integration tasks.
The application changes are an optional instrumentation hook and documentation;
its pre-existing memory changes were preserved. The integration implementation
and acceptance runner live in the separate `witdem-langfuse` repository.
