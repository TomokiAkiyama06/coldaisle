# Fan Hardware Backend（#77）

M8 では `SimulatedFanBackend` のみを提供する。これは `EffectiveZoneDemand` を
profile の target RPM / Airflow Index / raw PWM に写像し、readback と zone fault を
返すメモリ内の実装である。sysfs、`hwmonX`、subprocess、実機 PWM の書込みは行わない。

実機 backend は同じ `FanHardwareBackend` Protocol を実装するが、次が揃うまで追加しない。

- #75 の characterization による profile と header の確認
- #57 による `coldaisle-fand` 専用の OS 権限、udev/systemd の allow-list、単一 writer
- 決定記録 0028 §2.7 の takeover / handoff と異常停止時 Max

`FanHardwareConfig` は driver・label・`pwmN` / `fanN_input` / `pwmN_enable` の組だけを
受け、`hwmonN` の番号・絶対 path・任意 header の指定を拒否する。backend の公開入口は
検証済みの3 zone の `PerZone[EffectiveZoneDemand]` だけなので、上位層が生 PWM や
対象外 header を渡す経路はない。

profile が表す minimum stable demand 未満へは写像しない。起動直後は startup demand を
使い、その後も minimum stable demand を下限とする。profile の値はすべて設定由来であり、
実測値をコードへ追加しない。
