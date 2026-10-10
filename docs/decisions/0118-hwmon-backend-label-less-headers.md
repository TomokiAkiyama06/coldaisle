# 決定記録 0118: 実機の hwmon backend・label の無い header の特定・Max の書き方

- **種別**: Decision Record
- **Status**: FINAL（2026-10-10 所有者が承認。段階 A の結果を受けた 2.3 / 2.4 と、Codex の P1 3件を受けた 2.1 / 2.2 / 2.3 の修正も同日に承認。2.3a も同日に承認）
- **Date**: 2026-10-10
- **Supersedes**:
  - [0028](0028-fan-control-contracts.md) §2.7「異常終了」の制約の表の1行目（書ける値）と
    「制御を取るとき」の Max の書き方のみ（2.3）。引き継ぎ記録を書く時点・記録の中身・正常停止の規則は変えない
  - [0080](0080-fand-systemd-unit.md) §2.5 の記録の検査の項（`hwmonN/<属性>` の path へ書く・driver 名と label の照合・
    書けるのは Max と manual）のうち、**書き込み先を書く時点で探し直すこと（2.2）・`label: null` の照合（2.2）・
    書ける値が `pwmN_enable=0` と `pwmN=255` になること（2.3）のみ**。実行部の起動方法・記録の有無で決めることは変えない
  - [0080](0080-fand-systemd-unit.md) §2.7 の書き手の4（既存の記録の元の値を引き継ぐ条件）のうち、
    **照合に `pwm_attribute` と `enable_attribute` を足すことのみ**（2.2）
  - [0080](0080-fand-systemd-unit.md) の、引き継ぎ実行部が「Max・manual を書く」とする記述
    （§2.4 の起動拒否の箇条・§2.5・§2.6 の終了コード 7 の箇条・§2.7 の書き手の4 の「実行部が書いた Max・manual」）の
    **書く値のみ**。すべて「2.3 の Max（`pwmN_enable=0`。拒否されて manual のときだけ `pwmN=255`）」と読み替える。
    takeover の失敗を数える規則・記録を消さない規則・再起動の規則は変えない
- **関連**: [0028](0028-fan-control-contracts.md)（§2.7 / 未決 6） /
  [0080](0080-fand-systemd-unit.md)（§2.7 / §2.8 / §2.10 段階 4 / §5 の 4・11） /
  [0060](0060-control-loop-runtime.md)（未決 1） / `docs/fan-hardware-backend.md` / `docs/fan-header-mapping.md`
- **対象 Issue**: #74 / #78（#57）

## 1. Context

`coldaisle-fand` の Hardware Backend は `SimulatedFanBackend` しか無い（`docs/fan-hardware-backend.md`）。
導入先の GPU サーバーでは監視系の常駐を始めた。実機で Fan 制御を取るには実機の backend が要る。
着手の前提（#75 の header 対応、#57 の権限、0028 §2.7 の takeover / handoff）のうち、header 対応は
`docs/fan-header-mapping.md` に記録が揃った（Front=`pwm6`、Rear=`pwm5`、Top=`pwm2`。driver は `nct6799`）。

調べると、既存の約束が**導入先のチップと噛み合わない**点が3つある。

1. **`fan-hardware.yaml` の `label` が何を指すかが決まっていない。** 0028 は「driver 名・label・属性名で特定し、
   `hwmonN` の番号は書かない」とだけ決めた。`FanHeader.label` は必須である。
   一方、`nct6799` には Fan の `fanN_label` / `pwmN_label` が無い（温度の `tempN_label` だけがある）。
   `config/internal-telemetry.yaml` は同じ理由で、label ではなく driver 内の channel で Fan を指している
2. **引き継ぎ実行部（`safety_handoff.py`）は `(fan|pwm)N_label` のファイルを必須にしている。**
   導入先では記録が作れず、異常終了時に Max を書けない
3. **Max の書き方が導入先のチップで成り立たない。** 2026-10-10 に導入先で確かめた（2.6）。
   `nct6799` は自動制御（`pwmN_enable=5`）のまま `pwmN` へ書くと `EBUSY` で拒否する。
   実行部の「自動のまま `pwmN=255` を書き、読み戻しが 255 なら manual にする」も、fand の `STARTUP` の Max も、
   最初の書き込みで失敗する。また manual で `pwmN=255` のとき、ドライバは `pwmN_enable` を `0` と報告する

## 2. Decision

### 2.1 label の無い header は `label: null` を明示して、driver の一意性で特定する

- `fan-hardware.yaml` を schema version 2 にする。`label` は**必須のまま**、値に `null` を許す
  - `null` は「この driver は Fan の label を持たない」という宣言。省略は v1 と同じく拒否する
    （黙って label 無しの特定に落とさない）
  - 文字列のときは従来どおり。`fanN_label` を探し、無ければ `pwmN_label` を探す。番号は `pwm_attribute` と同じ
- `label: null` の header は、**hwmon の `name` が driver 名に一致する device が、ちょうど1つ**のときだけ特定できたとする。
  0 個・2 個以上なら「header を一意に特定できない」（0028 §2.7）として**制御を取らない**
- channel は既存の `pwm_attribute` / `tach_attribute` / `enable_attribute` が表す。新しい欄は足さない
- v2 では **`tach_attribute` の番号を `pwm_attribute` と同じに限る**（`fan2_input` と `pwm2`）。番号が違うと、
  label で確かめた Fan とは別の Fan の回転数を読む設定を許し、止まった Fan を回っている別の Fan が隠しうる。
  導入先の3 zone はどれも同じ番号である（`docs/fan-header-mapping.md`）
- 3 zone の重複の検査（driver・label・`pwm_attribute`）は、`null` を1つの値として同じに扱う
- v1 は v2 として補完しない（読み込み時に拒否し、`fan-hardware.yaml` の不正として扱う）
- 束ねた版 `CONTROL_CONFIG_VERSION` を **14 → 15** に上げる（ほかの3ファイルの版は変えない）。
  ファイルの版を上げるたびに束ねた版を上げてきた規約（`control/config.py`・`docs/control-config.md`）に従い、
  trace と Learned の frame が新しい hardware の形を名乗るようにする
  - 移行: 運用の `fan-hardware.yaml` を v2 の形（`label` を明示、`tach_attribute` の番号を揃える）に書き直し、
    最後に `schema_version: 2` へ上げる。いまは導入先に運用のファイルが無い
  - 束ねた版に依存する保存物（trace の読み手・registry の artifact の束縛など）の扱いは、v13 → v14 のときと同じ手順を
    段階 B で確かめ、`docs/control-config.md` に「Control Config v15 と `fan-hardware.yaml` v2」の節を足す

### 2.2 引き継ぎの記録と実行部も label を省ける形にする

- 記録の `label_path` / `expected_label` を、`label: null` の header では `null` にする。記録の schema version を 2 に上げる
- 実行部は `expected_label` が `null` の zone で、次の2つを確かめてから書く
  - `name` が記録と一致する
  - `/sys/class/hwmon` の下で、同じ `name` の device がちょうど1つ
- **起動時に既存の記録の元の値を引き継ぐ条件**（0080 §2.7 の4）に、zone・driver 名・label に加えて
  **`pwm_attribute` と `enable_attribute` の一致**を足す。`label: null` では3 zone の (driver, label) が同じになり、
  対応表を変えた後に残った記録から、別の header の元の値を引き継ぎうるため。
  一致しない記録は引き継がず、0080 §2.7 の4のとおり error を残して上書きする
  （いまの値が実行部の Max なら、元の値は manual か全速になり、正常停止でも Max のまま終える。冷却は弱まらない）
- **実行部は、書く時点で hwmon の device を探し直す。** 記録の `hwmonN` は監査のためだけに残し、書き込み先の決定には使わない
  - `label: null` の zone は、同じ `name` の device がちょうど1つのときにその device を使う
  - label のある zone は、`name` と、`tach_attribute` の番号の `fanN_label`（無ければ `pwmN_label`）が記録と一致する device が
    ちょうど1つのときに使う
  - 0 個・2 個以上なら、その zone には書かず失敗として報告する
  - ドライバの再 bind で `hwmonN` が変わっても、記録の古い path に書こうとして Max を書けない、ということが無い
- 標準ライブラリだけで書く・記録が無ければ何もしない、は変えない
- v1 の記録は読める（label のある header の導入先を壊さない）

### 2.3 Max は `pwmN_enable=0` で書く（0028 §2.7 の書ける値を置き換える）

hwmon の ABI では `pwmN_enable=0` は「制御なし（全速）」である。導入先では、どの状態からでも書け、
すぐに `pwmN=255` になった（2.6）。下げる瞬間が無い。

**引き継ぎ実行部**が書けるのは `pwmN_enable=0` と `pwmN=255` だけにする。値は引数・設定・記録から受け取らない。

1. `pwmN_enable=0` を書く
2. `pwmN` を読み戻し、`255` なら成功とする
3. 1 が拒否されたか、2 が `255` でなければ、`pwmN_enable` を読む。**`1`（manual）のときだけ**
   `pwmN=255` を書き、`pwmN` と `pwmN_enable` を読み戻して、`pwmN` が `255` かつ `pwmN_enable` が `0` か `1` なら成功とする。
   `pwmN_enable` が 2 以上（自動）なら `pwmN` には書かず、失敗として報告する。自動のまま一瞬 `255` が読めても、
   実行部が終わった後に自動制御が下げうるため、成功と扱わない
4. `pwmN_enable=1` は書かない（Max を保つのに要らない。導入先では `0` と区別して読めない）

**fand が制御を取るとき**（記録を書いた後の `STARTUP` の Max）

1. `pwmN_enable=0` を書く（全速。ここで Max になる）
2. `pwmN_enable=1` を書く（manual。`pwmN` は 255 のまま残る）
3. 以降は毎 tick `pwmN` を書く。ただし **255 未満を書く前に `pwmN_enable` が `0` と読めたら、先に `pwmN_enable=1` を書く**。
   `0`（全速）のまま `pwmN` だけを書くと、driver によってはモードが変わらず、読み戻しの `pwmN` は下がっても Fan は全速のまま残りうる。
   導入先では manual の 255 が `0` と読めるので、Max から下げる最初の tick では毎回この手順を通る

`config_invalid` の経路の Max（0080 §2.7 の5）も同じ順にする。

### 2.3a 扱える driver を、`pwmN_enable` の意味を確かめたものに限る

`pwmN_enable=0` を全速として扱うのは、2.6 で確かめた driver だけにする。ドライバによっては `0` が
Fan を止める意味になる（例: Linux の `pwm-fan` では PWM と電源の停止）。

- 実機 backend と引き継ぎ実行部は、**確かめ済みの driver の一覧**を持つ。いまは `nct6799` だけ
  - 一覧はコードの定数として両方に置く。実行部は設定・パッケージに依存しないため、設定には置かない。
    2つの定数が一致することを試験で確かめる
- `--backend hwmon` で一覧に無い driver を指定されたら、**制御を取らない**（`pwmN_enable` に触らず、BIOS の制御のまま
  終了コード 5 で終える。0080 §2.7 の3と同じ扱い）。`--backend simulated` は driver を問わない
- 実行部は、記録の driver が一覧に無ければ、その zone に何も書かず失敗として報告する
- driver を一覧に足すには、2.6 と同じ確認（自動の間の書き込み、`0` で全速になるか、manual の 255 の読み戻し、自動へ戻せるか）を
  導入先で行い、新しい決定記録で決める

### 2.4 `ENABLE_REVERTED` は「自動制御へ戻された」ときだけにする

- 読み戻した `pwmN_enable` が **2 以上**のときだけ `ENABLE_REVERTED` とする
- `0` は manual の 255 と区別できない（2.6）。どちらも全速で、冷却を弱めないので fault にしない
  - `0` のまま `pwmN` を下げようとして書けない・保たれないときは、既存の `WRITE_FAILURE` / `READBACK_MISMATCH` が拾う
- `1` は正常

### 2.5 実機 backend の振る舞い

`src/coldaisle/control/hardware/hwmon.py` に `HwmonFanBackend` を置く。`FanHardwareBackend` Protocol を実装し、
`SimulatedFanBackend` と同じ写像（profile・`stable_demand()`・`applied_demand` の規則）を使う。

| 場面 | 振る舞い |
|---|---|
| 起動 | 2.1 で header を特定し、`pwmN` / `pwmN_enable` が書けて `fanN_input` が読めることを確かめる。元の `pwmN` / `pwmN_enable` を読み、0080 §2.7 の手順で記録を書く。その後に 2.3 の順で `STARTUP` の Max |
| 毎 tick | 2.3 の 3 の手順で `pwmN` を書き、`pwmN` と `pwmN_enable` を読み戻す（2.4）。**255 未満を書いた tick は、`pwmN` が書いた値と一致し、かつ `pwmN_enable` が `1` と読めたときだけ `readback_ok`** とし、そうでなければ `READBACK_MISMATCH`（`applied_demand` を返さない）。`fanN_input` を読み、読めなければ `rpm=None` |
| 特定の再確認 | 毎 tick、header の device の `name` を読み直す。変わっていれば（ドライバの再 bind など）`WRITE_FAILURE` とし、既存の連続失敗の規則（`write_fail_emergency_after` / `hardware_write_fail_exit_ms`）に任せる。backend 自身は運転中に device を探し直さない（終了の後に実行部が 2.2 で探し直して Max を書く） |
| 正常停止 | 0080 §2.8 のとおり。導入先では元の `pwmN_enable=5` へ書けば BIOS の曲線へ戻った（2.6） |

- **`pwmN` / `pwmN_enable` 以外の属性には書かない**（`pwmN_mode`・`pwmN_floor`・`pwmN_start`・`pwmN_auto_point*`・
  `pwmN_step_*` など）。BIOS が決めた値をそのまま残し、返したときに BIOS の制御が元どおりに動くようにする
- path は起動時に1回だけ解決し、`hwmonN` の番号を設定にも記録の外にも出さない
- `coldaisle-fand` に `--backend {simulated,hwmon}` を足す。**既定は `simulated`**。
  `deploy/systemd/coldaisle-fand.service` の `ExecStart=` に `--backend hwmon` を入れる（unit は引き続き `enable` しない）

### 2.6 導入先での確認の記録（段階 A。2026-10-10、所有者の立ち会い）

Top（`nct6799` の `pwm2` / `fan2`）だけで行った。各段の前後で `pwm2` / `pwm2_enable` / `fan2_input` を1秒ごとに読んだ。

| 段 | 書いた値 | 結果 |
|---|---|---|
| A-1 | `enable=5` のまま `pwm2=255` | `EBUSY` で拒否。値は変わらず、BIOS の曲線が続いた |
| A-2 | `pwm2_enable=0` | 直後に `pwm2=255`。約4秒で 1511 → 約 2400 rpm |
| A-3 | `pwm2_enable=1` | `pwm2=255` のまま。`pwm2_enable` は書いた直後だけ `1`、約1秒後から `0` と読めた |
| A-4 | `pwm2_enable=5` | BIOS の曲線へ戻った（`pwm2` は約8 / 秒で下がった） |
| A-5 | `0` → `1` → `pwm2=254` → `pwm2=255` → `1` → `5` | `pwm2=254` では `enable=1`。`255` にすると約1秒後に `0`。`1` を書き直しても同じ。manual の間は `pwm2` を書けた。最後に `5` で BIOS へ戻った |

以後の段階:

| 段階 | 内容 | 誰が |
|---|---|---|
| B（実装） | 2.1〜2.5。偽の sysfs で試験する。偽の sysfs は 2.6 の挙動（自動の間の `EBUSY`、manual の 255 が `0` と読める）を再現する | エージェント |
| C（実装の後） | 0080 §2.10 の段階 4・5 と、0028 §2.9 の承認点 3。3 zone で、Max から 255 未満へ下げたときに tach の回転数が実際に下がることも確かめる | 所有者 |

## 3. Consequences

- `nct6799` のように Fan の label を持たないチップでも、0028 §2.7 の「一意に特定できなければ制御を取らない」を保ったまま特定できる
- 実行部が導入先でも記録を読め、自動制御のままでも manual でも Max を書ける
- label のある導入先は、v2 へ上げるとき値を書き換えずに済む
- `fan-hardware.yaml` の版が上がるので、運用のファイルは書き直しが要る（いまは導入先に運用のファイルが無い）
- 外から `pwmN_enable=0` にされても fault にならない。全速なので冷却は弱まらない。
  その状態で `pwmN` を下げられないときは、書き込みの失敗として既存の規則で扱われる
- 同じ driver 名のチップが2つある機械では `label: null` を使えない（制御を取らない）。必要になったら、
  hwmon の親 device で区別する方法を別の記録で決める（§5）
- 2.6 は Top の1 zone だけで確かめた。Front / Rear も同じチップの同じ実装だが、段階 C で3 zone を確かめる

## 4. 却下した代替案

| 案 | 却下した理由 |
|---|---|
| `label` を省略可能にする | 書き忘れと「label が無い」の宣言を区別できない |
| `channel` の欄を新しく足す（Telemetry の設定と同じ形） | `pwm_attribute` と同じことを2か所に書くことになり、食い違いを検査する手間が増える |
| マザーボード上のヘッダ名（CHA_FAN1 など）を label に書く | sysfs から読めず、照合に使えない。対応も記録されていない |
| 実行部が記録の `hwmonN` の path にそのまま書く | ドライバの再 bind で番号が変わると Max を書けない。fand は書き込みの失敗の連続で終了するが、終了の後に Max を書く者が居なくなる |
| fand が運転中に device を探し直して書き続ける | 書き手が別の device へ移る経路を運転中の制御に持たせることになる。終了して実行部に任せれば、Max を書くことだけに限れる |
| `pwmN_enable=0` をどの driver でも全速として扱う | `0` が停止を意味する driver では、異常終了時に Fan を止める |
| 確かめ済みの driver の一覧を設定に置く | 実行部が設定に依存することになる。設定の書き換えだけで、確かめていない driver に `0` を書けるようになる |
| 自動のまま `pwmN=255` が読めたら成功とする | 実行部が終わった後に自動制御が下げうる |
| hwmon の親 device の名前（`nct6775.<port>` など）でも照合する | 導入先では driver 名が一意で足りる。値が導入先ごとに違い、テンプレートに書けない |
| 既定の backend を `hwmon` にする | 開発機で引数を付け忘れると、実機の Fan に書く |
| Max を `pwmN_enable=1` → `pwmN=255` の順で書く | manual に切り替えた瞬間から 255 を書くまで、BIOS が直前に出していた値に固定される。`enable=0` なら下がる瞬間が無い |
| Max の後は `pwmN` だけを書く | `0` のまま `pwmN` を書いてもモードが変わらない driver では、Fan が全速のまま `applied_demand` が下がった値を名乗る |
| `ENABLE_REVERTED` を `pwmN_enable != 1` のまま残す | 導入先では Max のたびに fault になり、Max から抜けられなくなる |
| `pwmN=255` のときだけ `0` を許す | 読み戻しの `pwmN` と `pwmN_enable` は別の read で、間にドライバの更新が挟まりうる。全速の `0` を fault にする安全上の理由も無い |

## 5. 未決事項

| # | 内容 | どこで |
|---|---|---|
| 1 | Front / Rear で 2.6 と同じ挙動になるか | 段階 C |
| 2 | 同じ driver 名のチップが複数ある機械での特定 | 必要になったら |
| 5 | `tach_attribute` と `pwm_attribute` の番号が違うのが正しい導入先での特定 | 必要になったら |
| 3 | 0028 未決 6（`ExecStopPost` も動かないときにチップが PWM をどう保つか） | 段階 C |
| 4 | 0060 未決 1（外から `pwmN_enable` を戻されたときに Max を書き直すか） | 段階 C の後 |
