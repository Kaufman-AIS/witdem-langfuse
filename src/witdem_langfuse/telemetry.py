"""Export existing application OTel spans to Langfuse without reinstrumenting.

Witdem's existing SDK remains responsible for its execution lifecycle and
explicit business reports. This adapter only adds an engineering trace sink.
"""

from __future__ import annotations

import base64
import os
import threading
from urllib.parse import urlsplit

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_lock = threading.Lock()
_configured: dict[TracerProvider, tuple[str, str, str]] = {}


def configure_langfuse(
    *,
    base_url: str,
    public_key: str,
    secret_key: str,
    service_name: str,
    provider: TracerProvider | None = None,
) -> TracerProvider:
    """Add one Langfuse sink, leaving any existing Witdem sink intact.

    Configure before starting application work. Repeated identical calls are
    safe; switching projects on the same provider requires a new process.
    Credentials are explicit exporter headers, never global OTel environment
    variables (which could redirect or authenticate Witdem's exporter wrongly).
    """
    parsed = urlsplit(base_url)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or (
            parsed.scheme != "https"
            and not (
                parsed.scheme == "http"
                and parsed.hostname in ("localhost", "127.0.0.1", "::1")
            )
        )
    ):
        raise ValueError(
            "Langfuse base URL must be an HTTPS origin or local HTTP origin"
        )
    if not public_key or not secret_key or ":" in public_key:
        raise ValueError("Langfuse public and secret keys are required")
    config = (base_url.rstrip("/"), public_key, secret_key)
    with _lock:
        selected = provider if provider is not None else trace.get_tracer_provider()
        if isinstance(selected, trace.ProxyTracerProvider):
            selected = TracerProvider(
                resource=Resource.create({"service.name": service_name})
            )
            trace.set_tracer_provider(selected)
        if not isinstance(selected, TracerProvider):
            raise TypeError("Langfuse requires an OpenTelemetry SDK TracerProvider")
        previous = _configured.get(selected)
        if previous is not None:
            if previous != config:
                raise ValueError(
                    "Langfuse is already configured for this tracer provider"
                )
            return selected
        authorization = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        exporter = OTLPSpanExporter(
            endpoint=f"{config[0]}/api/public/otel/v1/traces",
            headers={
                "Authorization": f"Basic {authorization}",
                "x-langfuse-ingestion-version": "4",
            },
            timeout=10,
        )
        selected.add_span_processor(BatchSpanProcessor(exporter))
        _configured[selected] = config
        return selected


def configure_langfuse_from_env(*, service_name: str) -> TracerProvider:
    """Explicit opt-in entry point; incomplete configuration fails visibly."""
    return configure_langfuse(
        base_url=os.environ.get("LANGFUSE_BASE_URL", ""),
        public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
        secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
        service_name=service_name,
    )
