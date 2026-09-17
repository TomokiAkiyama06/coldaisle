# Control Config

#103 は Fan Control の設定を `fan-hardware.yaml`、`safety.yaml`、
`fan-policy.yaml` の3ファイルで一括検証する。

現時点では実機測定がないため、リポジトリに実運用用の3ファイルは置かない。
仮の hwmon 対応や温度閾値をコミットして実機を作動させないためである。
`ControlConfig.from_directory()` は3ファイルすべてが揃い、schema version・型・範囲・
相互条件を満たす場合にだけ設定を返す。欠損または不正なら、書き込み層へ設定を渡さない。
#78 はこの例外を Critical Safety の config-invalid failure semantics（書込み対象を特定できなければ
BIOS制御のまま終了、特定済みなら安全側へ引継ぎ）へ接続するconsumerである。

`fan-hardware.yaml` の `approval.status` が `confirmed` かつ根拠 `basis` を持つまで、
`actuation_permitted` は false になる。実機の header 対応・Fan profile は #75 の測定記録を
根拠として確認する。`hwmonN`、絶対パス、個体識別子は設定に書かない。

設定の読み直しは候補全体を別オブジェクトで検証してから原子的に入れ替える。
Safety、hardware mapping、Reactive Guard、authority stage の変更には明示的な承認が必要で、
`ConfigReloadApproval(reference=...)` なしには現行設定を維持する。成功時は
`last_reload_event.trace_metadata()` が旧新のsource名・schema version・SHA-256・承認参照を
#82 の decision trace に渡す。`provisional_values()` は起動時の構造化ログへ、暫定値そのものを
露出せずに位置と根拠だけを渡す。

数値の確定や実機での書き込み許可は決定記録 0028 §2.8–2.9 に従い、所有者の承認を要する。
