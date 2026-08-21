"""
TikTok動画合成モジュール。

`src/video/fetcher.py` が取得した動画素材、VOICEVOX ENGINEによるナレーション音声、
テロップ字幕、（任意の）BGMを組み合わせ、9:16(1080x1920)の縦型動画として出力する。

ナレーション音声合成にはローカルで起動したVOICEVOX ENGINE
（https://github.com/VOICEVOX/voicevox_engine 、既定 http://127.0.0.1:50021 ）を使う。
本モジュールの実行前にエンジンを起動しておくこと
（例: `docker run -p 50021:50021 voicevox/voicevox_engine:cpu-latest`）。
接続先・話者IDは `config/settings.py` の `VOICEVOX_ENGINE_URL` / `VOICEVOX_SPEAKER_ID` で変更できる。

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

import requests
from moviepy import (
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    TextClip,
    VideoFileClip,
    concatenate_videoclips,
)
from moviepy.audio.fx import AudioLoop
from moviepy.video.fx import FadeIn, FadeOut, Rotate

from config.settings import DATA_DIR, settings

logger = logging.getLogger(__name__)

TARGET_SIZE = (1080, 1920)  # 9:16 縦型
DEFAULT_CLIP_SEGMENT_SECONDS = 4.0  # 3〜5秒の中間値
BGM_DIR = DATA_DIR / "raw_assets" / "bgm"
BGM_VOLUME = 0.15
# 台本の全セリフを漏れなく会話口調で読み上げる方針(prompts/generation_prompt.md)により
# ナレーションが以前より長くなった上、このマシンはCPU2コアのみで負荷変動により
# audio_query単体でも数十秒〜要することが実測されたため、大きめに余裕を持たせる。
VOICEVOX_TIMEOUT_SECONDS = 120
CAPTION_FONT_SIZE = 64
CAPTION_MAX_CHARS = 18
CAPTION_MIN_SECONDS = 0.6  # 1チャンクあたりの最低表示秒数（短いチャンクが一瞬で消えるのを防ぐ）
CAPTION_FADE_SECONDS = 0.12  # 出現/消失時のふわっとしたフェード
# 縦位置のバリエーション（0=画面最上部, 1=画面最下部の比率）。TikTok自体のUI
# （上部のユーザー名/フォローボタン、下部のキャプション欄・操作アイコン）と
# 被りにくい範囲で、字幕ごとに位置を変えることで「ナレーションをそのまま
# 字幕化しただけ」に見えないようにする。CapCut下書き側(capcut_draft.py)でも
# 同じ比率を使い、見た目を揃える。
CAPTION_POSITION_RATIOS = [0.22, 0.38, 0.55, 0.70]
CAPTION_FONT_SCALE_JITTER = 0.08  # 文字サイズに±8%のランダムな揺らぎを加え、毎回同じ大きさに揃わないようにする
# 傾き（時計回り度数）のバリエーション。0を多めに入れて「たまに傾く」程度に留め、
# 人が手でテロップを貼ったような、揃いすぎない見た目にする。
CAPTION_ROTATION_DEGREES = [-6, -4, -3, 0, 0, 0, 3, 4, 6]
# 横方向のわずかなズレ（画面幅に対する比率）。TikTok自体の右側UI（いいね/コメント等の
# アイコン列）と被らないよう、右方向には広げすぎず気持ち左寄りにランダムに散らす。
CAPTION_X_JITTER_RATIO_RANGE = (-0.03, 0.01)

# 素材動画が1080pに満たない場合（yt-dlp側の取得事情等）でも9:16へのリサイズで
# 大きく引き伸ばされてボケて見えないよう、控えめなシャープネスと高めの画質で
# エンコードする。
VIDEO_ENCODE_PRESET = "slow"
VIDEO_ENCODE_CRF = "18"
VIDEO_UNSHARP_FILTER = "unsharp=5:5:0.8:5:5:0.4"

# ポップなテロップ配色パターン。動画ごとにランダムで1つ選ぶ（`_pick_caption_style`）ことで
# 生成のたびに見た目のバリエーションが出るようにする。色そのものの厳密な統一感より
# 「太字+太い縁取りでポップに見える」ことを優先し、あえて幅広い配色を用意している。
# `font_style` は fontconfig の style指定（find_japanese_font()参照）。
CAPTION_STYLES: list[dict] = [
    {"color": "#FFFFFF", "stroke_color": "#FF3D8E", "stroke_width": 10, "font_style": "Black"},
    {"color": "#FFF200", "stroke_color": "#111111", "stroke_width": 10, "font_style": "Black"},
    {"color": "#00E5FF", "stroke_color": "#1A1A5E", "stroke_width": 9, "font_style": "Bold"},
    {"color": "#FFFFFF", "stroke_color": "#7C3AED", "stroke_width": 10, "font_style": "Black"},
    {"color": "#FF9F1C", "stroke_color": "#FFFFFF", "stroke_width": 8, "font_style": "Bold"},
    {"color": "#111111", "stroke_color": "#7CFFCB", "stroke_width": 9, "font_style": "Black"},
    {"color": "#FFFFFF", "stroke_color": "#0B2545", "stroke_width": 10, "font_style": "Bold"},
    {"color": "#FF6B6B", "stroke_color": "#FFFFFF", "stroke_width": 8, "font_style": "Black"},
]

# ディストリビューションによってパスが異なるため、fc-match(fontconfig)を優先し、
# 見つからない場合の保険としてよくあるインストールパスも直接チェックする。
_COMMON_JAPANESE_FONT_PATHS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/chromeos/notocjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
]


def find_japanese_font(style: str = "Regular") -> Optional[str]:
    """日本語テロップ描画用のフォントファイルパスを探す。見つからなければ None。

    `style` は fontconfig の style指定（Regular/Bold/Black 等）。Regular以外は
    実行環境に該当ウェイトが無い場合があるため、見つからなければ呼び出し側で
    Regular にフォールバックすること。
    """

    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", f"Noto Sans CJK JP:style={style}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        path = result.stdout.strip()
        if path and Path(path).exists():
            return path
    except Exception:
        logger.debug("fc-matchによるフォント検索に失敗しました。", exc_info=True)

    if style == "Regular":
        for candidate in _COMMON_JAPANESE_FONT_PATHS:
            if Path(candidate).exists():
                return candidate

    logger.warning("日本語フォント(style=%s)が見つかりませんでした。", style)
    return None


def generate_narration_audio(
    text: str,
    dest_path: Path,
    speaker_id: Optional[int] = None,
    engine_url: Optional[str] = None,
) -> Optional[Path]:
    """VOICEVOX ENGINEでナレーション音声(wav)を合成する。

    事前にローカルでVOICEVOX ENGINEを起動しておく必要がある
    （既定 http://127.0.0.1:50021 、`config/settings.py` 経由で変更可）。
    """

    if not text or not text.strip():
        logger.error("ナレーションテキストが空です。")
        return None

    speaker_id = speaker_id if speaker_id is not None else settings.voicevox_speaker_id
    engine_url = (engine_url or settings.voicevox_engine_url).rstrip("/")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        query_resp = requests.post(
            f"{engine_url}/audio_query",
            params={"text": text, "speaker": speaker_id},
            timeout=VOICEVOX_TIMEOUT_SECONDS,
        )
        query_resp.raise_for_status()

        synth_resp = requests.post(
            f"{engine_url}/synthesis",
            params={"speaker": speaker_id},
            json=query_resp.json(),
            timeout=VOICEVOX_TIMEOUT_SECONDS * 2,
        )
        synth_resp.raise_for_status()
        dest_path.write_bytes(synth_resp.content)
    except Exception:
        logger.exception(
            "ナレーション音声の生成に失敗しました。VOICEVOX ENGINE(%s)が起動しているか確認してください。",
            engine_url,
        )
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


# 台本(body)はcontentごとに見出し・ラベルの表記ゆれが大きい
# （「テロップ」「文字オーバーレイ」「字幕」「ナレーション（TTS用）」等）が、
# 実際に画面表示/発話される文言は一貫して「」または『』で括られているため、
# この記法だけを頼りに抽出する。
_SCRIPT_QUOTE_PATTERN = re.compile(r"[「『]([^」』]+)[」』]")


def extract_script_lines(body: str) -> list[str]:
    """台本(body)から「」/『』で囲まれたセリフ・テロップ行を出現順に抽出する。

    narration_text はLLMがnarration_text/search_keywordsとして別途要約したもので、
    body中の一部の行（テロップ専用の行等）を取りこぼすことがある。本関数は
    body自体から直接、書かれた文言を漏れなく取り出す。
    """

    if not body:
        return []
    return [line.strip() for line in _SCRIPT_QUOTE_PATTERN.findall(body) if line.strip()]


def resolve_caption_chunks(narration_text: str, body: Optional[str] = None) -> list[str]:
    """字幕として表示するテキストチャンクを決定する。

    台本(body)に「」/『』で括られたセリフ・テロップ行があれば、それらを
    出現順そのまま字幕チャンクとして使う（ナレーション音声化されないテロップ専用の
    行も含め、台本に書かれた文言を漏れなく反映するため）。無ければナレーション
    テキストを句読点で分割したものにフォールバックする（narration_textのみで
    bodyが無い/空の場合の後方互換）。
    """

    script_lines = extract_script_lines(body) if body else []
    if script_lines:
        return script_lines
    return split_narration_into_captions(narration_text)


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


def _pick_caption_style() -> dict:
    """`CAPTION_STYLES` からポップな配色パターンを1つランダムに選ぶ。"""

    return random.choice(CAPTION_STYLES)


def caption_font_scale(chunk: str, jitter: float = CAPTION_FONT_SCALE_JITTER) -> float:
    """チャンクの文字数から強調度合い（フォントサイズ倍率）を決める。

    短い一言ほど「決め台詞」的に大きく、長い説明的なチャンクほど小さく表示することで、
    ナレーションをただ字幕化するのではなく、テキストコンテンツとして強弱をつける。
    さらに `jitter` 分のランダムな揺らぎを掛け、毎回きっちり同じ大きさに揃わない
    （人が手で作ったような）見た目にする。
    """

    length = len(chunk)
    if length <= 6:
        base = 1.35
    elif length <= 12:
        base = 1.05
    else:
        base = 0.85

    if jitter:
        base *= random.uniform(1 - jitter, 1 + jitter)
    return base


def caption_durations(chunks: list[str], target_duration: float) -> list[float]:
    """チャンクの文字数に応じて表示時間(秒)を配分する（均等割りにしない）。"""

    if not chunks:
        return []

    weights = [max(len(c), 1) for c in chunks]
    total_weight = sum(weights)
    durations = [target_duration * w / total_weight for w in weights]

    floor = min(CAPTION_MIN_SECONDS, target_duration / len(chunks))
    durations = [max(d, floor) for d in durations]

    scale = target_duration / sum(durations) if sum(durations) > 0 else 1.0
    return [d * scale for d in durations]


def caption_position_ratios(
    count: int, ratios: Optional[list[float]] = None
) -> list[float]:
    """同じ縦位置が連続しないよう、位置バリエーションをランダムに割り当てる。"""

    ratios = ratios or CAPTION_POSITION_RATIOS
    chosen: list[float] = []
    prev: Optional[float] = None
    for _ in range(count):
        candidates = [r for r in ratios if r != prev] or ratios
        pick = random.choice(candidates)
        chosen.append(pick)
        prev = pick
    return chosen


def caption_rotation_degrees(count: int, choices: Optional[list[float]] = None) -> list[float]:
    """字幕ごとの傾き（時計回り度数）をランダムに割り当てる（`CAPTION_ROTATION_DEGREES`）。"""

    choices = choices or CAPTION_ROTATION_DEGREES
    return [random.choice(choices) for _ in range(count)]


def caption_x_jitter_ratios(
    count: int, jitter_range: tuple[float, float] = CAPTION_X_JITTER_RATIO_RANGE
) -> list[float]:
    """字幕ごとの横方向のズレ（画面幅に対する比率）をランダムに割り当てる。"""

    low, high = jitter_range
    return [random.uniform(low, high) for _ in range(count)]


def _build_caption_clips(
    chunks: list[str],
    target_duration: float,
    font_path: Optional[str],
    style: Optional[dict] = None,
):
    """字幕チャンク群に装飾を加えたTextClip群を返す。

    `chunks` は表示順の字幕テキスト一覧（`resolve_caption_chunks` で台本(body)の
    セリフ・テロップ行、無ければナレーションを句読点分割したものを渡す想定）。
    `target_duration` の全体に渡って均等割りの位置・時間・文字サイズ・配色で
    そのまま字幕化するのではなく、チャンクごとに配色（`_pick_caption_style`）・
    文字サイズ（`caption_font_scale`）・表示時間（`caption_durations`）・
    縦位置（`caption_position_ratios`）・横方向のズレ（`caption_x_jitter_ratios`）・
    傾き（`caption_rotation_degrees`）をランダムに変化させ、フェードイン/アウトを
    添えることで、人が手作りしたようなテキストコンテンツとして装飾する。

    `style` を明示的に渡した場合はそれを全チャンク共通で使う（テスト等での固定用途）。
    省略時はチャンクごとに `CAPTION_STYLES` からランダムに選び直す。

    `font_path` は日本語フォントが1つも見つからない場合の空実行判定にのみ使い、
    実際の描画には選ばれた配色の `font_style` に対応するウェイトのフォントを探して使う
    （見つからなければ `font_path` にフォールバック）。
    """

    if not font_path or not chunks:
        return []

    durations = caption_durations(chunks, target_duration)
    position_ratios = caption_position_ratios(len(chunks))
    rotation_degrees = caption_rotation_degrees(len(chunks))
    x_jitter_ratios = caption_x_jitter_ratios(len(chunks))

    caption_clips = []
    start = 0.0
    for chunk, duration, position_ratio, rotation_cw, x_jitter_ratio in zip(
        chunks, durations, position_ratios, rotation_degrees, x_jitter_ratios
    ):
        chunk_style = style or _pick_caption_style()
        styled_font_path = find_japanese_font(chunk_style["font_style"]) or font_path

        font_size = int(round(CAPTION_FONT_SIZE * caption_font_scale(chunk)))
        stroke_width = chunk_style["stroke_width"]
        # moviepy(Pillow)のTextClipはmethod="caption"+太いstroke_width使用時、
        # 最終行のストローク分の余白を実際より小さく見積もることがあり、
        # 字幕の下側がキャンバスの外に切れて見切れることがある。下マージンを
        # 明示的に確保して回避する（テキスト自体の縦位置は変わらない）。
        bottom_padding = int(font_size * 0.35 + stroke_width * 2)
        text_clip = (
            TextClip(
                font=styled_font_path,
                text=chunk,
                font_size=font_size,
                color=chunk_style["color"],
                stroke_color=chunk_style["stroke_color"],
                stroke_width=stroke_width,
                size=(int(TARGET_SIZE[0] * 0.9), None),
                margin=(0, 0, 0, bottom_padding),
                method="caption",
                text_align="center",
            )
            .with_duration(duration)
            .with_start(start)
        )
        fade = min(CAPTION_FADE_SECONDS, duration / 3)
        if text_clip.mask is not None and fade > 0:
            text_clip = text_clip.with_mask(
                text_clip.mask.with_effects([FadeIn(fade), FadeOut(fade)])
            )

        # 傾ける前の（意図した）表示中心を求めておき、回転でバウンディングボックスが
        # 膨らんでも見た目の中心がずれないよう、回転後のサイズから位置を計算し直す。
        w0, h0 = text_clip.size
        x_jitter_px = x_jitter_ratio * TARGET_SIZE[0]
        center_x = TARGET_SIZE[0] / 2 + x_jitter_px
        center_y = int(TARGET_SIZE[1] * position_ratio) + h0 / 2

        if rotation_cw:
            # CapCut側(capcut_draft.py)のclip_settings.rotationは時計回りが正だが、
            # moviepyのRotateは反時計回りが正のため符号を反転して見た目を揃える。
            text_clip = text_clip.with_effects([Rotate(-rotation_cw)])
        w1, h1 = text_clip.size
        text_clip = text_clip.with_position((center_x - w1 / 2, center_y - h1 / 2))

        caption_clips.append(text_clip)
        start += duration
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
    body: Optional[str] = None,
) -> Optional[Path]:
    """動画素材・ナレーション・字幕・BGMを合成し、TikTok向け縦型動画を書き出す。

    `body`（台本全文）を渡すと、そこに書かれた「」/『』のセリフ・テロップ行を
    字幕として使う（`resolve_caption_chunks` 参照）。ナレーション音声化されない
    テロップ専用の行も含めて台本の文言を漏れなく反映し、かつ字幕はナレーションと
    同じ動画全体の尺に渡って配置されるため、前半にテキストが集中することを防ぐ。

    動画の尺はナレーション音声の長さ（`narration_text` の内容・
    prompts/generation_prompt.md の締めの一言の有無で決まる）にそのまま合わせる。
    ナレーションより後ろにB-rollだけの無音区間を作らない（動画の終わりと
    ナレーションの終わりを揃えるため）。

    いかなる異常時も例外を送出せず、ログを出力した上で `None` を返す。
    """

    if not clip_paths:
        logger.error("動画素材が1件もありません。")
        return None

    bgm_dir = bgm_dir or BGM_DIR
    work_dir = work_dir or output_path.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    narration_path = work_dir / f"_narration_{output_path.stem}.wav"
    narration_path = generate_narration_audio(narration_text, narration_path)
    if narration_path is None:
        return None

    opened_video_clips: list = []
    opened_audio_clips: list = []
    try:
        narration_audio = AudioFileClip(str(narration_path))
        opened_audio_clips.append(narration_audio)
        # 動画の尺はナレーション音声の長さにそのまま合わせる（動画の終わりと
        # ナレーションの終わりを揃えるため、ここより後ろにB-rollだけの
        # 無音区間を追加しない）。
        target_duration = narration_audio.duration

        visual, opened_video_clips = _build_visual_track(clip_paths, target_duration)

        font_path = find_japanese_font()
        # 字幕は台本(body)のセリフ・テロップ行を漏れなく使い、動画全体に配置する
        # （narration_textだけでは取りこぼす行があるため。resolve_caption_chunks参照）。
        caption_chunks = resolve_caption_chunks(narration_text, body)
        captions = _build_caption_clips(caption_chunks, target_duration, font_path)

        audio_tracks = [narration_audio]
        bgm_path = _select_bgm_file(bgm_dir)
        if bgm_path is not None:
            bgm_audio = AudioFileClip(str(bgm_path))
            opened_audio_clips.append(bgm_audio)
            bgm_audio = bgm_audio.with_effects([AudioLoop(duration=target_duration)])
            bgm_audio = bgm_audio.with_volume_scaled(BGM_VOLUME)
            audio_tracks.append(bgm_audio)

        final_audio = CompositeAudioClip(audio_tracks).with_duration(target_duration)
        final_video = CompositeVideoClip([visual, *captions], size=TARGET_SIZE)
        final_video = final_video.with_duration(target_duration).with_audio(final_audio)

        final_video.write_videofile(
            str(output_path),
            fps=30,
            codec="libx264",
            preset=VIDEO_ENCODE_PRESET,
            audio_codec="aac",
            ffmpeg_params=["-crf", VIDEO_ENCODE_CRF, "-vf", VIDEO_UNSHARP_FILTER],
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
