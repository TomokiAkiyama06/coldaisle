#!/usr/bin/env bash
# GitHub Issue 同期スクリプト
#
# - labels / milestones を冪等に作成
# - issues/*.md を GitHub Issue へ作成または更新
# - 論理 Issue 番号（ファイル名先頭）を実際の GitHub Issue 番号へ本文中で解決
# - 完了済み / superseded の Issue を自動で close
# - 実機要件ラベルを付与
#
# 使い方:
#   GH_TOKEN=... ./scripts/create_issues.sh
#   DRY_RUN=1 ./scripts/create_issues.sh

set -euo pipefail

DRY_RUN="${DRY_RUN:-0}"
ISSUE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../issues" && pwd)"

run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '[dry-run]'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

create_label() {
  run gh label create "$1" --color "$2" --description "$3" --force
}

create_milestone() {
  local title="$1"
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] milestone: $title"
    return
  fi

  local existing
  existing=$(gh api "repos/{owner}/{repo}/milestones?state=all&per_page=100" \
    --jq ".[] | select(.title == \"$title\") | .number" | head -1 || true)
  if [ -z "$existing" ]; then
    gh api "repos/{owner}/{repo}/milestones" -f title="$title" >/dev/null
  fi
}

is_completed() {
  case "$1" in
    1|2|3|4|5|6|7|8|9|10|14|18|21|22|23|25|38|39|40|41|42) return 0 ;;
    *) return 1 ;;
  esac
}

is_not_planned() {
  case "$1" in
    24|28|31) return 0 ;;
    *) return 1 ;;
  esac
}

# 「実装できるか」ではなく、受入基準の完了に実GPUサーバーが必要か。
requires_server() {
  case "$1" in
    19|26|27|28|30|32|33|34|37|43) return 0 ;;
    *) return 1 ;;
  esac
}

# 受入基準の完了に XIAO + DS18B20×5 + AM2320 が必要か。
requires_sensor_module() {
  case "$1" in
    11|12|13|14|15|19|26|33|37) return 0 ;;
    *) return 1 ;;
  esac
}

issue_state() {
  if is_completed "$1"; then
    echo completed
  elif is_not_planned "$1"; then
    echo not_planned
  else
    echo open
  fi
}

issue_labels() {
  local spec_id="$1"
  local file="$2"
  local labels
  labels=$(sed -n 's/^labels: \(.*\)$/\1/p' "$file" | head -1 | tr -d ' ')

  labels=$(printf '%s' "$labels" | sed 's/,\?blocked-by-hardware//g; s/blocked-by-hardware,\?//g')

  if requires_server "$spec_id"; then
    labels="${labels:+$labels,}requires:server"
  fi
  if requires_sensor_module "$spec_id"; then
    labels="${labels:+$labels,}requires:sensor-module"
  fi
  if ! requires_server "$spec_id" && ! requires_sensor_module "$spec_id"; then
    labels="${labels:+$labels,}hardware-independent"
  fi

  case "$spec_id" in
    36) labels="${labels:+$labels,}needs-decision" ;;
    43) labels="${labels:+$labels,}blocked-by-design" ;;
  esac

  echo "$labels"
}

strip_frontmatter() {
  awk 'n>=2{print; next} /^---$/{n++}' "$1"
}

echo "==> labels"
create_label "priority:must"           "b60205" "必須"
create_label "priority:should"         "d93f0b" "推奨"
create_label "priority:could"          "fbca04" "任意"
create_label "safety"                  "e11d21" "安全性に関わる。人間レビュー必須"
create_label "infra"                   "0e8a16" "基盤"
create_label "core"                    "1d76db" "コアロジック"
create_label "api"                     "1d76db" "API"
create_label "ui"                      "c5def5" "UI"
create_label "ai"                      "5319e7" "LLM関連"
create_label "firmware"                "006b75" "ESP32ファームウェア"
create_label "hardware"                "006b75" "ハードウェア作業"
create_label "qa"                      "bfd4f2" "検証"
create_label "design"                  "d4c5f9" "設計・ADR"
create_label "integration"             "0e8a16" "外部連携"
create_label "requires:server"         "6f42c1" "GPUサーバー実機が完了条件に必要"
create_label "requires:sensor-module"  "006b75" "XIAO ESP32-S3 + DS18B20×5 + AM2320 の自作センサーモジュールが完了条件に必要"
create_label "hardware-independent"    "c2e0c6" "サーバー / 自作センサーモジュールなしで完了可能"
create_label "blocked-by-design"       "d876e3" "安全設計・ADRの承認まで実装開始しない"
create_label "needs-decision"          "f9d0c4" "実装前に人間の設計判断が必要"
create_label "blocked-by-hardware"     "ededed" "旧ラベル（非推奨）。requires:* を使用"

echo "==> milestones"
for milestone in \
  "M0 基盤" \
  "M1 データ基盤" \
  "M2 実機接続" \
  "M3 UI" \
  "M4 アラート" \
  "M5 AI" \
  "M6 移行" \
  "M7 拡張"; do
  create_milestone "$milestone"
done

if [ "$DRY_RUN" = "1" ]; then
  echo "==> issues (dry-run)"
  for f in "$ISSUE_DIR"/*.md; do
    raw_id=$(basename "$f" | cut -d- -f1)
    spec_id=$((10#$raw_id))
    title=$(sed -n 's/^title: "\(.*\)"$/\1/p' "$f" | head -1)
    echo "  spec#$spec_id [$(issue_state "$spec_id")] $title"
    echo "    labels: $(issue_labels "$spec_id" "$f")"
  done
  exit 0
fi

declare -A ISSUE_NUM
map_file=$(mktemp)
trap 'rm -f "$map_file"' EXIT

echo "==> pass 1: create/find issues"
for f in "$ISSUE_DIR"/*.md; do
  raw_id=$(basename "$f" | cut -d- -f1)
  spec_id=$((10#$raw_id))
  title=$(sed -n 's/^title: "\(.*\)"$/\1/p' "$f" | head -1)
  labels=$(issue_labels "$spec_id" "$f")
  milestone=$(sed -n 's/^milestone: "\(.*\)"$/\1/p' "$f" | head -1)

  existing=$(gh issue list --state all --limit 500 --json number,title \
    --jq ".[] | select(.title == \"$title\") | .number" | head -1 || true)

  if [ -n "$existing" ]; then
    number="$existing"
    echo "  found spec#$spec_id -> #$number  $title"
  else
    body_file=$(mktemp)
    strip_frontmatter "$f" > "$body_file"
    url=$(gh issue create --title "$title" --body-file "$body_file" \
      --label "$labels" --milestone "$milestone")
    rm -f "$body_file"
    number="${url##*/}"
    echo "  created spec#$spec_id -> #$number  $title"
  fi

  ISSUE_NUM[$spec_id]="$number"
  printf '%s\t%s\n' "$spec_id" "$number" >> "$map_file"
done

echo "==> pass 2: update bodies / labels / state"
for f in "$ISSUE_DIR"/*.md; do
  raw_id=$(basename "$f" | cut -d- -f1)
  spec_id=$((10#$raw_id))
  number="${ISSUE_NUM[$spec_id]}"
  title=$(sed -n 's/^title: "\(.*\)"$/\1/p' "$f" | head -1)
  labels=$(issue_labels "$spec_id" "$f")
  milestone=$(sed -n 's/^milestone: "\(.*\)"$/\1/p' "$f" | head -1)
  state=$(issue_state "$spec_id")

  raw_body=$(mktemp)
  final_body=$(mktemp)
  strip_frontmatter "$f" > "$raw_body"

  python3 - "$raw_body" "$map_file" "$final_body" "$spec_id" "$(basename "$f")" <<'PY'
import re
import sys
from pathlib import Path

body_path, map_path, out_path, spec_id, filename = sys.argv[1:]
mapping = {}
for line in Path(map_path).read_text().splitlines():
    logical, actual = line.split("\t", 1)
    mapping[int(logical)] = int(actual)

text = Path(body_path).read_text()
text = re.sub(
    r"#(\d+)\b",
    lambda m: f"#{mapping.get(int(m.group(1)), int(m.group(1)))}",
    text,
)
source = f"> Source spec: `issues/{filename}` (logical #{spec_id})\n\n"
Path(out_path).write_text(source + text)
PY

  gh issue edit "$number" --title "$title" --body-file "$final_body" \
    --add-label "$labels" --milestone "$milestone" >/dev/null
  gh issue edit "$number" --remove-label "blocked-by-hardware" >/dev/null 2>&1 || true

  case "$state" in
    completed)
      gh issue close "$number" --reason completed >/dev/null 2>&1 || true
      ;;
    not_planned)
      gh issue close "$number" --reason "not planned" >/dev/null 2>&1 || true
      ;;
    open)
      gh issue reopen "$number" >/dev/null 2>&1 || true
      ;;
  esac

  rm -f "$raw_body" "$final_body"
  echo "  synced spec#$spec_id -> #$number ($state)"
done

echo "==> done"
