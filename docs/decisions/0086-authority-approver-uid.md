# 決定記録 0086: Authority の昇格の承認者を、CLI を実行した人の uid に結びつける

- **種別**: Decision Record
- **Status**: FINAL（2026-10-01、リポジトリ所有者が承認）
- **Date**: 2026-10-01
- **Supersedes**:
  - [0080](0080-fand-systemd-unit.md) §2.1 の最後から2つ目の項目のうち、**`authority.json` の置き場所**
    （`/var/lib/coldaisle-fand` の下）のみ。§2.2 で専用のディレクトリへ移す。
    「`/var/lib/coldaisle` に置かない」は有効
  - [0080](0080-fand-systemd-unit.md) §2.2 の表のうち、`ExecStart=` の `--authority-root` の値・
    `SupplementaryGroups=`・`ReadWritePaths=` の3つの値のみ（§2.2 で1つずつ足す）。
    `StateDirectory=coldaisle-fand` と `StateDirectoryMode=0700` は変えない

  旧記録（0080）側への `Superseded by` の追記は、本記録を FINAL にした PR で行った。
- **関連**: [0057](0057-authority-rollout-stage-changes.md) §2.1 / §2.3 / §2.6 /
  [0062](0062-model-registry-operations.md) §2.2（承認を引数から組み立てない前例） /
  [0072](0072-control-admin-entry.md) §2.1 / §2.5 / §2.6 / §2.10 段階 3、未決 5 /
  [0080](0080-fand-systemd-unit.md) §2.1 / §2.2、未決 8 /
  [0082](0082-authority-wiring-settled-points.md) §2.1（journal v3 と一方向の書き直し） /
  AGENTS.md「絶対に守るルール」9・10
- **対象 Issue**: #92（0072 §2.10 段階 3：`coldaisle-authority raise` / `rollback`）

---

## 1. Context

0057 §2.3 は昇格の承認（`StageApproval`）に `approver` を持たせたが、それが**誰か**を
確かめる手段を決めていない。いまの `approver` は `^[a-z][a-z0-9_.-]*$` に合う任意の文字列で、
承認を作った人が自分で書く。0072 は管理ソケットの操作者を `SO_PEERCRED` の `uid.<数値>` で
記録すると決めた（§2.5）が、昇格はソケットを通らず CLI（`coldaisle-authority raise`）が
`AuthorityStore.raise_stage()` を直接呼ぶ（§2.1）ので、この仕組みが効かない。

0072 未決 5 と 0080 未決 8 がこれを #92 段階 3 へ送った。

**2026-10-01、所有者は「結びつける」を選んだ。** 承認者は実行した人の uid を、管理ソケットの
操作者と同じ `uid.<数値>` の形で記録し、journal の版を上げる。細部（§2 の 2.1〜2.9）は
本記録の案（PR #208）で選択肢として示し、**同日、所有者がすべて推奨案で承認した。**
選ばれなかった案は §4 に理由とともに残す。

前提として、次の事実がある。

| 事実 | 出典 |
|---|---|
| `authority.json` は fand 専用の `/var/lib/coldaisle-fand`（所有者 `coldaisle-fan`、`0700`）の下に置く | 0080 §2.1 / §2.2（本記録で置き換え） |
| `AuthorityStore` は journal・一時ファイル・lock を `0600`、作るディレクトリを `0700` で作る | `control/authority.py` |
| `coldaisle-fan` は hwmon の PWM を書けるグループでもある | 0080 §2.1 / §2.6 |
| 管理ソケットは同じ uid も root も暗黙には認めない | 0072 §2.5 |
| 走行中の fand は journal を読めないと `SHADOW` へ下げ、起動時に読めなければ終了コード 5 | 0072 §2.6 / 0082 §2.3 |
| journal は v3。書くたびに v3 へ一方向に書き直す | 0082 §2.1 |

このままでは、人が自分の uid で CLI を実行すると `0700` のディレクトリに書けない。
書くために `coldaisle-fan` や root になると、実行した uid は人の uid ではなくなる。
**uid の取り方と、誰が書けるかは切り離せない**ので、両方をここで決める。

### この束縛が守るもの・守らないもの

- **守る**: CLI を正しく使う人が、別の人の名前で承認を書くこと（書き間違い・なりすまし）。
  承認者の欄が自己申告ではなく、カーネルが知っている実行者になる
- **守らない**: journal のディレクトリに書ける人が、CLI を通らずに `authority.json` を直接書き換えること。
  これはいま（fand の uid が書ける）も同じで、journal の改ざん検知は本記録の範囲外とする（§5）。
  0057 の検証（承認・証拠・Registry の束縛）を回避できるのも同じ範囲である

## 2. Decision

### 2.1 uid の取り方：`os.getuid()`。`os.geteuid()` と違えば拒む。`SUDO_UID` は読まない

- 承認者は `uid.<os.getuid()>` とする
- `os.getuid() != os.geteuid()` なら拒む（setuid のラッパー経由で、実行した人と書いた権限が
  食い違う状態を作らない）
- `SUDO_UID` / `SUDO_USER` / `LOGNAME` / `USER` などの**環境変数は一切読まない**。
  `sudo -u <人>` で他人として実行したときはその人の uid が残るが、それができるのは root だけで、
  root は journal を直接書けるので、守る範囲（§1）は変わらない
- 数値の範囲は `0 <= uid <= 4294967294`（`uid_t` の `-1` は「無効」の意味なので除く）
- 名前解決（`pwd.getpwuid`）の結果は**記録に使わない**（0072 §2.5 と同じ。§2.8）

### 2.2 書ける人：承認者のグループと、authority 専用のディレクトリ

承認者は**自分の uid のまま** CLI を実行する。そのために:

- 承認者のグループ `coldaisle-authority`（**仮の名前**）を作り、昇格・rollback を行う人だけを入れる。
  **`coldaisle`（API / 取り込み）と AI 層のユーザーは入れない**（0072 §2.5 の配置の必須条件と同じ）
- journal を `/var/lib/coldaisle-authority`（**仮の名前**。所有者 `coldaisle-fan`、グループ
  `coldaisle-authority`、`2770`）に置く。setgid で、人が作ったファイルもグループが
  `coldaisle-authority` になる。**systemd の `StateDirectory=` にしない**（`StateDirectory=` は
  所有者とグループを unit の `User=` / `Group=` へ付け替えるので、グループが `coldaisle-fan` に戻る）。
  作成は導入手順（`docs/ubuntu-deploy.md`。`install -d -o coldaisle-fan -g coldaisle-authority -m 2770`）で行う
- fand の unit（0080 §2.2 の3つの値を置き換える）:
  - `SupplementaryGroups=` に `coldaisle-authority` を足す（人が書いた `0660` の journal を読み書きするため）
  - `ReadWritePaths=` に `/var/lib/coldaisle-authority` を足す（`ProtectSystem=strict` の下で開ける）
  - `ExecStart=` の `--authority-root` を `/var/lib/coldaisle-authority` に向ける
- `/var/lib/coldaisle-fand` は `0700` のまま残す（`StateDirectory=coldaisle-fand` と
  `StateDirectoryMode=0700` は変えない。中身が無ければ空でよい）

**PR #209（0080 段階 1 の unit テンプレート。本記録の時点で未マージ）との整合**: #209 は 0080 のとおり
`--authority-root /var/lib/coldaisle-fand/authority` と、`coldaisle-authority` を含まない
`SupplementaryGroups=` / `ReadWritePaths=` で書かれている。**#209 のマージ前にその PR の中で、または
マージ後すぐの追従 PR で**、journal の置き場所・グループ・`ReadWritePaths=` を本記録に合わせる
（静的試験 `tests/test_deploy_templates.py` も同じ値を確かめるよう直す）。合わせるまでの間、
テンプレートの値は本記録と食い違っており、導入には使わない（0080 §2.10 のとおり `enable` は承認点 3 の後）。

### 2.3 承認できない uid：root と fand の実行ユーザー

昇格は次のとき拒む（journal を読む前に、終了コードで区別できる error にする）。
**この拒否は `AuthorityStore.raise_stage()` の中で行う**（§2.5 の束縛の確認と同じ場所）。CLI だけで
確かめると、fand の実行ユーザーとして Python から `raise_stage()` を直接呼べば、`uid.<fand の uid>` の
承認を組み立てて通せてしまう（§2.5 の束縛はその承認を実行者と一致するので通す）。CLI は error を
表示と終了コードへ写すだけにする。

- `uid == 0`（root）
- `uid` が authority のディレクトリ（`--authority-root`）の所有者と同じ（= fand の実行ユーザー。
  fand が乗っ取られても自分で昇格を書けない）
- `os.getuid() != os.geteuid()`（§2.1）

誰が承認できるかは、§2.2 のグループの所属という1つの事実に集める（設定に uid の一覧は置かない）。
**rollback は §2.6 で別に扱う**（安全側なので、ここの制限を掛けない）。

### 2.4 ファイルの mode：CLI と fand の両方が `0660` で作る。CLI はディレクトリを作らない

人の shell の `umask` は `0022` が多く、そのまま作ると journal と lock が `0640` になり、
fand が lock を `O_RDWR` で開けない（降格を書き残せなくなる）。

- `AuthorityStore` は journal の一時ファイルと lock を作ったあと `fchmod(0o660)` する
  （`umask` に依らない）。既にある lock の mode は変えない（他人のファイルの mode を変えようとして
  `EPERM` で止まらないため。導入手順で揃える）
- CLI の store は authority のディレクトリを**作らない**（無ければ error。いまの `_exclusive_lock` は
  `0700` で作るので、人の uid が所有する、fand が読めないディレクトリができてしまう）。fand は従来どおり
  無ければ作ってよい（開発用の `var/authority`）
- CLI は書く前に、authority のディレクトリが「ディレクトリ・other に権限が無い・setgid 付き」であることを
  確かめ、違えば書かずに止める（導入の誤りを、journal が読めなくなる前に見つける）

### 2.5 束縛を確かめる場所：`AuthorityStore` が実行者を自分で知る。承認ファイルに承認者を書かない

- `AuthorityStore` は時計と同じく**実行者の source**（`ProcessIdentity`。`uid` と `euid` を返す。
  既定は §2.1 の取り方。試験では差し替える）を持つ。`raise_stage()` は、まず §2.3 の3つ（`uid == 0`・
  authority のディレクトリの所有者と同じ uid・`uid != euid`）を journal を読む前に拒み、次に
  `approval.approver` がこの source の `uid.<数値>` と一致しなければ `AuthorityApprovalError` で拒む。0057 §2.3 の「承認者が値を持ち込む余地を残さない」を
  承認者の欄にも当てはめる
- CLI に `--approver` は作らない。承認は 0062 §2.2 と同じくファイルで渡すが、そのファイルに
  `approver` の欄を**置かない**（あれば拒む）。CLI が §2.1 の uid で `StageApproval` を組み立てる

### 2.6 rollback（`coldaisle-authority rollback`）も uid を記録する。ただし誰も拒まない

- rollback の `actor` は `uid.<os.getuid()>`（§2.1 と同じ取り方。root なら `uid.0`）
- **uid によって拒まない**（root・fand の実行ユーザーでも通す）。降格は安全側で、0057 §2.6 は
  「安全側へは常に動ける」を求める。誰が戻したかは記録に残る。0072 §2.1 の「fand が止まっているときの
  rollback」はこの CLI なので、障害時に root しか残っていなくても journal を戻せる必要がある

### 2.7 journal v4 の形と、v3 からの移行

**版を上げる理由**: 新しい昇格の event が「承認者は実行した uid に束縛済み」を持つ。v3 までの reader は
その欄を知らないので、0082 §2.1 と同じく「知らない journal」として一律に拒ませる。

- `StageApproval` に `approver_binding: Literal["process_uid"]` を足す（欄が無い＝v3 までの
  自己申告の承認。既存の event は書き換えない）
- この欄があるとき、`approver` は `^uid\.(0|[1-9][0-9]*)$` に合い、数値が `1..4294967294`
  （`uid.0` は §2.3 で作れないので journal でも拒む）
- この欄は v4 の journal にだけ置ける
- **束縛は一度入ったら外せない**: 束縛した昇格のあとに、束縛の無い昇格を記録しない
  （`air_balance_config_sha256` の束縛と同じ規則。0073 §2.6 の実装）
- `raise_stage()` は束縛の無い承認を書かない（§2.5）
- 降格の event の形は変えない（人の降格の `actor` は既に `uid.<数値>` か `control_admin`。
  自動降格は `control_runtime` など）

**移行（0082 §2.1 と同じ一方向）**

- v1〜v3 の journal はそのまま読み、次に書くとき（CLI の昇格・rollback、fand の降格のどれでも）に
  v4 で書き直す。既存の event は書き換えない
- **新しい reader を先に配る。** v4 を読めない fand は、走行中なら `authority_journal_unreadable` で
  `SHADOW` へ下がり、起動時なら終了コード 5 で止まる（0072 §2.6 / 0082 §2.3。安全側だが制御を取れない）。
  導入手順は「パッケージを更新 → fand を再起動 → それから CLI を使う」の順とする
- 切り戻しは 0082 と同じく journal の退避と昇格のやり直しが要る（`docs/authority-rollout.md` に追記）
- **置き場所の移行**（§2.2）: fand を止め、`authority.json` と `.authority.lock` を新しいディレクトリへ
  移し、グループ `coldaisle-authority`・`0660` に揃え、unit を差し替えてから起動する。
  手順は `docs/ubuntu-deploy.md` に書く（実機の path・ユーザー名は書かない。AGENTS.md ルール10）

### 2.8 表示と監査：journal には `uid.<数値>` だけ。名前は表示のときだけ

- journal・decision trace（`authority_last_change.actor`）・管理ソケットの `status` には
  `uid.<数値>` だけを残す。trace の形は変えない
- CLI の出力（`raise` / `rollback` の結果）では、その場で `pwd.getpwuid` を引けたら
  `uid.<数値>（<名前>）` と表示する。引けなければ数値だけ。**名前を記録へ書き戻さない**
  （アカウントの改名・削除・別ホストで意味が変わるため）
- CLI は昇格・rollback のたびに JSON Lines の構造化ログを1行出す（`event`・`uid`・`euid`・
  `from_stage` / `to_stage`・新しい `revision`・承認が指した報告の `report_sha256`）。
  失敗（§2.3 の拒否を含む）も理由の code 付きで出す
- 管理操作の監査の表（`control_admin_audit`。0072 §2.7）には**書かない**。CLI は DB を開かない
  （承認者のグループに DB の書き込み権を渡さないため）。authority の変更の正本は journal である

### 2.9 試験（実機不要。`tmp_path` と差し替えた `ProcessIdentity`）

- 昇格の event の `actor` / `approver` が `uid.<n>`・`approver_binding = "process_uid"` になる
- `approval.approver` が実行者と違えば拒む。`uid == 0`・ディレクトリの所有者と同じ uid・
  `getuid != geteuid` を、それぞれ journal を書かずに拒む。**CLI を通さず `AuthorityStore.raise_stage()` を
  直接呼んでも**、実行者と一致する承認（`uid.<ディレクトリの所有者の uid>` など）で同じく拒む
- 環境変数 `SUDO_UID` を別の値にしても、記録される uid が変わらない
- CLI の引数に `--approver` が無い。承認ファイルに `approver` があれば拒む
- rollback は root（差し替えで uid 0）でも通り、`uid.0` が残る
- v3 の journal（束縛の無い昇格を含む）を読み、昇格・降格のどちらで書いても v4 になり、既存の event が
  同じ内容のまま残る。束縛の欄を持つ v3 の journal・束縛した昇格の後の束縛の無い昇格・
  `approver_binding` 付きの `uid.0` や `uid` の形でない承認者を、それぞれ検証で拒む
- `umask 0022` のもとで CLI 側の store が作った journal と lock が `0660` になる。
  CLI の store は authority のディレクトリが無ければ作らずに error、setgid が無ければ書かずに error
- 既存の「管理ソケットは昇格を受けない」「`model_registry.py` は authority を参照しない」の走査は変えない
- root で走る CI では mode と所有者の試験の一部が意味を持たないので、理由を書いて skip する（0080 と同じ）

### 2.10 実装の段階

| 段階 | 担当 Issue | 内容 |
|---|---|---|
| 3a | #92 | `ProcessIdentity`・journal v4（§2.7）・`raise_stage()` の拒否（§2.3）と束縛の確認（§2.5）・store の `0660`（§2.4）と試験 |
| 3b | #92 | `coldaisle-authority raise` / `rollback` の CLI（§2.1・§2.6・§2.8。§2.3 の拒否は store の error を表示と終了コードへ写すだけ）と `docs/authority-rollout.md` |
| 3c | #57（PR #209 の中、またはその直後の追従 PR） | fand の unit の3つの値（§2.2）とその静的試験・`docs/ubuntu-deploy.md` の承認者のグループとディレクトリ、置き場所の移行（§2.7） |

3a と 3b は1つの PR でもよい（いずれも #92。分けるかは実装の大きさで決める）。

## 3. Consequences

### 良くなること

- 0072 未決 5・0080 未決 8 が閉じる。昇格の承認者が自己申告ではなく、カーネルが知っている実行者になる
- 管理ソケットの降格（`SO_PEERCRED`）と CLI の昇格・rollback が、同じ `uid.<数値>` の形で journal に並ぶ
- 承認者が hwmon を書けるアカウントにも root にもならずに昇格できる
- fand の実行ユーザーは自分で昇格を書けない（乗っ取られても authority を上げる経路が1つ減る）

### 悪くなること・その緩和

| トレードオフ | 緩和策 |
|---|---|
| グループとディレクトリが1つずつ増え、0080 の値を3つ変える | 名前は仮の値のまま、テンプレートと導入手順にだけ書く。fand の `0700` の状態ディレクトリはそのまま |
| PR #209 のテンプレートが本記録と食い違ったまま入りうる | §2.2 のとおり、マージ前またはマージ直後の追従 PR で揃える。`enable` は承認点 3 の後なので、揃うまで導入には使わない |
| 承認者のグループの人は `authority.json` を直接書き換えられる（CLI の検証を回避できる） | いまの fand の uid と同じ限界。束縛は「正しく CLI を使う人の帰属」を守るもので、改ざん検知は §5 に送る。グループには昇格を任せる人だけを入れる |
| journal v4 への一方向の書き直し。古い fand は v4 を読めない | 新しい reader を先に配る順序を手順に書く。読めない場合も `SHADOW` / 終了コード 5 の安全側（0072 §2.6 / 0082 §2.3） |
| `sudo -u <人>` の実行はその人の uid として記録される | できるのは root だけで、root は journal を直接書ける。守る範囲の外として明記する（§1） |
| CLI は監査の表に残らない | authority の変更は journal が正本で、構造化ログにも残る（§2.8） |

## 4. 却下した代替案

- **束縛しない（`approver` を自己申告のまま）**: 所有者が 2026-10-01 に「結びつける」を選んだ
- **昇格を管理ソケットで受けて `SO_PEERCRED` を使う**: 0072 §4 の D で却下済み（ソケットの認可の誤りが
  authority の増加になる。Registry の lock 待ちが control process に入る）

**uid の取り方（§2.1）**

- `os.geteuid()` だけを見る: setuid のラッパーで実行者と記録が食い違っても気づかない
- root で実行し、`SUDO_UID` を承認者として信じる: `SUDO_UID` は root なら誰でも好きな値にできる
  環境変数で、カーネルの事実ではない
- 特権の小さな補助プロセスへ Unix ソケットで繋ぎ、`SO_PEERCRED` で uid を取る: uid はカーネルの事実に
  なるが、0072 §4 の D と同じく Registry の lock 待ちと報告の検証を別のデーモンへ持ち込む。入口が1つ増える

**書ける人（§2.2）**

- `sudo -u coldaisle-fan coldaisle-authority ...` を sudoers で許し、`SUDO_UID` を使う: 0080 を変えずに
  済むが、承認者が **hwmon の PWM を書けるアカウント**でプログラムを動かすことになる。uid が環境変数の
  慣習に依存し（`su` 経由では無い）、`coldaisle-fan` で動く任意のプロセス（fand 自身を含む）も偽れる
- `sudo` で root として実行し、`SUDO_UID` を承認者とする: root を暗黙に認めない 0072 §2.5 の原則に反し、
  Registry と数 MB の報告の検証を root で走らせる
- 人を `coldaisle-fan` グループに入れ、`/var/lib/coldaisle-fand` を `0770` にする: そのグループは
  hwmon の PWM を書ける（0080 §2.6）ので、承認者が Fan を直接動かせるようになる

**承認できない uid（§2.3）**

- root を認め、`SUDO_UID` があればそれを承認者にする: §2.1 の却下と同じ理由
- 許す uid の一覧を設定に置く: グループの所属と二重管理になり、食い違ったときにどちらが正しいかが決まらない
- 拒否を CLI だけで行い、store は承認者と実行者の一致だけを見る: fand の実行ユーザーが Python から
  `raise_stage()` を直接呼ぶと、自分の uid の承認で昇格を書ける（§2.5 の「CLI だけで確かめる」の却下と同じ理由）

**ファイルの mode（§2.4）**

- `umask` に任せ、導入手順で「CLI の前に `umask 0007`」と書く: 忘れると fand が降格を書けなくなり、
  気づくのが運転中になる
- `0640` でよいとし、fand が書くときに `fchmod` で直す: 人が書いてから fand が書くまでの間、lock を開けない

**束縛を確かめる場所（§2.5）**

- CLI だけで確かめ、`raise_stage()` は `approver` を信じる: Python から直接 `raise_stage()` を呼ぶ経路
  （試験・将来の別の入口）で束縛が外れる
- 承認ファイルに `approver` を書かせ、CLI が uid と一致することを確かめる: 書き手が毎回自分の uid を
  調べて書く手間が増え、合わなければ拒まれるだけで得るものが無い

**rollback（§2.6）**

- 昇格と同じ制限を掛ける: 障害時に root しか残っていない状況で、fand が止まっていると journal 上の
  rollback ができなくなる
- rollback には uid を記録しない: 管理ソケット経由の降格が `uid.<数値>` なのに、CLI 経由だけが自己申告になる

**journal v4 の形（§2.7）**

- `approver_uid: int` を足し、`approver == f"uid.{approver_uid}"` を検証する: 同じ事実を2つの欄に持ち、
  型の上で食い違いうる
- 欄を足さず、v4 では昇格の `approver` を `uid.<数値>` の形に限る: v3 までの自己申告の承認
  （たまたま `uid.1000` のように書いたものを含む）と区別できず、v3 の event を含む journal を v4 で書き直せない

**表示と監査（§2.8）**

- journal に名前も残す: 改名・削除で記録の意味が変わり、0072 §2.5 と食い違う
- CLI も監査の表に書く: 承認者に `coldaisle` グループ（DB）の書き込み権が要り、API / 取り込みと
  同じ DB を人が書けるようになる

## 5. 未決事項

| # | 内容 | 決める場所 |
|---|---|---|
| 1 | journal の改ざん検知（署名・追記専用の別記録など）。承認者のグループが直接書ける限界を塞ぐか | 実運用の後、別の決定記録 |
| 2 | Model Registry の昇格（`coldaisle-registry promote` の `approver`）も実行者の uid に結びつけるか | 0062 の側（本記録は authority だけ） |
| 3 | 承認者が Model Registry の lock を取り、報告と制御設定を読むための権限（Registry のディレクトリのグループ） | 3c の導入手順。足りなければ別の記録 |
| 4 | グループ名・ディレクトリ名の実際の値 | 導入先で決める（リポジトリには仮の値だけ） |

**2026-10-01、リポジトリ所有者がこの記録を承認した。** 承認の対象は §2.1〜§2.9 の細部すべて
（PR #208 で示した判断点 1〜9 を、いずれも推奨案で）と、0080 §2.1 / §2.2 の一部の置き換えである。

承認の後、マージ前のレビューで、§2.3 の拒否を行う場所を CLI から `AuthorityStore.raise_stage()` へ移した
（§2.3 / §2.5 / §2.9 / §2.10、§4 に1項目）。拒む条件は変えておらず、§2.5 で承認された「束縛を store で
確かめる」理由を §2.3 にも当てはめた締め付けである。
