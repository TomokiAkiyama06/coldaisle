# Fan Hardware Backend（#77）

M8 では `SimulatedFanBackend` のみを提供する。これは `DemandComposer` が発行した
`ComposedDemands` を profile の target RPM / Airflow Index / raw PWM に写像し、readback と zone fault を
返すメモリ内の実装である。sysfs、`hwmonX`、subprocess、実機 PWM の書込みは行わない。

実機 backend は同じ `FanHardwareBackend` Protocol を実装するが、次が揃うまで追加しない。

- #75 の characterization による profile と header の確認
- #57 による `coldaisle-fand` 専用の OS 権限、udev/systemd の allow-list、単一 writer
- 決定記録 0028 §2.7 の takeover / handoff と異常停止時 Max

`FanHardwareConfig` は driver・label・`pwmN` / `fanN_input` / `pwmN_enable` の組だけを
受け、`hwmonN` の番号・絶対 path・任意 header の指定を拒否する。backend の公開入口は
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
使い、その後も minimum stable demand を下限とする。profile の値はすべて設定由来であり、
実測値をコードへ追加しない。
