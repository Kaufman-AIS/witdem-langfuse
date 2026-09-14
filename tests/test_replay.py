import copy
import unittest

from witdem_langfuse.backfill import identity
from witdem_langfuse.replay import remap

PAGE = {
    "version": "1.0",
    "source": "application_records",
    "project_id": "project",
    "source_execution_id": "execution",
    "source_trace_id": "trace",
    "records": [
        {
            "version": "1.0",
            "kind": "outcome",
            "event_id": "event",
            "execution_id": "execution",
            "trace_id": "trace",
            "span_id": "span",
            "name": "product_goal",
            "value": False,
            "attributes": {
                "contract_version": "3",
                "failed_requirement_ids": ["refund_check"],
            },
        }
    ],
}


class ReplayTest(unittest.TestCase):
    def test_preserves_explicit_false_and_bindings_are_stable(self):
        before = copy.deepcopy(PAGE)
        row = remap(PAGE)[0]
        self.assertIs(row["value"], False)
        self.assertEqual(row["attributes"], PAGE["records"][0]["attributes"])
        self.assertEqual(
            row["trace_id"], identity("project", "trace", "trace", 16).hex()
        )
        self.assertEqual(row["execution_id"], row["trace_id"])
        self.assertEqual(row["span_id"], identity("project", "trace", "span", 8).hex())
        self.assertEqual(row, remap(PAGE)[0])
        self.assertEqual(PAGE, before)

    def test_rejects_wrong_execution_trace_and_observation_metadata(self):
        for field in ("execution_id", "trace_id"):
            document = copy.deepcopy(PAGE)
            document["records"][0][field] = "unrelated"
            with self.assertRaises(ValueError):
                remap(document)
        with self.assertRaises(ValueError):
            remap({**PAGE, "source": "langfuse_metadata"})

    def test_duplicate_ids_and_unsupported_versions_fail(self):
        with self.assertRaises(ValueError):
            remap({**PAGE, "records": PAGE["records"] * 2})
        with self.assertRaises(ValueError):
            remap({**PAGE, "version": "2.0"})
