# Dependency updates with Renovate

[Documentation index](README.md) · [Configuration](../renovate.json)

Renovate proposes dependency updates as pull requests. Maintainers review and
merge them; automatic merging is disabled.

## Enable it

An organization administrator must install or configure the
[Renovate GitHub App](https://github.com/apps/renovate) and grant it access to
`Kaufman-AIS/witdem-langfuse`. Select this repository rather than all repositories
unless broader access is intended. Complete any onboarding PR presented by the
app. Committing the configuration alone does not install or activate the app.

After onboarding, check the repository's Dependency Dashboard issue for detected
packages, pending updates, and errors. No bot token or scheduled GitHub workflow
is needed when using the hosted app.

## What it maintains

- Python dependencies declared in `pyproject.toml`, including Duckle and the
  Witdem SDK, together with `deploy/requirements.lock`.
- Container image references in Dockerfiles and Compose files, including the
  version-pinned example and benchmark deployments.
- GitHub Actions references if workflows are added later.

The `pip-compile` manager reads the lockfile's generated command header and runs
`uv pip compile` with the original extras. It updates the source declarations and
regenerates the lock together. The standalone Python managers are omitted to
avoid duplicate PRs or independent edits to generated pins. Keep the command
header and explicit output path intact. Use `--extra=value` arguments as shown
below: Renovate's parser requires the equals sign. Python resolution targets 3.12.
The runtime lock intentionally excludes development and build tools; extraction
can warn that Python, setuptools, httpx2, and ruff have no corresponding lock pin.
They are still detected from the source declaration.
See [Renovate's manager documentation](https://docs.renovatebot.com/modules/manager/pip-compile/).

## Update policy

Routine PR creation is scheduled for Mondays before 06:00 Europe/Berlin, with at
most three open Renovate PRs and two new PRs per hour. Actual execution depends on
the hosted app's availability. Renovate's recommended preset enables the Dependency
Dashboard; vulnerability alerts may bypass the routine schedule and limits.

Minor and patch Python updates are grouped, except Duckle and `witdem-sdk`, which
receive individual review. Major updates remain separate. Weekly lock maintenance
refreshes transitive dependencies within the declared constraints. Every update,
including a pre-1.0 minor release, needs compatibility review.

## Review and verify

Inspect release notes and both the dependency declaration and generated lockfile.
From the repository root, the lock can be regenerated with:

```sh
uv pip compile pyproject.toml --extra=etl --extra=telemetry --extra=contracts --output-file=deploy/requirements.lock
uv pip install --python .venv/bin/python -e '.[etl,telemetry,contracts,dev]'
.venv/bin/python -m unittest discover -s tests -q
docker build -f deploy/Dockerfile -t witdem-langfuse-worker:dependency-check .
```

For Duckle or SDK changes, also run the existing container recovery check:

```sh
.venv/bin/python benchmarks/run.py --image witdem-langfuse-worker:dependency-check \
  --output output/dependency-check --sizes 1000 --faults --interrupt
```

Use a fresh output directory each time. Image updates affecting Witdem or Langfuse
need the connected example checks as well. Update compatibility notes only after
verification; retain the original version labels on historical benchmark results.
The [CI workflow](../.github/workflows/ci.yml) runs lint, formatting, tests, lockfile
consistency, and the synthetic container recovery check on pull requests and main.
Live-service example verification remains a maintainer check. Main requires both
`Python checks` and `Container recovery` to pass against an up-to-date branch,
and changes must go through a pull request. Direct pushes, force pushes, and
branch deletion are blocked, including for administrators. No approving review
is required, so a solo maintainer can merge after checks and conversations are
resolved.

Validate edits to Renovate's configuration before merging:

```sh
npx --yes --package renovate renovate-config-validator renovate.json
```

If PRs do not appear, first check app access and onboarding, then the Dependency
Dashboard and schedule. Do not manually edit generated lock pins to work around
a failed refresh; inspect the artifact error and regenerate from `pyproject.toml`.
