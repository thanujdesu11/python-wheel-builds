# Python wheel PipelineRun scale tests (Konflux / lightwell-dev)

Tools and manifests for **KONFLUX-15499** (PoC) and **KONFLUX-15500** (scale test): parallel
`python-wheel-build` PipelineRuns on lightwell-dev, with concurrency metrics and KubeArchive
post-run verification.

## What this repo contains

```
pipeline/          Tekton Pipeline manifest (inlined fromager task, no bundle resolver)
examples/          Single PipelineRun example
scripts/           Launch parallel runs, collect metrics, verify via KubeArchive
results/           Completed KONFLUX-15500 scale-test summary (Aug 2026)
```

## Prerequisites

- `oc` logged into **lightwell-dev**
- Namespace: `konflux-perfscale`
- Push secret linked to the `builder` service account (PoC uses `quay-push-secret`)
- Python 3.9+
- For archive verification: [kubectl-ka](https://kubearchive.github.io/kubearchive/main/cli/installation.html)

## Deploy the pipeline

Log in to **lightwell-dev**, then:

```bash
oc apply -f pipeline/python-wheel-build.yaml
```

Push target used by the scale-test launcher defaults to
`quay.io/rhtap-perf-test/perf-release-service-trusted-artifacts-stage` (override with `IMAGE_BASE`).

## Run a scale test

```bash
# Example: 100 parallel PipelineRuns
./scripts/run-python-wheels-parallel.sh -n 100 -w
```

Environment variables (defaults are enough for tested scale up to 2000 PLRs):

| Variable | Default | Purpose |
|----------|---------|---------|
| `NS` | `konflux-perfscale` | Target namespace |
| `PIPELINE` | `python-wheel-build` | Pipeline name |
| `PACKAGE` | `urllib3==2.5.0` | Package pin |
| `PIPELINE_TIMEOUT` | `30m` | Per-PLR Tekton timeout (~1–2 min per build) |
| `COLLECTOR_TIMEOUT` | `30m` | Max metrics poll duration (~12 min for 2000 PLRs) |
| `IDLE_EXIT` | `3` | Exit after N idle polls (pending=0, running=0) |

Metrics are written to `metrics/batch-<timestamp>.{json,tsv,log}` (gitignored). Poll interval (5s) and snapshot frequency are fixed in the collector script. Without `-w`, the collector runs in the background.

## Verify results via KubeArchive

Completed PipelineRuns are pruned from the cluster ~5 minutes after finish. 
For larger numbers of PLRs, completed runs are pruned from the cluster before the whole
batch finishes. Live `oc get` counts then drop mid-run, which breaks concurrency and
success tracking unless you also record what was seen before pruning. The collector
handles this with `seen_terminal` / `seen_succeeded_true`; KubeArchive is the fallback
to verify final counts once objects leave the cluster.

Use `finalize-batch-from-archive.py` after a batch finishes (do **not** rely on
`kubectl ka ... --count`; paginate instead):

```bash
./scripts/finalize-batch-from-archive.py \
  --batch batch-<timestamp> \
  --expected 1000 \
  --metrics-json metrics/batch-<timestamp>.json \
  --tsv
```

## KONFLUX-15500 results (Aug 2026)

All steps succeeded (1, 10, 100, 200, 500, 1000, 2000 PLRs). Summary:

| PLRs | pending.max | running.max | total.max | Succeeded |
|------|-------------|-------------|-----------|-----------|
| 1 | 1 | 1 | 1 | 1/1 |
| 10 | 3 | 10 | 10 | 10/10 |
| 100 | 45 | 100 | 100 | 100/100 |
| 200 | 89 | 190 | 200 | 200/200 |
| 500 | 236 | 165 | 393 | 500/500 |
| 1000 | 564 | 498 | 963 | 1000/1000 |
| 2000 | 1451 | 497 | 1900 | 2000/2000 |

Full results: [KONFLUX-15500 scale test spreadsheet](https://docs.google.com/spreadsheets/d/1JDOBUNgx4W7Rus34n5mpXbFQL8cfQCmXg1TZ2Rh-dxs/edit?usp=sharing).
Local copy: [`results/scale-test-summary.tsv`](results/scale-test-summary.tsv)

Key finding: parallel execution caps at ~497–498 running PLRs; higher N increases Kueue pending queue.