# Offline Evaluation（#91）

同じ Dataset / Replay 条件で Controller 構成を比べ、rollout 可否の材料を作る。
規則は [`decisions/0054-offline-evaluation-attribution-and-gates.md`](decisions/0054-offline-evaluation-attribution-and-gates.md)
（**Status: Proposed。所有者の承認が要る**）。Shadow の記録側は
[`decisions/0053-control-shadow-mode-and-counterfactual-logging.md`](decisions/0053-control-shadow-mode-and-counterfactual-logging.md)。

## まず知っておくこと

**実測の成果（温度・ΔT・Air Balance・RPM）は、実際に適用された構成にしか付かない。**
Shadow の間の Learned MPC は Fan へ届いていないので、その区間の温度は
「Fallback が動いた結果」であって MPC の成績ではない。レポートの型も分かれている。

| 区分 | 何が載るか |
|---|---|
| `applied:<controller>+<supervisor>@<stage>/<mode>` | 温度・threshold margin・ΔT・Air Balance・demand / RPM の変動とハンチング・approximate acoustic cost・Safety / Guard / Fallback の介入回数 |
| `counterfactual:<controller>+<supervisor>@<stage>` | 要求 demand の変動とハンチング・approximate acoustic cost・optimizer の latency / timeout・cost 改善率・**`scored` な予測の誤差だけ**・**coverage** |

**適用側の鍵にだけ `operating_mode` が入る。** `MANUAL` / `CALIBRATION` では人が、
`MAX` では forced_max が適用 demand を決めるので、制御器が回した区間と混ぜない。
counterfactual 側に mode が無いのは意図的で、提案は mode に依らず作られ、
`MANUAL` / `CALIBRATION` の tick はそもそも counterfactual を持たない（0053 §2.1）。

**「MPC 単体 vs MPC + Guard」は比較対象にならない。** 適用された制御に Guard と
Critical Safety を通らない経路が無いからである（AGENTS.md ルール2）。代わりに、
同じ arm の `requested_demand` と `effective_demand` の差、そして `interventions` を見る。

## coverage を先に見る

counterfactual の予測を採点してよいのは、**その候補 plan が実際に掛かっていた区間だけ**
（0053 §2.3）。Shadow の間はほとんどが `unidentifiable` になる。

```
coverage.outcomes                 突き合わせた予測の数
coverage.scored                   採点できた数
coverage.identifiable_fraction    scored / outcomes（outcome が無ければ null）
coverage.unidentifiable_reasons   applied_action_differs / applied_action_unknown の内訳
coverage.unmatched_reasons        no_usable_observation ほか
coverage.sufficient               設定の下限を満たしたか
```

`sufficient` が `false` の arm には **`predictions` が入らない**。少数の当たりを
全体の予測精度に見せないためで、gate もその arm を `blocked` にする。

**coverage は segment ごとに判定する。** 評価した holdout segment が1つでも足りなければ
（`insufficient_coverage_segments` > 0）、ほかの segment がどれだけ揃っていても `blocked`。
`scored_outcomes` の判定も **segment ごとの最小**で見る。足りない区間の採点数をほかの
区間と足すと、「証拠は伏せた区間から、指標は出した区間から」という取り合わせになる。

## 実行

```bash
uv run coldaisle-evaluate --runs var/evaluation-runs.yaml --out var/evaluation.json
```

`--runs` の manifest（比べる run を明示する。「直近」のような相対指定は置かない）:

```yaml
schema_version: 1
runs:
  - run_id: baseline-a
    start_ms: 1787616000000
    end_ms: 1787623200000
    # 時系列 split の境界。**最後の区間が holdout** で、gate はそこだけを見る
    split_boundaries_ms: [1787619600000]
  - run_id: mpc-shadow-a
    start_ms: 1787702400000
    end_ms: 1787709600000
    # #90 の export をそのまま取り込む（省略すると同じ照合器で作り直す）
    shadow_jsonl: shadow/mpc-shadow-a.jsonl
```

そのほかの引数（既定値あり）:

| 引数 | 既定 | 何を読むか |
|---|---|---|
| `--db` | `var/coldaisle.db` | decision trace と観測（**読み取りのみ**） |
| `--config` | `config/evaluation.yaml` | 評価設定（percentile・室温帯・不感帯・gate） |
| `--control-config` | `config` | `fan-hardware.yaml` / `safety.yaml` / `fan-policy.yaml` |
| `--metrics` | `config/metrics.yaml` | ΔT の式（`derived`） |
| `--acoustic` | なし | `acoustic.yaml`。無ければ acoustic cost は「欠測」として残る |

**評価設定に閾値を写さない。** 絶対温度上限は `safety.yaml`、ΔT の式は `metrics.yaml`、
照合の許容幅は `fan-policy.yaml` の `shadow` から取る。写すと評価だけが別の契約で動く。

**外から渡した `shadow_jsonl` は、counterfactual を持つ tick と1対1でなければ受け取らない。**
行の重複・欠落・余分、同じ識別子の outcome の重複、その tick の counterfactual と結べない
outcome は、すべてその場で拒む（数えないだけにすると、行を複製するだけで coverage の下限を
満たせてしまう）。`split_boundaries_ms` が tick の無い区間を作る場合も拒む。

さらに、**渡された export は同じ trace と観測から数え直した結果と1欄ずつ照らします。**
識別子と許容幅だけを見ても `status` / `observed` / `error` / 時刻は書き換えられるので、
それだけでは「採点していない区間を `scored` に仕立てる」ことを防げません。

**同じ metric・同じ時刻に食い違う観測があれば受け取りません。** 小さいほうを採れば絶対上限の
超過が消え、大きいほうを採れば予測の当たりが消えるため、評価が選ぶべきものではありません。
まったく同じ観測が2度届くのは許し、1つに畳んでから数えます。

## レポートの読み方

- `provenance` — 設定の hash、run ごとの trace / 観測の digest、現れた model / controller の版、
  `conditions_sha256`（**これが同じなら同じ条件で比べている**）
- `segments[]` — run × 区間。`role` は `calibration` / `holdout`。`purged_outcomes` は
  境界を跨ぐため採点に使えなかった予測の数、`unattributed_observations` はどの tick からも
  許容幅の外にあって、どの arm にも帰属させなかった観測の数（**黙って落とさずに数える**）
- `segments[].groups[]` — `overall` / `workload_regime` / `room_temperature_band` の切り口
- `worst_cases[]` — 最小 threshold margin・最高温度・上限超過（metric ごと／segment の合計）・
  EMERGENCY・最大 underprediction・最小 identifiable fraction の上位。**平均に埋もれさせない**
- `gates[]` — arm ごとの `pass` / `blocked`。段は `safety` → `evidence` → `cost` で、
  **上の段が落ちたら下では覆らない**。欠測・未設定・coverage 不足も `blocked`。
  適用 arm の Safety の段は、**設定したすべての温度 metric が、評価したすべての segment に
  揃っているとき**だけ判定する（一部だけなら `incomplete_temperature_evidence` で `blocked`。
  1つも無いときの `no_temperature_evidence` とは区別する）。
  **超過数は1つの segment の中で metric をまたいで足します**（metric ごとの最大では、
  3つ同時に超えていても「1つ分」に見えるため）

**gate は助言である。** 昇格・降格の判断は #92 と人が行う。

## 再現性

- レポートに**生成時刻は入らない**。同じ入力からは同じ bytes が出る
- `conditions_sha256` は設定の hash・run ごとの digest に加え、**時系列 split の境界**と
  出力の形を決めるコード側の版を覆う。**同じ hash なら本当に同じ条件**である
- 評価は `coldaisle.control.hardware` / `safety` / `reactive` / `serial` / `subprocess` を
  import しない（`tests/test_offline_evaluation.py` が走査する）
- 時刻はすべて decision trace と観測から来る。壁時計は使わない

## いまの限界

- **適用された Learned MPC の optimizer latency / timeout は読めない。**
  `ControlTick`（v6）は `ControllerProposal` を埋め込んでいないため、counterfactual として
  動いた区間からしか取れない。欄は埋め合わせず「無い」と書く
- 適用された arm の gate には cost の条件が無い。同じ条件の別の運転が無い以上、
  合否を決められないため（決められないものを置かない）
- 閾値はすべて `provisional`。確定には基準となる測定が要る
- 欠けている証拠は理由付きで残す。`no_*`（1つも無い）/ `partial_*`（一部だけ）/
  `applied_optimizer_record_unavailable`（この arm には記録の場所が無い）を区別する
