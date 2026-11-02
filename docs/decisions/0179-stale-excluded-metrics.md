# 決定記録 0179: 記録をやめた metric（`nvml.excluded_metrics`）を、API の鮮度の判定から外す

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-11-02
- **Supersedes**: なし（0176 の収集と表示の決定は変えない。0176 が見ていなかった API の鮮度の判定を足す）
- **関連**: [`0009`](0009-read-api.md) / [`0167`](0167-opt-update-recheck-scope.md) / [`0173`](0173-ingest-rss-trend-check.md) /
  [`0176`](0176-no-gpu-hotspot-mem.md) / [`0178`](0178-opt-update-5cd9820-to-750f05a.md)
- **対象 Issue**: #404

## 1. Context

0176 は `gpu.0.hotspot` / `gpu.0.mem` の行を書かないことにし、既に保存した行は保持期間で自然に消えるとした（§2.5）。
ところが `store.latest()` は metric ごとの最新の行を時刻の上限なしに返し、古くなった行の品質を `stale` に落とす。
API の `_is_stale`（`api/app.py`）は `EVENT_METRICS` 以外をすべて周期メトリクスとして見るので、最後の行が約 10 秒古くなった時点から次の状態になる。

- `/api/v1/health` が `stale: true`・`ok: false`（`data_age_seconds` は 1 s 未満で正常）
- `/api/v1/latest` の `stale` も true で、画面に「データが古い」の警告が出続ける（`web/app.js` はこの 2 つで決める）
- 生データの保持（`config/retention.yaml` の `raw_days: 30`）で行が消えるまで続き、その間は本当に取り込みが止まったときと見分けが付かない

2026-11-02 の 0178 の更新の I1 で、§2.3 の「`/api/v1/health` の `stale` が false」が不合格になって分かった。
`stale` になっていた周期メトリクスはこの 2 つだけで、ほかの metric の古さは 2 s 未満、`server-health` の `signal` は green だった。
所有者はこの項目を例外として更新を進め、修正を 0173 の RSS の 24 時間の後に入れると判断した（§5）。

## 2. Decision

### 2.1 鮮度の判定

- `_periodic`（`/health` の `stale`・`ok`・`last_sample_*`・`/latest` の `stale` が共有する）から、`EVENT_METRICS` に加えて `nvml.excluded_metrics` の metric を外す
- 一覧は、API が既に読む `InternalTelemetryConfig`（`COLDAISLE_INTERNAL_TELEMETRY`。導入先では `/etc` の写し）から取る。収集と同じ設定を見るので、別の一覧を持たない
- ほかの周期メトリクスの判定は変えない。一部が止まれば今までどおり `stale` になる

### 2.2 変えないもの

- 保存した行は消さない（0176 §2.5）。手で `DELETE` もしない
- `/api/v1/latest` の `metrics` には、保持期間で消えるまで `quality: stale` のまま残す。値が古いことは事実で、`EVENT_METRICS` の扱い（0009 §2.12）と同じである
- `EVENT_METRICS` には足さない。あれは「起きたときにしか書かない metric」で、意味が違い、fand の閉包の `channels.py` を変えることにもなる
- `store/`・`control/`・収集・`config/` は変えない。fand の入力は 0176 §2.4 のとおりで、変わらない

### 2.3 試験

実装の PR で次を試験で固定する。

- 除外した metric の古い行だけが古いとき、`/health` は `stale: false`・`ok: true`、`/latest` の `stale` は false
- 除外していない周期メトリクスが古ければ、除外した metric があっても `stale: true`・`ok: false`
- 除外の一覧が空なら、今までと同じ結果になる

### 2.4 導入先への反映

- 差分は `src/coldaisle/api/` と `tests/`・`docs/` だけの想定で、0167 §2.1 の区分は **O**（共通の確認だけ）。実際の差分の path で当てはめ、所有者が確かめる（0167 §2.1）
- 0178 §2.4 の手順 10 の RSS の 24 時間の判定が済むまで `/opt` を更新しない（0173 §2.4）
- 更新の後に、`/api/v1/health` の `stale` が false で `ok` が true であること、画面に「古い」の警告が出ないことを確かめ、0178 §2.3 で例外にした項目の確認とする。結果は #74 に書く

## 3. Consequences

- 記録をやめた metric で、取り込みの健全度が偽って赤にならない。本当の止まりと見分けられる
- 次に `excluded_metrics` へ足すときも、同じことが起きない
- `excluded_metrics` に書いた metric は、その行が古くても `/health` で気づけない。ただし書いた時点で記録しないと決めた metric で、設定の誤記は 0176 §2.1 が起動時に拒否する
- API の設定と収集の設定が別の写しを読むと一覧がずれうる。導入先はどちらも `/etc` の写しを読む（`/etc` の写しの差分は 0178 の C3 で見ている）

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| 古い行を手で `DELETE` する | 0176 §2.5 が消さないとしている。DB への手の操作を増やし、別の導入先や次の除外で同じことが起きる |
| 30 日待つ | その間 `/health` と画面の警告が役に立たず、本当の止まりを隠す |
| `_is_stale` を「最近 N 時間に行があった metric だけ」にする | 長く止まった metric を健全と扱い、`/health` の「古いときに ok を返さない」（FR-305）を弱める |
| `/latest` の応答からも除外した metric を消す | 古い行があること自体は隠さなくてよく、応答の形の扱いが増える。0176 のとおり画面には載らない |
| `/opt` を戻してから直す | 0178 の更新（約 4.5 時間）のやり直しになる。この項目は fand の入力に影響しない。所有者が進むと判断した |

## 5. 承認

- 2026-11-02: 所有者が 0178 の I1 のこの項目を例外として更新を進め、修正を別 Issue で進めて RSS の後に入れると判断した
- 本記録の §2 の承認は未了（Proposed）
