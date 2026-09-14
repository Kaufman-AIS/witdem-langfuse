# Reproduce the benchmark

[Deployment guide](../docs/deployment.md) · [Integration example](../examples/cuad-evaluations/README.md)

This harness generates synthetic Langfuse-shaped HTTP responses, runs the actual
Linux integration container and Duckle pipelines, and verifies received semantic
identities in a batch-committed SQLite sink. It also tests actual score writeback
requests. It does not run a Langfuse server, any model, or a judge.

The workload is one trace containing N observations and N source scores. Two
scores match the YAML requirements; the rest are unrelated diagnostics. This
stresses both observation volume and a large score snapshot for one execution.
It does not model N distinct contract assessments or arbitrary production payloads.

## Run

Install this repository with `etl,telemetry,contracts` extras and Docker, then:

```sh
docker build -f deploy/Dockerfile -t witdem-langfuse-worker:stage12 .
.venv/bin/python benchmarks/run.py --output output/benchmark-baseline \
  --sizes 1000,10000,100000
.venv/bin/python benchmarks/run.py --output output/benchmark-faults \
  --sizes 1000 --faults --interrupt
```

Each output directory must be new. The worker has a 2-CPU / 1-GiB / 128-process
limit. The fixture is on the host and is not included in those worker limits.
The artificial quota is 60,000 requests/minute to measure the implementation;
this is not a recommended Langfuse allowance. Normal deployment defaults to 30.
The fixture listens on an ephemeral host port reachable by Docker. It contains
only synthetic data; run this on an isolated test machine/network.

Saved outputs include wall time, HTTP request count, container peak memory,
CPU accounting, checkpoint disk size, exact identity counts, worker logs, and
whether rerunning the finished job made any requests. Peak memory is Linux cgroup
`memory.peak` for import/assessment, including child processes and charged cache;
it is not Python RSS and does not include the separate writeback container.
Wall time includes container startup, importing, assessment, and writeback; the
completed-job no-op check is outside that timing. Image pulls/builds are excluded.

The fault case injects 429 on observation and score reads, 503 on each write path,
a lost ingestion acknowledgement after persistence, and a forced container kill.
It also tries a competing owner of the same workspace and requires exit code 3.
The interruption/overlap case uses a Linux-owned Docker named volume, matching
the deployment configuration. Baseline and OSS-volume measurements use a host
bind mount for inspection. Docker Desktop shared macOS directories did not enforce
cross-container file locks in testing and are not supported for job ownership.
State exports in the interruption case are included in its wall time. Its memory
and CPU counters describe the resumed container lifetime, excluding the killed
segment; baseline measurements have no interruption.
No duplicate identities or changed bodies are accepted by the verifier.

## Include actual Witdem ingestion

```sh
docker compose -f benchmarks/oss.compose.yaml up -d
.venv/bin/python benchmarks/run.py --output output/benchmark-oss \
  --sizes 1000,10000 --receiver http://127.0.0.1:24339
.venv/bin/python benchmarks/verify_oss.py --output output/benchmark-oss
```

The fixture forwards to an isolated, unmodified Witdem OSS 0.2.11 deployment.
Its three service resources are separate from the integration worker. Acceptance
by the receiver is not proof of completed analytics: inspect the public dashboard
API at port 28522 and verify projected operation/evaluation counts separately.
The benchmark source and score writeback endpoint remain synthetic.

The test Compose volume retains data. Reusing it reuses stable identities; use a
fresh Compose project/volume for a clean ingestion measurement. Stop this isolated
stack after testing. Do not delete volumes belonging to an existing deployment.

## Render the figure

Install matplotlib in your reporting environment, then:

```sh
.venv/bin/python benchmarks/report.py output/benchmark-baseline/results.json \
  --output output/benchmark-baseline/benchmark.png
```

The chart is generated directly from saved measurements. Report hardware, image
identity, versions, workload shape, and sample count with any shared result. A
single run at each size is a characterization, not a statistical throughput SLA.
