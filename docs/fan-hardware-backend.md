# Fan Hardware Backend（#77）

Backend は2つある。どちらも同じ `FanHardwareBackend` Protocol を実装し、command の受理
（runtime binding・takeover の確認・tick の順序）と profile の写像（`stable_demand()`・startup の kick・
`applied_demand` の規則）を `src/coldaisle/control/hardware/_common.py` で共有する。

| backend | 選び方 | 書く先 |
|---|---|---|
| `SimulatedFanBackend` | `coldaisle-fand` の既定（`--backend simulated`） | メモリの中だけ。sysfs・`hwmonX`・subprocess に触れない |
| `HwmonFanBackend`（`control/hardware/hwmon.py`） | `--backend hwmon`（`deploy/systemd/coldaisle-fand.service` が明示する） | 実機の `pwmN` / `pwmN_enable` だけ |

既定を simulated にしているのは、開発機で引数を付け忘れて実機の Fan に書かないためである（決定記録 0118 §2.5）。

## 実機 backend（決定記録 0118 §2.5）

前提の #75（header と profile。`docs/fan-header-mapping.md`）・#57（OS 権限・udev・unit）・0028 §2.7 の
takeover / handoff（0080 §2.7 / §2.8）に従う。header の特定・Max の書き方・扱える driver は次のとおり。

- `fan-hardware.yaml` v2 の `label` は必須で、Fan の label を持たない driver（導入先の `nct6799`）は
  `label: null` と明示する。そのときは hwmon の `name` が driver に一致する device がちょうど1つのときだけ
  特定できたとする。label のある header は、同じ番号の `fanN_label`（無ければ `pwmN_label`）も一致する
  device がちょうど1つのとき（0118 §2.1。`docs/control-config.md`「Control Config v15」）。
  特定できなければ**制御を取らない**（終了コード 2）
- `pwmN_enable=0` を全速として扱うのは、確かめ済みの driver の一覧（`coldaisle.control.hardware.VERIFIED_HWMON_DRIVERS`。
  いまは `nct6799` だけ）にある driver だけ。一覧に無ければ何も開かず、`pwmN_enable` に触らずに終了コード 5。
  引き継ぎ実行部も同じ一覧を定数で持ち、一致を試験で確かめる（0118 §2.3a）
- 起動時に `pwmN` / `pwmN_enable` を書き込み用に開け、`fanN_input` を読めることを確かめ、元の `pwmN` /
  `pwmN_enable` を読む。開けない（udev の権限が付いていない など）・読めなければ制御を取らない（終了コード 5）
- 引き継ぎ記録（`/run/coldaisle/fan-handoff.json`、schema version 2）を、`pwmN_enable` に触る**前**に書く。
  同じディレクトリの一時ファイル（`O_EXCL | O_NOFOLLOW`、`0600`）→ `rename`。書けなければ制御を取らない
  （終了コード 5）。path は合成の起点（`control_daemon.py`）が引き継ぎ実行部の定数を渡し、この package は
  上位の module を import しない。既存の記録があり、3 zone すべてで zone・driver・label・`pwm_attribute`・
  `enable_attribute` が一致するときだけ元の値を引き継ぐ（一致しなければ error を残して上書きする。0080 §2.7 / 0118 §2.2）
- takeover の Max は記録の後に `pwmN_enable=0`（全速）→ `pwmN_enable=1`（manual。`pwmN` は 255 のまま）。
  `0` を書けなければ `1` を書かない。下げる瞬間が無い（0118 §2.3）。`config_invalid` の経路も同じ順
- 毎 tick: 255 未満を書く前に `pwmN_enable` が `0` と読めたら先に `1` を書く。`pwmN` を書き、`pwmN` と
  `pwmN_enable` を読み戻す。255 未満の tick は `pwmN` が一致し**かつ** `pwmN_enable` が `1` のときだけ
  `readback_ok`（でなければ `READBACK_MISMATCH`）。255 の tick は `pwmN_enable` が `0` か `1` なら成功
  （導入先の driver は manual の 255 を `0` と報告する。0118 §2.6）
- 読み戻した `pwmN_enable` が 2 以上（自動制御へ戻された）なら `ENABLE_REVERTED`。自動の間の `pwmN` の
  書き込みが拒まれた（`EBUSY`）ときも同じ。**`pwmN_enable` を書き直して取り戻さない**（0060 未決 1 /
  0118 未決 4）。`0` は全速なので fault にしない（0118 §2.4）
- `fanN_input` を毎 tick 読み、読めなければ `rpm=None`（tach の判定は Critical Safety）
- 毎 tick device の `name` を読み直し、変わっていれば書かずに `WRITE_FAILURE`。運転中に device を探し直さない
  （続けば `hardware_write_fail_exit_ms` で終了コード 7。終了の後に引き継ぎ実行部が探し直して Max を書く）
- 正常停止（SIGTERM・`--max-ticks`）: 元の `pwmN_enable` がすべて `0` または 2 以上なら、PWM を下げずに
  元の値へ戻して読み戻しで確かめ、**全 zone を戻せたときだけ**記録を消す。いずれかが `1`（manual）なら
  戻さず Max（`0` → `1`）のまま、構造化ログの event `shutdown_left_max_manual`（zone を含む）を出して記録を
  残す。途中の失敗も記録を残す（0080 §2.8）。返す手順は書込み境界とは別の `HandsBackOnStop` で、
  合成の起点は正常に終えるときだけ呼ぶ（例外・終了コード 7 では呼ばない）
- **`pwmN` / `pwmN_enable` 以外の属性に書かない**（`pwmN_mode`・`pwmN_floor` など BIOS が決めた値を残す）。
  path は起動時に1回だけ解決して fd で持つ。class entry 以外の symlink はたどらない。`hwmonN` の番号は
  引き継ぎ記録（監査のため）の外へ出さない

試験は `tests/test_hwmon_fan_backend.py`。偽の sysfs が 0118 §2.6 の挙動（自動の間の `EBUSY`、`0` で全速、
manual の 255 が `0` と読める）を再現する。実機での確認（3 zone で Max から下げたとき tach が実際に下がるか）は
0118 の段階 C で所有者が行う。

## 共通の規則

`FanHardwareConfig` は driver・label（または `null`）・`pwmN` / `fanN_input` / `pwmN_enable` の組だけを
受け、`hwmonN` の番号・絶対 path・任意 header の指定を拒否する。`fanN_input` の番号は `pwmN` と同じに限る。backend の公開入口は
検証済みの3 zone の `ComposedDemands` だけなので、上位層が個別の
`EffectiveZoneDemand`、生 PWM、対象外 header を渡す経路はない。

起動時に full validated `ControlConfig` から一意な runtime binding を発行し、
`CriticalSafety` と backend が同じ binding を1回ずつ取得する。command は full config/source
metadata とこの session の opaque lineage に束縛される。別 SafetyConfig で作った低 demand、
同じ設定でも別プロセス/runtime が作った command、replay、古い tick は、状態更新や書込みの
前に backend が拒否する。
Safety / Policy が不正で full config を構築できないときだけ、検証済み hardware 設定とその
source metadata から別の emergency session を作れる。source hash は caller から受け取らず、
同じ `fan-hardware.yaml` bytes の load・validation・SHA-256 を一体で行う。通常・emergency
session とも hardware approval が `confirmed` の場合だけ発行する。この session は config-invalid の
全 zone Max 以外を発行できず、通常 session との途中切替えも許さない。

profile が表す minimum stable demand 未満へは写像しない。起動直後は startup demand を
使い、その後も minimum stable demand を下限とする。minimum stable demand への引き上げは
`FanProfile.stable_demand()` の1箇所に置き、Learned MPC の Air Balance の項も同じ関数を使う
（決定記録 0073 §2.3）。backend は写像後・PWM 直前の demand を `FanHardwareResult.applied_demand`
として返す。`write_ok` と `readback_ok` の両方が真のときだけ値を持ち、PWM の raw 値から逆算しない
（0073 §2.5 (a)。実機 backend も同じ規則で返す）。profile の値はすべて設定由来であり、
実測値をコードへ追加しない。
