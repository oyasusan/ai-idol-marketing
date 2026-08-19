"""
SQLite初期化・テーブル作成スクリプト。

3テーブルを管理する:
- contents: YouTube動画メタデータ・実績データ
- ai_analyses: AIによる勝ちパターン・負けパターンの分析履歴
- generated_contents: 生成コンテンツ案・ターゲット属性・事前評価スコア・承認ステータス

単体実行すると `config.settings.settings.database_path` にDBファイルを作成する。
    python -m src.db.models
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from config.settings import settings

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS contents (
    id TEXT PRIMARY KEY,                    -- YouTube動画ID
    title TEXT NOT NULL,
    description TEXT,
    published_at TEXT NOT NULL,             -- ISO8601
    channel_id TEXT NOT NULL,
    duration_seconds INTEGER,
    thumbnail_url TEXT,
    tags TEXT,                              -- JSON配列文字列
    view_count INTEGER NOT NULL DEFAULT 0,
    like_count INTEGER NOT NULL DEFAULT 0,
    comment_count INTEGER NOT NULL DEFAULT 0,
    collected_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_contents_published_at ON contents(published_at);

CREATE TABLE IF NOT EXISTS ai_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_date TEXT NOT NULL DEFAULT (datetime('now')),
    period_start TEXT,                      -- 分析対象期間 開始 (ISO8601)
    period_end TEXT,                        -- 分析対象期間 終了 (ISO8601)
    target_content_ids TEXT,                -- 分析対象 contents.id のJSON配列
    win_patterns TEXT,                      -- 勝ちパターン (Markdown/JSON)
    loss_patterns TEXT,                     -- 負けパターン (Markdown/JSON)
    summary TEXT,
    model_name TEXT NOT NULL,               -- 使用したAIモデル名 (例: llama-3.3-70b-versatile)
    raw_response TEXT,                      -- 監査用の生レスポンス
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ai_analyses_analysis_date ON ai_analyses(analysis_date);

CREATE TABLE IF NOT EXISTS generated_contents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER REFERENCES ai_analyses(id) ON DELETE SET NULL,
    platform TEXT NOT NULL CHECK (platform IN ('X', 'Instagram', 'TikTok', 'YouTube', 'note')),
    content_type TEXT NOT NULL,             -- post_text / video_script / thumbnail_idea 等
    title TEXT,
    body TEXT NOT NULL,
    target_persona TEXT,                    -- ターゲット属性 (JSON/テキスト)
    evaluation_score REAL CHECK (evaluation_score IS NULL OR (evaluation_score BETWEEN 0 AND 100)),
    evaluation_reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'needs_revision')),
    draft_file_path TEXT,                   -- content/drafts/ 配下の出力ファイルパス
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_generated_contents_status ON generated_contents(status);
CREATE INDEX IF NOT EXISTS idx_generated_contents_platform ON generated_contents(platform);
"""


class Platform(str, Enum):
    X = "X"
    INSTAGRAM = "Instagram"
    TIKTOK = "TikTok"
    YOUTUBE = "YouTube"
    NOTE = "note"


class ContentStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVISION = "needs_revision"


class Content(BaseModel):
    """contents テーブル1行分のデータモデル。"""

    id: str
    title: str
    description: Optional[str] = None
    published_at: str
    channel_id: str
    duration_seconds: Optional[int] = None
    thumbnail_url: Optional[str] = None
    tags: Optional[str] = None  # JSON配列文字列
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0


class AIAnalysis(BaseModel):
    """ai_analyses テーブル1行分のデータモデル。"""

    id: Optional[int] = None
    period_start: Optional[str] = None
    period_end: Optional[str] = None
    target_content_ids: Optional[str] = None  # JSON配列文字列
    win_patterns: Optional[str] = None
    loss_patterns: Optional[str] = None
    summary: Optional[str] = None
    model_name: str
    raw_response: Optional[str] = None


class GeneratedContent(BaseModel):
    """generated_contents テーブル1行分のデータモデル。"""

    id: Optional[int] = None
    analysis_id: Optional[int] = None
    platform: Platform
    content_type: str
    title: Optional[str] = None
    body: str
    target_persona: Optional[str] = None
    evaluation_score: Optional[float] = Field(default=None, ge=0, le=100)
    evaluation_reason: Optional[str] = None
    status: ContentStatus = ContentStatus.PENDING
    draft_file_path: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """SQLite接続を返す。行を辞書ライクに扱えるよう row_factory を設定する。"""

    path = db_path or settings.database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    """テーブルが存在しなければ作成する（冪等）。"""

    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"[{datetime.now().isoformat(timespec='seconds')}] DB initialized at: {settings.database_path}")
