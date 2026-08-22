#!/usr/bin/env python3
"""
`data/database.sqlite` 用のgitマージドライバ。

sqlite3ファイルはバイナリのため、通常のgitマージ（テキストの3-way diff）では
必ずコンフリクトになる。このDBのテーブル（contents/ai_analyses/generated_contents）
はいずれも単純な主キー(id)を持つ構造なので、行単位で3-wayマージすることで
「ローカルでの手動編集（例: narration_textの修正）」と「originでの日次パイプライン
実行結果（新規行の追加）」を両方取りこぼさずに自動統合する。

git用のマージドライバとして .gitattributes 経由で呼び出される想定:
    python3 scripts/git_merge_sqlite.py %O %A %B
（%O=共通祖先, %A=ローカル側。結果はこのファイルに書き戻す, %B=マージ対象=origin側）

行ごとの解決規則:
- 片方にしか無いid（新規行）は無条件で採用する。
- 両側にあり片方だけ変更されていれば、変更された側を採用する。
- 両側で同じ内容に変更されていれば、それを採用する。
- 両側で異なる内容に変更されていた場合（真の衝突）は、ローカル(%A)側の内容を
  優先しつつ、標準エラー出力に警告を出す（人間が明示的に編集した内容を
  自動処理で静かに消さないため）。
"""
from __future__ import annotations

import sqlite3
import sys
from typing import Any


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]


def _load_rows(conn: sqlite3.Connection, table: str) -> tuple[list[str], dict[Any, tuple]]:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    rows = {
        row[0]: row
        for row in conn.execute(f"SELECT {','.join(cols)} FROM {table} ORDER BY {cols[0]}")
    }
    return cols, rows


def _merge_rows(
    table: str,
    base_rows: dict[Any, tuple],
    ours_rows: dict[Any, tuple],
    theirs_rows: dict[Any, tuple],
) -> dict[Any, tuple]:
    merged: dict[Any, tuple] = {}
    for rid in set(ours_rows) | set(theirs_rows):
        ours_row = ours_rows.get(rid)
        theirs_row = theirs_rows.get(rid)

        if ours_row is None:
            merged[rid] = theirs_row  # originでのみ追加された新規行
            continue
        if theirs_row is None:
            merged[rid] = ours_row  # ローカルでのみ追加された新規行
            continue
        if ours_row == theirs_row:
            merged[rid] = ours_row
            continue

        base_row = base_rows.get(rid)
        if base_row == ours_row:
            merged[rid] = theirs_row  # ローカル側は無変更→origin側の変更を採用
        elif base_row == theirs_row:
            merged[rid] = ours_row  # origin側は無変更→ローカル側の変更を採用
        else:
            print(
                f"警告: {table}.id={rid!r} が両側で異なる内容に変更されていたため、"
                "ローカル側の内容を優先しました。",
                file=sys.stderr,
            )
            merged[rid] = ours_row
    return merged


def _sync_sequence(conn: sqlite3.Connection, table: str, max_id: int) -> None:
    cur = conn.execute("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (max_id, table))
    if cur.rowcount == 0:
        conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, max_id))


def main(base_path: str, ours_path: str, theirs_path: str) -> int:
    base_conn = sqlite3.connect(base_path)
    theirs_conn = sqlite3.connect(theirs_path)
    ours_conn = sqlite3.connect(ours_path)
    ours_conn.execute("PRAGMA foreign_keys=OFF")

    ours_tables = set(_table_names(ours_conn))
    theirs_only = set(_table_names(theirs_conn)) - ours_tables
    if theirs_only:
        print(
            f"警告: origin側にのみ存在するテーブル {sorted(theirs_only)} は"
            "自動マージ対象外です。手動でご確認ください。",
            file=sys.stderr,
        )

    has_sequence_table = "sqlite_sequence" in (
        r[0] for r in ours_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    )

    for table in sorted(ours_tables):
        cols, ours_rows = _load_rows(ours_conn, table)
        _, base_rows = _load_rows(base_conn, table) if _table_exists(base_conn, table) else (cols, {})
        _, theirs_rows = _load_rows(theirs_conn, table) if _table_exists(theirs_conn, table) else (cols, {})

        merged = _merge_rows(table, base_rows, ours_rows, theirs_rows)

        ours_conn.execute(f"DELETE FROM {table}")
        if merged:
            placeholders = ",".join("?" for _ in cols)
            ours_conn.executemany(
                f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
                list(merged.values()),
            )

        if has_sequence_table:
            numeric_ids = [rid for rid in merged if isinstance(rid, int)]
            if numeric_ids:
                _sync_sequence(ours_conn, table, max(numeric_ids))

    ours_conn.commit()
    ours_conn.close()
    theirs_conn.close()
    base_conn.close()
    return 0


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone() is not None


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("使い方: git_merge_sqlite.py <base> <ours> <theirs>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(*sys.argv[1:4]))
