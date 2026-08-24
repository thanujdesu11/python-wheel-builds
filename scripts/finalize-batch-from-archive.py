#!/usr/bin/env python3
"""Verify scale-test results from KubeArchive after cluster pruning.

Completed PipelineRuns disappear from `oc get` within minutes. This script
paginates `kubectl ka get pipelineruns --archived` (the --count flag is broken)
and merges live poll metrics with authoritative archived success counts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True, help="konflux-perfscale/load-test label value")
    parser.add_argument("--namespace", default="konflux-perfscale")
    parser.add_argument("--expected", type=int, required=True)
    parser.add_argument("--metrics-json", help="Live metrics JSON from collect-plr-batch-metrics.py")
    parser.add_argument("--tsv", action="store_true", help="Print spreadsheet row (needs --metrics-json)")
    parser.add_argument("--output", help="Write combined JSON summary here")
    return parser.parse_args()


def fetch_archived(namespace: str, batch: str) -> list[dict]:
    """Page through archived PLRs 100 at a time using --before cursor."""
    selector = f"konflux-perfscale/load-test={batch}"
    before = None
    all_items: list[dict] = []

    while True:
        cmd = [
            "kubectl", "ka", "get", "pipelineruns",
            "-n", namespace,
            "-l", selector,
            "--archived",
            "-o", "json",
            "--limit", "100",
        ]
        if before:
            cmd.extend(["--before", before])

        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
        items = json.loads(proc.stdout).get("items", [])
        if not items:
            break

        all_items.extend(items)
        if len(items) < 100:
            break

        # Next page: items are ordered newest-first; --before walks backward in time.
        before = items[-1]["metadata"]["creationTimestamp"]

    return all_items


def count_outcomes(items: list[dict]) -> dict[str, int]:
    succeeded = failed = unknown = 0
    for item in items:
        matched = False
        for cond in item.get("status", {}).get("conditions", []):
            if cond.get("type") != "Succeeded":
                continue
            matched = True
            if cond.get("status") == "True":
                succeeded += 1
            elif cond.get("status") == "False":
                failed += 1
            else:
                unknown += 1
            break
        if not matched:
            unknown += 1

    return {
        "archived_total": len(items),
        "succeeded": succeeded,
        "failed": failed,
        "unknown": unknown,
    }


def spreadsheet_row(metrics: dict, archive: dict, expected: int) -> str:
    """Spreadsheet row: concurrency from live poll, success from archive."""
    return "\t".join(
        str(v)
        for v in [
            1,  # NSs
            expected,
            expected,
            metrics.get("created", expected),
            metrics.get("failed_creation", 0),
            metrics.get("collected", archive["archived_total"]),
            metrics.get("failed_collection", 0),
            metrics["pending"]["avg"],
            metrics["pending"]["p99"],
            metrics["pending"]["max"],
            metrics["running"]["avg"],
            metrics["running"]["p99"],
            metrics["running"]["max"],
            metrics["total"]["avg"],
            metrics["total"]["p99"],
            metrics["total"]["max"],
            len(metrics["total"]["data"]),
            archive["archived_total"],
            archive["succeeded"],
        ]
    )


def main() -> int:
    args = parse_args()
    archive = count_outcomes(fetch_archived(args.namespace, args.batch))
    archive["batch"] = args.batch
    archive["namespace"] = args.namespace
    archive["expected"] = args.expected
    archive["missing_from_archive"] = max(args.expected - archive["archived_total"], 0)

    live = {}
    if args.metrics_json:
        with open(args.metrics_json, encoding="utf-8") as handle:
            live = json.load(handle)
        archive["live_metrics"] = {
            "exit_reason": live.get("exit_reason"),
            "seen_terminal": live.get("seen_terminal"),
            "seen_succeeded_true": live.get("seen_succeeded_true"),
            "final_collected": live.get("final_collected"),
        }

    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(archive, handle, indent=2)
        print(f"Wrote {args.output}", file=sys.stderr)

    print(json.dumps(archive, indent=2))

    if args.tsv:
        if not live:
            print("ERROR: --tsv requires --metrics-json", file=sys.stderr)
            return 1
        print(spreadsheet_row(live, archive, args.expected))

    if archive["archived_total"] < args.expected:
        print(
            f"WARNING: only {archive['archived_total']}/{args.expected} PLRs in archive "
            "(wait a few minutes and re-run if the batch just finished)",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
