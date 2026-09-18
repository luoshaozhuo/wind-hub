#!/usr/bin/env bash
set -euo pipefail

scope="${1:-}"
if [ -z "$scope" ]; then
  # 兼容当前会话已加载的旧自动调用；无显式范围时不执行检查。
  exit 0
fi
if [ "$scope" != "staged" ] && [ "$scope" != "all" ]; then
  echo "usage: $0 staged|all" >&2
  exit 2
fi

roots=()
[ -d "src/whale/ingest" ] && roots+=("src/whale/ingest")
[ -d "src/whale/shared/source" ] && roots+=("src/whale/shared/source")

if [ "${#roots[@]}" -eq 0 ]; then
  exit 0
fi

paths=()
if [ "$scope" = "all" ]; then
  paths=("${roots[@]}")
else
  while IFS= read -r path; do
    [ -f "$path" ] || continue
    case "$path" in
      src/whale/ingest/*.py|src/whale/ingest/**/*.py|src/whale/shared/source/*.py|src/whale/shared/source/**/*.py)
        paths+=("$path")
        ;;
    esac
  done < <(git diff --cached --name-only --diff-filter=ACMRT | sort -u)
fi

if [ "${#paths[@]}" -eq 0 ]; then
  exit 0
fi

tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT

if grep -R --include='*.py' -nE '(^|[[:space:]])(import|from)[[:space:]]+tools\.source_lab' "${paths[@]}" >"$tmp_file" 2>/dev/null; then
  echo "BLOCKED: production path imports tools.source_lab:" >&2
  cat "$tmp_file" >&2
  exit 2
fi
