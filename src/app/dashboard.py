"""
Streamlit Webダッシュボード。

生成された各種コンテンツ企画（`generated_contents`）と生成動画を、ブラウザ上で
メディア別・日付別に統合して閲覧・確認できるダッシュボード。

- タブ1: 今日のコンテンツ一覧（カード表示、ワンクリックコピー、採用/却下ボタン）
- タブ2: メディア別アーカイブ（時系列カード表示、キーワード検索）
- タブ3: 生成動画プレビュー（content/videos/ の.mp4をブラウザ再生）
- タブ4: 成果記録・フィードバック（実績データをDBへ直接記録するフォーム）

本ダッシュボードはSNSへの自動投稿を一切行わない。「採用する」ボタンは
`generated_contents.status` を更新するのみで、投稿自体は人間が手動で行う。

起動:
    streamlit run src/app/dashboard.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Optional

# `streamlit run src/app/dashboard.py` のように直接実行された場合、
# プロジェクトルートがsys.pathに入らず src/config パッケージを解決できないため補正する。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from streamlit.runtime.scriptrunner import get_script_run_ctx  # noqa: E402

from config.settings import VIDEOS_DIR  # noqa: E402
from src.db.models import ContentStatus, Platform, get_connection, init_db  # noqa: E402
from src.db.record_result import record_result  # noqa: E402

PLATFORM_OPTIONS = ["すべて"] + [p.value for p in Platform]
STATUS_LABEL_TO_VALUE = {
    "すべて": None,
    "DRAFT (未承認)": ContentStatus.PENDING.value,
    "APPROVED (承認済み)": ContentStatus.APPROVED.value,
    "PUBLISHED (投稿済み)": ContentStatus.PUBLISHED.value,
}


# ---------------------------------------------------------------------------
# データ取得・更新（Streamlit非依存の純粋関数。pytestで直接テストする）
# ---------------------------------------------------------------------------


def fetch_available_dates(conn: sqlite3.Connection, limit: int = 30) -> list[str]:
    """コンテンツが生成された日付一覧(新しい順)を返す。"""

    rows = conn.execute(
        """
        SELECT DISTINCT date(created_at) AS d
        FROM generated_contents
        ORDER BY d DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [row["d"] for row in rows]


def fetch_contents(
    conn: sqlite3.Connection,
    target_date: Optional[str] = None,
    platform: Optional[str] = None,
    status: Optional[str] = None,
    search_query: Optional[str] = None,
    limit: int = 200,
) -> list[sqlite3.Row]:
    """フィルタ条件に応じて generated_contents を取得する（新しい順）。"""

    conditions: list[str] = []
    params: list[object] = []

    if target_date:
        conditions.append("date(created_at) = ?")
        params.append(target_date)
    if platform:
        conditions.append("platform = ?")
        params.append(platform)
    if status:
        conditions.append("status = ?")
        params.append(status)
    if search_query:
        conditions.append("(title LIKE ? OR body LIKE ?)")
        like = f"%{search_query}%"
        params.extend([like, like])

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    return conn.execute(
        f"""
        SELECT * FROM generated_contents
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def build_summary_dataframe(rows: list[sqlite3.Row]) -> pd.DataFrame:
    """コンテンツ一覧を一覧性の高いテーブル表示用のDataFrameに変換する。"""

    columns = ["id", "platform", "content_type", "title", "status", "evaluation_score", "created_at"]
    if not rows:
        return pd.DataFrame(columns=columns)

    records = [
        {
            "id": row["id"],
            "platform": row["platform"],
            "content_type": row["content_type"],
            "title": row["title"] or "(タイトルなし)",
            "status": row["status"],
            "evaluation_score": row["evaluation_score"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    return pd.DataFrame.from_records(records, columns=columns)


def update_status(conn: sqlite3.Connection, content_id: int, status: ContentStatus) -> None:
    """採用/却下ボタンから呼ばれる、承認ステータスのみの更新（投稿は行わない）。"""

    conn.execute(
        "UPDATE generated_contents SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status.value, content_id),
    )
    conn.commit()


def list_video_files(videos_dir: Path) -> list[Path]:
    """content/videos/ 配下の.mp4を新しい順に列挙する。"""

    if not videos_dir.exists():
        return []
    return sorted(videos_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)


def parse_video_content_id(video_path: Path) -> Optional[int]:
    """`YYYY-MM-DD_tiktok_<id>.mp4` のようなファイル名からcontent_idを抽出する。"""

    parts = video_path.stem.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1])
    return None


# ---------------------------------------------------------------------------
# 設定解決（テスト時は st.session_state 経由でDBパス/動画ディレクトリを差し替える）
# ---------------------------------------------------------------------------


def _resolve_db_path() -> Optional[Path]:
    override = st.session_state.get("_dashboard_db_path")
    return Path(override) if override else None


def _resolve_videos_dir() -> Path:
    override = st.session_state.get("_dashboard_videos_dir")
    return Path(override) if override else VIDEOS_DIR


# ---------------------------------------------------------------------------
# UI描画
# ---------------------------------------------------------------------------


def _render_content_card(row: sqlite3.Row, key_prefix: str, db_path: Optional[Path]) -> None:
    with st.container(border=True):
        title = row["title"] or "(タイトルなし)"
        st.markdown(f"**{title}**")

        info_cols = st.columns(4)
        info_cols[0].caption(f"📱 {row['platform']}")
        info_cols[1].caption(f"📝 {row['content_type']}")
        score = row["evaluation_score"]
        info_cols[2].caption(f"⭐ {score:.0f}点" if score is not None else "⭐ 未評価")
        info_cols[3].caption(f"状態: {row['status']}")

        st.code(row["body"], language=None)

        if row["narration_text"]:
            with st.expander("🎙️ ナレーション読み上げテキスト"):
                st.write(row["narration_text"])

        if row["evaluation_reason"]:
            with st.expander("🤖 AI事前評価の詳細"):
                st.write(row["evaluation_reason"])

        btn_cols = st.columns([1, 1, 2])
        if btn_cols[0].button("✅ 採用する", key=f"{key_prefix}_approve_{row['id']}"):
            conn = get_connection(db_path)
            try:
                update_status(conn, row["id"], ContentStatus.APPROVED)
            finally:
                conn.close()
            st.toast(f"ID {row['id']} を採用しました。")
            st.rerun()

        if btn_cols[1].button("❌ 却下", key=f"{key_prefix}_reject_{row['id']}"):
            conn = get_connection(db_path)
            try:
                update_status(conn, row["id"], ContentStatus.REJECTED)
            finally:
                conn.close()
            st.toast(f"ID {row['id']} を却下しました。")
            st.rerun()

        btn_cols[2].caption(f"ID: {row['id']}")


def _render_today_tab(
    selected_date: Optional[str],
    selected_platform: Optional[str],
    selected_status: Optional[str],
    db_path: Optional[Path],
) -> None:
    if not selected_date:
        st.info("まだ生成されたコンテンツがありません。日次パイプラインの実行後にご確認ください。")
        return

    conn = get_connection(db_path)
    try:
        rows = fetch_contents(
            conn,
            target_date=selected_date,
            platform=selected_platform,
            status=selected_status,
        )
    finally:
        conn.close()

    st.subheader(f"{selected_date} のコンテンツ（{len(rows)}件）")
    if not rows:
        st.info("該当するコンテンツがありません。サイドバーのフィルターを変更してください。")
        return

    for row in rows:
        _render_content_card(row, key_prefix="today", db_path=db_path)


def _render_archive_tab(
    selected_platform: Optional[str],
    selected_status: Optional[str],
    db_path: Optional[Path],
) -> None:
    st.subheader("メディア別アーカイブ")
    search_query = st.text_input("🔍 キーワード検索（タイトル・本文）", key="archive_search")

    conn = get_connection(db_path)
    try:
        rows = fetch_contents(
            conn,
            platform=selected_platform,
            status=selected_status,
            search_query=search_query or None,
            limit=100,
        )
    finally:
        conn.close()

    st.caption(f"{len(rows)}件表示中（最大100件）")

    if rows:
        with st.expander("📋 一覧テーブル表示", expanded=False):
            st.dataframe(build_summary_dataframe(rows), width="stretch", hide_index=True)

    for row in rows:
        _render_content_card(row, key_prefix="archive", db_path=db_path)


def _render_video_tab(videos_dir: Path, db_path: Optional[Path]) -> None:
    st.subheader("生成動画プレビュー")

    videos = list_video_files(videos_dir)
    if not videos:
        st.info(
            "まだ生成された動画がありません。"
            "`python src/video/render_tiktok.py --content-id <ID>` で生成してください。"
        )
        return

    conn = get_connection(db_path)
    try:
        for video_path in videos:
            content_id = parse_video_content_id(video_path)
            title = None
            if content_id is not None:
                row = conn.execute(
                    "SELECT title FROM generated_contents WHERE id = ?", (content_id,)
                ).fetchone()
                title = row["title"] if row else None

            st.markdown(f"**{title or video_path.name}**")
            st.video(str(video_path))
            st.caption(f"{video_path.name}" + (f"（ID: {content_id}）" if content_id else ""))
    finally:
        conn.close()


def _render_feedback_tab(db_path: Optional[Path]) -> None:
    st.subheader("成果記録・フィードバック")
    st.caption(
        "実際にSNSへ投稿した後の再生数・いいね数などをDBへ記録します"
        "（このダッシュボードがSNSへ自動投稿することはありません）。"
    )

    with st.form("record_result_form"):
        content_id = st.number_input("コンテンツID (generated_contents.id)", min_value=1, step=1)
        status_label = st.selectbox(
            "ステータス", ["変更しない"] + [s.value.upper() for s in ContentStatus]
        )
        col1, col2 = st.columns(2)
        views = col1.number_input("再生数", min_value=0, step=1, value=None, placeholder="未入力なら更新しない")
        likes = col2.number_input("いいね数", min_value=0, step=1, value=None, placeholder="未入力なら更新しない")
        comments = col1.number_input("コメント数", min_value=0, step=1, value=None, placeholder="未入力なら更新しない")
        impressions = col2.number_input(
            "インプレッション数", min_value=0, step=1, value=None, placeholder="未入力なら更新しない"
        )
        submitted = st.form_submit_button("記録する")

    if submitted:
        status = None if status_label == "変更しない" else ContentStatus(status_label.lower())
        result = record_result(
            content_id=int(content_id),
            status=status,
            views=views,
            likes=likes,
            comments=comments,
            impressions=impressions,
            db_path=db_path,
        )
        if result["ok"]:
            st.success(f"ID {content_id} の実績を記録しました。")
        else:
            st.error(f"記録に失敗しました: {result['error']}")


def main() -> None:
    st.set_page_config(page_title="AI集客コンテンツ ダッシュボード", layout="wide")

    db_path = _resolve_db_path()
    videos_dir = _resolve_videos_dir()

    init_db(db_path)

    st.sidebar.header("フィルター")

    conn = get_connection(db_path)
    try:
        available_dates = fetch_available_dates(conn)
    finally:
        conn.close()

    if available_dates:
        selected_date = st.sidebar.selectbox("📅 日付", options=available_dates, index=0)
    else:
        st.sidebar.selectbox("📅 日付", options=["(データなし)"], index=0, disabled=True)
        selected_date = None

    selected_platform_label = st.sidebar.selectbox("📱 メディア", PLATFORM_OPTIONS, index=0)
    selected_platform = None if selected_platform_label == "すべて" else selected_platform_label

    selected_status_label = st.sidebar.selectbox(
        "🏷️ ステータス", list(STATUS_LABEL_TO_VALUE.keys()), index=0
    )
    selected_status = STATUS_LABEL_TO_VALUE[selected_status_label]

    st.title("🎤 AI集客コンテンツ ダッシュボード")

    tab1, tab2, tab3, tab4 = st.tabs(
        [
            "📅 今日のコンテンツ一覧",
            "🗂️ メディア別アーカイブ",
            "🎬 生成動画プレビュー",
            "📊 成果記録・フィードバック",
        ]
    )

    with tab1:
        _render_today_tab(selected_date, selected_platform, selected_status, db_path)
    with tab2:
        _render_archive_tab(selected_platform, selected_status, db_path)
    with tab3:
        _render_video_tab(videos_dir, db_path)
    with tab4:
        _render_feedback_tab(db_path)


# `streamlit run` / AppTest 経由の本物のスクリプト実行時のみ main() を呼ぶ。
# Streamlit の `__name__` は常に "__main__" になるとは限らないため
# (Streamlitランタイム自身がこのファイルへの `if __name__ == "__main__"` ガードを
# 使えない仕様と明言している)、代わりに実行コンテキストの有無で判定する。
# これにより、他コードからこのモジュールをただimportした場合
# （pytestが純粋関数をテストする場合等）に main() が誤って実行されるのを防ぐ。
if get_script_run_ctx() is not None:
    main()
