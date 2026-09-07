#!/usr/bin/env python3
"""Create the reproducible project completion denominator from the reference sources."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ITEM_RE = re.compile(r"^\s*(?:-\s+|\d+\.\s+)(.+?)\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("master_spec", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--audit-complete", action="store_true")
    parser.add_argument("--verified-criteria", type=Path)
    parser.add_argument(
        "--scope-overrides", type=Path,
        default=Path(__file__).resolve().parents[1] / "docs/progress/scope-overrides.json",
    )
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    criteria: list[dict] = []

    tests = [
        (record["path"], case)
        for record in manifest["files"]
        for case in record["test_cases"]
    ]
    for index, (path, case) in enumerate(tests, 1):
        criteria.append(
            {
                "id": f"LEGACY-TEST-{index:04d}",
                "kind": "legacy_regression",
                "source": f"{path}:{case['line']}",
                "requirement": case["name"],
                "status": "pending",
            }
        )

    interactions = [
        (record["path"], item)
        for record in manifest["files"]
        for item in record["streamlit_interactions"]
    ]
    for index, (path, item) in enumerate(interactions, 1):
        criteria.append(
            {
                "id": f"LEGACY-UI-{index:04d}",
                "kind": "legacy_interaction",
                "source": f"{path}:{item['line']}",
                "requirement": f"{item['kind']}: {item['label_expression']}",
                "status": "pending",
            }
        )

    headings: list[str] = []
    spec_items = []
    for line_number, line in enumerate(
        args.master_spec.read_text(encoding="utf-8-sig").splitlines(), 1
    ):
        heading_match = HEADING_RE.match(line)
        if heading_match:
            depth = len(heading_match.group(1))
            headings = headings[: depth - 1]
            headings.append(heading_match.group(2))
            continue
        item_match = ITEM_RE.match(line)
        if item_match:
            spec_items.append((line_number, " > ".join(headings), item_match.group(1)))

    for index, (line_number, section, requirement) in enumerate(spec_items, 1):
        accepted = args.audit_complete and (
            "FASE 0 — AUDIT DEL REPOSITORY" in section
            or section.startswith("45. DOCUMENTI CHE CODEX DEVE MANTENERE AGGIORNATI")
        )
        criteria.append(
            {
                "id": f"MASTER-{index:04d}",
                "kind": "master_spec",
                "source": f"MARKETPLACE_HUB_MASTER_SPEC_CODEX.md:{line_number}",
                "section": section,
                "requirement": requirement,
                "status": "verified" if accepted else "pending",
                "evidence": "docs/CURRENT_STATE_AUDIT.md" if accepted else "",
            }
        )

    verified_by_id: dict[str, dict] = {}
    if args.verified_criteria:
        ledger = json.loads(args.verified_criteria.read_text(encoding="utf-8"))
        verified_by_id = {item["id"]: item for item in ledger["criteria"]}

    known_ids = {item["id"] for item in criteria}
    unknown_ids = sorted(set(verified_by_id) - known_ids)
    if unknown_ids:
        raise SystemExit(f"Unknown acceptance criteria: {', '.join(unknown_ids)}")

    for item in criteria:
        verified_item = verified_by_id.get(item["id"])
        if verified_item:
            item["status"] = "verified"
            item["block"] = verified_item["block"]
            item["evidence"] = verified_item["evidence"]

    # Explicit user decisions amend the SaaS scope, never the frozen source baseline.
    scope = json.loads(args.scope_overrides.read_text(encoding="utf-8"))
    excluded = {item["id"]: item for item in scope["excluded"]}
    amended = {item["id"]: item for item in scope["amended"]}
    unknown_scope = (set(excluded) | set(amended)) - known_ids
    if unknown_scope or set(excluded) & set(amended) or set(excluded) & set(verified_by_id):
        raise SystemExit("Invalid, overlapping or still-verified scope override IDs")
    for item in criteria:
        if item["id"] in excluded:
            item["status"] = "excluded"
            item["scope_reason"] = excluded[item["id"]]["reason"]
            item["scope_evidence"] = scope["evidence"]
        elif item["id"] in amended:
            item["effective_requirement"] = amended[item["id"]]["effective_requirement"]
            item["scope_evidence"] = scope["evidence"]

    verified = sum(item["status"] == "verified" for item in criteria)
    active_total = len(criteria) - len(excluded)
    output = {
        "method": {
            "description": (
                "One criterion for every original executable regression test, "
                "Streamlit interaction, and numbered/bulleted Master Spec obligation."
            ),
            "metric": "verified criteria / active criteria (baseline minus explicit exclusions)",
            "warning": "This is acceptance coverage, not an estimate of hours remaining.",
        },
        "source_counts": {
            "legacy_regression": len(tests),
            "legacy_interaction": len(interactions),
            "master_spec": len(spec_items),
        },
        "summary": {
            "verified": verified,
            "baseline_total": len(criteria),
            "excluded": len(excluded),
            "total": active_total,
            "percent": round(verified * 100 / active_total, 2),
        },
        "criteria": criteria,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
