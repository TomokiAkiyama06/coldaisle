# 騒音の測り方: 「うるさいか」の回答

#94 の Acoustic Cost Model の曲線に、仮置きより良い根拠を与えるための手順（#95）。
方針の正本は [決定記録 0067](decisions/0067-acoustic-measurement-study.md)（**Proposed**）。

> **騒音計は使わない。** dBA は測らず、本人が「うるさいか」を答えて、不快に感じ始める
> Demand の下限を探す。ここで作った曲線は `source.kind: approximate` のままにする。
> 数値の目安はすべて暫定値。回答の入口と集計は**まだ実装されていない**（0067 §2.8）。

## 1. 普段の運転中に答える（主な方法）

1. UI（エアフロー画面など。表示専用）で、いまの各 Zone の Demand と RPM を見る
2. うるさいと感じたら / 気にならないと感じたら、書き込み専用の入口へ答える

   ```bash
   # 予定の形（0045 の入口に noise_feedback を足す。実装は別 Issue）
   uv run coldaisle-event noise-feedback loud
   uv run coldaisle-event noise-feedback ok
   ```

3. 答えるのは気づいたときでよい。「気にならない」も答えると、下限の見積もりが安定する

- **答えても Fan は変わらない。** 回答は記録だけで、制御ループは読まない（0067 §2.3）
- 回答は、その時刻の decision trace の **effective の Demand** と RPM に結びつけて集計する（0067 §2.4）

## 2. Zone を1つずつ確かめる（補助の方法）

普段の運転では高い Demand の状態がなかなか現れず、どの Zone がうるさいかも分けにくい。
そのときは次の手順で確かめる。

1. CPU / GPU を idle にする（GPU 本体の Fan とコイル鳴きを混ぜない）
2. `coldaisle-fand` を `CALIBRATION` モードにし、他の 2 Zone を低めの基準 Demand に固定する
3. 1つの Zone だけを 0.0 / 0.25 / 0.5 / 0.75 / 1.0 と上げる
4. 各段で回転数が落ち着くのを待ち（目安 20 秒）、普段の位置で聞いて `loud` / `ok` を答える
5. 他の Zone でも繰り返す。別の日にもう一度回すと、その日の気分による揺れが分かる

- PWM を直接書かない。Fan を動かすのは `CALIBRATION` だけ（0067 §2.5）
- Critical Safety と Reactive Guard の floor は外れないので、Top は CPU cooling floor 未満にならない。
  持ち上がった段は、持ち上がった後の effective の Demand として扱う

## 3. 集計の考え方

- Zone ごとに、effective Demand の帯ごとの `loud` の割合を出す
- `loud` の割合が大きくなり始める Demand（目安: 半分を超える）を**不快の下限**とする
- 普段の回答は 3 Zone が同時に動いているので、1つの回答からはどの Zone がうるさいか分からない。
  Zone ごとの下限は、補助の方法（§2）の回答を優先して使う
- 次の回答は分けて見る
  - CPU / GPU が高負荷のとき
  - GPU 本体の Fan（`gpu.0.fan_speed`）が回っているとき
  - 回答の時刻の近くに decision trace の tick が無いとき（状態が分からないので使わない）

## 4. 曲線への写し方

- 下限までは緩やかに、下限より上で `acoustic_cost` が急に上がる形にする
- 具体の値は人が確認する `acoustic.yaml` の PR で決める。回答から自動では書き換えない
- `source.basis` には「noise_feedback の集計（期間・回答数）」を書く
- 曲線は `demand=0.0` の点が必須。floor があって低い Demand が現れない Zone は、
  観測した最小の点のコストを 0.0 まで平らに延ばし、そうしたことを `source.basis` に書く
- 下限は Fan Demand の上限にはしない（温度が上がればうるさくても回す）

## 5. 回答を区切る契機

- Fan の交換・追加、ケースの向きや設置場所の変更の後は、それ以前の回答を下限の集計に混ぜない
- 決定記録 0056 §2.5 の宣言された変更（`DeclaredChange`）と同じ契機で区切る
