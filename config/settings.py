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

    youtube_api_key: str = Field(default="", alias="YOUTUBE_API_KEY")
    youtube_channel_id: str = Field(default="", alias="YOUTUBE_CHANNEL_ID")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")

    database_path: Path = DATA_DIR / "database.sqlite"


settings = Settings()
