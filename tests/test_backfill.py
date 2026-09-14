import tempfile
import unittest
from pathlib import Path

import httpx
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

from witdem_langfuse.backfill import (
    Backfill,
    duckle_encode,
    encode,
    retry_delay,
    token_usage,
)
from witdem_langfuse.client import Client, SourceError
from witdem_langfuse.quota import SharedBudget

ROW = {
    "projectId": "p",
    "id": "child",
    "traceId": "trace",
    "parentObservationId": "parent",
    "startTime": "2026-09-01T00:00:01Z",
    "endTime": "2026-09-01T00:00:02Z",
    "type": "GENERATION",
    "model": "test-model",
    "inputUsage": 3,
    "metadata": {"product_goal_achieved": True},
    "input": "private text",
}


class BackfillTest(unittest.TestCase):
    def test_usage_units_and_exclusive_cache_buckets(self):
        self.assertEqual(
            token_usage(
                {
                    "type": "GENERATION",
                    "inputUsage": 279523,
                    "totalUsage": 279524,
                    "usageDetails": {
                        "input_bytes": 279523,
                        "ocr_pages": 1,
                        "total": 279524,
                    },
                }
            ),
            {},
        )
        self.assertEqual(
            token_usage(
                {
                    "type": "EMBEDDING",
                    "usageDetails": {
                        "input": 160,
                        "output": 0,
                        "input_items": 3,
                        "output_vectors": 3,
                        "vector_dimensions": 1024,
                    },
                }
            ),
            {"input_tokens": 160, "output_tokens": 0, "total_tokens": 160},
        )
        self.assertEqual(
            token_usage(
                {
                    "type": "GENERATION",
                    "usageDetails": {
                        "input": 100,
                        "input_cached_tokens": 50,
                        "output": 20,
                        "output_reasoning_tokens": 10,
                        "total": 180,
                    },
                }
            ),
            {"input_tokens": 150, "output_tokens": 30, "total_tokens": 180},
        )

    def test_source_cooldown_blocks_another_checkpoint(self):
        requests = []

        def get(params):
            requests.append(params)
            raise SourceError(429, "120")

        source = Client("https://example.test", "pk", "sk", "p", transport=get)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("first", "second"):
                budget = SharedBudget(root / "quota.sqlite", "org", clock=lambda: 1000)
                result = Backfill(
                    root / (name + ".sqlite"),
                    source,
                    "https://receiver.test",
                    "2026-09-01T00:00:00Z",
                    "2026-09-02T00:00:00Z",
                    send=lambda _: None,
                    transform=encode,
                    clock=lambda: 1000,
                    budget=budget,
                ).run()
                self.assertEqual(result["retry_at"], 1120)
            self.assertEqual(len(requests), 1)
            self.assertEqual(result["last_error"], "shared_source_budget")

    def test_receiver_cooldown_keeps_pending_page_without_refetch(self):
        now, fetched, delivered = [1000], [], []

        def get(params):
            fetched.append(params)
            return {"data": [ROW], "meta": {"cursor": None}}

        source = Client("https://example.test", "pk", "sk", "p", transport=get)

        def send(payload):
            delivered.append(payload)
            if len(delivered) == 1:
                response = httpx.Response(
                    503,
                    headers={"Retry-After": "10"},
                    request=httpx.Request("POST", "https://receiver.test/v1/traces"),
                )
                response.raise_for_status()

        with tempfile.TemporaryDirectory() as directory:
            backfill = Backfill(
                Path(directory) / "state.sqlite",
                source,
                "https://receiver.test",
                "2026-09-01T00:00:00Z",
                "2026-09-02T00:00:00Z",
                send=send,
                transform=encode,
                clock=lambda: now[0],
            )
            self.assertEqual(backfill.run()["retry_at"], 1010)
            backfill.run()
            self.assertEqual(len(delivered), 1)
            now[0] = 1010
            self.assertTrue(backfill.run()["complete"])
            self.assertEqual(len(fetched), 1)
            self.assertEqual(delivered[0], delivered[1])

    def test_rate_limit_survives_restart_without_requesting_early(self):
        now, calls = [1000], []

        def get(params):
            calls.append(params)
            if len(calls) == 1:
                raise SourceError(429, "120")
            return {"data": [], "meta": {"cursor": None}}

        source = Client("https://example.test", "pk", "sk", "p", transport=get)
        with tempfile.TemporaryDirectory() as directory:
            args = (
                Path(directory) / "state.sqlite",
                source,
                "https://receiver.test",
                "2026-09-01T00:00:00Z",
                "2026-09-02T00:00:00Z",
            )

            def run():
                return Backfill(
                    *args, send=lambda _: None, transform=encode, clock=lambda: now[0]
                ).run()

            self.assertEqual(run()["retry_at"], 1120)
            self.assertFalse(run()["complete"])
            self.assertEqual(len(calls), 1)
            now[0] = 1120
            self.assertTrue(run()["complete"])
            self.assertEqual(len(calls), 2)

    def test_expired_cursor_restarts_same_window_once(self):
        cursors = []

        def get(params):
            cursor = params.get("cursor")
            cursors.append(cursor)
            if cursor:
                raise SourceError(400)
            return {"data": [ROW], "meta": {"cursor": "expired"}}

        source = Client("https://example.test", "pk", "sk", "p", transport=get)
        with tempfile.TemporaryDirectory() as directory:
            backfill = Backfill(
                Path(directory) / "state.sqlite",
                source,
                "https://receiver.test",
                "2026-09-01T00:00:00Z",
                "2026-09-02T00:00:00Z",
                send=lambda _: None,
                transform=encode,
            )
            with self.assertRaises(SourceError):
                backfill.run(10)
            self.assertEqual(cursors, [None, "expired", None, "expired"])
            with self.assertRaises(SourceError):
                backfill.run(10)
            self.assertEqual(cursors[-1], "expired")

    def test_retry_after_dates_and_invalid_values(self):
        self.assertEqual(retry_delay("Thu, 01 Jan 1970 00:20:00 GMT", 1000, 1), 200)
        self.assertEqual(retry_delay("NaN", 1000, 3), 8)
        self.assertEqual(retry_delay("-1", 1000, 2), 4)
        self.assertEqual(retry_delay("bad header", 1000, 20), 256)

    def test_duckle_projection_is_stable_and_drops_business_claims(self):
        payload = duckle_encode([ROW], "p")
        self.assertEqual(payload, encode([ROW], "p"))
        span = (
            ExportTraceServiceRequest.FromString(payload)
            .resource_spans[0]
            .scope_spans[0]
            .spans[0]
        )
        attributes = {item.key for item in span.attributes}
        self.assertIn("langfuse.trace.id", attributes)
        self.assertNotIn("product_goal_achieved", attributes)
        self.assertNotIn(b"private text", payload)
        self.assertEqual(len(span.trace_id), 16)
        self.assertNotEqual(payload, encode([{**ROW, "projectId": "other"}], "other"))

    def test_ambiguous_delivery_replays_saved_bytes_before_fetching_next_page(self):
        calls, sent = [], []

        def get(params):
            calls.append(params)
            return {"data": [ROW], "meta": {"cursor": None}}

        source = Client("https://example.test", "pk", "sk", "p", transport=get)

        def fail(payload):
            sent.append(payload)
            raise TimeoutError("acknowledgement lost")

        with tempfile.TemporaryDirectory() as directory:
            args = (
                Path(directory) / "state.sqlite",
                source,
                "http://localhost:4318",
                "2026-09-01T00:00:00Z",
                "2026-09-02T00:00:00Z",
            )
            with self.assertRaises(TimeoutError):
                Backfill(*args, send=fail, transform=encode).run()
            result = Backfill(*args, send=sent.append, transform=encode).run()
            self.assertTrue(result["complete"])
            self.assertEqual(result["rows"], 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(sent[0], sent[1])
            Backfill(*args, send=sent.append, transform=encode).run()
            self.assertEqual(len(sent), 2)
            with self.assertRaises(ValueError):
                Backfill(
                    *args[:-1],
                    "2026-09-03T00:00:00Z",
                    send=sent.append,
                    transform=encode,
                ).run()

    def test_daily_windows_and_page_budget_resume(self):
        calls = []

        def get(params):
            calls.append(params)
            return {"data": [], "meta": {"cursor": None}}

        source = Client("https://example.test", "pk", "sk", "p", transport=get)
        with tempfile.TemporaryDirectory() as directory:
            backfill = Backfill(
                Path(directory) / "state.sqlite",
                source,
                "http://localhost:4318",
                "2026-09-01T00:00:00Z",
                "2026-09-03T12:00:00Z",
                send=lambda _: self.fail("empty page delivered"),
                transform=encode,
            )
            self.assertFalse(backfill.run(1)["complete"])
            self.assertTrue(backfill.run(2)["complete"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["toStartTime"], calls[1]["fromStartTime"])
        self.assertEqual(calls[1]["toStartTime"], calls[2]["fromStartTime"])
