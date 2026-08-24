#!/usr/bin/env python3
"""Poll PipelineRun concurrency for a scale-test batch.

Used by run-python-wheels-parallel.sh. Each poll records pending/running/total
concurrency samples for spreadsheet stats (avg, p99, max).

Completed PLRs are pruned from the cluster ~5 minutes after finish, so live
`oc get` counts drop over time. We track unique PLR names ever seen terminal
or succeeded (seen_terminal / seen_succeeded) so success counts survive pruning.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

POLL_INTERVAL = 5.0  # seconds between oc get polls
SNAPSHOT_EVERY = 1  # rewrite metrics files every poll (crash-safe partial output)
IDLE_EXIT = 3  # consecutive idle polls before exit when queue is drained


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="konflux-perfscale")
    parser.add_argument("--batch", required=True, help="konflux-perfscale/load-test label value")
    parser.add_argument("--expected", type=int, required=True, help="PLRs requested for this step")
    parser.add_argument("--created", type=int, help="PLRs successfully created (default: expected)")
    parser.add_argument("--failed-creation", type=int, default=0)
    parser.add_argument("--timeout", default="30m", help="Max poll loop duration, e.g. 2h or 90m")
    parser.add_argument(
        "--idle-exit",
        type=int,
        default=IDLE_EXIT,
        help="Exit after N consecutive polls with pending=0 and running=0",
    )
    parser.add_argument("--output", help="Write full JSON metrics here")
    parser.add_argument("--tsv-output", help="Write one spreadsheet TSV row here")
    parser.add_argument("--quiet", action="store_true", help="Do not print JSON to stdout")
    return parser.parse_args()


def parse_timeout(timeout: str) -> float:
    """Convert Tekton-style durations (30m, 2h, 90s) to seconds."""
    timeout = timeout.strip().lower()
    if timeout.endswith("m"):
        return float(timeout[:-1]) * 60
    if timeout.endswith("h"):
        return float(timeout[:-1]) * 3600
    if timeout.endswith("s"):
        return float(timeout[:-1])
    return float(timeout)


def percentile(values: list[int], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (pct / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def stats(values: list[int]) -> dict:
    return {
        "avg": round(sum(values) / len(values), 2) if values else 0.0,
        "p99": round(percentile(values, 99), 2),
        "max": max(values) if values else 0,
        "data": values,
    }


def fetch_pipelineruns(namespace: str, batch: str) -> list[dict]:
    proc = subprocess.run(
        [
            "oc", "-n", namespace, "get", "pipelinerun",
            "-l", f"konflux-perfscale/load-test={batch}",
            "-o", "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout).get("items", [])


def classify_pipelinerun(pr: dict) -> str:
    """Bucket a PLR into pending, running, or terminal for concurrency counts."""
    status = pr.get("status") or {}
    if status.get("completionTime"):
        return "terminal"

    for cond in status.get("conditions") or []:
        if (cond.get("reason") or "").lower() in {"succeeded", "failed", "cancelled", "completed"}:
            return "terminal"

    if status.get("startTime"):
        return "running"
    return "pending"


def succeeded_true(pr: dict) -> bool:
    for cond in pr.get("status", {}).get("conditions") or []:
        if cond.get("type") == "Succeeded" and cond.get("status") == "True":
            return True
    return False


def count_states(items: list[dict]) -> dict[str, int]:
    counts = {"pending": 0, "running": 0, "terminal": 0}
    for pr in items:
        counts[classify_pipelinerun(pr)] += 1
    counts["total"] = counts["pending"] + counts["running"]
    return counts


def record_seen_states(
    items: list[dict],
    *,
    peak_collected: int,
    seen_terminal: set[str],
    seen_succeeded: set[str],
) -> int:
    """Remember PLR names we have ever seen finish; counts survive later pruning."""
    peak_collected = max(peak_collected, len(items))
    for pr in items:
        name = pr.get("metadata", {}).get("name")
        if not name:
            continue
        if classify_pipelinerun(pr) == "terminal":
            seen_terminal.add(name)
        if succeeded_true(pr):
            seen_succeeded.add(name)
    return peak_collected


def build_metrics(
    *,
    namespace: str,
    batch: str,
    expected: int,
    created: int,
    failed_creation: int,
    pending_samples: list[int],
    running_samples: list[int],
    total_samples: list[int],
    final_items: list[dict],
    peak_collected: int,
    seen_terminal: set[str],
    seen_succeeded: set[str],
    exit_reason: str,
) -> dict:
    return {
        "namespace": namespace,
        "batch": batch,
        "expected": expected,
        "created": created,
        "failed_creation": failed_creation,
        "collected": peak_collected,
        "failed_collection": max(expected - peak_collected, 0),
        "final_collected": len(final_items),
        "final_terminal": sum(1 for pr in final_items if classify_pipelinerun(pr) == "terminal"),
        "final_succeeded_true": sum(1 for pr in final_items if succeeded_true(pr)),
        "seen_terminal": len(seen_terminal),
        "seen_succeeded_true": len(seen_succeeded),
        "exit_reason": exit_reason,
        "pending": stats(pending_samples),
        "running": stats(running_samples),
        "total": stats(total_samples),
        "Succeeded": {
            "total": len(seen_terminal),
            "True": len(seen_succeeded),
        },
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }


def format_tsv_row(metrics: dict) -> str:
    """One row for the KONFLUX-15500 scale-test spreadsheet."""
    return "\t".join(
        str(v)
        for v in [
            metrics["expected"],  # NSs=1, PLRs per NS
            metrics["expected"],
            metrics["created"],
            metrics["failed_creation"],
            metrics["collected"],
            metrics["failed_collection"],
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
            metrics["Succeeded"]["total"],
            metrics["Succeeded"]["True"],
        ]
    )


def write_snapshot(metrics: dict, *, output: str | None, tsv_output: str | None) -> None:
    if output:
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(metrics, handle, indent=2)
    if tsv_output:
        with open(tsv_output, "w", encoding="utf-8") as handle:
            handle.write(format_tsv_row(metrics))
            handle.write("\n")


def main() -> int:
    args = parse_args()
    created = args.created if args.created is not None else args.expected
    deadline = time.time() + parse_timeout(args.timeout)

    pending_samples: list[int] = []
    running_samples: list[int] = []
    total_samples: list[int] = []
    peak_collected = 0
    seen_terminal: set[str] = set()
    seen_succeeded: set[str] = set()
    idle_streak = 0
    exit_reason = "timeout"
    final_items: list[dict] = []

    print(
        f"Polling konflux-perfscale/load-test={args.batch} in {args.namespace} "
        f"(expected={args.expected}, created={created}, interval={POLL_INTERVAL}s, "
        f"idle_exit={args.idle_exit}, timeout={args.timeout})",
        file=sys.stderr,
    )

    poll_number = 0
    while time.time() < deadline:
        poll_number += 1
        final_items = fetch_pipelineruns(args.namespace, args.batch)
        counts = count_states(final_items)
        peak_collected = record_seen_states(
            final_items,
            peak_collected=peak_collected,
            seen_terminal=seen_terminal,
            seen_succeeded=seen_succeeded,
        )

        pending_samples.append(counts["pending"])
        running_samples.append(counts["running"])
        total_samples.append(counts["total"])

        # Idle = nothing actively queued or running. After pruning, present count drops
        # but seen_terminal keeps the success tally.
        if counts["pending"] == 0 and counts["running"] == 0 and seen_terminal:
            idle_streak += 1
        else:
            idle_streak = 0

        print(
            f"  pending={counts['pending']} running={counts['running']} "
            f"terminal={counts['terminal']} present={len(final_items)}/{args.expected} "
            f"seen_done={len(seen_terminal)}/{created} "
            f"seen_ok={len(seen_succeeded)}/{created} "
            f"idle={idle_streak}/{args.idle_exit}",
            file=sys.stderr,
        )

        if SNAPSHOT_EVERY > 0 and poll_number % SNAPSHOT_EVERY == 0:
            write_snapshot(
                build_metrics(
                    namespace=args.namespace,
                    batch=args.batch,
                    expected=args.expected,
                    created=created,
                    failed_creation=args.failed_creation,
                    pending_samples=pending_samples,
                    running_samples=running_samples,
                    total_samples=total_samples,
                    final_items=final_items,
                    peak_collected=peak_collected,
                    seen_terminal=seen_terminal,
                    seen_succeeded=seen_succeeded,
                    exit_reason="running",
                ),
                output=args.output,
                tsv_output=args.tsv_output,
            )

        # Fast path: every created PLR is still in the cluster and terminal.
        if len(final_items) >= created and all(classify_pipelinerun(pr) == "terminal" for pr in final_items):
            exit_reason = "complete"
            break

        # Normal exit for large batches: work queue drained and we have seen completions.
        if idle_streak >= args.idle_exit:
            exit_reason = "idle"
            break

        time.sleep(POLL_INTERVAL)

    metrics = build_metrics(
        namespace=args.namespace,
        batch=args.batch,
        expected=args.expected,
        created=created,
        failed_creation=args.failed_creation,
        pending_samples=pending_samples,
        running_samples=running_samples,
        total_samples=total_samples,
        final_items=final_items,
        peak_collected=peak_collected,
        seen_terminal=seen_terminal,
        seen_succeeded=seen_succeeded,
        exit_reason=exit_reason,
    )
    write_snapshot(metrics, output=args.output, tsv_output=args.tsv_output)

    if args.output:
        print(f"Wrote {args.output}", file=sys.stderr)
    if args.tsv_output:
        print(f"Wrote {args.tsv_output}", file=sys.stderr)
    if not args.quiet:
        print(json.dumps(metrics, indent=2))

    if exit_reason == "timeout":
        print("WARNING: timeout reached before batch settled", file=sys.stderr)
        return 2

    if metrics["seen_succeeded_true"] < created:
        print(
            f"WARNING: only {metrics['seen_succeeded_true']}/{created} succeeded "
            f"(exit_reason={exit_reason})",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
