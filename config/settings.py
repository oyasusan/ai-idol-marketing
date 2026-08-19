"""
アプリケーション設定モジュール。

.env ファイル（存在すれば）および環境変数から設定値を読み込む。
APIキー等の秘匿情報はコードにハードコードせず、必ずここを経由して取得する。
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
REPORTS_DIR = BASE_DIR / "reports"
DRAFTS_DIR = BASE_DIR / "content" / "drafts"
PROMPTS_DIR = BASE_DIR / "prompts"


class Settings(BaseSettings):
    """環境変数 / .env から読み込まれるアプリケーション設定。"""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 任意: src/collectors/youtube.py でYouTube Data APIを直接叩く場合のみ使用。
    # 既定のパイプライン(src/main.py)は youtube_analytics_db_url 経由のデータ取り込みを使うため不要。
    youtube_api_key: str = Field(default="", alias="YOUTUBE_API_KEY")
    youtube_channel_id: str = Field(default="", alias="YOUTUBE_CHANNEL_ID")

    # 空想ロマンスのYouTube実績データを収集している別リポジトリの公開DBファイル
    youtube_analytics_db_url: str = Field(
        default="https://raw.githubusercontent.com/oyasusan/youtube-analytics/main/data/youtube_analytics.db",
        alias="YOUTUBE_ANALYTICS_DB_URL",
    )

    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    # llama-3.3-70b-versatile はGroq側で廃止されたため利用不可。
    # 動作確認済みの openai/gpt-oss-20b を既定値とする。
    groq_model: str = Field(default="openai/gpt-oss-20b", alias="GROQ_MODEL")

    database_path: Path = DATA_DIR / "database.sqlite"


settings = Settings()
