import wave
from pathlib import Path

import pytest
from moviepy import ColorClip


def _write_silent_wav(path: Path, duration_seconds: float, framerate: int = 44100) -> None:
    """テスト用の無音WAVファイルを作る（moviepyのAudioClip経由の書き出しは、
    procedurally生成した無音音声だと長さが正しく書き出されない不具合があるため、
    標準ライブラリの wave モジュールで直接書き出す）。"""

    n_frames = int(framerate * duration_seconds)
    with wave.open(str(path), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(framerate)
        f.writeframes(b"\x00\x00" * n_frames)

from src.video.generator import (
    TARGET_SIZE,
    _build_visual_track,
    _resize_and_crop_to_vertical,
    _select_bgm_file,
    compose_tiktok_video,
    find_japanese_font,
    generate_narration_audio,
    split_narration_into_captions,
)


# ---- split_narration_into_captions (純粋なテキスト処理) ----


def test_split_narration_into_captions_splits_by_punctuation():
    text = "今日はいい天気です。散歩に行きましょう！"
    chunks = split_narration_into_captions(text, max_chars=30)
    assert chunks == ["今日はいい天気です。", "散歩に行きましょう！"]


def test_split_narration_into_captions_splits_long_sentence():
    text = "あ" * 50
    chunks = split_narration_into_captions(text, max_chars=18)
    assert all(len(c) <= 18 for c in chunks)
    assert "".join(chunks) == text


def test_split_narration_into_captions_empty_text_returns_empty():
    assert split_narration_into_captions("") == []
    assert split_narration_into_captions("   ") == []


# ---- generate_narration_audio ----


def test_generate_narration_audio_empty_text_returns_none(tmp_path):
    result = generate_narration_audio("", tmp_path / "out.mp3")
    assert result is None


def test_generate_narration_audio_success(monkeypatch, tmp_path):
    class FakeGTTS:
        def __init__(self, text, lang):
            self.text = text
            self.lang = lang

        def save(self, path):
            Path(path).write_bytes(b"fake-mp3-data")

    monkeypatch.setattr("src.video.generator.gTTS", FakeGTTS)

    dest = tmp_path / "narration.mp3"
    result = generate_narration_audio("テストです", dest)
    assert result == dest
    assert dest.exists()


def test_generate_narration_audio_handles_exception(monkeypatch, tmp_path):
    class FailingGTTS:
        def __init__(self, text, lang):
            pass

        def save(self, path):
            raise RuntimeError("network error")

    monkeypatch.setattr("src.video.generator.gTTS", FailingGTTS)
    result = generate_narration_audio("テストです", tmp_path / "out.mp3")
    assert result is None


# ---- find_japanese_font ----


def test_find_japanese_font_returns_existing_path_or_none():
    # 環境依存のため厳密な値ではなく型と存在性のみ検証する
    font_path = find_japanese_font()
    assert font_path is None or Path(font_path).exists()


# ---- 映像合成 (合成ColorClipを実素材の代わりに使用) ----


@pytest.fixture()
def synthetic_clip_paths(tmp_path):
    """640x360・2秒の単色クリップを2本作り、ダウンロード済み素材の代わりに使う。"""
    paths = []
    for i, color in enumerate([(255, 0, 0), (0, 255, 0)]):
        clip = ColorClip(size=(640, 360), color=color, duration=2)
        clip = clip.with_fps(24)
        path = tmp_path / f"clip{i}.mp4"
        clip.write_videofile(str(path), fps=24, codec="libx264", audio=False, logger=None)
        clip.close()
        paths.append(path)
    return paths


def test_resize_and_crop_to_vertical_produces_target_size(synthetic_clip_paths):
    from moviepy import VideoFileClip

    clip = VideoFileClip(str(synthetic_clip_paths[0]))
    result = _resize_and_crop_to_vertical(clip)
    assert (result.w, result.h) == TARGET_SIZE
    clip.close()


def test_build_visual_track_fills_target_duration_by_looping(synthetic_clip_paths):
    # 素材2本(各2秒=計4秒)しかないが、target_duration=7秒を要求 → ループして埋まること
    visual, opened_clips = _build_visual_track(synthetic_clip_paths, target_duration=7.0, segment_seconds=3.0)
    try:
        assert visual.duration == pytest.approx(7.0, abs=0.1)
        assert (visual.w, visual.h) == TARGET_SIZE
    finally:
        visual.close()
        for c in opened_clips:
            c.close()


# ---- BGM選択 ----


def test_select_bgm_file_returns_none_when_dir_missing(tmp_path):
    assert _select_bgm_file(tmp_path / "does_not_exist") is None


def test_select_bgm_file_returns_none_when_empty(tmp_path):
    bgm_dir = tmp_path / "bgm"
    bgm_dir.mkdir()
    assert _select_bgm_file(bgm_dir) is None


def test_select_bgm_file_picks_from_available_files(tmp_path):
    bgm_dir = tmp_path / "bgm"
    bgm_dir.mkdir()
    (bgm_dir / "a.mp3").write_bytes(b"fake")
    (bgm_dir / "b.wav").write_bytes(b"fake")
    result = _select_bgm_file(bgm_dir)
    assert result is not None
    assert result.name in ("a.mp3", "b.wav")


# ---- compose_tiktok_video (End-to-End, gTTSのみモック) ----


def test_compose_tiktok_video_no_clips_returns_none(monkeypatch, tmp_path):
    result = compose_tiktok_video([], "テスト", tmp_path / "out.mp4")
    assert result is None


def test_compose_tiktok_video_end_to_end(monkeypatch, synthetic_clip_paths, tmp_path):
    class FakeGTTS:
        def __init__(self, text, lang):
            pass

        def save(self, path):
            # 2秒の無音音声を生成して実ファイルとして書き出す（ネットワーク不要）
            _write_silent_wav(Path(path), duration_seconds=2.0)

    monkeypatch.setattr("src.video.generator.gTTS", FakeGTTS)

    output_path = tmp_path / "output" / "test_tiktok.mp4"
    result = compose_tiktok_video(
        synthetic_clip_paths,
        "これはテストのナレーションです。うまく合成されるでしょうか？",
        output_path,
        bgm_dir=tmp_path / "no_bgm_here",
        work_dir=tmp_path / "work",
    )

    assert result == output_path
    assert output_path.exists()
    assert output_path.stat().st_size > 0

    from moviepy import VideoFileClip

    produced = VideoFileClip(str(output_path))
    try:
        assert (produced.w, produced.h) == TARGET_SIZE
        assert produced.duration == pytest.approx(2.0, abs=0.3)
    finally:
        produced.close()


def test_compose_tiktok_video_narration_failure_returns_none(monkeypatch, synthetic_clip_paths, tmp_path):
    class FailingGTTS:
        def __init__(self, text, lang):
            pass

        def save(self, path):
            raise RuntimeError("network error")

    monkeypatch.setattr("src.video.generator.gTTS", FailingGTTS)

    result = compose_tiktok_video(
        synthetic_clip_paths, "テスト", tmp_path / "out.mp4", work_dir=tmp_path / "work"
    )
    assert result is None
