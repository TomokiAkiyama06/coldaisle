# 決定記録 0095: 0077 段階 1（`coldaisle-fand` 側の Learned worker の経路）の実装で決着した点

- **種別**: Decision Record
- **Status**: FINAL（2026-10-07、リポジトリ所有者が推奨案で承認）
- **Date**: 2026-10-07
- **Supersedes**: なし（0077 が決めていなかった点への追加と、0077 の文面の読み方の明記。0077 / 0092 の
  決定は置き換えない）
- **関連**: [0077](0077-learned-proposal-handoff.md) §2.2 / §2.3 / §2.7 / §2.8 / §2.10 /
  [0092](0092-learned-frame-applied-from-hardware-result.md) /
  [0072](0072-control-admin-entry.md) §2.5 / [0076](0076-implementation-settled-points.md) §2.7 /
  [0078](0078-air-balance-fallback-coordination.md) /
  `src/coldaisle/learned_channel/` / `src/coldaisle/control/loop.py` / `config/learned-channel.yaml`
- **対象 Issue**: #86（実装 PR #222）

## 1. Context

0077 §2.10 の段階 1（PR #222）で、`coldaisle-fand` 側の Learned worker の経路（役割ごとの `SOCK_SEQPACKET`
ソケット・受付スレッド・受け渡し口・認可・heartbeat・frame の送り出し）を実装した。その実装で、0077 に
書かれていない点、または文面だけでは読み方が分かれうる点が6点あり、PR #222 の「人間レビューが必要な点」で
推奨案とともに示した。加えて Codex のレビューで、出荷設定のソケットの置き場所について1点が残った。
2026-10-07、所有者が7点すべてを推奨案で承認した。決定記録は追記のみなので、0077 の本文には手を入れず、
ここに残す。

## 2. Decision

### 2.1 frame の `baseline` は raw baseline（Fallback の出力）

frame の `baseline` には、その tick の **Fallback Controller の出力そのもの（raw baseline）** を入れる。
0078 の Air Balance の協調で Gate が受け取る coordinated baseline ではない。0077 §2.3 の表の
「その tick の Fallback の `ControllerProposal`」の文言どおりの読み方である。Fallback が値を出せなかった
tick は `null`。coordinated baseline を worker へ渡すように変えるなら、別の記録が要る。

### 2.2 `accept()` の失敗は受付スレッドの死として扱う

`ECONNABORTED`（相手が accept の前に切った）以外の `accept()` の失敗（fd の枯渇など）は、受付スレッドの
死（`channel_dead`）とする。管理ソケットの backoff（0076 §2.7）は持ち込まない。安全側（Fallback /
RulePolicy）へ倒れるだけで、Max にはしない（0077 §2.2）。一時的な fd の枯渇でも `coldaisle-fand` の
再起動まで Learned が止まるが、Learned は任意の部品である。

### 2.3 「接続数の上限」は `limits.listen_backlog`

0077 §2.7 の設定の列挙にある「接続数の上限」は、`listen()` の待ち行列（`limits.listen_backlog`）とする。
同時に持つ接続は 0077 §2.7 の決定どおり**役割ごとに1つ**で、設定値にしない。

### 2.4 `hello` に `run_id` も載せる

接続を認めた最初の応答（`hello`）は `heartbeat_interval_ms` に加えて `run_id` を運ぶ。worker が最初の frame を
受け取る前から、heartbeat の封筒に正しい `run_id` を付けられるようにするため（付けられないと、受付が
`run_id` の照合で捨て、`worker_idle_timeout_ms` で切ってしまう）。`run_id` は起動ごとの乱数で、
個体識別子を含まない（AGENTS.md ルール10）。

### 2.5 段階 1 の Supervisor のソケットは接続・heartbeat・frame の受信まで

段階 1 では、Supervisor のソケットは接続の認可・heartbeat・frame の受信までを行う。RL の出力の本文は
`SupervisorOutputSource` の形を広げる段階 4（#89。0077 §2.8）で足す。それまでは Supervisor のソケットに
届いた結果の本文は受け渡し口へ置かず、捨てて数える（`unsupported_body`）。

### 2.6 `--learned-channel-config` を省いた起動は経路を配線しない

`coldaisle-fand` は `--learned-channel-config` を与えたときだけ経路を開く。省いたときは loop に経路を
配線せず、運転と decision trace はこれまでと同じ（`learned_proposal_unavailable` の detail は空のまま）。
与えたのに開けない（設定の不正・ソケットを開けない・グループの重なりなど）ときだけ、loop は
`channel_disabled` を detail に出す（0077 §2.5 / §2.7）。

### 2.7 ソケットの親ディレクトリは役割ごとに分ける

役割ごとのソケットは**別の親ディレクトリ**に置く（出荷設定の例: `var/run/learned-mpc/mpc.sock` と
`var/run/learned-rl/supervisor.sock`）。親ディレクトリは無ければ役割のグループで 0750 として作られ
（0072 §2.5 と同じ門）、2つの役割が共通の親を使うと、後から待ち受ける役割のグループがその親を
たどれずに経路全体を開けない（`channel_disabled`）。共通の親に o+x を付けて両方からたどれるようにする案は、
他の利用者からもたどれるようになるので採らない。変えるのは出荷設定の path だけで、コードは変えない。

**共通の祖先は起動前に用意する（決着、2026-10-07 所有者の決定、推奨案）。** 親を分けても、存在しない祖先
（出荷設定では `var`・`var/run`）は最初に待ち受ける役割が同じ規則（0750・その役割のグループ）で作るので、
もう一方の役割がたどれず経路全体を開けない（PR #222 の Codex P2）。そこで path はいまのままにし、
**共通の祖先（開発では `var/run`、本番では systemd の `RuntimeDirectory`）を `coldaisle-fand` の起動前に
用意し、両方の役割のグループがたどれる**（グループが合って g+x か、o+x）ことを配置の前提にする。満たさない
構成は起動時の検査（`check_group_can_traverse`）で `channel_disabled` になるだけで、制御は止まらない。
本番の値（`RuntimeDirectory` の権限・path）は段階 6 の unit テンプレートで確定する（0077 §2.10）。

## 3. Consequences

### 良くなること

- 記録だけを読んだ人が、実装と違う挙動（coordinated baseline を送る・accept の失敗で回復を待つ・
  同時接続の上限が設定できる、など）を前提にしない
- 出荷設定のまま起動しても、ソケットの親ディレクトリの権限で経路全体が開けなくなることがない

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| 一時的な fd の枯渇でも再起動まで Learned が止まる（§2.2） | 安全側に倒れるだけ。頻度が問題になれば、0076 §2.7 と同じ backoff を別の記録で足す |
| 段階 4 まで RL の出力は経路を通らない（§2.5） | 0077 §2.10 の段階の区切りどおり |

## 4. 却下した代替案

| 案 | 却下理由 |
|---|---|
| frame の `baseline` に coordinated baseline を入れる | 0077 §2.3 の文言と違う。MPC の目的関数がどちらを基準にすべきかは段階 3 の検討で、別の記録が要る |
| `accept()` の失敗に 0076 §2.7 の backoff を持ち込む | 0077 に無い挙動と設定値が増える。Learned は任意の部品で、失敗は安全側に倒れる |
| 同時接続の上限を設定値にする | 0077 §2.7 が役割ごとに1接続と決めている |
| 共通の親ディレクトリに o+x を付ける | 他の利用者からもソケットの手前までたどれるようになる |

## 5. 未決事項

なし（ソケットの path・グループの名前の本番の値は、0077 §5 のとおり段階 6 の unit テンプレートで決める）。
