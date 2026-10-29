#!/usr/bin/env bash
# 連続運転テスト（soak）の RSS の採取（#47 / 決定記録 0173 §2.2）。
#
# 1 回の起動で、対象の unit ごとに 1 行ずつ CSV に追記して終わる。常駐しない。
# 1 分ごとの起動は導入先の一時的な timer（`systemd-run --user`）が行う。手順は
# docs/ubuntu-deploy.md 4.4。**読むだけ**（`/proc/<pid>/status` と cgroup の memory.*）なので
# sudo も専用の権限も要らない。取り込みのデーモンにもほかの unit にも DB にも触らない。
#
#   rss-sample.sh <CSV のパス> [unit ...]
#
# unit を省くと、判定する取り込み（coldaisle-daemon）と参考の常駐の unit を採る。
# 列: ts_ms,unit,main_pid,rss_anon_kb,vm_rss_kb,rss_file_kb,cg_memory_current,cg_anon,cg_file
#   - *_kb は /proc/<MainPID>/status の値（kB）。cg_* は cgroup の memory.current と memory.stat の
#     anon / file（バイト）
#   - 読めない値は空欄（推測で埋めない）。MainPID が 0（止まっている）なら PID も空欄
#
# 試験のために、読む場所を環境変数で差し替えられる（tests/test_soak.py。実機の /proc を読まない）:
#   COLDAISLE_SOAK_PROC_ROOT（既定 /proc）・COLDAISLE_SOAK_CGROUP_ROOT（既定 /sys/fs/cgroup）

set -u

if [[ $# -lt 1 || -z $1 ]]; then
  echo "使い方: $0 <CSV のパス> [unit ...]" >&2
  exit 2
fi
csv=$1
shift
if [[ $# -gt 0 ]]; then
  units=("$@")
else
  units=(coldaisle-daemon coldaisle-fand coldaisle-telemetry coldaisle-api coldaisle-slackbot)
fi

proc_root=${COLDAISLE_SOAK_PROC_ROOT:-/proc}
cgroup_root=${COLDAISLE_SOAK_CGROUP_ROOT:-/sys/fs/cgroup}
header="ts_ms,unit,main_pid,rss_anon_kb,vm_rss_kb,rss_file_kb,cg_memory_current,cg_anon,cg_file"

# CSV の列を壊す名前（カンマ・改行）を書かない
for unit in "${units[@]}"; do
  if [[ ! $unit =~ ^[A-Za-z0-9@._-]+$ ]]; then
    echo "unit の名前が不正: $unit" >&2
    exit 2
  fi
done

# 数字だけを通す。それ以外（読めなかった・形が違う）は空欄にする
digits() {
  if [[ $1 =~ ^[0-9]+$ ]]; then
    printf '%s' "$1"
  fi
}

# `/proc/<pid>/status` の 1 項目（例 `RssAnon:   55296 kB` の 55296）
status_field() {
  local file=$1 key=$2
  [[ -r $file ]] || return 0
  digits "$(awk -v key="$key:" '$1 == key { print $2; exit }' "$file" 2>/dev/null)"
}

# memory.stat の 1 項目（例 `anon 56623104`）
stat_field() {
  local file=$1 key=$2
  [[ -r $file ]] || return 0
  digits "$(awk -v key="$key" '$1 == key { print $2; exit }' "$file" 2>/dev/null)"
}

single_value() {
  local file=$1 value
  [[ -r $file ]] || return 0
  read -r value <"$file" 2>/dev/null || return 0
  digits "$value"
}

ts_ms=$(date +%s%3N)
if [[ ! $ts_ms =~ ^[0-9]+$ ]]; then
  echo "時刻を ms で得られない（GNU date が要る）: $ts_ms" >&2
  exit 1
fi

lines=""
for unit in "${units[@]}"; do
  pid=$(digits "$(systemctl show "$unit.service" -p MainPID --value 2>/dev/null)")
  [[ $pid == 0 ]] && pid=""
  rss_anon="" vm_rss="" rss_file=""
  if [[ -n $pid ]]; then
    status="$proc_root/$pid/status"
    rss_anon=$(status_field "$status" RssAnon)
    vm_rss=$(status_field "$status" VmRSS)
    rss_file=$(status_field "$status" RssFile)
  fi
  cgroup="$cgroup_root/system.slice/$unit.service"
  cg_current=$(single_value "$cgroup/memory.current")
  cg_anon=$(stat_field "$cgroup/memory.stat" anon)
  cg_file=$(stat_field "$cgroup/memory.stat" file)
  lines+="$ts_ms,$unit,$pid,$rss_anon,$vm_rss,$rss_file,$cg_current,$cg_anon,$cg_file"$'\n'
done

# 見出しは空のファイルにだけ書く。行はまとめて 1 回で追記する（途中の行を残さない）
if [[ ! -s $csv ]]; then
  printf '%s\n' "$header" >>"$csv" || exit 1
fi
printf '%s' "$lines" >>"$csv" || exit 1
