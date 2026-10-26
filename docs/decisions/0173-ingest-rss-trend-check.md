# 決定記録 0173: 取り込みのデーモンの RSS が 24 時間で増加傾向を示さないことの測り方と判定基準

- **種別**: Decision Record
- **Status**: Proposed
- **Date**: 2026-10-26
- **Supersedes**: なし（[`0070`](0070-soak-acceptance-interpretation.md) の読み方に RSS の項目を足す。0070 の決定は変えない）
- **関連**: `issues/15-soak-test.md` / [`0070`](0070-soak-acceptance-interpretation.md) /
  [`0069`](0069-ubuntu-deploy-templates.md) / [`0080`](0080-fand-systemd-unit.md) /
  [`0167`](0167-opt-update-recheck-scope.md) / [`0017`](0017-daily-report.md) §2.4（出力を `docs/` に自動で書かない理由）/
  `src/coldaisle/soak.py`（`NOT_COVERED`）/ `config/soak.yaml` / `deploy/systemd/coldaisle-daemon.service` /
  `docs/fan-header-mapping.md`（T1 の節）
- **対象 Issue**: #47

## 1. Context

#47 の受入基準は「欠測率 1% 未満」「デーモンの RSS が 24 時間で増加傾向を示さない」「意図しない再起動 0 回」の 3 つ。
T1（2026-10-15 16:36〜10-16 16:36）で欠測率 0.000%・デバイス再起動 0・シリアルの切断 0 を確かめた（#47 のコメント）。
**RSS だけは記録の経路が無く、確かめていない。**

- `coldaisle-soak-report`（`src/coldaisle/soak.py`）は DB を読むだけで、RSS を `NOT_COVERED` に置く。
  `NOT_COVERED` が空でない限り総合の `verdict` は合格にならない（判定不能）
- 受入基準は「増加傾向」とだけ書き、**何を・どの間隔で測り・何をもって傾向なしとするか**が仕様に無い
- 取り込みは systemd の unit `coldaisle-daemon` で常駐している。同じ機械で fand・telemetry・api・slackbot も常駐している

導入先で 2026-10-26 に読んだ値（参考。判定には使わない）:

| 出どころ | 値 |
|---|---|
| `systemctl show coldaisle-daemon -p MemoryCurrent`（cgroup の `memory.current`） | 約 117 MiB |
| cgroup の `memory.stat` の `anon` / `file` | 約 54 MiB / 約 61 MiB |
| `/proc/<MainPID>/status` の `VmRSS` / `RssAnon` / `RssFile` | 約 74 MiB / 約 54 MiB / 約 20 MiB |

`memory.current` の半分はページキャッシュ（`file`）で、SQLite へ書き続けるほど増え、メモリが足りなくなれば回収される。
`VmRSS` も共有ライブラリなどの `RssFile` を含む。**どちらもリークが無くても増えうる。**
リークとして見たいのはプロセスが確保したまま返さないメモリで、それに当たるのは `RssAnon`（cgroup の `anon` とほぼ同じ）である。

## 2. Decision（案。§5 の推奨で書く）

### 2.1 何を測るか: 取り込みの MainPID の `RssAnon` で判定する

- **判定に使うのは `coldaisle-daemon` の MainPID の `/proc/<pid>/status` の `RssAnon`**（kB）だけ
  - `memory.current` と `VmRSS` はページキャッシュ・共有ライブラリで増減するので判定に使わない（§1）
  - unit の中のプロセスは 1 つなので cgroup の `anon` と同じものを見る。`/proc` の方がプロセスの入れ替わり（MainPID の変化）を同じ行で取れる
- 同じ行に参考として `VmRSS`・`RssFile`・cgroup の `memory.current`・`memory.stat` の `anon` / `file` も残す（判定に使わない）
- **判定するのは取り込みだけ。** 受入基準は「デーモン」（#47 の文脈では取り込みのデーモン）であり、
  fand・telemetry・api・slackbot の RSS は同じ採取で**参考として記録するだけ**にする（合否を出さない。§5 の 2）

### 2.2 記録の経路: 常駐の経路を増やさず、導入先の一時的な採取で CSV に残す

- **取り込みのデーモンにもほかの常駐の unit にもコードを足さない。** DB にも書かない（新しいメトリクスを作らない）
- 採取は短いシェルのスクリプト `deploy/soak/rss-sample.sh`（実装の PR で足す。仮の値だけ・個体識別子を含めない。ルール 10）:
  1. 対象の unit ごとに `systemctl show <unit> -p MainPID --value` で PID を取り、`/proc/<pid>/status` と
     `/sys/fs/cgroup/system.slice/<unit>/memory.current`・`memory.stat` を読む
  2. 1 回の起動で 1 行ずつ CSV に追記する。列は
     `ts_ms,unit,main_pid,rss_anon_kb,vm_rss_kb,rss_file_kb,cg_memory_current,cg_anon,cg_file`
  3. 読めない値は空欄にする（推測で埋めない）。MainPID が 0（止まっている）なら PID も空欄
- 起動は `systemd-run --user` の**一時的な timer**（`--on-unit-active=60s`）で 1 分ごとに 1 回。
  人のシェル（端末）の外で動くので、T1 のように見張りが途中で止まることを避ける。
  ログアウトで止まらないよう、始める前に `loginctl show-user <自分> -p Linger` を確かめる
  - `/proc/<pid>/status` と cgroup の `memory.*` は誰でも読めるので、**sudo も専用の権限も要らない**（読むだけ）
  - 一時的な unit なので再起動で消える。再起動があれば試験はやり直しになるので、それでよい
- 書く先は導入先のリポジトリの外の作業用のディレクトリ（例 `~/coldaisle-soak/rss-<開始日>.csv`）。
  `docs/` に置かない（0017 §2.4 と同じ。生活のパターンは含まないが、生の記録は人が読んでから `docs/soak-test-YYYY-MM-DD.md` に要約する）

### 2.3 判定: `coldaisle-soak-report --rss-csv` が CSV を読み、`NOT_COVERED` から RSS を外す

`coldaisle-soak-report` に任意の引数 `--rss-csv <path>` を足す（実装の PR）。**読むだけ**で、DB の扱い（0070 §2.4）は変えない。

- `--rss-csv` を渡したときだけ、RSS を `Check`（受入基準の 1 項目）として判定し、`NOT_COVERED` から外す。
  渡さなければ今のまま `NOT_COVERED` に残る（既存の呼び方の結果を変えない）
- 判定は `unit` が `coldaisle-daemon` の行の `rss_anon_kb` だけで行う。ほかの unit の行は表に出すだけ
- 判定の区間は soak の期間（`--start` / `--end`。24 時間）から**最初の `warmup_hours`（2 時間）を除いた残り（22 時間）**
  - 起動直後は import・キャッシュ・SQLite の接続の確保で増えるのが普通なので、傾向に入れない
- **合格の条件（両方を満たす）**:
  1. 判定の区間の `rss_anon_kb` の最小二乗の直線の**傾きが `max_slope_kib_per_hour` 以下**（初期値 256 KiB/h。続けば 1 日 6 MiB）
  2. 判定の区間の**最大値と最小値の差が `max_range_mib` 以下**（初期値 16 MiB）
- **判定不能にする場合**（合格と書かない）:
  - 判定の区間に `coldaisle-daemon` の MainPID が 2 つ以上ある（プロセスが入れ替わった。§2.4 も見る）
  - 判定の区間の行が期待数（区間 ÷ 60 s）の 95% 未満、または行の間隔が 10 分を超えるところがある（`min_coverage` / `max_gap_minutes`）
  - CSV の時刻が soak の期間を覆っていない、または期間が終わっていない（0070 §2.3 と同じ）
- 値（`warmup_hours` / `max_slope_kib_per_hour` / `max_range_mib` / `min_coverage` / `max_gap_minutes` / `sample_interval_s`）は
  `config/soak.yaml` の `rss:` に置く（ルール 9）。**値は測る前に決め、測った後に動かさない**（結果に合わせて閾値を緩めない）
- レポート（Markdown / JSON）に、判定の区間・行数・傾き・最大と最小・MainPID の一覧・参考の unit の最初と最後の値を出す
- 不合格はリークの断定ではない。そのときは #47 を閉じず、48 時間で測り直すか `tracemalloc` で調べる（別の Issue）

### 2.4 取り込みのプロセスの再起動を「意図しない再起動」に数える

0070 §2.3 は DB の `sys.device_restarts`（基板の再起動）だけを数える。採取の CSV で取り込みの MainPID が変わったら、
それは取り込みのプロセスの再起動であり、#47 の「意図しない再起動」に当たる。

- `--rss-csv` を渡したとき、**soak の期間（暖機を含む 24 時間）での `coldaisle-daemon` の MainPID の変化の回数**を
  別の `Check`「取り込みのプロセスの再起動」として出し、0 回なら合格・1 回以上なら不合格にする
- 0070 §2.3 のデバイス再起動の項目はそのまま残す（置き換えない。項目を 1 つ足す）

### 2.5 再接続の回数は判定の項目から外し、参考に回す

`NOT_COVERED` にはシリアルの再接続回数も入っている。RSS を外しても、これが残る限り `verdict` は合格にならない。
再接続回数は `issues/15` の「やること」の集計対象だが**受入基準には無い**。

- `NOT_COVERED` を「受入基準のうち判定できないもの」に限り、再接続回数は新しい一覧「参考（判定に使わない。journal から数える）」へ移す
- 手順書には `journalctl -u coldaisle-daemon` で「シリアルが切れた」を数える方法を書く（#47 のコメントと同じ数え方）

### 2.6 測る条件: 普段の使い方のまま、負荷の試験とは重ねない

- fand・telemetry・api・slackbot・report / rollup の timer が**普段どおり動いている状態**で測る。
  取り込みの仕事の量は基板の送信周期で決まり、GPU / CPU の負荷では変わらないので、負荷の試験と重ねる必要は無い
- 期間中は `/opt/coldaisle` の更新・`coldaisle-daemon` の再起動・基板の送信周期の変更・基板の抜き差しをしない
- **同じ 24 時間で `coldaisle-soak-report` を回し、欠測率とデバイス再起動も同時に確かめ直す**（T1 の結果に頼らず 1 回で受入基準をすべて揃える）
- 取り込みが期間の前から長く動いていてもよい（暖機の除外は期間の始まりから数える。長く動いていたなら除外は余分なだけで甘くならない）

### 2.7 試験（実機不要。ルール 7）

実装の PR で `tests/test_soak.py` に足す（CSV は合成した値）。

1. 一定の値の CSV → RSS 合格。256 KiB/h を超える一定の増加 → 不合格。範囲が 16 MiB を超える山 → 不合格
2. 暖機の 2 時間の中だけで増える CSV → 合格（暖機を除くこと）
3. 区間の途中で MainPID が変わる → RSS は判定不能、「取り込みのプロセスの再起動」は不合格
4. 行の欠け（95% 未満・10 分を超える隙間）→ 判定不能
5. ほかの unit の行がどれだけ増えても RSS の判定は変わらない
6. `--rss-csv` を渡さない → 今と同じ出力（RSS は `NOT_COVERED`、再接続回数は参考の一覧）
7. RSS・DB の項目がすべて合格 → `verdict` が合格になる（`NOT_COVERED` が空になる）
8. 採取のスクリプトは `bash -n` と、偽の `/proc` / cgroup のディレクトリを渡す小さな試験で列の形を確かめる（実機の `/proc` を読まない）

### 2.8 #47 を閉じる条件

次のすべてで閉じる。

1. 実装の PR（`--rss-csv`・`config/soak.yaml` の `rss:`・`deploy/soak/rss-sample.sh`・試験・手順）がマージされている
2. 導入先で §2.6 の条件の 24 時間を採取し、`coldaisle-soak-report --start ... --rss-csv ...` の `verdict` が合格
3. 結果を人が確かめ、`docs/soak-test-YYYY-MM-DD.md` に要約（判定の表・傾き・範囲・MainPID・参考の unit の値）を書いてマージしている

## 3. Consequences

### 良くなること

- 常駐の unit・取り込みのコード・DB のスキーマを変えずに RSS を確かめられる。`/opt/coldaisle` の更新でも、
  `soak.py` は fand の import の閉包の外なので 0167 の区分 O で済む見込み（更新の時点で確かめる）
- ページキャッシュに惑わされず、リークに当たる量（`RssAnon`）だけで判定する
- 閾値が `config/soak.yaml` にあり、判定が試験で確かめられる。測る前に値を決めるので、結果に合わせて緩められない
- 欠測率・デバイス再起動・プロセスの再起動・RSS が 1 回の 24 時間でそろい、#47 を 1 つのレポートで閉じられる

### 悪くなること・その緩和

- **一時的な採取なので、24 時間の後のリークは見えない。** 長期の監視にはならない。
  緩和: 不合格や疑いが出たら、常駐の記録（§4 の案 A）を別の決定記録で検討する
- **24 時間では、1 度だけの段差（アロケータの arena の確保）とゆっくりした増加を見分けられない。** 段差で不合格になりうる。
  緩和: 不合格を断定にせず、48 時間の測り直しか `tracemalloc` で調べる（§2.3）。偽の合格より偽の不合格の方を許す
- 閾値の初期値（256 KiB/h・16 MiB）は経験からの見積もりで、根拠の実測が無い。
  緩和: 値は §5 の 3 で人が決める。基板の送信 1 回（2.5 s ごと。1 日約 34,560 回）あたり約 250 バイトの漏れ（1 日約 8 MiB・約 340 KiB/h）を捉えられる水準にしてある（256 KiB/h は送信 1 回あたり約 190 バイト）
- 採取は人が始めて止める作業で、止め忘れ・始め忘れがありうる。
  緩和: 手順書に開始・確認（`systemctl --user list-timers`）・停止（`systemctl --user stop`）を書き、行の欠けは判定不能になる（§2.3）
- 0070 の再起動の項目に、プロセスの再起動を 1 つ足す。
  緩和: 置き換えではなく `--rss-csv` を渡したときだけの追加で、DB だけの判定（`db_verdict`）は変わらない

## 4. 却下した代替案

- **A. 取り込みのデーモンが自分の `RssAnon` を `sys.*` のメトリクスとして DB に書く**: 長期の監視とダッシュボードにも使えるが、
  取り込み（ルール 6 のプロセス）のコードと DB の中身を変える。0167 の区分 I に当たり、更新ごとの確認が重くなる。
  24 時間の受入には過剰。長期の監視が要ると分かった時点で別の記録にする
- **B. 常駐の監視の unit（node_exporter・新しい `coldaisle-*` のサービス）を足す**: 常駐の経路と権限の面が増える。受入の 1 回のためには過剰
- **C. cgroup の `memory.current`（`systemctl show -p MemoryCurrent`）で判定する**: ページキャッシュを含み、SQLite へ書くほど増える（§1 で全体の約半分）。
  リークが無くても「増加傾向」と出る
- **D. `VmRSS` で判定する**: 共有ライブラリ・mmap の `RssFile` を含み、他のプロセスの動きでも変わる
- **E. 人がスプレッドシートや即席のスクリプトで傾きを出す**: 判定の手順が残らず試験もできない。閾値を後から動かしやすい
- **F. 傾きだけで判定する**: 短時間の大きな山（急な確保と解放の繰り返し）を見逃す。範囲の上限と組にする
- **G. 最初と最後の 1 時間の中央値の差だけで判定する**: 途中の山と、区間の端の揺れに弱い。傾き（全点を使う）を主にする
- **H. 常駐の全 unit を判定する**: 受入基準は取り込みのデーモンの基準。fand など他の unit の基準は誰も決めていない。参考の記録に留める
- **I. 負荷の試験（GPU / CPU の全負荷）と重ねて測る**: 取り込みの仕事の量は負荷で変わらない。重ねると試験の失敗の切り分けが難しくなる
- **J. 暖機を除かない**: 起動直後の正常な増加で不合格になる。期間の前から動いているときは除外が余分になるだけ

## 5. 判断点（推奨を先頭に）

| # | 内容 | 選択肢（推奨を先に） |
|---|---|---|
| 1 | 何で判定するか | **(推奨) 取り込みの MainPID の `RssAnon`**（cgroup の `anon` と同じものを PID の変化と一緒に取れる）/ cgroup の `memory.stat` の `anon` / `VmRSS` / cgroup の `memory.current` |
| 2 | どの unit を見るか | **(推奨) 判定は取り込みだけ、fand・telemetry・api・slackbot は同じ採取で参考に記録** / 取り込みだけを採取する / 常駐の全 unit を判定する（基準を別に決める） |
| 3 | 判定の基準と値 | **(推奨) 暖機 2 時間を除いた 22 時間で、傾き ≦ 256 KiB/h かつ 最大−最小 ≦ 16 MiB（`config/soak.yaml`）** / 同じ形で傾き ≦ 1 MiB/h・範囲 ≦ 32 MiB（緩い）/ 26 時間を採り、暖機を除いて 24 時間を判定する |
| 4 | 記録の経路 | **(推奨) 導入先の一時的な timer（`systemd-run --user`・1 分ごと）が CSV に書き、`coldaisle-soak-report --rss-csv` が読む** / 取り込みが自分の `RssAnon` を DB に書く（§4 の A）/ 端末の `while sleep 60` のループ（見張りが止まりやすい） |
| 5 | 採取の間隔 | **(推奨) 60 s**（24 時間で 1,440 点）/ 10 s / 300 s |
| 6 | 取り込みのプロセスの再起動（MainPID の変化） | **(推奨) RSS は判定不能、別の項目「取り込みのプロセスの再起動」を不合格**（0070 に項目を足す）/ RSS だけ判定不能にして再起動の項目は足さない / 変化の前後を別々に判定する |
| 7 | シリアルの再接続回数（受入基準に無い） | **(推奨) `NOT_COVERED` から参考の一覧へ移し、journal で数える**（`verdict` を合格にできるようにする）/ `NOT_COVERED` に残す（`verdict` は合格にならず、人が #47 を閉じる）/ 再接続を DB に記録する経路を作る |
| 8 | 測る条件 | **(推奨) 普段の使い方のまま、同じ 24 時間で欠測率・デバイス再起動も確かめ直す** / 普段の使い方で RSS だけ（ほかは T1 の結果を使う）/ 負荷の試験と重ねる |
