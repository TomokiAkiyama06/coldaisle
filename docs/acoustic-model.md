# Acoustic Cost Model

`coldaisle.control.acoustic` は、MPC が **任意で読むだけ** の
`acoustic_cost` を返す。これは無単位の最適化コストであり、dBA / SPL の推定値ではない。
Thermal Model、Reactive Guard、Critical Safety、Hardware Backend への依存・書き込みは持たない。

## 設定

設定ファイル名は `acoustic.yaml`。起動時に一度だけ検証して読み込む。
初期値は実測のない `source.kind: approximate` として扱う。実測 SPL・周波数特性・
subjective annoyance score が揃った後は、同じ zone 曲線の契約のまま
`source.kind: measured` と根拠を設定できる。

各 zone は別々の単調・区分線形曲線を持つ。設定値が非線形の関係を定義し、補間以外の
騒音値・閾値・重みはコードに持たない。

## 将来の interaction

`AcousticInteraction` は `PerZone[Demand]` を受け、無単位の追加コストを返す読み取り専用契約である。
Fan の組み合わせ、共振、周波数特性を扱う実測済みモデルは、この契約で後から追加できる。
モデル出力には source、設定ハッシュ、適用した interaction 名を metadata として残す。
