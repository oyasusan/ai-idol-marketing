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
        CHECK (status IN ('pending', 'approved', 'rejected', 'needs_revision', 'published')),
    draft_file_path TEXT,                   -- content/drafts/ 配下の出力ファイルパス
    reviewed_by TEXT,
    reviewed_at TEXT,
    -- 以下は実際に投稿された後の成果（フィードバック・学習ループ用）。
    -- 本システムは投稿を自動化しないため、これらは人間が
    -- src/db/record_result.py 経由で手動記録する。
    actual_view_count INTEGER,
    actual_like_count INTEGER,
    actual_comment_count INTEGER,
    actual_impression_count INTEGER,
    actual_result_recorded_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_generated_contents_status ON generated_contents(status);
CREATE INDEX IF NOT EXISTS idx_generated_contents_platform ON generated_contents(platform);
CREATE INDEX IF NOT EXISTS idx_generated_contents_actual_view_count
    ON generated_contents(actual_view_count);
"""

# SQLiteはALTER TABLEでCHECK制約を変更できないため、既存の generated_contents
# (旧スキーマ: statusにpublishedが無い/actual_*列が無い)を新スキーマへ
# 安全に移行するためのテーブル再作成SQL。既存データは全件そのままコピーする。
_MIGRATE_GENERATED_CONTENTS_SQL = """
ALTER TABLE generated_contents RENAME TO generated_contents_old;

CREATE TABLE generated_contents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER REFERENCES ai_analyses(id) ON DELETE SET NULL,
    platform TEXT NOT NULL CHECK (platform IN ('X', 'Instagram', 'TikTok', 'YouTube', 'note')),
    content_type TEXT NOT NULL,
    title TEXT,
    body TEXT NOT NULL,
    target_persona TEXT,
    evaluation_score REAL CHECK (evaluation_score IS NULL OR (evaluation_score BETWEEN 0 AND 100)),
    evaluation_reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'needs_revision', 'published')),
    draft_file_path TEXT,
    reviewed_by TEXT,
    reviewed_at TEXT,
    actual_view_count INTEGER,
    actual_like_count INTEGER,
    actual_comment_count INTEGER,
    actual_impression_count INTEGER,
    actual_result_recorded_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO generated_contents (
    id, analysis_id, platform, content_type, title, body, target_persona,
    evaluation_score, evaluation_reason, status, draft_file_path,
    reviewed_by, reviewed_at, created_at, updated_at
)
SELECT
    id, analysis_id, platform, content_type, title, body, target_persona,
    evaluation_score, evaluation_reason, status, draft_file_path,
    reviewed_by, reviewed_at, created_at, updated_at
FROM generated_contents_old;

DROP TABLE generated_contents_old;

CREATE INDEX IF NOT EXISTS idx_generated_contents_status ON generated_contents(status);
CREATE INDEX IF NOT EXISTS idx_generated_contents_platform ON generated_contents(platform);
CREATE INDEX IF NOT EXISTS idx_generated_contents_actual_view_count
    ON generated_contents(actual_view_count);
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
    PUBLISHED = "published"  # 実際にSNSへ投稿済み（人間が手動投稿した後に記録する）


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
    # 実際に投稿された後の成果（record_result.py が記録する）
    actual_view_count: Optional[int] = None
    actual_like_count: Optional[int] = None
    actual_comment_count: Optional[int] = None
    actual_impression_count: Optional[int] = None
    actual_result_recorded_at: Optional[str] = None


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """SQLite接続を返す。行を辞書ライクに扱えるよう row_factory を設定する。"""

    path = db_path or settings.database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _needs_generated_contents_migration(conn: sqlite3.Connection) -> bool:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(generated_contents)")}
    if not columns:
        return False  # テーブル未作成。SCHEMA_SQL側のCREATE TABLEで新スキーマが作られる。
    return "actual_view_count" not in columns


def init_db(db_path: Optional[Path] = None) -> None:
    """テーブルが存在しなければ作成する（冪等）。

    既存DBが旧スキーマ（実績記録用カラムが無い generated_contents）の場合は、
    テーブルを安全に再作成して移行する（データは保持される）。
    """

    conn = get_connection(db_path)
    try:
        # 移行はSCHEMA_SQL実行前に行う。SCHEMA_SQLには新カラムを対象とした
        # CREATE INDEXが含まれるため、旧スキーマのテーブルが残ったまま
        # 実行すると「no such column」エラーになる。
        if _needs_generated_contents_migration(conn):
            conn.executescript(_MIGRATE_GENERATED_CONTENTS_SQL)
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def upsert_content(conn: sqlite3.Connection, content: Content) -> None:
    """contents テーブルへ冪等にUPSERTする。

    複数のコレクター（src/collectors/youtube.py, src/collectors/youtube_analytics_import.py）
    から共通で利用される。
    """

    conn.execute(
        """
        INSERT INTO contents (
            id, title, description, published_at, channel_id, duration_seconds,
            thumbnail_url, tags, view_count, like_count, comment_count,
            collected_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            published_at = excluded.published_at,
            channel_id = excluded.channel_id,
            duration_seconds = excluded.duration_seconds,
            thumbnail_url = excluded.thumbnail_url,
            tags = excluded.tags,
            view_count = excluded.view_count,
            like_count = excluded.like_count,
            comment_count = excluded.comment_count,
            collected_at = datetime('now'),
            updated_at = datetime('now')
        """,
        (
            content.id,
            content.title,
            content.description,
            content.published_at,
            content.channel_id,
            content.duration_seconds,
            content.thumbnail_url,
            content.tags,
            content.view_count,
            content.like_count,
            content.comment_count,
        ),
    )


if __name__ == "__main__":
    init_db()
    print(f"[{datetime.now().isoformat(timespec='seconds')}] DB initialized at: {settings.database_path}")
