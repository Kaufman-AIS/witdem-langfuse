# Headless deployment

[Documentation index](README.md) · [CUAD example](../examples/cuad-evaluations/README.md)

Run a finite backfill job on a Linux host with Docker. A job executes a reviewed
list of integration commands sequentially, retains checkpoints on a persistent
volume, and exits. It does not create recurring schedules or discover new jobs.

## Prepare and run

From a clean clone of this repository:

```sh
mkdir -p deploy/config
cp deploy/job.example.json deploy/config/job.json
cp examples/cuad-evaluations/contract.yaml deploy/config/contract.yaml
```

Edit `job.json` with your source, project, receiver, and fixed intervals. Supply
`deploy/config/bindings.yaml` using the [score-binding example](../examples/cuad-evaluations/README.md#the-yaml-bridge).
The score interval must include when evaluations were created, which can be later
than the trace. The first task imports observations in the interval; the second
assesses the explicitly selected execution. Additional selected executions can be
listed as additional tasks, each with its own assessment workspace.

Inject `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and optional `WITDEM_API_KEY`
through your shell or deployment secret manager, then:

```sh
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml up --abort-on-container-exit --exit-code-from worker
```

The container runs as UID 10001, with two CPUs, 1 GiB memory, and 128 processes.
It exposes no ports. `/config` is read-only; `/state` is persistent. Temporary
Duckle files use the container's disposable disk. The image installs a locked
Python dependency set and pins Duckle 0.7.2.

Only point it at endpoints reachable from the container. For a local Docker
Desktop demo, `host.docker.internal` reaches host-published ports; append
`--allow-http` to each command only for your intended HTTP test endpoints.

## Restart and ownership

Run the same Compose command again after an interruption. Finished tasks are
skipped. Unfinished tasks resume their own durable checkpoints; duplicate delivery
attempts retain the same semantic identities. This is at-least-once delivery,
not an exactly-once network guarantee.

Use the provided Docker named volume for state. Docker Desktop host-shared bind
mounts are not supported for concurrent ownership: our stress test found that
cross-container file locks were not enforced on the macOS shared filesystem.

A job-wide OS lock prevents a second process from owning the same state directory.
The manifest fingerprint is immutable for that directory. To change the range,
bindings, or task list, create a new job/state volume. Older assessment workspaces
created before compact score selection should also use a fresh workspace;
cross-version assessment-manifest compatibility is not asserted. Stop containers before
backing up the state volume; do not delete the volume to restart a job.

The worker forwards SIGTERM/SIGINT to its subprocess group. A hard kill releases
OS locks; the next process reads the persisted receipts. No background scheduler
or resident web console is installed.

## Pacing and failures

`WITDEM_REQUESTS_PER_MINUTE` defaults to 30. All HTTP requests made by the job
share a host-local budget per endpoint origin, including observation reads,
score reads, destination delivery, and score writeback. Choose an allowance below
your actual source/destination limits. Two origins have separate budgets; multiple
projects using one origin share its budget. This is deliberately conservative.

429, 5xx, and transport failures are retried up to `WITDEM_HTTP_ATTEMPTS` (default
five) per request, honoring `Retry-After` or exponential backoff. Existing backfill
and delivery checkpoints may defer further attempts. Authentication, configuration,
and schema failures terminate visibly; fix the cause and rerun the same manifest.

NDJSON logs report task start/completion, page counts, retries, and wait times.
Logs omit credentials, request bodies, and command arguments. Exit codes are 0
for a complete job, 1 for failure, 2 for an exhausted pass budget, and 3 for a busy
workspace. SIGTERM/SIGINT exit with 128 plus the signal number. The default pass
budget is 10,000 command invocations; `--max-passes` can lower it for bounded runs.

The process is a batch job: successful exit is the health signal. It does not
serve an HTTP health endpoint. Alert on nonzero exit and stalled progress using
your existing infrastructure.

## Memory and capacity

HTTP responses are bounded to 16 MiB. Observation pages are limited to 1,000 rows
and can be lowered with `--page-size`. Scores are persisted a page at a time and
scanned from disk; only matching evidence enters the assessment. Matching evidence
is limited to 1,000 scores and 8 MiB. Exceeding that bound fails explicitly instead
of silently truncating evidence or converting it to an unknown assessment.

Disk use grows with the retained score snapshot and receipts. Provision persistent
storage for the intended interval and keep completed jobs according to your data
retention policy. Increasing Duckle cores does not parallelize Python evidence
selection or eliminate API/destination limits.

This deployment is single-host. Do not horizontally replicate it over NFS and
assume SQLite/OS locks form a distributed job coordinator. Scheduling and
multi-machine partition ownership remain outside this release.

Duckle also offers [headless/server deployment](https://duckle.org/docs/automation.html)
and [batch workers](https://duckle.org/deploy.html). This adapter uses its own
Python coordinator for API pagination and delivery receipts, so running a pipeline
JSON alone does not run the complete integration. The integration pins 0.7.2
and continues to use its existing pipeline runner;
this upgrade does not switch to Duckle server or distributed batch workers.
