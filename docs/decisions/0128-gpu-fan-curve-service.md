# 決定記録 0128: GPU の Fan を、温度の曲線で書く root の小さな専用サービス（`coldaisle-gpu-fan`）で制御する

- **種別**: Decision Record
- **Status**: Proposed（2026-10-14 にリポジトリ所有者が承認。未決 1 を閉じ、レビューの後に FINAL）
- **Date**: 2026-10-14
- **Supersedes**: なし
- **関連**: [`0028`](0028-fan-control-contracts.md) /
  [`0080`](0080-fand-systemd-unit.md) §2.5 /
  [`0119`](0119-safety-values-first-host.md) §2.12 /
  [`0121`](0121-hardware-test-procedure.md) §2.1 / §2.7 / §2.9 /
  [`0122`](0122-cpu-power-publisher.md)（同じ形の root の小さなサービス） /
  `docs/fan-header-mapping.md`「0121 の試験の結果」T5
- **対象 Issue**: #285

---

## 1. Context

2026-10-14 の 0121 T5 で、GPU 600 W の定常を作ろうとした。fand が筐体の Fan（Front / Rear / Top）を 0.96 まで上げても、
負荷の開始から約 80 s で GPU コアが 80 °C（0121 §2.1 の中止の基準 83 °C の 3 °C 手前の見張り）に届いた。
このとき GPU（RTX PRO 6000 Blackwell Workstation Edition、Fan 2 基）の Fan は、GPU 自身の既定の制御で 47 % だった。
既定の制御は静音寄りで、64 °C で 36 %、79 °C で 47 % だった。

NVML で GPU の Fan を固定し、fand を本番の設定・auto で動かして GPU 600 W をかけた（2026-10-14 12:01〜12:15）。

| GPU の Fan | GPU コア | 12V-2x6 | 筐体の Fan の Demand |
|---|---|---|---|
| 既定の制御（47 % まで） | 約 80 s で 80 °C（中止） | 44 °C | 0.96 |
| 70 % 固定 | 約 3.8 分で 80 °C（中止。まだ上がっていた） | 49 °C | 0.96 |
| 100 % 固定 | 約 4 分で 74 °C で落ち着いた | 48 °C | 0.83 |

GPU の熱は主に GPU 自身の Fan で冷える。600 W の負荷が続く間を中止の基準の内側で運転するには、GPU の Fan をほぼ全開にする
必要がある。GPU 自身の温度の基準は、上限（`GPU_MAX`）93 °C・slowdown 95 °C・shutdown 98 °C。

この GPU は NVML の `nvmlDeviceSetFanSpeed_v2` で Fan の回転を指定できる（30〜100 %）。指定すると、GPU 自身の自動の制御は止まる。
`nvmlDeviceSetDefaultFanSpeed_v2` で自動の制御に戻る。driver を読み込み直したときも自動に戻る。

所有者は、本番でも GPU の Fan を制御する方針を選んだ（2026-10-14）。方式は、次の3案から「温度の曲線の専用サービス」を選んだ。

- 温度の曲線の専用サービス
- 常に 100 % 固定（アイドルでも大きな音が続く）
- fand の 4 つ目の系統にする（3 系統を前提にした設計と試験を広く変える）

## 2. Decision

### 2.1 位置づけ: fand の対象外で、AIO ポンプと同じ「安全側の決まった運用」

- GPU の Fan は fand の Control Pipeline の対象（Front / Rear / Top）に入れない。fand・Learned MPC・Supervisor・LLM は、
  GPU の Fan を読まないし書かない。AGENTS.md の「AIO Pump は通常制御対象外。高い安全側の固定運用」と同じ扱いにする
  （AGENTS.md ルール 2 の Control Pipeline は筐体の 3 系統に対するもの）
- 曲線は決定論的で、ML を使わない。GPU 自身の既定の制御より、どの温度でも同じか高い回転にする（2.3）
- このサービスが止まったときは、GPU 自身の既定の制御に戻す（2.4）。これは今の運用と同じ状態で、GPU は 93 / 95 / 98 °C で自分を守る
- fand は変えない。fand の Fallback は今までどおり GPU の温度で筐体の Fan を上げる
- 制御系は GPU AI Service に依存しない（AGENTS.md）。このサービスも NVML だけを使い、GPU AI Service・DB・telemetry に依存しない
- AGENTS.md の「制御アーキテクチャの固定前提」と「ファイル構成」に、本サービスを足す（本記録の実装の PR で行う）

### 2.2 専用のサービス `coldaisle-gpu-fan`

- **中身**: `src/coldaisle/gpu_fan_curve.py`。0122 と同じく**標準ライブラリだけ**を使い、`coldaisle` パッケージを import しない。
  NVML は `ctypes` で `libnvidia-ml.so.1` を開いて使う（pynvml に依存しない）。システムの `/usr/bin/python3 -I -S <ファイル>` で動かす
- **対象**: NVML の device がちょうど 1 つのときだけ制御する。0 個・2 個以上なら何も書かずに終了コード 2 で終わる
  （知らない構成の GPU に書かない）。起動時は、device が見つかるまで 1 s ごとに探し直し、60 s（引数）を過ぎたら終了コード 2
- **周期**: 1000 ms ごと（単調時計）に GPU コアの温度（`nvmlDeviceGetTemperature`）を読み、曲線から目標の回転（%）を求める。
  各 Fan の目標（`nvmlDeviceGetTargetFanSpeed`）が求めた値と違えば `nvmlDeviceSetFanSpeed_v2` で書く。
  driver が自動に戻したときも、次の周期で書き直す
- **上げ下げ**: 上げるのはすぐ。下げるときは、温度にヒステリシス（2 °C）を足した値で曲線を引き、1 s あたり 1 % までしか下げない
  （100 % から 30 % まで約 70 s。温度の小さな揺れで回転が上下しないようにする）
- **引数**: 曲線・周期・起動時に device を待つ時間・ヒステリシス・下げる速さは、**すべて unit が渡す必須の引数**にし、
  コードに既定値を持たない（AGENTS.md ルール 9。0122 と同じく YAML を読まない）
- **ログ**: JSON Lines を標準エラーへ出す（journal）。目標の回転が変わったとき・書き直したとき・エラーのときだけ出す
- **出力**: ファイルや socket は持たない。回転は `coldaisle-telemetry` がすでに `gpu.0.fan_speed` として集めている

### 2.3 曲線（`provisional`）

| GPU コア | 回転 |
|---|---|
| 45 °C 以下 | 30 %（NVML の最小。アイドルは今と同じ） |
| 55 °C | 50 % |
| 65 °C | 80 % |
| 70 °C 以上 | 100 % |

点の間は直線で補う。

- 引数の検査: 温度は真に増加、回転は減らない（単調非減少）、回転は NVML の最小〜最大の範囲。最後の点は 100 %。
  どれかに反すれば何も書かずに終了コード 2
- 2026-10-14 に観測した既定の制御（64 °C で 36 %、79 °C で 47 %）より、どの温度でも高い
- 600 W の定常では 100 % になる。100 % 固定の試行で 74 °C に落ち着いたので、曲線でも同じになる見込み（2.7 で確かめる）
- 値は導入先の試験（2.7）で確かめるまで `provisional`。変えるときは決定記録と所有者の承認を要する（0028 §2.9 の Safety の値と同じ扱い）

### 2.4 止めたとき・落ちたとき

| 場面 | 動き |
|---|---|
| `systemctl stop`（SIGTERM） | 各 Fan を `nvmlDeviceSetDefaultFanSpeed_v2` で自動に戻して終わる |
| 落ちた・SIGKILL・hang（watchdog） | unit の `ExecStopPost=` が、自動に戻す小さな処理（同じファイルの `--restore-default`）を走らせる。その後 `Restart=always` で起動し直す |
| 温度が読めない・書けない（NVML のエラー） | 自動に戻して終了コード 1。unit が起動し直す |
| driver の読み込み直し・GPU のリセット | NVML が自動に戻す。サービスはエラーで終わり、起動し直して曲線を書き直す |

hang の検出に `WatchdogSec=5s` を使う（`sd_notify` は `socket` で `NOTIFY_SOCKET` に送る。標準ライブラリだけで書ける）。
落ちたときに 100 % へ固定するのではなく自動に戻すのは、自動は GPU 自身が守る状態で、今の運用と同じだからである。
自動のままだと 600 W の定常では 80 °C を超えうるが、GPU は自分の上限で守り、fand の Fallback が筐体の Fan を上げる。

### 2.5 unit `deploy/systemd/coldaisle-gpu-fan.service`

- `User=root`。capability は**まず持たせずに**確かめ、NVML の書き込みが拒まれたら `CAP_SYS_ADMIN` だけを足す
  （実装の前に、導入先で `systemd-run` で確かめて本記録の未決 1 を閉じる）
- device: `DevicePolicy=closed`・`DeviceAllow=/dev/nvidiactl rw`・`DeviceAllow=/dev/nvidia0 rw`（足りない device は未決 1 と一緒に確かめる）
- sandbox は 0122 に合わせる: `NoNewPrivileges=yes`・`ProtectSystem=strict`・`ProtectHome=yes`・`PrivateTmp=yes`・
  `PrivateNetwork=yes`・`RestrictAddressFamilies=AF_UNIX`・`ProtectKernelModules=yes`・`ProtectControlGroups=yes`・
  `LockPersonality=yes`・`RestrictRealtime=yes`・`SystemCallArchitectures=native`・`ProtectClock=yes`・`ProtectHostname=yes`
- `Type=notify`・`WatchdogSec=5s`・`Restart=always`・`RestartSec=2`・`RestartPreventExitStatus=2`
- `After=systemd-modules-load.service`。fand・telemetry とは依存させない
- 導入先では、所有者の承認の後に `enable --now` する（Fan に書くので、監視系と違い自動では有効にしない）

### 2.6 テスト

実機が無くても通るように、NVML の呼び出しを小さな境界に分け、偽の NVML で次を確かめる（AGENTS.md ルール 7）:
曲線の補間・引数の検査・上げはすぐで下げは 1 %/s とヒステリシス・目標が違うときだけ書く・自動に戻されたら書き直す・
SIGTERM と `--restore-default` で自動に戻す・device が 0 個・2 個のとき書かない・NVML のエラーで自動に戻して終わる。

### 2.7 導入先での確認（実装の後）

1. 起動と停止: `systemctl stop`・`kill -9`・`kill -STOP`（watchdog）で、GPU の Fan が自動（`policy 0`）に戻ること
2. アイドルの回転が 30 % のままであること（音が今と変わらない）
3. GPU 600 W を 15 分: GPU コアが 80 °C 未満で落ち着くこと（100 % 固定では 74 °C）
4. その後、0121 T5 の ii-gpu600 と iii-gpu600 を、このサービスを動かした状態で行う。T7 も同じ状態で行う
   （BIOS と fand の比較は筐体の Fan の比較で、GPU の Fan は両方で同じ曲線にする）

## 3. Consequences

### 良くなること

- GPU 600 W の負荷が続いても、GPU コアを中止の基準より下に保てる見込み（100 % で 74 °C）
- アイドルの音は今と同じ（30 %）
- 筐体の Fan の Demand が下がる（100 % のとき 0.83。既定の制御では 0.96）
- サービスが止まっても今の運用（GPU 自身の既定の制御）に戻るだけ

### 悪くなること・その緩和

- root で動き、Fan に書くプロセスが1つ増える。標準ライブラリだけ・ネットワークなし・書ける device は NVIDIA の2つだけに絞る
- 負荷のときの GPU の Fan の音が大きくなる（600 W の定常では 100 %）
- fand の外に Fan の制御ができる。役割は GPU の Fan だけに限り、fand・ML・LLM からは触れない。曲線の変更は決定記録と承認を要する
- 落ちたときは自動に戻るので、その間の 600 W の定常は 80 °C を超えうる。GPU 自身の保護と fand の Fallback に委ね、
  サービスは 2 s で起動し直す

## 4. 却下した代替案

| 案 | 却下の理由 |
|---|---|
| 常に 100 % 固定（起動時に 1 回） | アイドルでも大きな音が続く（所有者の選択） |
| fand の 4 つ目の系統にする | 3 系統を前提にした設計・設定・試験（0121 の T1〜T9 の一部）を広く変える。完成形に近いが、今の段階では大きすぎる |
| `nvidia-settings` の Coolbits で曲線を設定する | X のセッションに依存し、gdm だけの導入先で常駐の制御にならない |
| `coldaisle-telemetry` に書かせる | 集める役割のプロセスに書き込みの権限を持たせることになる。役割を分ける |
| 落ちたら 100 % に固定する | `ExecStopPost=` で固定すると、サービスを止めている間ずっと全開になる。自動に戻せば今と同じ状態で、GPU が自分を守る |

## 5. 未決事項

| # | 内容 | 時期 |
|---|---|---|
| 1 | NVML の書き込みに `CAP_SYS_ADMIN` が要るか、ほかに要る device があるか | 実装の前に導入先の `systemd-run` で確かめる |
| 2 | 曲線の値を `confirmed` にする | 2.7 の確認の後 |
| 3 | GPU のメモリの温度（`gpu.0.mem`）も曲線の入力にするか | 運転データの後、別の記録 |
| 4 | 0127（T5 の GPU を 450 W に置き換え）の扱い | 2.7 の 4 の後。600 W で行えたら 0127 は取り下げる |
