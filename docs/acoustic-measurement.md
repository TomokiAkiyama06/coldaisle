# 騒音の実測プロトコル（Acoustic sensor study）

#94 の Acoustic Cost Model を実測ベースへ移すための測定方式（#95）。
方針の正本は [決定記録 0067](decisions/0067-acoustic-measurement-study.md)（**Proposed**）で、
この文書は手順と候補値を持つ。

> **この文書の数値はすべて実測前の候補値である。** 確定値ではない。
> 実際に使った値は run の manifest に残し、確定は study の結果を見て所有者が決める。

## 1. 前提

- 測定は `coldaisle` の常駐プロセスの外で行う study である。マイク / 騒音計を
  ingest daemon・control daemon・API・AI 層から読まない（0067 §2.1）
- Fan を動かすのは `coldaisle-fand` の `CALIBRATION` モードだけ。Reactive Guard の floor と
  Critical Safety は測定中も有効で、介入した区間は集計から除く（0067 §2.2）
- 生の音声は既定で保存しない。残すのは segment ごとの派生量だけ（0067 §2.3）
- AIO Pump は制御対象外。測定中も BIOS / safety 設定の固定運用のまま記録だけする

## 2. 何を分けて測るか

| 音源 | 制御 | 扱い |
|---|---|---|
| Front（NF-A12x25 G2 ×3） | coldaisle | sweep の対象 |
| Rear（ケース純正 ×1） | coldaisle | sweep の対象 |
| Top（AIO ラジエーター 360） | coldaisle | sweep の対象。CPU cooling floor 未満には下げられない |
| AIO Pump | 制御外 | 固定。`fan.aio_pump.rpm` を記録 |
| GPU 本体の Fan | 制御外（GPU 側） | `gpu.0.fan_speed` を記録。変わった segment は分けて集計 |
| 電源・VRM Fan・コイル鳴き | 制御外 | 背景の一部として扱い、負荷条件ごとに記録 |

Fan 曲線の study は **CPU / GPU を idle にした条件**を基本にする。負荷をかけると GPU の Fan と
コイル鳴きが混ざり、coldaisle が変えられる音と分けられないため。負荷条件は別の run として取り、
「制御外の音がどれだけ乗るか」を見る参照にする。

## 3. 機材の候補（Q-26。選定は所有者）

| 案 | 構成 | 得られるもの | 弱いところ |
|---|---|---|---|
| A | 記録機能付き騒音計（IEC 61672-1 Class 2） | 規格に沿った `LAeq` | スペクトルが取れない機種が多い。純音・うなりを見られない |
| B | 個別校正ファイル付きの USB 測定用マイク（無指向性・20 Hz〜20 kHz） | スペクトル・純音・変調 | 絶対レベルは校正ファイルと入力ゲインの固定に依存する |
| C | B + 音響校正器（94 dB @ 1 kHz） | B の絶対レベルを測定ごとに確認できる | 費用が増える |
| D | B + A（騒音計で絶対レベルを突き合わせる） | 絶対レベルの確認と規格準拠の `LAeq` の両方 | 機材が 2 つになり、同時設置の位置合わせが要る |

- **推奨は C。** 0067 §2.4 の量をすべて取れ、絶対レベルを run ごとに確かめられる
- 共通の要件: 三脚への固定、入力ゲインを固定できる録音経路、OS 側の自動ゲイン・
  ノイズ抑制を無効にできること
- 機材の個体識別子（シリアル番号など）はコミットしない。manifest には alias を書く

## 4. 測定条件

### 4.1 位置

- **聴取位置**: 普段座る位置の耳の高さ。`annoyance_score` を付ける位置と同じにする
- **近接位置**: ケース前面から 1 m、ケース中央の高さ（ケースの向きによる差を見る参照）
- 位置は「ケースからの距離・高さ・角度」の相対値だけを記録する。部屋の見取り図や
  住所につながる情報は残さない
- 位置・ケースの向き・扉や窓の開閉は run の途中で変えない。変えたら別の run にする

### 4.2 背景騒音

- 各セッションの最初と最後に、**サーバーの電源を切った状態**で 60 秒の背景を取る
- segment ごとに背景との差 `ΔL`（dB）を計算する。候補の扱い:

  | `ΔL` | 扱い |
  |---|---|
  | 15 dB 以上 | 補正しない |
  | 6〜15 dB | エネルギー差で補正し、印を付ける |
  | 6 dB 未満 | 集計に使わない |

- 空調・冷蔵庫など周期的に鳴る機器は、止められるものは止め、止められないものは
  セッションのメモに残す

### 4.3 定常の待ち方

- Demand を変えたら、各 Zone の実測 RPM が目標付近で安定するまで待つ
  （候補: 直近 10 秒の RPM の変動が 2 % 以内）。そのうえで追加の 5 秒を待つ
- 録音区間は候補 30 秒。これを 1 segment とする
- 待ちの間も decision trace を記録し、segment の開始・終了時刻を trace の時刻に合わせる

## 5. 測定計画

測定計画は `CALIBRATION` の requested として `coldaisle-fand` に渡す（0067 §2.2）。

1. **単一 Zone sweep**: 他の 2 Zone を基準 Demand に固定し、1 Zone だけを
   候補 0.0 / 0.1 / … / 1.0 で動かす。Top は CPU cooling floor 未満の点を計画に入れない
   （入れても floor で持ち上がり、その segment は除外される）
2. **2 Zone の格子**: Front × Rear、Front × Top、Rear × Top を粗い格子（候補 5 × 5）で。
   Fan 同士のうなりと interaction（#94 の `AcousticInteraction`）の材料
3. **3 Zone の粗い格子**: 候補 3 × 3 × 3。#94 の曲線と interaction を足した予測の検証用
4. **基準点への戻り**: 10 segment ごとに基準の設定へ戻る。背景や温度のドリフトの検出用

- 各計画の順序はセッションごとに無作為化する（seed を manifest に残す）
- 同じ設定を **別の時間帯のセッションで少なくとも 3 回** 測る（0067 §2.6）
- Guard / Safety が介入した segment、背景との差が足りない segment、GPU の Fan 速度が
  基準から変わった segment は、除外理由を付けて残す（数から消さない）

## 6. 解析の設定（候補値）

| 項目 | 候補 |
|---|---|
| サンプリング | 48 kHz、24 bit（16 bit 以上） |
| 周波数特性 | マイクの個別校正ファイルで補正。A 特性と Z 特性の両方を計算 |
| レベル | segment 全体の `LAeq` / `LZeq` |
| 帯域 | 1/3 オクターブ帯 20 Hz〜20 kHz（IEC 61260-1 の帯域中心） |
| スペクトル | Welch 法。Hann 窓、FFT 長 1 秒（1 Hz 分解能）、50 % overlap で segment 全体を平均 |
| 純音 | スペクトルのピークのうち、周囲の帯域より突出したもの（周波数・突出量 dB）。各 Zone の羽根通過周波数（RPM / 60 × 羽根枚数）とその倍音に近いものに印を付ける |
| 変調 | 帯域ごとの包絡線のスペクトル 0.5〜20 Hz。最大ピークの周波数と変調深さ |

- 解析設定は 1 つの YAML にまとめ、その hash を manifest に残す。**解析設定が違う run 同士は比較しない**
- 羽根枚数など Fan の仕様値は解析設定の YAML に置き、解析コードに書かない（AGENTS.md ルール 9）

## 7. `annoyance_score` の集め方

- 聴取位置で、所有者が 0（気にならない）〜10（作業できない）で評価する
- 1 回の評価は 1 segment の再生ではなく**実機の音**を聞いて付ける。録音の再生は
  スピーカーの特性が乗るため
- 評価者には Demand の値を見せない。計画の順序は無作為化し、同じ設定を
  セッション内で 2 回混ぜて**本人の一貫性**を測る
- 評価者が 1 人であることを study の限界として報告に書く（0067 §2.7）

## 8. Acoustic Dataset（案）

保存先は `data/acoustic/<run alias>/`（`.gitignore` 済み）。0031 と同じく、
run / 機材には不透明な alias（32 桁の hex）を払い出す。

### 8.1 `manifest.json`

| キー | 内容 |
|---|---|
| `schema_version` | `1` |
| `run_alias` / `session_aliases` | 不透明 alias |
| `instrument` | 機材案（A〜D）・機材の alias・校正ファイルの hash・校正器での確認値 |
| `geometry` | 位置の種類（聴取 / 近接）・ケースからの距離・高さ・角度 |
| `conditions` | 室温・湿度（`air.room` から）、負荷条件、扉・窓・空調のメモ |
| `analysis_config_sha256` | §6 の解析設定の hash |
| `plan` | 測定計画の種類・格子・無作為化の seed |
| `control` | run で有効だった制御設定（`fan-hardware.yaml` / `safety.yaml` / `acoustic.yaml` など）の hash |
| `raw_audio_retained` | 生の音声を一時保存したか（既定 `false`） |

### 8.2 `segments.jsonl`（1 行 1 segment）

| キー | 内容 |
|---|---|
| `segment_id` / `session_alias` | |
| `start_ms` / `end_ms` | decision trace の時刻 |
| `requested_demand` / `effective_demand` | Front / Rear / Top。effective は trace から |
| `rpm` | Front / Rear / Top / AIO Pump の実測 RPM（区間平均と変動） |
| `gpu_fan_speed` | `gpu.0.fan_speed` の区間平均 |
| `interventions` | 区間内の Guard / Safety 介入（無ければ空） |
| `background_delta_db` / `background_corrected` | §4.2 |
| `laeq_db` / `lzeq_db` | |
| `third_octave_db` | 帯域中心周波数 → レベル |
| `tonal_peaks` | `[{hz, prominence_db, zone_bpf_match}]` |
| `modulation` | `[{band_hz, mod_hz, depth}]` |
| `valid` / `exclusion_reasons` | 除外したものも行として残す |

### 8.3 `ratings.jsonl`

| キー | 内容 |
|---|---|
| `segment_id` | 評価した segment |
| `annoyance_score` | 0〜10 |
| `repeat_of` | セッション内で繰り返した設定なら、その 1 回目の segment |

## 9. #94 へ渡すもの

| 区分 | 内容 |
|---|---|
| feature | 各 Zone の `effective_demand` と実測 RPM |
| target | `laeq_db`、帯域レベル、純音の突出量、変調深さ、`annoyance_score` |
| 渡し方 | 単一 Zone sweep から Zone 別の曲線点、2 Zone の格子から interaction を作り、`acoustic.yaml` に書く |

- 曲線は `acoustic_cost`（無単位）のまま。dBA をそのまま曲線の値にしない
- `source.kind: measured` にするのは 0067 §2.5 の条件がそろったときだけ。
  `source.basis` には根拠の manifest の hash を書く

## 10. 判定

### 10.1 繰り返しのばらつき

- 設定ごとに、セッションをまたいだ `laeq_db` と各帯域レベルの標準偏差・範囲を出す
- 候補の許容幅: `laeq_db` の標準偏差 1 dB 以内。超えた設定は背景・位置・GPU の Fan を確認する
- 基準点への戻りの値が時間とともに動いていれば、ドリフトとしてその区間を報告する

### 10.2 SPL のみの案（A）と周波数情報を使う案（B）

- A: `laeq_db` だけで `annoyance_score` を説明する
- B: A に純音の突出量と変調深さを加える
- **セッション単位の交差検証**（1 セッションを抜いて残りで当てはめる）で、抜いたセッションの
  順位相関（Spearman）を比べる
- `laeq_db` の差が小さい（候補: 1 dB 以内）のに `annoyance_score` が大きく違う設定の組を
  列挙し、A が区別できない事例として報告に残す

## 11. 再測定の契機

- Fan の交換・追加、ケースの向きや設置場所の変更、ダストフィルターの変更
- 決定記録 0056 §2.5 の宣言された変更（`DeclaredChange`）と同じ契機で再測定を検討する
- 再測定した run は新しい run alias とし、古い run を上書きしない
