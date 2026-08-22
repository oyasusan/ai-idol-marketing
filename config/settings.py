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
VIDEOS_DIR = BASE_DIR / "content" / "videos"


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

    # 任意: src/video/fetcher.py がyt-dlpでYouTube動画を取得する際に使う
    # Netscape形式のcookieファイルへのパス。GitHub Actions等のデータセンターIPからの
    # アクセスがYouTubeのbot判定("Sign in to confirm you're not a bot")に引っかかる場合の
    # 追加対策として任意で使用する。未設定でも動作する。
    youtube_cookies_file: str = Field(default="", alias="YOUTUBE_COOKIES_FILE")

    # 空想ロマンスのYouTube実績データを収集している別リポジトリの公開DBファイル
    youtube_analytics_db_url: str = Field(
        default="https://raw.githubusercontent.com/oyasusan/youtube-analytics/main/data/youtube_analytics.db",
        alias="YOUTUBE_ANALYTICS_DB_URL",
    )

    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    # llama-3.3-70b-versatile はGroq側で廃止されたため利用不可。
    # 動作確認済みの openai/gpt-oss-20b を既定値とする。
    groq_model: str = Field(default="openai/gpt-oss-20b", alias="GROQ_MODEL")

    # 任意: src/video/capcut_draft.py がCapCut Desktopの下書き保存フォルダを
    # 自動検出できない場合（非標準インストール先、対応外OS等）の明示的な指定先。
    # 未設定でもOS別の既定パスからの自動検出を試みる。
    capcut_draft_root: str = Field(default="", alias="CAPCUT_DRAFT_ROOT")

    # src/video/generator.py のナレーション音声合成に使うVOICEVOX ENGINEの接続先。
    # 事前にローカルでエンジンを起動しておく必要がある
    # （例: `docker run -p 50021:50021 voicevox/voicevox_engine:cpu-latest`）。
    voicevox_engine_url: str = Field(default="http://127.0.0.1:50021", alias="VOICEVOX_ENGINE_URL")
    # 話者ID。既定値53は「麒ヶ島宗麟（ノーマル）」。他の話者IDは起動中のエンジンの
    # `GET /speakers` で一覧を確認できる（VOICEVOX GUIのキャラクター選択でも確認可）。
    voicevox_speaker_id: int = Field(default=53, alias="VOICEVOX_SPEAKER_ID")

    database_path: Path = DATA_DIR / "database.sqlite"


settings = Settings()
