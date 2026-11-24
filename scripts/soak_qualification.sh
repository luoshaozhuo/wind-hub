#!/usr/bin/env bash
# Isolated real-service Sink capacity + Storage persistence qualification.
# This is NOT a substitute for the eight-service full-chain qualification.
set -Eeuo pipefail
umask 077

usage() {
  echo "Usage: $0 smoke|soak [global_batches_per_second] [evidence_directory]" >&2
  echo "  smoke: static gates + real Storage CLI/TDengine + 30-device Sink 60s" >&2
  echo "  soak:  same admission gates, then 30-device Sink 600s (configurable)" >&2
}
if [[ $# -lt 1 || $# -gt 3 || "$1" != "smoke" && "$1" != "soak" ]]; then
  usage
  exit 2
fi

mode="$1"
rate="${2:-120}"
if [[ ! "$rate" =~ ^[1-9][0-9]*$ ]]; then
  echo "rate must be a positive integer (global batches/s)" >&2
  exit 2
fi
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
if [[ "$(git branch --show-current)" != "feat/production-readiness-final-20261123" ]]; then
  echo "wrong branch: refusing long-run qualification" >&2
  exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "dirty worktree: refusing unverifiable long-run qualification" >&2
  exit 1
fi
sha="$(git rev-parse HEAD)"
evidence="${3:-/tmp/wind-hub-soak/$sha}"
mkdir -p "$evidence"
evidence="$(cd "$evidence" && pwd)"
if [[ -z "$evidence" || ! -d "$evidence" ]]; then
  echo "unable to create evidence directory" >&2
  exit 1
fi
# Logs must be created outside the tracked repository or in ignored artifacts.
if ! git check-ignore -q "$evidence" && [[ "$evidence" == "$repo/"* ]]; then
  echo "evidence path is tracked/unignored: use an external or ignored directory" >&2
  exit 1
fi

command -v docker >/dev/null
docker info >/dev/null
python - <<'PY'
import importlib
for name in ("pytest", "taosws", "aiokafka", "grpc", "nats", "ruff", "mypy"):
    importlib.import_module(name)
PY
export PYTHONPATH="$repo/src:$repo"
export R14_RESULT_DIR="$evidence"
export R14_DEGREE=30
export R14_POINTS=500
export R14_TARGET_BATCH_RATE="$rate"
# Reliable NATS retention must accommodate the target's window before
# Kafka/TDengine capacity is measured, otherwise source retention becomes the
# artificial bottleneck. 16 GiB provides headroom for 300 x 500-point batches/s.
if (( rate >= 300 )); then
  export R14_STREAM_MAX_BYTES="$((16 * 1024 * 1024 * 1024))"
fi
soak_duration="${WIND_HUB_SOAK_DURATION_S:-600}"
if [[ ! "$soak_duration" =~ ^[1-9][0-9]*$ ]] ||
   (( soak_duration < 600 || soak_duration > 86400 )); then
  echo "WIND_HUB_SOAK_DURATION_S must be an integer in [600, 86400]" >&2
  exit 2
fi

printf 'sha=%s\nmode=%s\nrate=%s\nnominal_target_batches_per_s=300\ndevices=30\npoints_per_batch=500\n' \
  "$sha" "$mode" "$rate" > "$evidence/manifest.txt"
if (( rate < 300 )); then
  printf 'load_profile=DEGRADED_CAPACITY_NOT_30x10HZ\n' >> "$evidence/manifest.txt"
  echo "WARNING: global $rate batches/s is below 30 devices x 10 Hz = 300 batches/s" >&2
else
  printf 'load_profile=TARGET_CAPACITY\n' >> "$evidence/manifest.txt"
fi

run_gate() {
  local name="$1"
  shift
  echo "Running $name: $*"
  "$@" > "$evidence/$name.log" 2>&1 || {
    echo "FAIL: $name; see $evidence/$name.log" >&2
    exit 1
  }
  if [[ "$name" == candidate || "$name" == module_* || "$name" == storage_* || "$name" == sink_* ]]; then
    if grep -Eq '[0-9]+ skipped|[0-9]+ xfailed|SKIPPED \[' "$evidence/$name.log"; then
      echo "NOT_EXECUTED: $name had skipped/xfail cases" >&2
      exit 1
    fi
  fi
}

run_gate ruff_check python -m ruff check src tests deploy
run_gate ruff_format python -m ruff format --check src tests deploy
run_gate mypy python -m mypy src
run_gate rpc_contract python scripts/generate_rpc.py --check
run_gate candidate python -m pytest -q -rs \
  tests/system/e2e/test_long_run_candidate_preflight.py \
  tests/cli/test_config_loader.py
run_gate storage_semantics python -m pytest -q -rs \
  tests/storage_service/unit/test_device_binding.py \
  tests/storage_service/component/test_grpc_common.py
run_gate module_real python -m pytest -q -rs \
  tests/collector/system/test_collector_grpc_system.py \
  tests/industrial_gateway/component/test_config_snapshots.py \
  tests/data_generator/integration/test_grpc_server.py \
  tests/commander/integration/test_grpc.py \
  tests/sink_service/system/test_grpc_api.py
run_gate storage_real python -m pytest -q -rs \
  tests/storage_service/integration/test_cli_restart_activation.py
R14_DURATION_S=60 run_gate sink_60s python -m pytest -q -rs \
  tests/sink_service/system/test_capacity_r14_sustained.py::TestR14SustainedCapacity::test_sustained_capacity_600s

if [[ "$mode" == "soak" ]]; then
  R14_DURATION_S="$soak_duration" run_gate sink_long python -m pytest -q -rs \
    tests/sink_service/system/test_capacity_r14_sustained.py::TestR14SustainedCapacity::test_sustained_capacity_600s
fi
printf 'status=PASS\nscope=SEGMENTED_REAL_SERVICE_ONLY\nfull_eight_service=NOT_RUN\nraw_source_provenance=NOT_RUN\n' \
  >> "$evidence/manifest.txt"
echo "PASS: segmented real-service qualification; SHA=$sha; evidence=$evidence"
echo "Eight-service full-chain and RAW provenance remain separate admission gates."
