"""
CapCut Desktop 下書き(Draft)自動生成モジュール。

`render_tiktok.py` が行う縦型動画のレンダリング(mp4書き出し)とは別の出力先として、
動画素材とナレーション音声をタイムラインに配置しただけの CapCut Desktop プロジェクト
(下書き = draft_content.json 一式)を生成する。字幕(テロップ)も
generator.split_narration_into_captions() で分割して配置するため、CapCut側では
BGM追加や画角の微調整など仕上げの編集から再開できる。

`pyCapCut` (https://github.com/GuanYixuan/pyCapCut) を用いて draft_content.json を
構築する。生成される下書きは動画素材・音声素材をこのマシン上の絶対パスで参照するため、
本モジュールは CapCut Desktop がインストールされた同一マシン上でのみ意味を持つ
(GitHub Actions等のCI環境からは利用しない)。

本システムはSNSへの自動投稿は一切行わない方針(CLAUDE.md)であり、本モジュールも
CapCutの起動・書き出し・投稿は一切行わない。下書きを置くところまでが役割であり、
続きは人間がCapCut Desktopを開いて編集・確認した上で行う。
"""

from __future__ import annotations

import logging
import os
import platform
import random
from datetime import date
from pathlib import Path
from typing import Any, Optional

import pycapcut as cc

from config.settings import settings
from src.video.generator import (
    CAPTION_STYLES,
    DEFAULT_CLIP_SEGMENT_SECONDS,
    caption_durations,
    caption_font_scale,
    caption_position_ratios,
    caption_rotation_degrees,
    caption_x_jitter_ratios,
    resolve_caption_chunks,
)

logger = logging.getLogger(__name__)

TARGET_SIZE = (1080, 1920)  # 9:16 縦型（render_tiktok.py の出力動画と揃える）
DEFAULT_FPS = 30
CAPTION_FONT_SIZE = 8.0
# CapCutのtransform_yは「画面下端(-0.8実測)〜画面上端」を単位半画面高で表す独自スケール。
# generator.py側(_build_caption_clips)のCAPTION_POSITION_RATIOS(0=最上部,1=最下部)は
# 符号反転するとほぼそのまま流用できる実測値のため、変換はその比率をそのまま使う
# （render_tiktok.py --target render / --target capcut で位置バリエーションを揃える）。
# generator.py側(_build_caption_clips)のstroke_widthはmoviepyのピクセル単位、
# CapCutのTextBorder.widthは別スケールの独自単位のため、両者の元々の既定値
# (stroke_width=3 / border width=40.0)の比率を基準に換算する。
_CAPCUT_BORDER_WIDTH_SCALE = 40.0 / 3
# 字幕出現時のポップな入退場アニメーション（テロップをそのまま置いただけに
# 見えないようにするための装飾）。持続時間はセグメント長を超えないよう
# add_animation() 側で自動的にクランプされる。
_CAPCUT_CAPTION_INTRO = cc.TextIntro.弹入
_CAPCUT_CAPTION_OUTRO = cc.TextOutro.弹出
_CAPCUT_CAPTION_ANIM_DURATION = 200_000  # 0.2秒（マイクロ秒）


def _hex_to_unit_rgb(hex_color: str) -> tuple[float, float, float]:
    """"#RRGGBB" 形式の16進カラーコードを pycapcut が使う 0.0〜1.0 のRGBタプルに変換する。"""

    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return (r, g, b)


def find_capcut_draft_root() -> Optional[Path]:
    """CapCut Desktopの下書き保存フォルダを自動検出する。

    環境変数 `CAPCUT_DRAFT_ROOT`（`config/settings.py` 経由）が設定されていれば
    最優先で使用する。CapCutのインストール先が既定と異なる場合や、このスクリプトを
    実行するマシンでの自動検出に対応していないOS（本開発機のLinux等）で使う場合の
    保険として用意している。

    未設定の場合はOSごとの既定パスを確認する:
      - Windows: `%LOCALAPPDATA%\\CapCut\\User Data\\Projects\\com.lveditor.draft`
      - macOS:   `~/Movies/CapCut/User Data/Projects/com.lveditor.draft`

    どちらも見つからなければ None を返す（呼び出し側でエラー扱いする）。
    """

    override = settings.capcut_draft_root
    if override:
        path = Path(override).expanduser()
        if path.is_dir():
            return path
        logger.warning("CAPCUT_DRAFT_ROOT=%s が存在しないため無視します。", override)

    system = platform.system()
    candidates: list[Path] = []

    if system == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(
                Path(local_app_data) / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
            )
    elif system == "Darwin":
        candidates.append(
            Path.home() / "Movies" / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
        )

    for candidate in candidates:
        if candidate.is_dir():
            return candidate

    logger.warning(
        "CapCut Desktopの下書き保存フォルダを自動検出できませんでした(OS=%s)。"
        "環境変数 CAPCUT_DRAFT_ROOT で明示的にパスを指定してください。",
        system,
    )
    return None


def _cover_scale(
    material_width: int, material_height: int, canvas_width: int, canvas_height: int
) -> float:
    """素材のアスペクト比に関わらず縦型キャンバス全体を覆うスケール倍率を返す。"""

    if material_width <= 0 or material_height <= 0:
        return 1.0
    return max(canvas_width / material_width, canvas_height / material_height)


def _build_video_segments(
    clip_paths: list[Path],
    target_duration_us: int,
    canvas_size: tuple[int, int],
    segment_seconds: float = DEFAULT_CLIP_SEGMENT_SECONDS,
) -> list["cc.VideoSegment"]:
    """`generator._build_visual_track` と同じ方針で、素材を3〜5秒ごとに切り替えながら
    target_duration_us を埋める CapCut 用セグメント列を作る。

    素材の合計尺が足りない場合は先頭のクリップから繰り返し使用する。
    """

    materials = [cc.VideoMaterial(str(p)) for p in clip_paths]
    segment_us = int(segment_seconds * cc.SEC)

    segments: list[cc.VideoSegment] = []
    elapsed = 0
    i = 0
    # 極端に短い/壊れたクリップが混じっていても無限ループしないよう上限を設ける
    max_iterations = max(1, target_duration_us // max(segment_us, 1) + len(materials) + 10)

    for _ in range(max_iterations):
        if elapsed >= target_duration_us:
            break
        material = materials[i % len(materials)]
        seg_len = min(segment_us, material.duration, target_duration_us - elapsed)
        if seg_len > 0:
            scale = _cover_scale(material.width, material.height, *canvas_size)
            segments.append(
                cc.VideoSegment(
                    material,
                    cc.Timerange(elapsed, seg_len),
                    source_timerange=cc.Timerange(0, seg_len),
                    clip_settings=cc.ClipSettings(scale_x=scale, scale_y=scale),
                )
            )
            elapsed += seg_len
        i += 1

    return segments


def _caption_durations_us(chunks: list[str], target_duration_us: int) -> list[int]:
    """`generator.caption_durations`（秒単位・文字数比例配分）をマイクロ秒に変換する。

    丸め誤差は最終チャンクに寄せ、合計が target_duration_us ちょうどになるようにする。
    """

    seconds = caption_durations(chunks, target_duration_us / 1_000_000)
    durations_us = [int(round(s * 1_000_000)) for s in seconds]
    durations_us[-1] += target_duration_us - sum(durations_us)
    return durations_us


def _build_caption_segments(
    chunks: list[str], target_duration_us: int
) -> list["cc.TextSegment"]:
    """字幕チャンク群に装飾を加えたテロップ用セグメント群を返す。

    `chunks` は表示順の字幕テキスト一覧（`resolve_caption_chunks` で台本(body)の
    セリフ・テロップ行、無ければナレーションを句読点分割したものを渡す想定）。
    `render_tiktok.py --target render`（generator.py側の`_build_caption_clips`）と
    見た目・振る舞いを揃えるため、同じ `CAPTION_STYLES` に加え、チャンクごとの
    配色・文字サイズ（`caption_font_scale`）・表示時間（`caption_durations`）・
    縦位置（`caption_position_ratios`）・横方向のズレ（`caption_x_jitter_ratios`）・
    傾き（`caption_rotation_degrees`）の変化ロジックを共有する。均等割りのテロップを
    そのまま置くのではなく、ポップな入退場アニメーションも添えて、人が手作りしたような
    テキストコンテンツとして装飾する。
    """

    if not chunks:
        return []

    durations_us = _caption_durations_us(chunks, target_duration_us)
    position_ratios = caption_position_ratios(len(chunks))
    rotation_degrees = caption_rotation_degrees(len(chunks))
    x_jitter_ratios = caption_x_jitter_ratios(len(chunks))

    segments: list[cc.TextSegment] = []
    start = 0
    for chunk, duration, position_ratio, rotation_cw, x_jitter_ratio in zip(
        chunks, durations_us, position_ratios, rotation_degrees, x_jitter_ratios
    ):
        caption_style = random.choice(CAPTION_STYLES)
        style = cc.TextStyle(
            size=CAPTION_FONT_SIZE * caption_font_scale(chunk),
            align=1,
            auto_wrapping=True,
            bold=True,
            color=_hex_to_unit_rgb(caption_style["color"]),
        )
        border = cc.TextBorder(
            color=_hex_to_unit_rgb(caption_style["stroke_color"]),
            width=caption_style["stroke_width"] * _CAPCUT_BORDER_WIDTH_SCALE,
        )
        # transform_xの単位は半画面幅、rotationは時計回り度数（generator.py側の
        # CAPTION_ROTATION_DEGREESと同じ向き）。
        clip_settings = cc.ClipSettings(
            transform_x=x_jitter_ratio * 2,
            transform_y=-position_ratio,
            rotation=rotation_cw,
        )
        segment = cc.TextSegment(
            chunk,
            cc.Timerange(start, duration),
            style=style,
            border=border,
            clip_settings=clip_settings,
        )
        segment.add_animation(_CAPCUT_CAPTION_INTRO, _CAPCUT_CAPTION_ANIM_DURATION)
        segment.add_animation(_CAPCUT_CAPTION_OUTRO, _CAPCUT_CAPTION_ANIM_DURATION)
        segments.append(segment)
        start += duration
    return segments


def _fail(content_id: int, message: str) -> dict[str, Any]:
    logger.error(message)
    return {"ok": False, "content_id": content_id, "draft_path": None, "draft_name": None, "error": message}


def build_capcut_draft(
    content_id: int,
    clip_paths: list[Path],
    narration_path: Path,
    narration_text: str,
    draft_root: Optional[Path] = None,
    canvas_size: tuple[int, int] = TARGET_SIZE,
    fps: int = DEFAULT_FPS,
    body: Optional[str] = None,
) -> dict[str, Any]:
    """動画素材・ナレーション音声・字幕を配置したCapCut Desktop下書きを生成する。

    `body`（台本全文）を渡すと、そこに書かれた「」/『』のセリフ・テロップ行を
    字幕として使う（`resolve_caption_chunks` 参照）。字幕はナレーションと同じ
    動画全体の尺に渡って配置されるため、前半にテキストが集中することを防ぐ。

    動画の尺はナレーション音声の長さにそのまま合わせる（ナレーションより後ろに
    B-rollだけの無音区間を作らない。動画の終わりとナレーションの終わりを揃えるため）。

    どのステップで失敗しても例外を送出せず、`ok: False` とエラー内容を含む
    辞書を返す。

    Returns:
        {"ok": bool, "content_id": int, "draft_path": Optional[str],
         "draft_name": Optional[str], "error": Optional[str]}
    """

    resolved_root = draft_root or find_capcut_draft_root()
    if resolved_root is None:
        return _fail(
            content_id,
            "CapCut Desktopの下書き保存フォルダが見つかりませんでした。"
            "環境変数 CAPCUT_DRAFT_ROOT で明示的にパスを指定してください。",
        )

    if not clip_paths:
        return _fail(content_id, "動画素材が1件もありません。")

    try:
        audio_material = cc.AudioMaterial(str(narration_path))
    except Exception:
        logger.exception("ナレーション音声の読み込みに失敗しました。content_id=%s", content_id)
        return _fail(content_id, "ナレーション音声の読み込みに失敗しました。")
    # 動画の尺（映像トラック）はナレーション音声の長さにそのまま合わせる。
    video_duration_us = audio_material.duration

    try:
        video_segments = _build_video_segments(clip_paths, video_duration_us, canvas_size)
    except Exception:
        logger.exception("動画素材の読み込みに失敗しました。content_id=%s", content_id)
        return _fail(content_id, "動画素材の読み込みに失敗しました。")

    if not video_segments:
        return _fail(content_id, "有効な動画セグメントを作成できませんでした。")

    draft_name = f"{date.today().isoformat()}_tiktok_{content_id}"

    try:
        draft_folder = cc.DraftFolder(str(resolved_root))
        script = draft_folder.create_draft(
            draft_name, canvas_size[0], canvas_size[1], fps, allow_replace=True
        )
        script.add_track(cc.TrackType.video)
        script.add_track(cc.TrackType.audio)
        script.add_track(cc.TrackType.text)

        for segment in video_segments:
            script.add_segment(segment)

        script.add_segment(cc.AudioSegment(audio_material, cc.Timerange(0, video_duration_us)))

        # 字幕は台本(body)のセリフ・テロップ行を漏れなく使い、動画全体に配置する
        # （narration_textだけでは取りこぼす行があるため。resolve_caption_chunks参照）。
        caption_chunks = resolve_caption_chunks(narration_text, body)
        for text_segment in _build_caption_segments(caption_chunks, video_duration_us):
            script.add_segment(text_segment)

        script.save()
    except Exception:
        logger.exception("CapCut下書きの生成に失敗しました。content_id=%s", content_id)
        return _fail(content_id, "CapCut下書きの生成に失敗しました。")

    draft_path = resolved_root / draft_name
    logger.info("CapCut下書きの生成が完了しました: %s", draft_path)
    return {
        "ok": True,
        "content_id": content_id,
        "draft_path": str(draft_path),
        "draft_name": draft_name,
        "error": None,
    }
