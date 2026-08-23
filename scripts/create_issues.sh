#!/usr/bin/env bash
# GitHub Issue 一括登録スクリプト
#
# 事前準備:
#   gh auth login
#   cd <リポジトリのルート>
#
# 使い方:
#   ./scripts/create_issues.sh            # ラベル・マイルストーン作成 + Issue登録
#   DRY_RUN=1 ./scripts/create_issues.sh  # 何が登録されるかだけ表示
#
# 注意: フロントマターの milestone / labels は事前に作成されている必要があります。
#       本スクリプトは冒頭で自動作成します。

set -euo pipefail

DRY_RUN="${DRY_RUN:-0}"
ISSUE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../issues" && pwd)"

run() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

echo "==> ラベルを作成"
create_label() {
  gh label create "$1" --color "$2" --description "$3" 2>/dev/null \
    || echo "    (既存) $1"
}
if [ "$DRY_RUN" != "1" ]; then
  create_label "priority:must"        "b60205" "必須"
  create_label "priority:should"      "d93f0b" "推奨"
  create_label "priority:could"       "fbca04" "任意"
  create_label "blocked-by-hardware"  "5319e7" "GPUサーバー到着待ち"
  create_label "safety"               "e11d21" "安全性に関わる。人間レビュー必須"
  create_label "infra"                "0e8a16" "基盤"
  create_label "core"                 "1d76db" "コアロジック"
  create_label "api"                  "1d76db" "API"
  create_label "ui"                   "c5def5" "UI"
  create_label "ai"                   "5319e7" "LLM関連"
  create_label "firmware"             "006b75" "ESP32ファームウェア"
  create_label "hardware"             "006b75" "ハードウェア作業"
  create_label "qa"                   "bfd4f2" "検証"
  create_label "design"               "d4c5f9" "設計・ADR"
  create_label "integration"          "0e8a16" "外部連携"
fi

echo "==> マイルストーンを作成"
create_milestone() {
  gh api "repos/{owner}/{repo}/milestones" -f title="$1" >/dev/null 2>&1 \
    || echo "    (既存) $1"
}
if [ "$DRY_RUN" != "1" ]; then
  create_milestone "M0 基盤"
  create_milestone "M1 データ基盤"
  create_milestone "M2 実機接続"
  create_milestone "M3 UI"
  create_milestone "M4 アラート"
  create_milestone "M5 AI"
  create_milestone "M6 移行"
  create_milestone "M7 拡張"
fi

echo "==> Issue を登録"
for f in "$ISSUE_DIR"/*.md; do
  title=$(sed -n 's/^title: "\(.*\)"$/\1/p' "$f" | head -1)
  labels=$(sed -n 's/^labels: \(.*\)$/\1/p' "$f" | head -1 | tr -d ' ')
  milestone=$(sed -n 's/^milestone: "\(.*\)"$/\1/p' "$f" | head -1)

  # フロントマターを除いた本文を一時ファイルへ
  body_file=$(mktemp)
  awk 'n>=2{print; next} /^---$/{n++}' "$f" > "$body_file"

  echo "  - $title"
  run gh issue create \
    --title "$title" \
    --body-file "$body_file" \
    --label "$labels" \
    --milestone "$milestone"

  rm -f "$body_file"
done

echo "==> 完了"
echo "注意: 本文中の #番号 による依存関係は、登録後の実際のIssue番号に合わせて手動修正してください。"
