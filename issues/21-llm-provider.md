---
title: "LLM Provider抽象（Ollama ⇄ vLLM 切替）"
labels: ai, priority:must
milestone: "M5 AI"
---

## 背景
現在はMac + Ollama、本番はUbuntu + vLLM。どちらもOpenAI互換APIなので抽象化できる。
モデル差し替え（Qwen3.8-27B → より大きいモデル）も設定変更だけで済むようにする。

## やること
- [ ] `OPENAI_BASE_URL` / `MODEL_NAME` / `API_KEY` を環境変数で切替
- [ ] 思考モードのON/OFFを呼び出し側から制御（日常Q&Aは非思考で低レイテンシ、診断は思考モード）
- [ ] タイムアウトとリトライ
- [ ] **LLM不達時に例外を上位へ伝播させず、UIに「AI利用不可」と出すだけにする**（FR-507）
- [ ] Macでの手順を `docs/llm-setup.md` に記載（`ollama pull qwen3:8b` 等）
- [ ] vLLM起動コマンドを記載。`--gpu-memory-utilization` の上限設定理由も明記

## 受入基準
- Ollamaを止めてもダッシュボードとルールエンジンが正常動作する
- 環境変数の変更のみでバックエンドが切り替わる

## 依存
#9
