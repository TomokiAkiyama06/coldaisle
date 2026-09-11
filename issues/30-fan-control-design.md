---
title: "【設計のみ】段階式ファン制御の安全設計検討"
labels: design, safety, priority:must
milestone: "M7 拡張"
---

## 背景
実機運用で、GPU高負荷時にケースファンがGPU発熱へ十分追従しないことが確認された。
Front + Rear は同一Fan Hub系統として扱い、通常のケース換気はここを主制御する。

現在のFan topology:
- Front Intake: Noctua NF-A12x25 G2 ×3
- Rear Exhaust: Antec FLUX 純正Rear Fan ×1
- Top Exhaust / CPU Radiator: Cooler Master MasterLiquid Atmos II、120mm Fan ×3
- AIO Pump: BIOS / 安全設定で管理

フロント3基の吸気能力に対してRear 1基だけでは排気が不足する可能性がある一方、TopはCPU AIOのラジエーターファンでもある。
そのため、Topを常時ケース制御へ入れるのではなく、**Front + Rearで不足した場合だけ補助排気として上げる段階式制御**を採用する。

ファン制御の実装自体は別Issueで行うが、**高額ハードウェアの冷却制御なので安全設計を先に確定する。**

## 制御方針

### Stage 1: Front + Rearを主制御

通常時は **Front Intake ×3 + Rear Exhaust ×1** を主制御する。

GPU powerをfeed-forward、GPU Intake / Rear Exhaust / `d.case_delta = rear_exhaust - front_intake` をfeedbackとして使い、まずケースFan Hub側で必要な換気を行う。

### Stage 2: Top Radiator Fanは補助排気

Front + Rearを十分に上げても熱を捌き切れない状態が一定時間継続した場合のみ、Top Radiator Fanを追加で上げる。

TopをGPU負荷へ常時連動させない。
Top補助の候補判定入力:
- Case Fan HubのPWM / RPM
- `d.case_delta`
- GPU Intake / Rear Exhaust / Top Exhaust
- GPU power / core / hotspot
- CPU温度 / CPU側の冷却要求

正確な閾値・継続時間は #19 のベースライン実測後に確定する。

### CPU冷却を絶対に弱めない

Top Radiator FanはCPU AIOの冷却ファンでもあるため、coldaisleはCPU側の冷却要求を下げてはいけない。

- coldaisleのTop介入は原則「追加で上げる」方向だけ
- CPU/BIOS側要求を安全下限として扱える仲裁方法を実機確認する
- 安全な仲裁が確認できない場合、TopはBIOS管理のままとしソフトウェア制御しない
- AIO Pumpはcoldaisleから制御しない
- VRM Fanも初期版ではBIOS管理を維持する

## 風量の扱い

ファン径とRPMだけから絶対CFMを推定して制御しない。

理由:
- 羽根形状・モーター・静圧特性が異なる
- Frontはケース前面抵抗を受ける
- Topはラジエーター抵抗を受ける
- 同じRPMでも実流量は一致しない

扱い:
- メーカー公称の最大風量 / 最大RPM / 静圧を基礎情報として保持
- PWM duty、実RPM、`RPM / 最大RPM` を相対的な Airflow Proxy として使う
- 最終的な制御判断は `d.case_delta`、GPU Intake、Rear/Top Exhaust、GPU温度・GPU power等の**実際の熱応答**を優先する
- 正確なCFM実測を受入条件にはしない

## やること（設計のみ。コードを書かない）

- [ ] `docs/decisions/NNNN-fan-control-safety.md` を作成
- [ ] BIOS Q-Fan を最終防衛線として残す方法を実機で確認
- [ ] Linux hwmon上の `pwmX` / `fanX_input` と物理ヘッダーの対応を特定
- [ ] Front + Rear Fan Hubを制御しているヘッダーを特定
- [ ] Top Radiator Fanを制御しているヘッダーを特定
- [ ] Topを補助排気として上げる際のCPU冷却要求との仲裁方法を確認
- [ ] デッドマンスイッチ: 更新停止時に安全側へ移行する
- [ ] Front + Rearの最低PWM / 起動PWM / 停止しない下限を実測で確定
- [ ] Topの安全な制御範囲を実測で確定（制御可能な場合のみ）
- [ ] 指定PWMに対してRPMが上がらない場合の `FAN_FAULT` を定義
- [ ] NVML取得失敗時のフェイルセーフを定義
- [ ] T_SENSOR取得失敗時のフェイルセーフを定義
- [ ] fan daemon異常終了・SIGTERM・OS shutdown時の振る舞いを定義
- [ ] 起動直後 / センサー未初期化中は安全側の高回転とする
- [ ] `hwmonX`番号を固定せず、driver名/属性から安定して探索する方法を決める
- [ ] AIからfan daemonへの到達経路を作らないことを構造で保証する

## フェイルセーフ原則

以下のいずれかを検出した場合、**Front + Rearは安全側（原則100%）**へ倒す。

- NVMLのGPU telemetryが一定時間取得できない
- T_SENSOR / 主要温度センサーがstaleまたは取得不能
- fan daemon内部エラー
- Fan Hubへ十分なPWMを出しているのにtachが下限を下回る
- 制御ループのheartbeatが期限切れ

TopはCPU冷却を最優先する。
Top制御の安全性を実機で保証できない場合はBIOS管理から外さない。
BIOS自動制御へ安全に復帰できることを実機で確認できた場合のみ、障害時のBIOS復帰を選択肢にする。

## 受入基準

- 通常時はFront + Rearだけで制御が成立する設計になっている
- Front + Rearだけでは不足する条件でのみTop補助が発動する
- Top補助によってCPU冷却要求を下回らない
- 風量の絶対CFMを前提にせず、RPM/PWM proxy + 熱応答で判断する
- 「ソフトウェアが全停止しても冷却が止まらない」ことが設計上保証されている
- 対象外Fanへ誤ってPWMを書かない識別手順がある
- fault injection（NVML停止、センサー欠測、daemon kill、tach異常）の期待動作が決まっている
- 人間レビューでADRが承認されるまで #43 の実装を開始しない

## 依存
#34, #19
