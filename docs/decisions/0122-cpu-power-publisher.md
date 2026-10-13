# 決定記録 0122: CPU Package の消費電力を root の小さなサービスから渡す（RAPL）

- **種別**: Decision Record
- **Status**: Proposed（所有者の承認待ち）
- **Date**: 2026-10-13
- **Supersedes**: なし（0119 §5 未決 4 の入力の取り方を決める）
- **関連**: [`0047`](0047-cpu-utilization-metric.md) /
  [`0080`](0080-fand-systemd-unit.md) §2.5 /
  [`0117`](0117-telemetry-unit.md) /
  [`0119`](0119-safety-values-first-host.md) §2.4 / §5 /
  [`0121`](0121-hardware-test-procedure.md) / `config/internal-telemetry.yaml`
- **対象 Issue**: #278

---

## 1. Context

導入先は `power.cpu.package` を集めていない（0119 §5 の 4）。そのため:

- fand の制御入力の契約で `power.cpu.package` は重要度 `DEGRADED` で、欠けると telemetry の健全度が常に `DEGRADED` になる。
  Reactive Guard は常に degraded の閾値（CPU 温度の上昇 5 °C/s など）を使い、4 日分の模擬で Guard の時間が Top 9%（通常の閾値なら 5%）
- `safety.yaml` の CPU Power の cooling floor（100 W → 0.35、200 W → 1.0）と `telemetry.cpu_power_ms` は試験できず `provisional` のまま（0121）

導入先の CPU（Ryzen 9 9950X）は Linux の powercap に `intel-rapl:0`（`name` が `package-0`）を出し、
`energy_uj`（積算のエネルギー、μJ）と `max_energy_range_uj`（一周する値）を持つ。2 s の差から計算するとアイドルで約 30 W。
ただし `energy_uj` は root だけが読める（`0400`）。PLATYPUS（CVE-2020-8694 / 8695。電力の細かな変化から処理中のデータを推測する
攻撃）への kernel の対策で、一般のユーザーで動く `coldaisle-telemetry` は読めない。hwmon の `amdgpu` の `PPT` は内蔵 GPU だけの
電力（約 0.01 W）で使えない。

所有者は、読む権限を広げる案（そのファイルだけをグループ `coldaisle` に読ませる）ではなく、**root で動く小さな専用のサービスが
粗い平均の電力だけを渡す**案を選んだ（2026-10-13）。PLATYPUS の対策を緩めないためである。

## 2. Decision

### 2.1 専用のサービス `coldaisle-cpu-power`

- **中身**: `src/coldaisle/cpu_power_publisher.py`。**標準ライブラリだけ**を使い、`coldaisle` パッケージを import しない
  （引き継ぎ実行部と同じ形。0080 §2.5）。システムの `/usr/bin/python3 -I -S <ファイル>` で動かし、venv に依存しない
- **読む先**: `/sys/class/powercap/` の下で `name` が `package-0` の zone を**名前で探す**（番号で指定しない）。起動時は、ちょうど1つ見つかるまで
  1 s ごとに探し直し、60 s（引数で渡す）を過ぎても見つからなければ何も書かずに終了コード 2 で終わる（driver の読み込みを待つ）。
  見つかった後に読めなくなったら、終了コード 1 で終わり unit が再起動する。読むのは `energy_uj` と `max_energy_range_uj` だけ
- **計算**: 1000 ms ごと（単調時計）に `energy_uj` を読み、前回との差を経過時間で割って W にする。差が負なら一周とみなし
  `max_energy_range_uj` を足す。経過時間が 500〜2000 ms の外なら、その回は書かない（停止や遅れで平均の窓が崩れるため）
- **粗さ**: 窓は 1 s の平均、値は **1 W に丸める**。PLATYPUS は ms 単位の変化を使うので、1 s の平均と 1 W の丸めでは
  その情報を渡さない。0〜1000 W の外の値は書かない
- **渡し方**: `/run/coldaisle-power/cpu-package.json` に `{"schema_version": 1, "ts_ms": <wall clock>, "watts": <整数>, "window_ms": <整数>}`
  を書く。一時ファイルに書いて `rename` で置き換える（読み手が書きかけを読まない）
- **権限**: ディレクトリは unit の `RuntimeDirectory=coldaisle-power`（`0750`）、ファイルは `0640`。グループは unit の `Group=coldaisle`
  で付く（`chown` を使わない）。読めるのはグループ `coldaisle` だけ

### 2.2 unit `deploy/systemd/coldaisle-cpu-power.service`

- `User=root`・`Group=coldaisle`。**capability は持たない**（`CapabilityBoundingSet=` を空に、`AmbientCapabilities=` なし）。
  `energy_uj` は所有者が root で所有者の読み取りがあるので、uid 0 なら capability なしで読める。導入先で 2026-10-13 に
  `systemd-run`（`User=root`・`Group=coldaisle`・`CapabilityBoundingSet=`・`NoNewPrivileges=yes`・`ProtectSystem=strict`・
  `ProtectKernelTunables=yes`・`PrivateNetwork=yes`）で、`CapEff` が 0 のまま読めることを確かめた
- sandbox: `NoNewPrivileges=yes`・`ProtectSystem=strict`・`ProtectHome=yes`・`PrivateTmp=yes`・`PrivateNetwork=yes`・
  `RestrictAddressFamilies=AF_UNIX`・`ProtectKernelModules=yes`・`ProtectControlGroups=yes`・`LockPersonality=yes`・
  `RestrictRealtime=yes`・`SystemCallArchitectures=native`・`ProtectClock=yes`・`ProtectHostname=yes`。書けるのは
  `RuntimeDirectory` だけ
- `After=systemd-modules-load.service`（RAPL の driver を module で読み込む導入先で、先に起動しない）
- `Restart=always`・`RestartSec=2`・`RestartPreventExitStatus=2`（起動時に 60 s 探しても zone が無い導入先で繰り返さない）
- `coldaisle-telemetry` からは `Wants=` / `After=` だけで参照し、無くても telemetry は止まらない
- 監視系と同じく `enable --now` で常駐させる（Fan に書かない）

### 2.3 `coldaisle-telemetry` の adapter

- `config/internal-telemetry.yaml` に `cpu_power:` 節（`path`・`stale_after_ms`・`metric: power.cpu.package`・`status` / `basis`）を足す。
  既定は無効で、導入先で所有者の承認とともに有効にする（ほかの sensor と同じ）
- 読めない・JSON が壊れている・`schema_version` が違う・`watts` が整数でない → `missing`。`ts_ms` が `stale_after_ms`（2500 の 2 倍の
  5000）より古い → `stale`。`ts_ms` が収集の時刻より `max_future_skew_ms`（1000）を超えて未来 → `missing`（時計の巻き戻しで
  止まった値を新しいまま扱い続けないため。欠けると健全度は `DEGRADED` に戻り安全側）。値は W のまま保存する
- fand の側は変えない。`power.cpu.package` はすでに制御入力の契約にある（重要度 `DEGRADED`、`telemetry.cpu_power_ms` 5000）

### 2.4 有効にすると変わること

- fand の telemetry の健全度が `NORMAL` になり、Reactive Guard は通常の閾値を使う
- `safety.yaml` の CPU Power の cooling floor が効く。CPU の全スレッドの負荷（約 200 W）では Top が 1.0 になる
  （値は 0119 §2.4 の `provisional`。0121 の試験に CPU Power の floor と `cpu_power_ms` を足して確定する）
- `fan-policy.yaml` の Guard の `cpu_power_metric` は `null` のまま（CPU 電力の上昇での発火は使わない。変えるなら別の記録）

## 3. Consequences

### 良くなること

- PLATYPUS の対策を緩めずに CPU の電力を使える。一般のユーザーが読めるのは 1 s の平均の 1 W 単位だけ
- Guard が通常の閾値に戻り、アイドルで Guard の時間が減る
- CPU Power の floor と `cpu_power_ms` を試験して確定できるようになる

### 悪くなること・その緩和

- root で動くプロセスが1つ増える。標準ライブラリだけ・capability なし・ネットワークなし・書けるのは `/run/coldaisle-power` だけに絞る
- 1 s の平均なので、ms 単位の急な電力の変化は見えない。制御は 1 s の tick で、Guard も W/s の変化を使うので足りる
- CPU 負荷で Top が全開になる時間が増える（音）。安全側

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| `energy_uj` をグループ `coldaisle` に読ませる（udev / tmpfiles） | PLATYPUS の対策をそのグループに対して緩める（所有者の選択） |
| `coldaisle-telemetry` に `CAP_DAC_READ_SEARCH` を与える | すべてのファイルの読み取りの検査を越える強い権限で、範囲が広すぎる |
| 専用のサービスが DB へ直接書く | root のプロセスに DB と `coldaisle` パッケージを持たせることになる。渡すのは値だけにする |
| hwmon の `amdgpu` の `PPT` を使う | 内蔵 GPU だけの電力で CPU Package ではない |

## 5. 未決事項

| # | 内容 | 時期 |
|---|---|---|
| 1 | 0121 に CPU Power の floor と `cpu_power_ms` の試験を足す | 本記録の実装の後 |
| 2 | Guard の `cpu_power_metric` を使うか | 運転データの後、別の記録 |
