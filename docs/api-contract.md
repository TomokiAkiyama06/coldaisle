# API 契約（Workspace 連携の境界）

**バージョン**: v1
**位置づけ**: 本リポジトリと Personal AI Workspace の**唯一の接点**

> この文書が2つのリポジトリの境界を定義します。
> Workspace 側はこの契約だけに依存し、coldaisle の内部実装を知りません。
> 逆に coldaisle は Workspace の存在を知りません（起動もしないし、参照もしない）。

---

## 1. 基本原則

| 原則 | 内容 |
|---|---|
| **読み取り専用** | v1 に POST / PUT / DELETE は存在しない。Workspace から状態を変更できない |
| **一方向依存** | Workspace → coldaisle。逆向きの依存を作らない |
| **coldaisle は単独で動く** | Workspace が停止していても、取り込み・保存・アラート・通知は完全に機能する |
| **バージョンはパスに持つ** | `/api/v1/...`。破壊的変更は `/api/v2/` を並走させて移行する |
| **契約の変更は必ず両リポジトリに Issue を立てる** | 片側だけ変えない |

---

## 2. エンドポイント

| メソッド | パス | 用途 |
|---|---|---|
| GET | `/api/v1/health` | デーモンの稼働状態、最終受信時刻、ソース種別、欠損率 |
| GET | `/api/v1/health/summary` | **パネル1枚分。Workspace が最も多く叩く** |
| GET | `/api/v1/latest` | 全メトリクスの最新値 + 派生値 + quality |
| GET | `/api/v1/series` | 時系列。`metric` `from` `to` `agg` |
| GET | `/api/v1/stats` | min/max/mean/p95/傾き/欠測率 |
| GET | `/api/v1/alerts` | アラート一覧 |
| GET | `/api/v1/gpu/processes` | CUDA プロセス一覧と VRAM 使用量 |
| GET | `/api/v1/thermal-gate` | Compute 開始前の熱状態（signal + 理由） |
| WS | `/api/v1/stream` | 新サンプルの push |

---

## 3. Workspace が使う主要レスポンス

### `GET /api/v1/health/summary`

Server Health パネル1枚を描くのに必要な情報を、**1リクエストで**返します。
Workspace 側で複数エンドポイントを叩いて組み立てさせないこと。

```json
{
  "signal": "green",
  "summary": "室温 26.4℃、GPU吸気 28.3℃。異常なし",
  "gpu_mode": "ai",
  "metrics": {
    "air.room":         {"value": 26.42, "unit": "C",  "quality": "ok"},
    "air.room_humidity":{"value": 48.20, "unit": "%RH","quality": "ok"},
    "air.front_intake": {"value": 27.87, "unit": "C",  "quality": "ok"},
    "air.gpu_intake":   {"value": 28.25, "unit": "C",  "quality": "ok"},
    "air.gpu_exhaust":  {"value": 28.87, "unit": "C",  "quality": "ok"},
    "air.top_exhaust":  {"value": 27.06, "unit": "C",  "quality": "ok"},
    "air.rear_exhaust": {"value": 27.44, "unit": "C",  "quality": "ok"}
  },
  "derived": {
    "d.intake_rise": 1.45,
    "d.gpu_preheat": 0.38,
    "d.gpu_delta":   0.62
  },
  "active_alerts": [],
  "last_sample_at": "2026-08-23T12:34:56+09:00",
  "data_age_seconds": 2.4,
  "stale": false
}
```

**`signal` の判定規則**

| 値 | 条件 |
|---|---|
| `green` | 発生中のアラートが無く、かつ `stale` が false |
| `yellow` | warning のアラートが発生中、または一部メトリクスが `suspect` |
| `red` | critical のアラートが発生中、またはデータが取得できていない |

**データが古い場合に `green` を返してはいけません。**
無音で古い値を表示するのが、監視システムの最悪の失敗です。
`stale` が true のときは必ず `red` を返します。

### `GET /api/v1/thermal-gate`

```json
{
  "signal": "yellow",
  "reasons": ["室温 31.2℃（通常時 +5℃）", "GPU吸気 34.8℃"],
  "blocking": false
}
```

`blocking` は**常に false** です。判断は人間が行います（決定 D-08）。
将来もこのフィールドを true にする実装を入れないでください。

---

## 4. 型の共有方法

**Python パッケージを共有しない。** 依存が双方向になり、片方のリリースがもう片方を止めます。

代わりに **OpenAPI を経由**します。

```text
coldaisle (FastAPI)
    │  自動生成
    ▼
openapi.json  ──→  Workspace が TypeScript 型を生成
```

- coldaisle 側: FastAPI が `/openapi.json` を自動生成する。**手書きしない**
- coldaisle 側: CI で `openapi.json` をアーティファクトとして出力する
- Workspace 側: それを取り込んで型を生成する（`openapi-typescript` 等）
- Workspace 側: 生成物をコミットする。**生成できない環境でもビルドが通るように**

---

## 5. ネットワーク

| 項目 | 値 |
|---|---|
| bind | `127.0.0.1`（既定） |
| port | `8000`（既定） |
| 認証 | なし（単一利用者・ローカル限定のため。決定 Q-13） |
| CORS | Workspace がデスクトップアプリから叩くため、必要に応じて localhost のみ許可 |

**外部公開しないでください。** LAN公開が必要になった時点で認証を設計します。

---

## 6. 契約を変更するとき

1. 破壊的変更なら `/api/v2/` を新設し、v1 を残す
2. 両リポジトリに Issue を立て、相互にリンクする
3. Workspace 側の型生成を更新する
4. v1 の廃止は、Workspace 側の移行が完了してから

**非破壊的な追加**（新しいフィールド、新しいエンドポイント）は v1 のまま行って構いません。
Workspace 側は未知のフィールドを無視する実装にしてください。
