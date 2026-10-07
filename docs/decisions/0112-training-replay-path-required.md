# 決定記録 0112: 学習の入口で `--replay-path` の照合を必須にする（#237 の Codex P1）／0100 段 3（PR #259）の実装で決着した点

- **種別**: Decision Record
- **Status**: FINAL（2026-10-08、リポジトリ所有者が §2.1 を案1で、§2.2 の6点を推奨案で承認）
- **Date**: 2026-10-08
- **Supersedes**: [0100](0100-replay-export-binding.md) の部分のみ。§2.8 の「export の記録の digest と `ReplayBindingV2` の中身」の
  最後の項（「それでも、学習の入口だけでは元の CSV の bytes を読み直せない。…学習の入口に `--replay-path` を任意で
  渡せば、fingerprint を計算し直して `source_sha256` と照合する」）と、§5 #15 のうち代替 (a)「学習時に元の manifest と
  CSV（`--replay-path`）を必須にする」を採らなかった部分（理由「公開済みの dataset だけで学習できなくなる」）。
  学習の入口は `--replay-path` を**必須**にし、照合する（§2.1）。§5 #15 の推奨（`ReplayBindingV2` と
  `export_record_sha256` / `export_binding_sha256` を残し、`csv_exports` から計算し直して照合し、期間を確かめる）は
  有効で、§2.1 はその上に**足す**。0100 の他の節は有効。旧記録側への `Superseded by`（部分）の追記は本 PR で行った
  （README「追記のみ」の例外）
- **関連**: [0100](0100-replay-export-binding.md) §2.8 / §5 #15 / [0108](0108-export-manifest-settled-points.md) /
  [0111](0111-replay-binding-settled-points.md) / [0102](0102-calibration-change-log-settled-points.md) §2.3 /
  [0031](0031-thermal-dataset-contract.md) §2.4 / `src/coldaisle/calibration_log.py` / `src/coldaisle/dataset.py` /
  `src/coldaisle/ingest/replay.py` / `src/coldaisle/store/export_binding.py` / `docs/thermal-dataset.md`
- **対象 Issue**: #237（0100 段 3 の実装 PR #259）

## 1. Context

0100 段 3（PR #259）で、学習の入口は dataset に残した `ReplayBindingV2` を本番の DB の `csv_exports` から計算し直して
照合し、example の期間が束縛した日の区間に収まることを確かめるようになった。0100 §2.8 の最後の項が認めたとおり、
学習の入口は元の CSV の bytes を読み直さないので、ID・期間・digest をそろえて手で組んだ dataset は見分けられない。
#237 に残った Codex の P1 はこの穴を具体的に示した: 手で組んだ dataset が、同じ日を覆う別の正当な export B の
`ReplayBindingV2` を写し、公開物の checksum を計算し直し、export A から作った example を B の日の区間に収めると、
学習の入口の検査はすべて通る（較正の記録は B の DB から読まれる）。

所有者は 2026-10-08、PR #259 の報告で示した3案（案1: 学習時に `--replay-path` を必須にする／案2: dataset の
telemetry から計算し直せて `source_sha256` と結びつく digest を残す／案0: 0100 のまま残る穴を受け入れる）から
**案1** を選んだ。案2 は秘密を持たない限り、dataset を手で組む人も同じ digest を計算できるので穴を閉じない
（学習の入口が独立に持つ真実は `csv_exports` の hash だけで、照合には CSV の全行が要る）。

あわせて、PR #259 の「人間のレビューが必要な点」1〜6 を、所有者が 2026-10-08 に推奨案で承認した（§2.2）。

## 2. Decision

### 2.1 学習の入口は `--replay-path` を必須にし、元の bytes と照合する

**決着（2026-10-08 所有者の決定、案1）。** 学習の入口（0099 §2.6 / 0100 §2.8 の検査）は、dataset の source run ごとに
元の再生の入力（`--replay-path`。日次 CSV とその manifest）を**必須**で受け取り、次をすべて確かめる。1つでも
外れれば学習を拒否する。

1. その入力の fingerprint（`replay_sha256`。0111 §2.2 の v2）が、dataset の `SourceRun.source_sha256` と一致する
2. その入力のすべての CSV に manifest がある（manifest の無い入力・一部にだけある入力は拒否）。各 manifest の
   `csv_name` / `csv_sha256` が対の CSV と一致する（fingerprint と manifest は同じ1回の読み出しから得る）
3. manifest から計算した `(timezone, export_binding_sha256)` が、その run の `ReplayBindingV2` の
   `(local_timezone, export_binding_sha256)` と一致する

0100 §2.8 の既存の検査（`csv_exports` の行から `export_record_sha256` と `export_binding_sha256` を計算し直す・
写した欄の照合・example の期間）はそのまま行う。これで dataset の束縛は、CSV の bytes（fingerprint）・manifest・
本番の DB の行の三者に結びつく。

- 学習には、元の日次 CSV と manifest（`coldaisle-rollup --export-day` の出力）が要る。公開済みの dataset だけでは
  学習しない（0100 §5 #15 の (a) の理由を置き換える）
- 学習の CLI はまだ無い。PR では検査の関数に必須の引数として足し、CLI への配線は学習の CLI の PR で行う
  （0102 §2.3 と同じ扱い）

### 2.2 0100 段 3（PR #259）の実装で決着した点

いずれも **決着（2026-10-08 所有者の決定、推奨案）**。

1. **`replay_bindings` は `DatasetManifestV2` の省略可能な欄**とし、`schema_version` は 2 のまま。builder は必ず書き、
   学習の入口は欄の無い dataset を拒否する。束縛の無い既存の v2 の manifest は、読み直すと `"replay_bindings": null`
   が加わるので、`verify_training_dataset_artifact_v2` の canonical bytes の照合で拒否される（どのみち学習に使えない）
2. **v1 の builder の `replay_binding` は省略可能**（試験の呼び出しを変えないため）。`coldaisle-dataset` の v1 は必ず
   渡し、専用 DB の値（`NULL` を含む）との一致を求める
3. **学習の入口は関数のまま**（学習の CLI がまだ無いので配線しない。0102 §2.3 と同じ扱い）
4. **学習の入口は digest に加え、`ReplayBindingV2` に写した欄**（日の区間・`csv_sha256`・`row_seconds_sha256`・
   timezone）**も `csv_exports` の行と比べる**
5. **期間の検査**は、example の `[history_start_ms, label_end_ms]` が、束縛した export の `[day_start_ms, day_end_ms)` を
   隣り合う・重なるものどうしつないだ区間のどれか1つに収まることとする
6. **`ExportBinding` は1つの timezone を要求する**（違う timezone の export が混ざる入力は、再生が既に拒否している）

## 3. Consequences

### 良くなること

- 手で組んだ dataset（別の正当な export の束縛を写したもの）を、学習の入口で元の CSV の bytes まで遡って拒否できる
- dataset の束縛が、CSV の bytes・manifest・本番の DB の行の三者で閉じる

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 学習に元の日次 CSV と manifest が要る（公開済みの dataset だけでは学習できない） | 日次 CSV と manifest は `csv_dir` に残る（保持期間の削除の対象外）。複写するときは manifest も一緒に複写する（`docs/thermal-dataset.md`） |
| 学習の入口が入力を1回読み直す | hash を取るだけ（定数メモリ）。学習の前に1回 |
| 学習の CLI ができるまで、必須の引数を強制する入口が無い | 学習の CLI の PR で必須の引数として配線する（0102 §2.3 と同じ） |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| 案2: dataset の telemetry から計算し直せて `source_sha256` と結びつく digest を残す | 秘密を持たない限り、dataset を手で組む人も同じ digest を計算できる。学習の入口が独立に持つのは `csv_exports` の hash だけで、照合には CSV の全行が要る（案1と同じになる） |
| 案2の部分案: export ごとの全行の秒の列を dataset に入れ、`row_seconds_sha256` を計算し直して example の観測時刻がその列に入ることを確かめる | 時刻が同じで値の違う export を見分けられない |
| 案0: 0100 のまま、残る穴を受け入れる | 0031 §2.4 の公開物の checksum は計算し直せるので、穴が残る |
| `--replay-path` を任意のまま、渡されたときだけ照合する（0100 §2.8 のまま） | 渡さなければ穴が残る |

## 5. 未決事項

なし（学習の CLI への配線は学習の CLI の PR）。
