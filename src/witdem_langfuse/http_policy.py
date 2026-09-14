"""Bounded HTTP bodies, host-local pacing, and retryable transport handling.

Set WITDEM_JOB_STATE to enable shared pacing across all integration commands.
No credentials, query strings, or response bodies are included in progress logs.
"""

import json
import os
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .quota import SharedBudget


def log(event, **fields):
    print(json.dumps({"event": event, **fields}, allow_nan=False), flush=True)


def cooldown(value, attempt, now):
    try:
        seconds = float(value)
        if 0 <= seconds < float("inf"):
            return seconds
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            pass
    return min(60, 2**attempt)


def request(client, method, url, *, max_bytes=16 * 1024 * 1024, **kwargs):
    """Retries preserve the exact caller-supplied body and deterministic IDs.

    Default standalone behavior is one attempt without pacing. The headless job
    enables pacing and retries through environment configuration. This function
    is also used for acknowledgements so response limits apply before buffering.
    """
    state = os.getenv("WITDEM_JOB_STATE")
    attempts = int(os.getenv("WITDEM_HTTP_ATTEMPTS", "5" if state else "1"))
    if not 1 <= attempts <= 20:
        raise ValueError("WITDEM_HTTP_ATTEMPTS must be between 1 and 20")
    absolute = str(client.base_url.join(url)) if not urlsplit(url).netloc else url
    parsed = urlsplit(absolute)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    budget = None
    if state:
        budget = SharedBudget(
            Path(state) / "http-quota.sqlite",
            origin,
            requests_per_minute=float(os.getenv("WITDEM_REQUESTS_PER_MINUTE", "30")),
        )
    for attempt in range(1, attempts + 1):
        if budget:
            while deadline := budget.acquire():
                wait = min(30, max(0, deadline - time.time()))
                log("http_wait", seconds=round(wait, 3))
                time.sleep(wait)
        try:
            with client.stream(method, url, **kwargs) as response:
                parts, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("HTTP response exceeds configured byte limit")
                    parts.append(chunk)
                buffered = httpx.Response(
                    response.status_code,
                    headers={
                        k: v
                        for k, v in response.headers.items()
                        if k.lower()
                        not in {
                            "content-encoding",
                            "content-length",
                            "transfer-encoding",
                        }
                    },
                    content=b"".join(parts),
                    request=response.request,
                )
            if buffered.status_code != 429 and buffered.status_code < 500:
                buffered.raise_for_status()
                return buffered
            delay = cooldown(buffered.headers.get("Retry-After"), attempt, time.time())
            if budget:
                budget.defer_until(time.time() + delay)
            buffered.raise_for_status()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and (
                exc.response.status_code != 429 and exc.response.status_code < 500
            ):
                raise
            if attempt == attempts:
                raise
            delay = cooldown(
                exc.response.headers.get("Retry-After")
                if isinstance(exc, httpx.HTTPStatusError)
                else None,
                attempt,
                time.time(),
            )
            log(
                "http_retry",
                attempt=attempt,
                status=(
                    exc.response.status_code
                    if isinstance(exc, httpx.HTTPStatusError)
                    else "transport"
                ),
                delay_seconds=delay,
            )
            if budget:
                budget.defer_until(time.time() + delay)
            else:
                time.sleep(delay)
    raise AssertionError("unreachable")
