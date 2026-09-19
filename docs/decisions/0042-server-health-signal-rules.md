# 決定記録 0042: Server Health の signal 判定規則の詳細

- **種別**: Decision Record
- **Status**: FINAL（2026-09-18、リポジトリ所有者が承認）
- **Date**: 2026-09-18
- **Supersedes**: [`0040-server-health-api.md`](0040-server-health-api.md) の次の部分のみ。
  (1) §2.4 のうち、判定対象の metric・quality の扱い・source 状態の導出。§2.4 の
  green / yellow / red の3段階、AI 停止と事象メトリクスの扱いは有効。
  (2) §2.6 のうち、signal の判定対象を `config/server-health.yaml` だけで決めるとも読める
  部分。判定対象には `config/internal-telemetry.yaml` で有効な入力も加わる（本記録 §2.2）。
  監視必須 metric・パネル・`missing_tolerated` を `config/server-health.yaml` に置くことと、
  §2.6 の他の項目は有効。0040 の他の節も有効
- **関連**: [`0040-server-health-api.md`](0040-server-health-api.md) §2.4〜§2.6 /
  [`0009-read-api.md`](0009-read-api.md) §2.12 /
  [`0004-storage-read-contract.md`](0004-storage-read-contract.md) §2.8 /
  `docs/api-contract.md` §3 / `config/server-health.yaml`
- **対象 Issue**: #66（PR #135）
- **マージ前の訂正**: 2026-09-19、リポジトリ所有者の承認を得て §2.4 を実装に合わせて訂正した
  （PR #138 のレビュー指摘）。「有効な入力が1つも無ければ報告状態をそのまま使う」例外を
  lm_sensors に限り、NVML は監視必須 metric で評価することを明記した。同じ理由で、
  入力を無効にした metric が signal に影響しないという記述（§2.1 / §2.2 / §3）を、
  監視必須でない metric に限った

## 1. Context

決定記録 0040 §2.4 は signal を「周期メトリクスに signal を下げるものが無ければ green」、
source 状態を「必須 metric が全滅なら unavailable、一部なら degraded」と定めたが、
どの metric を対象にし、各 quality をどう数えるかは決めていなかった。PR #135 のレビューで、
保存済みの行をすべて見る当初の実装では次の誤判定が起きることが分かった。

- 入力を無効化・撤去した metric の最後の行が DB に残り、やがて `stale` になって signal を
  恒常的に yellow にする。パネルに載っているだけの metric（`board.chipset` など。実機では
  0 °C を返すため無効）も同じ
- 監視対象なのに一度も保存されていない metric は「行が無い」ため判定から漏れる
- 監視必須 metric がすべて `suspect` の source が `unavailable`（red）になる。値は届いて
  いるので、停止ではなく劣化である
- 有効な hwmon 入力が1つも無い lm_sensors が、必須データが無いのに常に `unavailable` になる
- アラート一覧は新しい順に件数で打ち切るため、それより古い critical を見落とす
- 一覧・件数・最新値を別々の文で読むため、間に resolve が入ると payload が食い違う

2026-09-18、リポジトリ所有者が PR #135 で入った規則を本記録として承認した。

## 2. Decision

### 2.1 パネルは表示専用

`config/server-health.yaml` の `panels.gpu` / `panels.environment` は payload に載せる
metric の一覧であり、signal の判定対象を決めない。入力が無効な metric は、値が古くても
パネルに `stale` と表示されるだけで signal に影響しない。ただし監視必須 metric
（`sources.*.required`）は入力の有効・無効にかかわらず判定対象に残る（§2.2 / §2.4）。

### 2.2 signal が見るのは監視必須 metric と有効な入力だけ

判定対象は次の和とする。

- `config/server-health.yaml` の source ごとの監視必須 metric（`sources.*.required`）
- `config/internal-telemetry.yaml` で有効な入力。NVML が有効なら NVML adapter の
  expected metrics、hwmon が有効ならそのうち `enabled: true` の sensor

監視必須 metric は `internal-telemetry.yaml` で入力を無効にしても判定対象から外れない。
入力の無効化で判定対象から外れるのは、監視必須でない入力（任意の hwmon sensor や、
`sources.nvml.required` に無い NVML の expected metrics）だけである。

事象メトリクス（`sys.dropped_samples` 等）は鮮度判定から外す（0009 §2.12 のまま）。

### 2.3 一度も保存されていない監視対象は `missing` と同じ

判定は保存済みの行ではなく、監視対象の**名前**を走査して行う。行が無い metric は
`quality=missing` として扱う。ただし `missing_tolerated` の metric は、保存済みの
`missing` と未保存のどちらも signal を下げない（0040 §2.5）。`suspect` / `stale` は下げる。

### 2.4 source 状態の導出

nvml / lm_sensors は、collector が報告した状態（`sys.telemetry_source.nvml` /
`sys.telemetry_source.hwmon`）を先に見る。

| 報告状態 | 導出 |
|---|---|
| 未記録 | `stopped`（red） |
| `unavailable` / `disabled` / `stopped` / 不正な値 | そのまま（不正な値は `unavailable`）。red |
| `ok` / `degraded` で、有効な hwmon 入力が1つも無い（**lm_sensors のみ**） | 報告状態をそのまま使う |
| 上記以外の `ok` / `degraded` | 下記の quality 規則 |

**評価する metric は source ごとに異なる。**

- nvml: `config/server-health.yaml` の監視必須 metric（`sources.nvml.required`。設定で
  1本以上が必須）。`config/internal-telemetry.yaml` で NVML を無効にしていても、
  保存済みの報告状態が `ok` / `degraded` のまま残っていれば、この監視必須 metric を評価する。
  1本も届いていなければ `unavailable`（red）になる。報告状態をそのまま使う例外は当てない
- lm_sensors: `config/internal-telemetry.yaml` で `enabled: true` の hwmon sensor。
  1本も無いときだけ上の表の例外で報告状態をそのまま使う

**sensor_unit は例外とする。** `sys.ingest_source` は状態ではなく取り込み元の種別
（`serial` / `mock` / `replay` など）であり、上の表の「不正な値」の規則を当てない。

- `sys.ingest_source` が未記録または空文字 → `stopped`（red）
- 空でない値が記録されていれば、値が何であっても報告状態 `ok` とみなし、下記の
  quality 規則で監視必須 metric（`sources.sensor_unit.required`。設定で1本以上が必須）を評価する

quality 規則では、`ok` と `suspect` を「値が届いている」、`stale` / `missing` / 未保存を
「届いていない」とする。

- 監視必須 metric が**1本も届いていない**（すべて `stale` / `missing` / 未保存）→ `unavailable`（red）
- 一部が `ok` 以外（`suspect` を含む）、または報告状態が `degraded` → `degraded`（yellow）
- それ以外 → `ok`

**すべてが `suspect` でも `degraded`（yellow）であり、red にはしない。** red は必須データが
取得できていないときに限る。

lm_sensors は有効な hwmon 入力の**いずれか1本**が届いていれば `ok` とし、届かない入力は
§2.2〜§2.3 の metric 単位の規則で yellow にする（0040 §5 未決1 のまま）。

### 2.5 アラートの重大度は発生中の全件から数える

signal と `compute_mode_advisory` は、件数上限の無い severity ごとの集計から判定する。
`active_alerts` の件数上限（`config/server-health.yaml` の `active_alerts_limit`）は
**表示だけ**に適用する。一覧から外れた件数は `compute_mode_advisory.warnings` に
severity ごとに残す（例: `more active alerts not listed: critical=1`）。発生中の総件数を
payload のフィールドとしては追加しない（schema_version 1 を変えない）。

### 2.6 payload は DB の1時点のスナップショットから作る

最新値・アラート一覧・severity の件数・source 状態・GPU mode は、同じ読み取り
トランザクション（`SqliteStore.read_snapshot()`、`BEGIN DEFERRED … COMMIT`）で読む。
WAL のため取り込み・ルールエンジンの書き込みは妨げない。AI 要約はスナップショットの外で
行い、読み取りトランザクションを長く保持しない。

## 3. Consequences

### 良くなること

- yellow が「いま監視している何かが劣化した」を意味し続ける。監視必須でない入力を
  撤去・無効化しても、その古い行や機種差で恒常的に下がらない
- 監視対象の取りこぼし（未保存）と、古い critical の見落としが無くなる
- REST / WS の1回の応答の中で、一覧・件数・signal が食い違わない

### 悪くなること・その緩和

- 入力を無効にした metric のうち監視必須でないもの（例: 無効にした任意の hwmon sensor）は、
  値が異常でも signal に出ない。緩和: パネルには値と quality がそのまま表示される。無効化は
  `internal-telemetry.yaml` の変更としてリポジトリの履歴に残る
- 監視必須 metric は入力を無効にしても判定対象に残る。NVML を無効にすると、報告状態が
  `disabled` なら nvml は red、`ok` / `degraded` が残っていても監視必須 metric が届かず
  `unavailable`（red）になる。`sources.nvml.required` は設定で1本以上が必須のため、
  NVML を外した運用で red を避ける手段は本記録では定めない
- すべて `suspect` の source は red にならない。緩和: yellow にはなり、`compute_mode_advisory.safe`
  は false になる

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| パネルの metric も判定対象にする | 無効な入力の古い行で signal が恒常的に下がる（実機の `board.chipset`） |
| 保存済みの行だけを判定する | 一度も届かない監視対象を見落とす |
| `suspect` を「届いていない」に数える | 値は届いており、停止と劣化の区別が失われる |
| 有効な hwmon 入力が無い lm_sensors を常に `unavailable` にする | 取得不能になりうる必須データが無いのに red になる |
| 総件数フィールドを payload に追加する | 公開契約（schema_version 1）の変更になる。warnings で足りる |
| 読み出しごとに別の文で読む（従来） | 間の書き込みで payload が食い違う |

## 5. 未決事項

0040 §5 の未決事項（lm_sensors の判定、監視必須 metric の選び方、パネルの metric、
#68 の比較時期）はそのまま残す。本記録は新たな未決事項を加えない。
