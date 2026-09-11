---
title: "【設計のみ】ファン制御の安全設計検討"
labels: design, safety, priority:must
milestone: "M7 拡張"
---

## 背景
実機運用で、GPU高負荷時にケースファンがGPU発熱へ十分追従しないことが確認された。
Front + Rear はツクモ出荷状態では同一Fan Hubで一括制御されている。

ファン制御の実装自体は別Issueで行うが、**高額ハードウェアの冷却制御なので安全設計を先に確定する。**

## 方針
- 最初の制御対象は **ケースFan Hub 1系統（Front + Rear）だけ**
- AIO Pump / Radiator Fan / VRM Fan は初期段階では BIOS Q-Fan 管理を維持する
- AI / LLM を制御ループへ一切入れない
- GUIがroot権限で直接sysfsへ書かない。制御は専用daemonへ分離する

## やること（設計のみ。コードを書かない）
- [ ] `docs/decisions/NNNN-fan-control-safety.md` を作成
- [ ] BIOS Q-Fan を最終防衛線として残す方法を実機で確認
- [ ] Linux hwmon上の `pwmX` / `fanX_input` と物理ヘッダーの対応を特定
- [ ] Front + Rear Fan Hubを制御しているヘッダーを特定
- [ ] デッドマンスイッチ: 更新停止時に安全側へ移行する
- [ ] 最低PWM / 起動PWM / 停止しない下限を実測で確定
- [ ] 指定PWMに対してRPMが上がらない場合の `FAN_FAULT` を定義
- [ ] NVML取得失敗時のフェイルセーフを定義
- [ ] T_SENSOR取得失敗時のフェイルセーフを定義
- [ ] fan daemon異常終了・SIGTERM・OS shutdown時の振る舞いを定義
- [ ] 起動直後 / センサー未初期化中は安全側の高回転とする
- [ ] `hwmonX`番号を固定せず、driver名/属性から安定して探索する方法を決める
- [ ] AIからfan daemonへの到達経路を作らないことを構造で保証する

## フェイルセーフ原則
以下のいずれかを検出した場合、**ケースファンは安全側（原則100%）**へ倒す。

- NVMLのGPU telemetryが一定時間取得できない
- T_SENSOR / 主要温度センサーがstaleまたは取得不能
- fan daemon内部エラー
- Fan Hubへ十分なPWMを出しているのにtachが下限を下回る
- 制御ループのheartbeatが期限切れ

BIOS自動制御へ安全に復帰できることを実機で確認できた場合のみ、100%固定ではなくBIOS復帰を選択肢にする。

## 受入基準
- 「ソフトウェアが全停止しても冷却が止まらない」ことが設計上保証されている
- ケースFan Hub以外へ誤ってPWMを書かない識別手順がある
- fault injection（NVML停止、センサー欠測、daemon kill、tach異常）の期待動作が決まっている
- 人間レビューでADRが承認されるまで #43 の実装を開始しない

## 依存
#34, #19
