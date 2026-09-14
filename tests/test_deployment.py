import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import httpx

from witdem_langfuse.evaluations import ScoreSnapshot
from witdem_langfuse.http_policy import request
from witdem_langfuse.job import run


class DeploymentTest(unittest.TestCase):
    def test_retry_preserves_writeback_body_and_honors_nonretryable_errors(self):
        seen = []

        def transport(req):
            seen.append(req.content)
            return httpx.Response(
                429 if len(seen) == 1 else 200,
                headers={"Retry-After": "0"},
                json={"id": "stable"},
            )

        with (
            patch.dict(os.environ, {"WITDEM_HTTP_ATTEMPTS": "2"}),
            httpx.Client(transport=httpx.MockTransport(transport)) as http,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                request(
                    http, "POST", "https://example.com/scores", json={"id": "stable"}
                ).json(),
                {"id": "stable"},
            )
        self.assertEqual(seen, [b'{"id":"stable"}'] * 2)
        seen.clear()

        def forbidden(req):
            seen.append(1)
            return httpx.Response(403)

        with (
            patch.dict(os.environ, {"WITDEM_HTTP_ATTEMPTS": "2"}),
            httpx.Client(transport=httpx.MockTransport(forbidden)) as http,
            self.assertRaises(httpx.HTTPStatusError),
        ):
            request(http, "GET", "https://example.com")
        self.assertEqual(len(seen), 1)

    def test_compressed_response_is_not_decoded_twice(self):
        import gzip

        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                yield gzip.compress(b'{"data":[]}')

        with httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, headers={"Content-Encoding": "gzip"}, stream=Stream()
                )
            )
        ) as http:
            self.assertEqual(
                request(http, "GET", "https://example.com").json(), {"data": []}
            )

    def test_response_limit_applies_while_streaming(self):
        class Stream(httpx.SyncByteStream):
            def __iter__(self):
                yield b"12345"
                yield b"67890"
                raise AssertionError("body continued buffering beyond limit")

        with (
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(200, stream=Stream())
                )
            ) as http,
            self.assertRaisesRegex(ValueError, "byte limit"),
        ):
            request(http, "GET", "https://example.com", max_bytes=6)

    def test_scores_scan_large_snapshot_without_materializing_unrelated_rows(self):
        def fetch(cursor):
            start = int(cursor or 0)
            rows = [
                {
                    "id": str(i),
                    "projectId": "p",
                    "name": "target" if i == 5000 else "noise",
                    "source": "EVAL",
                    "subject": {"kind": "trace", "id": "t"},
                    "comment": "x" * 1000,
                }
                for i in range(start, start + 100)
            ]
            return {
                "data": rows,
                "meta": {"cursor": str(start + 100) if start < 9900 else None},
            }

        with tempfile.TemporaryDirectory() as directory:
            snap = ScoreSnapshot(
                Path(directory) / "scores.sqlite", {"project": "p"}, fetch
            )
            self.assertEqual(snap.run(10, load_scores=False), (False, 1000))
            self.assertEqual(snap.run(100, load_scores=False), (True, 10000))
            bindings = {
                "requirements": {
                    "r": {
                        "evaluation": "target",
                        "name": "target",
                        "source": "EVAL",
                        "subject": "trace",
                        "score_id": "5000",
                    }
                }
            }
            self.assertEqual(
                [r["id"] for r in snap.select_scores(bindings, "t")], ["5000"]
            )
            with self.assertRaisesRegex(ValueError, "assessment limit"):
                snap.select_scores(max_scores=10)
            snap.fetch = lambda _: self.fail("completed snapshot fetched source again")
            self.assertEqual(snap.run(load_scores=False), (True, 10000))

    def test_shared_origin_budget_applies_to_get_and_post(self):
        class Budget:
            groups: ClassVar[list[str]] = []
            claims = 0

            def __init__(self, path, group, **kwargs):
                self.groups.append(group)

            def acquire(self):
                Budget.claims += 1
                return 0

        with (
            patch.dict(os.environ, {"WITDEM_JOB_STATE": "/unused"}),
            patch("witdem_langfuse.http_policy.SharedBudget", Budget),
            httpx.Client(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))
            ) as http,
        ):
            request(http, "GET", "https://example.com/observations")
            request(http, "GET", "https://example.com/scores")
            request(http, "POST", "https://example.com/scores", json={"id": "stable"})
        self.assertEqual(Budget.groups, ["https://example.com"] * 3)
        self.assertEqual(Budget.claims, 3)

    def test_job_rejects_modified_manifest_before_running_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            manifest = root / "job.json"
            manifest.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "tasks": [
                            {"id": "read", "command": "backfill", "arguments": []}
                        ],
                    }
                )
            )
            (state / "job.json").write_text(
                json.dumps({"fingerprint": "different", "completed": []})
            )
            with self.assertRaisesRegex(ValueError, "different manifest"):
                run(manifest, state)
