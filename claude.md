# AI集客コンテンツ自動生成・分析システム

アイドル・音楽活動のデータを自動収集・分析し、データ根拠に基づいた集客コンテンツ企画・投稿文・動画台本を生成するシステムの開発プロジェクト。

## 基本技術スタック
- Python 3.11+
- SQLite (data/database.sqlite)
- Gemini API (google-genai / gemini-2.5-flash)
- YouTube Data API v3
- GitHub Actions (日次パイプライン)

## プロジェクト構造
.
├── .github/workflows/   # daily_pipeline.yml
├── config/              # settings.py
├── data/                # database.sqlite
├── prompts/             # 各種AIプロンプト (.md)
├── reports/             # 日次分析結果 (Markdown)
├── content/drafts/      # 生成されたコンテンツ案
├── src/
│   ├── collectors/      # データ収集 (YouTube API)
│   ├── db/              # SQLiteモデル・接続
│   ├── ai/              # Gemini分析・生成・評価ロジック
│   └── main.py          # エントリーポイント
└── tests/               # pytest環境

## 開発ルール・制約
1. 過剰設計を避け、個人開発者が無料〜超低コストで運用できるシンプルなコードを保つ。
2. セキュリティ: APIキーは絶対にコードへハードコードせず `.env` または GitHub Secrets から取得する。
3. 安全性: SNSへの自動投稿（API書き込み）は行わない。成果物はすべて `content/drafts/` 以下へMarkdown/JSON形式で出力し、人間の承認待ちとする。
4. 対象プラットフォーム: X / Instagram / TikTok / YouTube / note
5. エラーハンドリング: APIエラーやレスポンス不整合時にシステムが例外停止しないようログを出力し安全にフォールバックさせる。

## コマンド
- テスト実行: `pytest`
- メインパイプライン実行: `python src/main.py`
