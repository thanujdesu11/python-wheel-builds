#!/usr/bin/env bash
# Run inline LWPython smoke in konflux-perfscale-3-tenant (KONFLUX-15401).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS="${NS:-konflux-perfscale-3-tenant}"
WAIT="${WAIT:-10m}"

if ! oc whoami >/dev/null 2>&1; then
  echo "Not logged in. Run: oc login --server=https://c111-e.us-east.containers.cloud.ibm.com:32325" >&2
  exit 1
fi

if ! oc get secret quay-push-secret -n "$NS" >/dev/null 2>&1; then
  echo "Missing quay-push-secret in $NS. Copy from konflux-perfscale:" >&2
  echo "  oc get secret quay-push-secret -n konflux-perfscale -o yaml \\" >&2
  echo "    | sed 's/namespace: konflux-perfscale/namespace: $NS/' | oc apply -f -" >&2
  exit 1
fi

name="$(oc -n "$NS" create -f "$ROOT/examples/lwpython-inline-smoke-3-tenant.yaml" 2>&1 | awk '{print $1}')"
pr="${name#pipelinerun.tekton.dev/}"
echo "Created PipelineRun: $pr"

if oc -n "$NS" wait --for=condition=Succeeded "pipelinerun/$pr" --timeout="$WAIT"; then
  echo "SUCCESS: $pr"
  oc -n "$NS" get "pipelinerun/$pr" -o jsonpath='digest={.status.results[?(@.name=="IMAGE_DIGEST")].value}{"\n"}'
else
  oc -n "$NS" get "pipelinerun/$pr" -o jsonpath='reason={.status.conditions[0].reason} message={.status.conditions[0].message}{"\n"}'
  exit 1
fi
