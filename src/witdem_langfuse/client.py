"""Bounded Langfuse v2 HTTP client; durable scheduling lives in sync.py."""

import base64
import json
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to a redirected host.


class SourceError(RuntimeError):
    def __init__(self, status, retry_after=None):
        super().__init__(
            f"Langfuse HTTP {status}; source job must be handled by scheduler"
        )
        self.status = status
        self.retry_after = retry_after


class Client:
    def __init__(
        self,
        base_url,
        public_key,
        secret_key,
        project_id,
        *,
        transport=None,
        allow_http=False,
    ):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in (("https", "http") if allow_http else ("https",))
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "base_url must be an approved HTTPS URL without credentials/query/fragment"
            )
        self.base_url = base_url.rstrip("/")
        self.project_id = project_id
        self.auth = (
            "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        )
        self.transport = transport or self._get

    def _get(self, params):
        request = Request(
            self.base_url + "/api/public/v2/observations?" + urlencode(params),
            headers={"Authorization": self.auth, "Accept": "application/json"},
        )
        try:
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                body = response.read(16 * 1024 * 1024 + 1)
                if len(body) > 16 * 1024 * 1024:
                    raise ValueError("response exceeds 16 MiB; lower page limit")
                return json.loads(body)
        except HTTPError as exc:
            raise SourceError(exc.code, exc.headers.get("Retry-After")) from None

    def page(
        self,
        start,
        end,
        *,
        cursor=None,
        limit=100,
        target_kind="",
        target_id="",
        fields="core,basic,time",
    ):
        if not set(fields.split(",")) <= {
            "core",
            "basic",
            "time",
            "usage",
            "model",
            "metadata",
        }:
            raise ValueError("unsupported observation fields")
        a, b = (datetime.fromisoformat(v) for v in (start, end))
        if a.tzinfo is None or b.tzinfo is None or a >= b or not 1 <= limit <= 1000:
            raise ValueError("invalid bounded source query")
        params = {
            "fromStartTime": start,
            "toStartTime": end,
            "limit": limit,
            "fields": fields,
        }
        if target_kind:
            if (
                target_kind not in ("trace", "observation", "session")
                or not isinstance(target_id, str)
                or not 1 <= len(target_id) <= 512
            ):
                raise ValueError("invalid source target")
            if target_kind == "observation":
                # Advanced filters override ordinary query parameters, so include
                # both time bounds in that filter as well as the exact identifier.
                params["filter"] = json.dumps(
                    [
                        {
                            "type": "string",
                            "column": "id",
                            "operator": "=",
                            "value": target_id,
                        },
                        {
                            "type": "datetime",
                            "column": "startTime",
                            "operator": ">=",
                            "value": start,
                        },
                        {
                            "type": "datetime",
                            "column": "startTime",
                            "operator": "<",
                            "value": end,
                        },
                    ]
                )
            else:
                params["traceId" if target_kind == "trace" else "sessionId"] = target_id
        if cursor:
            params["cursor"] = cursor
        payload = self.transport(params)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("data"), list)
            or not isinstance(payload.get("meta"), dict)
        ):
            raise ValueError("invalid v2 response")
        if len(payload["data"]) > limit:
            raise ValueError("source exceeded requested row bound")
        for row in payload["data"]:
            if not isinstance(row, dict) or row.get("projectId") != self.project_id:
                raise ValueError("source project mismatch")
            if not all(
                isinstance(row.get(k), str) and row[k]
                for k in ("id", "traceId", "startTime")
            ):
                raise ValueError("missing observation identity")
            if target_kind:
                field = {
                    "trace": "traceId",
                    "observation": "id",
                    "session": "sessionId",
                }[target_kind]
                if row.get(field) != target_id:
                    raise ValueError("source returned a different target")
        next_cursor = payload["meta"].get("cursor")
        if next_cursor is not None and (
            not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor
        ):
            raise ValueError("invalid or repeated cursor")
        return payload["data"], next_cursor

    def pages(self, start, end, *, limit=100, max_pages=1000):
        if max_pages < 1:
            raise ValueError("invalid page budget")
        cursor, seen = None, set()
        for _ in range(max_pages):
            rows, next_cursor = self.page(start, end, cursor=cursor, limit=limit)
            if next_cursor is not None and next_cursor in seen:
                raise ValueError("repeated cursor")
            yield rows
            if next_cursor is None:
                return
            seen.add(next_cursor)
            cursor = next_cursor
        raise ValueError("page budget exhausted; window not complete")
