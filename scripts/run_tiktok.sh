#!/usr/bin/env bash
# TikTok動画生成の実行準備を自動化するバッチ。
#
# 1. ローカルのdata/database.sqlite（generated_contents等）がGitHub(origin)上の
#    最新コミットからズレていないか確認し、fast-forwardできる場合は取り込む
#    （日次パイプラインのGitHub Actions実行がコミットした最新の投稿案・DB更新を
#    取りこぼしたまま古いデータで動画生成してしまうのを防ぐ）。
# 2. VOICEVOX ENGINE（ナレーション音声合成）が起動しているか確認し、
#    未起動ならDockerコンテナを起動して準備が整うまで待つ。
# 3. venv/ があれば有効化する。
# 4. src/video/render_tiktok.py --content-id <ID> を実行する。
#
# 使い方:
#   ./scripts/run_tiktok.sh <content_id> [--target render|capcut] [その他 render_tiktok.py のオプション]
#
# 例:
#   ./scripts/run_tiktok.sh 5
#   ./scripts/run_tiktok.sh 5 --target capcut

set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "使い方: $0 <content_id> [--target render|capcut] [その他 render_tiktok.py のオプション]" >&2
    exit 1
fi

CONTENT_ID="$1"
shift

if ! [[ "$CONTENT_ID" =~ ^[0-9]+$ ]]; then
    echo "エラー: content_id は数値で指定してください（例: $0 5）" >&2
    exit 1
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "ローカルのdata/database.sqlite等がGitHub(origin)上の最新コミットと"
echo "ズレていないか確認します..."

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "警告: gitリポジトリではないため、DB同期チェックをスキップします。" >&2
elif ! CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" || [ "$CURRENT_BRANCH" = "HEAD" ]; then
    echo "警告: 現在のブランチを特定できないため、DB同期チェックをスキップします。" >&2
elif ! git fetch origin "$CURRENT_BRANCH" 2>/dev/null; then
    echo "警告: origin/${CURRENT_BRANCH} のfetchに失敗しました（オフライン等）。" >&2
    echo "       ローカルのDBのまま続行します。" >&2
else
    LOCAL_REV="$(git rev-parse HEAD)"
    REMOTE_REV="$(git rev-parse "origin/${CURRENT_BRANCH}")"
    if [ "$LOCAL_REV" = "$REMOTE_REV" ]; then
        echo "database.sqlite等はGitHub上の最新と一致しています。"
    elif git merge-base --is-ancestor HEAD "origin/${CURRENT_BRANCH}"; then
        echo "originに新しいコミットがあるため、git pull --ff-only で取り込みます..."
        if ! git pull --ff-only origin "$CURRENT_BRANCH"; then
            echo "エラー: git pull --ff-only に失敗しました" \
                 "（ローカルの変更と競合している可能性があります）。手動でご確認ください。" >&2
            exit 1
        fi
    else
        echo "警告: ローカルブランチがoriginと分岐しているため、自動pullをスキップします。" >&2
        echo "       手動で状況を確認してください（古いDBのまま続行します）。" >&2
    fi
fi

VOICEVOX_CONTAINER_NAME="voicevox_engine"
VOICEVOX_DOCKER_IMAGE="${VOICEVOX_DOCKER_IMAGE:-voicevox/voicevox_engine:cpu-latest}"

# VOICEVOX_ENGINE_URL: シェル環境変数 > .env > 既定値 の優先順で決定する
# （config/settings.py が実際に読む値と揃える）。
ENGINE_URL="${VOICEVOX_ENGINE_URL:-}"
if [ -z "$ENGINE_URL" ] && [ -f .env ]; then
    ENGINE_URL="$(grep -E '^VOICEVOX_ENGINE_URL=' .env | tail -n1 | cut -d= -f2- || true)"
fi
ENGINE_URL="${ENGINE_URL:-http://127.0.0.1:50021}"
ENGINE_URL="${ENGINE_URL%/}"

# docker -p で使うポート番号をURLから取り出す（既定50021）
ENGINE_PORT="$(echo "$ENGINE_URL" | sed -n 's#.*:\([0-9]\+\)$#\1#p')"
ENGINE_PORT="${ENGINE_PORT:-50021}"

engine_is_ready() {
    curl -sf -o /dev/null --max-time 3 "$ENGINE_URL/version"
}

echo "VOICEVOX ENGINE ($ENGINE_URL) の起動状態を確認します..."

if engine_is_ready; then
    echo "VOICEVOX ENGINEは既に起動しています。"
else
    if ! command -v docker >/dev/null 2>&1; then
        echo "エラー: VOICEVOX ENGINEに接続できず、dockerコマンドも見つかりません。" >&2
        echo "手動でVOICEVOX ENGINEを起動するか、Dockerをインストールしてください。" >&2
        echo "  docker run -d -p ${ENGINE_PORT}:50021 --name ${VOICEVOX_CONTAINER_NAME} ${VOICEVOX_DOCKER_IMAGE}" >&2
        exit 1
    fi

    if docker ps -a --filter "name=^${VOICEVOX_CONTAINER_NAME}$" --format '{{.Names}}' | grep -q "^${VOICEVOX_CONTAINER_NAME}$"; then
        echo "既存のコンテナ ${VOICEVOX_CONTAINER_NAME} を起動します..."
        docker start "$VOICEVOX_CONTAINER_NAME" >/dev/null
    else
        echo "コンテナ ${VOICEVOX_CONTAINER_NAME} を新規作成して起動します..."
        docker run -d \
            --name "$VOICEVOX_CONTAINER_NAME" \
            -p "${ENGINE_PORT}:50021" \
            "$VOICEVOX_DOCKER_IMAGE" >/dev/null
    fi

    echo -n "VOICEVOX ENGINEの起動を待っています"
    for _ in $(seq 1 30); do
        if engine_is_ready; then
            echo " OK"
            break
        fi
        echo -n "."
        sleep 2
    done

    if ! engine_is_ready; then
        echo ""
        echo "エラー: VOICEVOX ENGINEが既定時間内に起動しませんでした。" >&2
        echo "  docker logs ${VOICEVOX_CONTAINER_NAME}" >&2
        echo "でログを確認してください。" >&2
        exit 1
    fi
fi

if [ -f "venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
fi

echo "TikTok動画生成を開始します: content_id=${CONTENT_ID}"
python src/video/render_tiktok.py --content-id "$CONTENT_ID" "$@"
