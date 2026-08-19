"""
TikTok動画合成モジュール。

`src/video/fetcher.py` が取得した動画素材、gTTSによるナレーション音声、
テロップ字幕、（任意の）BGMを組み合わせ、9:16(1080x1920)の縦型動画として出力する。

BGMは著作権リスクを避けるため、ローカルの `data/raw_assets/bgm/` に
ユーザー自身が用意したロイヤリティフリー素材のみを対象とする
（インターネットからの自動取得は行わない。素材が無ければBGM無しで出力する）。

いかなる異常時も例外を送出せず、ログを出力した上で `None` を返す。
"""

from __future__ import annotations

import logging
import random
import re
import subprocess
from pathlib import Path
from typing import Optional

from gtts import gTTS
from moviepy import (
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    TextClip,
    VideoFileClip,
    concatenate_videoclips,
)
from moviepy.audio.fx import AudioLoop

from config.settings import DATA_DIR

logger = logging.getLogger(__name__)

TARGET_SIZE = (1080, 1920)  # 9:16 縦型
DEFAULT_CLIP_SEGMENT_SECONDS = 4.0  # 3〜5秒の中間値
BGM_DIR = DATA_DIR / "raw_assets" / "bgm"
BGM_VOLUME = 0.15
NARRATION_LANG = "ja"
CAPTION_FONT_SIZE = 64
CAPTION_MAX_CHARS = 18

# ディストリビューションによってパスが異なるため、fc-match(fontconfig)を優先し、
# 見つからない場合の保険としてよくあるインストールパスも直接チェックする。
_COMMON_JAPANESE_FONT_PATHS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/chromeos/notocjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
]


def find_japanese_font() -> Optional[str]:
    """日本語テロップ描画用のフォントファイルパスを探す。見つからなければ None。"""

    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", "Noto Sans CJK JP"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        path = result.stdout.strip()
        if path and Path(path).exists():
            return path
    except Exception:
        logger.debug("fc-matchによるフォント検索に失敗しました。", exc_info=True)

    for candidate in _COMMON_JAPANESE_FONT_PATHS:
        if Path(candidate).exists():
            return candidate

    logger.warning("日本語フォントが見つかりませんでした。テロップなしで動画を生成します。")
    return None


def generate_narration_audio(
    text: str, dest_path: Path, lang: str = NARRATION_LANG
) -> Optional[Path]:
    """gTTSでナレーション音声を合成する。"""

    if not text or not text.strip():
        logger.error("ナレーションテキストが空です。")
        return None

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        gTTS(text=text, lang=lang).save(str(dest_path))
    except Exception:
        logger.exception("ナレーション音声の生成に失敗しました。")
        return None

    return dest_path if dest_path.exists() else None


def split_narration_into_captions(text: str, max_chars: int = CAPTION_MAX_CHARS) -> list[str]:
    """ナレーションテキストを字幕表示用の短いチャンクに分割する。

    句読点（。！？）で区切った上で、なお長い場合は max_chars 程度でさらに分割する。
    """

    sentences = [s for s in re.split(r"(?<=[。！？!?])", text) if s.strip()]
    if not sentences:
        sentences = [text] if text.strip() else []

    chunks: list[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) <= max_chars:
            chunks.append(sentence)
            continue
        for i in range(0, len(sentence), max_chars):
            piece = sentence[i : i + max_chars]
            if piece:
                chunks.append(piece)

    return chunks


def _resize_and_crop_to_vertical(clip):
    """任意サイズの動画クリップを 1080x1920 (9:16) にリサイズ・中央クロップする。"""

    target_w, target_h = TARGET_SIZE
    target_ratio = target_w / target_h
    clip_ratio = clip.w / clip.h

    resized = (
        clip.resized(height=target_h) if clip_ratio > target_ratio else clip.resized(width=target_w)
    )
    return resized.cropped(
        x_center=resized.w / 2, y_center=resized.h / 2, width=target_w, height=target_h
    )


def _build_visual_track(
    clip_paths: list[Path],
    target_duration: float,
    segment_seconds: float = DEFAULT_CLIP_SEGMENT_SECONDS,
):
    """ダウンロード済みクリップを3〜5秒ごとに切り替えながら target_duration 分つなぎ合わせる。

    素材の合計尺が足りない場合は先頭のクリップから繰り返し使用する。

    Returns:
        (連結後の映像クリップ, 開いたVideoFileClipのリスト)
        呼び出し元は書き出し完了後に開いたクリップを必ずcloseすること。
    """

    opened_clips = [VideoFileClip(str(p)) for p in clip_paths]
    segments = []
    elapsed = 0.0
    i = 0
    # 極端に短い/壊れたクリップが混じっていても無限ループしないよう上限を設ける
    max_iterations = max(1, int(target_duration / max(segment_seconds, 0.5)) + len(opened_clips) + 10)

    for _ in range(max_iterations):
        if elapsed >= target_duration:
            break
        source = opened_clips[i % len(opened_clips)]
        seg_len = min(segment_seconds, source.duration, target_duration - elapsed)
        if seg_len > 0:
            segment = source.subclipped(0, seg_len)
            segments.append(_resize_and_crop_to_vertical(segment))
            elapsed += seg_len
        i += 1

    if not segments:
        raise ValueError("有効な映像セグメントを作成できませんでした。")

    visual = concatenate_videoclips(segments, method="compose")
    return visual, opened_clips


def _build_caption_clips(narration_text: str, target_duration: float, font_path: Optional[str]):
    """ナレーションを字幕チャンクに分割し、均等に時間配置したTextClip群を返す。"""

    if not font_path:
        return []

    chunks = split_narration_into_captions(narration_text)
    if not chunks:
        return []

    per_chunk = target_duration / len(chunks)
    caption_clips = []
    for idx, chunk in enumerate(chunks):
        text_clip = (
            TextClip(
                font=font_path,
                text=chunk,
                font_size=CAPTION_FONT_SIZE,
                color="white",
                stroke_color="black",
                stroke_width=3,
                size=(int(TARGET_SIZE[0] * 0.9), None),
                method="caption",
                text_align="center",
            )
            .with_duration(per_chunk)
            .with_start(idx * per_chunk)
            .with_position(("center", int(TARGET_SIZE[1] * 0.75)))
        )
        caption_clips.append(text_clip)
    return caption_clips


def _select_bgm_file(bgm_dir: Path) -> Optional[Path]:
    """ローカルのBGM素材ディレクトリからランダムに1件選ぶ（無ければNone）。"""

    if not bgm_dir.exists():
        return None
    candidates = (
        sorted(bgm_dir.glob("*.mp3"))
        + sorted(bgm_dir.glob("*.wav"))
        + sorted(bgm_dir.glob("*.m4a"))
    )
    return random.choice(candidates) if candidates else None


def compose_tiktok_video(
    clip_paths: list[Path],
    narration_text: str,
    output_path: Path,
    bgm_dir: Optional[Path] = None,
    work_dir: Optional[Path] = None,
) -> Optional[Path]:
    """動画素材・ナレーション・字幕・BGMを合成し、TikTok向け縦型動画を書き出す。

    いかなる異常時も例外を送出せず、ログを出力した上で `None` を返す。
    """

    if not clip_paths:
        logger.error("動画素材が1件もありません。")
        return None

    bgm_dir = bgm_dir or BGM_DIR
    work_dir = work_dir or output_path.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    narration_path = work_dir / f"_narration_{output_path.stem}.mp3"
    narration_path = generate_narration_audio(narration_text, narration_path)
    if narration_path is None:
        return None

    opened_video_clips: list = []
    opened_audio_clips: list = []
    try:
        narration_audio = AudioFileClip(str(narration_path))
        opened_audio_clips.append(narration_audio)
        target_duration = narration_audio.duration

        visual, opened_video_clips = _build_visual_track(clip_paths, target_duration)

        font_path = find_japanese_font()
        captions = _build_caption_clips(narration_text, target_duration, font_path)

        audio_tracks = [narration_audio]
        bgm_path = _select_bgm_file(bgm_dir)
        if bgm_path is not None:
            bgm_audio = AudioFileClip(str(bgm_path))
            opened_audio_clips.append(bgm_audio)
            bgm_audio = bgm_audio.with_effects([AudioLoop(duration=target_duration)])
            bgm_audio = bgm_audio.with_volume_scaled(BGM_VOLUME)
            audio_tracks.append(bgm_audio)

        final_audio = CompositeAudioClip(audio_tracks) if len(audio_tracks) > 1 else narration_audio
        final_video = CompositeVideoClip([visual, *captions], size=TARGET_SIZE)
        final_video = final_video.with_duration(target_duration).with_audio(final_audio)

        final_video.write_videofile(
            str(output_path),
            fps=30,
            codec="libx264",
            audio_codec="aac",
            logger=None,
        )
    except Exception:
        logger.exception("動画合成に失敗しました。")
        return None
    finally:
        for c in opened_video_clips:
            try:
                c.close()
            except Exception:
                pass
        for c in opened_audio_clips:
            try:
                c.close()
            except Exception:
                pass
        try:
            narration_path.unlink(missing_ok=True)
        except OSError:
            pass

    return output_path if output_path.exists() else None
