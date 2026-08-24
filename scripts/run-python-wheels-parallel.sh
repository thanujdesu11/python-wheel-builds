#!/usr/bin/env bash
# Launch N parallel python-wheel-build PipelineRuns for Konflux scale testing.
#
# Each run gets a unique batch label (konflux-perfscale/load-test=batch-<epoch>) so
# metrics collection and KubeArchive queries can group them. With -w, a Python
# collector polls cluster state until the batch settles and writes spreadsheet metrics.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COLLECTOR="$ROOT/scripts/collect-plr-batch-metrics.py"

# Defaults match the lightwell-dev konflux-perfscale PoC.
NS="${NS:-konflux-perfscale}"
PIPELINE="${PIPELINE:-python-wheel-build}"
IMAGE_BASE="${IMAGE_BASE:-quay.io/rhtap-perf-test/perf-release-service-trusted-artifacts-stage}"
PACKAGE="${PACKAGE:-urllib3==2.5.0}"
COUNT="${COUNT:-3}"
EXPIRES_AFTER="${EXPIRES_AFTER:-1d}"
TIMEOUT="${TIMEOUT:-30m}"
PIPELINE_TIMEOUT="${PIPELINE_TIMEOUT:-$TIMEOUT}"       # per-PLR Tekton timeout
COLLECTOR_TIMEOUT="${COLLECTOR_TIMEOUT:-$TIMEOUT}"    # max metrics poll duration
IDLE_EXIT="${IDLE_EXIT:-3}"                           # exit after N idle polls
METRICS_DIR="$ROOT/metrics"
WAIT=false

usage() {
  cat <<'EOF'
Launch parallel python-wheel-build PipelineRuns in konflux-perfscale.

Usage:
  run-python-wheels-parallel.sh [options]

Options:
  -n, --count N          Number of parallel runs (default: 3)
  -p, --package SPEC     Python package pin, e.g. urllib3==2.5.0
  -w, --wait             Wait for batch to finish and write metrics summary
  -h, --help             Show this help

Environment:
  NS, PIPELINE, IMAGE_BASE, PACKAGE, COUNT, EXPIRES_AFTER, TIMEOUT
  PIPELINE_TIMEOUT, COLLECTOR_TIMEOUT, IDLE_EXIT

Example:
  ./run-python-wheels-parallel.sh -n 100 -w
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--count) COUNT="$2"; shift 2 ;;
    -p|--package) PACKAGE="$2"; shift 2 ;;
    -w|--wait) WAIT=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if ! [[ "$COUNT" =~ ^[0-9]+$ ]] || [[ "$COUNT" -lt 1 ]]; then
  echo "COUNT must be a positive integer, got: $COUNT" >&2
  exit 1
fi

if ! oc -n "$NS" get pipeline "$PIPELINE" >/dev/null 2>&1; then
  echo "Pipeline/$PIPELINE not found in namespace $NS" >&2
  exit 1
fi

if [[ ! -x "$COLLECTOR" ]]; then
  echo "Metrics collector not found or not executable: $COLLECTOR" >&2
  exit 1
fi

pkg_name="${PACKAGE%%==*}"
batch="batch-$(date +%s)"
created=0
failed_creation=0

mkdir -p "$METRICS_DIR"
metrics_json="$METRICS_DIR/${batch}.json"
metrics_csv="$METRICS_DIR/${batch}.csv"
metrics_log="$METRICS_DIR/${batch}.log"

echo "Creating $COUNT parallel PipelineRuns in $NS"
echo "  pipeline: $PIPELINE"
echo "  package:  $PACKAGE"
echo "  batch:    $batch"

# Create one PipelineRun per iteration; oc prints "pipelinerun.tekton.dev/<name> created".
for i in $(seq 1 "$COUNT"); do
  tag="python-wheel-${pkg_name}-${batch}-${i}-$RANDOM"

  set +e
  name="$(
    oc -n "$NS" create -f - <<EOF 2>&1 | awk '{print $1}'
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  generateName: python-wheel-build-run-
  namespace: ${NS}
  labels:
    konflux-perfscale/load-test: ${batch}
    konflux-perfscale/load-test-index: "${i}"
spec:
  pipelineRef:
    name: ${PIPELINE}
  params:
  - name: packages
    value:
    - "${PACKAGE}"
  - name: output-image
    value: ${IMAGE_BASE}:${tag}
  - name: image-expires-after
    value: "${EXPIRES_AFTER}"
  timeouts:
    pipeline: ${PIPELINE_TIMEOUT}
EOF
  )"
  create_rc=$?
  set -e

  if [[ "$create_rc" -ne 0 ]] || [[ "$name" != pipelinerun.tekton.dev/* ]]; then
    failed_creation=$((failed_creation + 1))
    echo "  [$i] FAILED to create PipelineRun" >&2
    continue
  fi

  created=$((created + 1))
  echo "  [$i] ${name#pipelinerun.tekton.dev/} -> ${IMAGE_BASE}:${tag}"
done

echo
echo "Created: $created  Failed creation: $failed_creation"
if [[ "$created" -eq 0 ]]; then
  exit 1
fi

echo "  metrics: $METRICS_DIR/${batch}.{json,csv,log}"
echo "Watch: oc -n $NS get pipelinerun -l konflux-perfscale/load-test=$batch -w"

run_metrics_collector() {
  "$COLLECTOR" \
    --namespace "$NS" \
    --batch "$batch" \
    --expected "$COUNT" \
    --created "$created" \
    --failed-creation "$failed_creation" \
    --timeout "$COLLECTOR_TIMEOUT" \
    --idle-exit "$IDLE_EXIT" \
    --output "$metrics_json" \
    --csv-output "$metrics_csv" \
    --quiet
}

if [[ "$WAIT" != "true" ]]; then
  echo
  echo "Starting metrics collection in background..."
  run_metrics_collector 2> "$metrics_log" &
  echo "  PID:  $!"
  echo "  JSON: $metrics_json"
  echo "  CSV:  $metrics_csv"
  echo "  Log:  $metrics_log"
  echo "Use -w to wait in foreground and print the summary when done."
  exit 0
fi

echo
echo "Collecting metrics (timeout: $COLLECTOR_TIMEOUT, idle exit: ${IDLE_EXIT} polls)..."
set +e
run_metrics_collector 2> >(tee "$metrics_log" >&2)
metrics_rc=$?
set -e

echo
echo "=== Metrics (batch=$batch) ==="
echo "  JSON: $metrics_json"
echo "  CSV:  $metrics_csv"
echo "  Log:  $metrics_log"
echo
echo "=== PipelineRun summary (live cluster; may be pruned soon) ==="
oc -n "$NS" get pipelinerun -l "konflux-perfscale/load-test=$batch" \
  -o custom-columns='INDEX:.metadata.labels.konflux-perfscale/load-test-index,NAME:.metadata.name,STATUS:.status.conditions[0].reason,START:.status.startTime,END:.status.completionTime'

if [[ "$metrics_rc" -ne 0 ]]; then
  echo
  echo "Metrics collection exited with status $metrics_rc." >&2
  exit "$metrics_rc"
fi

# Use seen_* counts from the collector; live oc get undercounts after KubeArchive pruning.
read -r succeeded_true exit_reason seen_terminal <<<"$(
  python3 -c "import json; m=json.load(open('$metrics_json')); print(m['Succeeded']['True'], m.get('exit_reason',''), m.get('seen_terminal', m['Succeeded']['total']))"
)"

echo "  exit_reason: $exit_reason"
echo "  seen_done:   $seen_terminal/$created"
echo "  seen_ok:     $succeeded_true/$created"

if [[ "$succeeded_true" -ne "$created" ]]; then
  echo
  echo "$succeeded_true/$created run(s) succeeded (seen_done=$seen_terminal)." >&2
  echo "For authoritative counts after pruning, run finalize-batch-from-archive.py." >&2
  exit 1
fi

echo
echo "All $created run(s) succeeded."
