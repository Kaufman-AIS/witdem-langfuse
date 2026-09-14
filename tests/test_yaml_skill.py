import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from test_evaluation_contracts import fixture

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "yaml_skill", ROOT / "skills/witdem-langfuse-yaml/scripts/validate_yaml.py"
)
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class YamlSkillTest(unittest.TestCase):
    def test_duplicate_keys_are_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "bindings.yaml"
            p.write_text("version: 1\nrequirements:\n  r: first\n  r: second\n")
            with self.assertRaisesRegex(ValueError, "duplicate YAML key"):
                HELPER.load_yaml(p)

    def test_offline_preview_preserves_status_and_saved_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = fixture()
            (root / "contract.yaml").write_text(yaml.safe_dump(page["contract"]))
            (root / "bindings.yaml").write_text(yaml.safe_dump(page["bindings"]))
            for case, expected in [
                ("pass", True),
                ("fail", False),
                ("missing", None),
                ("ambiguous", None),
                ("invalid", None),
            ]:
                sample = copy.deepcopy(page)
                if case == "fail":
                    sample["scores"][0]["value"] = 0.1
                elif case == "missing":
                    sample["scores"] = []
                elif case == "invalid":
                    sample["scores"][0]["value"] = float("nan")
                elif case == "ambiguous":
                    score = copy.deepcopy(sample["scores"][0])
                    score["id"] = "another-score"
                    sample["scores"].append(score)
                snapshot = root / "assessment.jsonl"
                original = json.dumps(sample)
                snapshot.write_text(original)
                with patch(
                    "socket.socket", side_effect=AssertionError("network forbidden")
                ):
                    result = HELPER.check(
                        root / "contract.yaml", root / "bindings.yaml", snapshot
                    )
                json.dumps(result, allow_nan=False)
                self.assertIs(result["preview"]["achieved"], expected)
                self.assertEqual(snapshot.read_text(), original)

    def test_unbound_requirement_fails_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = fixture()
            page["bindings"]["requirements"].pop("evidence_supported")
            (root / "contract.yaml").write_text(yaml.safe_dump(page["contract"]))
            (root / "bindings.yaml").write_text(yaml.safe_dump(page["bindings"]))
            with self.assertRaisesRegex(ValueError, "bind every declared requirement"):
                HELPER.check(root / "contract.yaml", root / "bindings.yaml")
