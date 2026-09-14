"""Compatibility proof for the shared OTel provider; no remote credentials."""

import unittest
from unittest.mock import patch

try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from witdem_langfuse.telemetry import configure_langfuse
except ImportError as exc:
    raise unittest.SkipTest(
        "Install the telemetry extra to test the OTel adapter"
    ) from exc


class TelemetryTest(unittest.TestCase):
    def test_shared_identifiers_and_no_duplicate_export(self):
        provider = TracerProvider()
        existing, langfuse = InMemorySpanExporter(), InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(existing))
        config = {
            "base_url": "http://127.0.0.1:3000",
            "public_key": "pk-test",
            "secret_key": "sk-test",
            "service_name": "cuad",
            "provider": provider,
        }
        with patch(
            "witdem_langfuse.telemetry.OTLPSpanExporter", return_value=langfuse
        ) as factory:
            self.assertIs(configure_langfuse(**config), provider)
            configure_langfuse(**config)
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(
                factory.call_args.kwargs["headers"]["x-langfuse-ingestion-version"], "4"
            )
            with self.assertRaises(ValueError):
                configure_langfuse(**{**config, "public_key": "other"})
        with (
            provider.get_tracer("haystack").start_as_current_span("review"),
            provider.get_tracer("haystack").start_as_current_span("component"),
        ):
            pass
        provider.force_flush()
        left, right = existing.get_finished_spans(), langfuse.get_finished_spans()
        self.assertEqual(len(left), 2)
        self.assertEqual(
            [(s.context.trace_id, s.context.span_id) for s in left],
            [(s.context.trace_id, s.context.span_id) for s in right],
        )
        self.assertEqual(left[0].parent.span_id, left[1].context.span_id)
        provider.shutdown()

    def test_reject_credentials_in_url_or_remote_http(self):
        for url in [
            "http://example.com",
            "https://user:pass@example.com",
            "https://example.com?secret=value",
            "https://example.com/path",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                configure_langfuse(
                    base_url=url, public_key="pk", secret_key="sk", service_name="cuad"
                )
