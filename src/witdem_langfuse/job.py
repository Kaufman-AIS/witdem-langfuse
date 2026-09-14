"""Run a finite manifest sequentially; leave recurring scheduling to the operator."""

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .http_policy import log


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    command: Literal["backfill", "evaluations", "writeback", "replay_delivery"]
    arguments: list[str]


class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    tasks: list[Task] = Field(min_length=1, max_length=1000)


def run(manifest, state, *, max_passes=10000):
    """One job owner; exact configuration and finished tasks survive restarts.

    Per-task commands own their existing durable checkpoints. An ambiguous HTTP
    acknowledgement is retried with the same semantic identities.
    """
    state.mkdir(parents=True, exist_ok=True)
    job = Job.model_validate_json(manifest.read_text())
    if len({t.id for t in job.tasks}) != len(job.tasks):
        raise ValueError("task IDs must be unique")
    encoded = job.model_dump_json()
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    with (state / "job.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt = state / "job.json"
        progress = (
            json.loads(receipt.read_text())
            if receipt.exists()
            else {"fingerprint": fingerprint, "completed": []}
        )
        if progress["fingerprint"] != fingerprint:
            raise ValueError("job state belongs to a different manifest")

        def save():
            temporary = state / "job.json.tmp"
            with temporary.open("w") as f:
                json.dump(progress, f)
                f.flush()
                os.fsync(f.fileno())
            temporary.replace(receipt)

        save()
        env = {
            **os.environ,
            "WITDEM_JOB_STATE": str(state.resolve()),
            "PYTHONUNBUFFERED": "1",
        }
        started = time.monotonic()
        for task in job.tasks:
            if task.id in progress["completed"]:
                log("task_skipped", task=task.id)
                continue
            log("task_started", task=task.id, command=task.command)
            for iteration in range(max_passes):
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "witdem_langfuse." + task.command,
                        *task.arguments,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    env=env,
                    start_new_session=True,
                )

                def terminate(signum, _frame, child=child, task_id=task.id):
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
                    log("job_interrupted", task=task_id)
                    raise SystemExit(128 + signum)

                previous = {
                    sig: signal.signal(sig, terminate)
                    for sig in (signal.SIGTERM, signal.SIGINT)
                }
                last = {}
                try:
                    for line in child.stdout:
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue
                        # Commands emit summaries; never echo command arguments or source bodies.
                        allowed = {
                            k: v
                            for k, v in item.items()
                            if k
                            in (
                                "event",
                                "complete",
                                "rows",
                                "pages",
                                "scores",
                                "published",
                                "retry_at",
                                "last_error",
                                "seconds",
                                "attempt",
                                "status",
                                "delay_seconds",
                            )
                        }
                        log("task_progress", task=task.id, progress=allowed)
                        last = item
                    returncode = child.wait()
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig, handler)
                if returncode:
                    log("task_failed", task=task.id, exit_code=returncode)
                    return 1
                if last.get("complete") or (
                    task.command == "writeback" and "published" in last
                ):
                    progress["completed"].append(task.id)
                    save()
                    log("task_completed", task=task.id)
                    break
                delay = min(30, max(0.05, last.get("retry_at", 0) - time.time()))
                time.sleep(delay)
            else:
                log("job_incomplete", task=task.id, reason="pass_budget")
                return 2
        metrics = {}
        peak = Path("/sys/fs/cgroup/memory.peak")
        if peak.exists():
            metrics["container_peak_memory_bytes"] = int(peak.read_text())
        cpu = Path("/sys/fs/cgroup/cpu.stat")
        if cpu.exists():
            metrics["container_cpu"] = dict(
                line.split() for line in cpu.read_text().splitlines()
            )
        log(
            "job_completed",
            tasks=len(job.tasks),
            elapsed_seconds=round(time.monotonic() - started, 3),
            **metrics,
        )
        return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path("/state"))
    parser.add_argument("--max-passes", type=int, default=10000)
    args = parser.parse_args()
    if args.max_passes < 1:
        parser.error("max passes must be positive")
    try:
        code = run(args.manifest, args.state, max_passes=args.max_passes)
    except BlockingIOError:
        log("job_busy")
        code = 3
    except (OSError, ValueError, RuntimeError) as exc:
        log("job_failed", error_type=type(exc).__name__)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
