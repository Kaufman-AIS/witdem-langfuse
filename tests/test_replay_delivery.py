import copy
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from witdem_langfuse.replay import remap
from witdem_langfuse.replay_delivery import ReplayDelivery, duckle_page

PAGE = {
    "version": "1.0",
    "source": "application_records",
    "project_id": "p",
    "source_execution_id": "e",
    "source_trace_id": "t",
    "records": [
        {
            "version": "1.0",
            "kind": "outcome",
            "event_id": "one",
            "execution_id": "e",
            "name": "result",
            "value": False,
        }
    ],
}


class DeliveryTest(unittest.TestCase):
    def test_cooldown_resumes_after_acknowledged_prefix(self):
        now, sent = [1000], []
        page = copy.deepcopy(PAGE)
        page["records"].append({**page["records"][0], "event_id": "two"})

        def send(body):
            sent.append(body)
            if len(sent) == 2:
                response = httpx.Response(
                    429,
                    headers={"Retry-After": "10"},
                    request=httpx.Request("POST", "https://receiver/sdk/v1/records"),
                )
                response.raise_for_status()
            return {"event_id": json.loads(body)["event_id"], "status": "accepted"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "pages.jsonl"
            manifest.write_text(json.dumps(page) + "\n")

            def run():
                return ReplayDelivery(
                    manifest,
                    root / "state.sqlite",
                    "https://receiver",
                    send=send,
                    transform=remap,
                    clock=lambda: now[0],
                ).run()

            self.assertEqual(run()["acknowledged"], 1)
            self.assertEqual(run()["retry_at"], 1010)
            self.assertEqual(len(sent), 2)
            now[0] = 1010
            self.assertTrue(run()["complete"])
            self.assertEqual(sent[1], sent[2])
            self.assertNotEqual(sent[0], sent[2])

    def test_real_duckle(self):
        self.assertEqual(duckle_page(PAGE), remap(PAGE))

    def test_resume_after_lost_ack_and_completed_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "pages.jsonl"
            manifest.write_text(json.dumps(PAGE) + "\n")
            sent = []

            def send(body):
                sent.append(body)
                if len(sent) == 1:
                    raise TimeoutError("connection dropped after acceptance")
                return {"event_id": json.loads(body)["event_id"], "status": "accepted"}

            replay = ReplayDelivery(
                manifest,
                root / "state.sqlite",
                "https://receiver",
                send=send,
                transform=remap,
            )
            with self.assertRaises(TimeoutError):
                replay.run()
            self.assertTrue(replay.run()["complete"])
            self.assertEqual(sent[0], sent[1])
            replay.run()
            self.assertEqual(len(sent), 2)
            manifest.write_text(json.dumps(PAGE) + "\n\n")
            with self.assertRaises(ValueError):
                replay.run()

    def test_conflict_on_later_page_sends_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "pages.jsonl"
            second = copy.deepcopy(PAGE)
            second["records"][0]["value"] = True
            manifest.write_text(json.dumps(PAGE) + "\n" + json.dumps(second) + "\n")
            replay = ReplayDelivery(
                manifest,
                root / "state.sqlite",
                "https://receiver",
                send=lambda _: self.fail("sent before full validation"),
                transform=remap,
            )
            with self.assertRaisesRegex(ValueError, "conflicting"):
                replay.run()

    def test_identical_cross_page_duplicates_are_delivered_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "pages.jsonl"
            manifest.write_text((json.dumps(PAGE) + "\n") * 2)
            sent = []

            def send(body):
                sent.append(body)
                return {"event_id": json.loads(body)["event_id"], "status": "accepted"}

            result = ReplayDelivery(
                manifest,
                root / "state.sqlite",
                "https://receiver",
                send=send,
                transform=remap,
            ).run()
            self.assertEqual(result["records"], 1)
            self.assertEqual(len(sent), 1)
