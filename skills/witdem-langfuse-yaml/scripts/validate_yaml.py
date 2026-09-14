"""Validate contract/binding YAML and optionally preview a saved assessment offline."""

import argparse
import json
import math
import sys
from pathlib import Path

import yaml

from witdem_langfuse.evaluations import assess, validate


class UniqueLoader(yaml.SafeLoader):
    pass


def mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(
                f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}"
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def load_yaml(path):
    data = yaml.load(Path(path).read_text(), Loader=UniqueLoader)
    if not isinstance(data, dict):
        raise TypeError("YAML document must be a mapping")
    return data


def check(contract_path, bindings_path, snapshot_path=None):
    contract, bindings = load_yaml(contract_path), load_yaml(bindings_path)
    spec = validate(contract, bindings)
    result = {
        "valid": True,
        "goal": spec.goal.name,
        "requirements": len(spec.goal.requirements),
    }
    if snapshot_path:
        page = json.loads(Path(snapshot_path).read_text())
        page.update(contract=contract, bindings=bindings)
        assessment = assess(page)
        result["preview"] = {
            "achieved": assessment["achieved"],
            "requirements": [
                {
                    key: row[key]
                    for key in ("requirement_id", "passed", "reason", "value", "target")
                }
                for row in assessment["requirements"]
            ],
        }
    for row in result.get("preview", {}).get("requirements", []):
        if isinstance(row["value"], float) and not math.isfinite(row["value"]):
            row["value"] = None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--bindings", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args()
    try:
        result = check(args.contract, args.bindings, args.snapshot)
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
