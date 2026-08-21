import json
import wave
from pathlib import Path

import pytest
from moviepy import ColorClip

from src.video.capcut_draft import build_capcut_draft, find_capcut_draft_root


def _write_silent_wav(path: Path, duration_seconds: float, framerate: int = 44100) -> None:
    n_frames = int(framerate * duration_seconds)
    with wave.open(str(path), "w") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(framerate)
        f.writeframes(b"\x00\x00" * n_frames)


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


@pytest.fixture()
def narration_wav(tmp_path):
    path = tmp_path / "narration.wav"
    _write_silent_wav(path, duration_seconds=5.0)
    return path


# ---- find_capcut_draft_root ----


def test_find_capcut_draft_root_uses_env_override(monkeypatch, tmp_path):
    monkeypatch.setattr("src.video.capcut_draft.settings.capcut_draft_root", str(tmp_path))
    assert find_capcut_draft_root() == tmp_path


def test_find_capcut_draft_root_ignores_nonexistent_override(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "src.video.capcut_draft.settings.capcut_draft_root", str(tmp_path / "does_not_exist")
    )
    monkeypatch.setattr("src.video.capcut_draft.platform.system", lambda: "Linux")
    assert find_capcut_draft_root() is None


def test_find_capcut_draft_root_windows_default(monkeypatch, tmp_path):
    monkeypatch.setattr("src.video.capcut_draft.settings.capcut_draft_root", "")
    monkeypatch.setattr("src.video.capcut_draft.platform.system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    expected = tmp_path / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
    expected.mkdir(parents=True)

    assert find_capcut_draft_root() == expected


def test_find_capcut_draft_root_macos_default(monkeypatch, tmp_path):
    monkeypatch.setattr("src.video.capcut_draft.settings.capcut_draft_root", "")
    monkeypatch.setattr("src.video.capcut_draft.platform.system", lambda: "Darwin")
    monkeypatch.setattr("src.video.capcut_draft.Path.home", lambda: tmp_path)

    expected = tmp_path / "Movies" / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
    expected.mkdir(parents=True)

    assert find_capcut_draft_root() == expected


def test_find_capcut_draft_root_returns_none_when_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr("src.video.capcut_draft.settings.capcut_draft_root", "")
    monkeypatch.setattr("src.video.capcut_draft.platform.system", lambda: "Linux")
    assert find_capcut_draft_root() is None


# ---- build_capcut_draft ----


def test_build_capcut_draft_no_draft_root_returns_error(synthetic_clip_paths, narration_wav, monkeypatch):
    monkeypatch.setattr("src.video.capcut_draft.find_capcut_draft_root", lambda: None)

    result = build_capcut_draft(1, synthetic_clip_paths, narration_wav, "ナレーション")
    assert result["ok"] is False
    assert "CAPCUT_DRAFT_ROOT" in result["error"]


def test_build_capcut_draft_no_clip_paths_returns_error(tmp_path, narration_wav):
    result = build_capcut_draft(1, [], narration_wav, "ナレーション", draft_root=tmp_path)
    assert result["ok"] is False
    assert "素材" in result["error"]


def test_build_capcut_draft_creates_valid_draft(tmp_path, synthetic_clip_paths, narration_wav):
    result = build_capcut_draft(
        42,
        synthetic_clip_paths,
        narration_wav,
        "こんにちは。今日もいい天気ですね。",
        draft_root=tmp_path,
    )

    assert result["ok"] is True
    assert result["content_id"] == 42
    draft_path = Path(result["draft_path"])
    assert draft_path.is_dir()

    content_file = draft_path / "draft_content.json"
    assert content_file.exists()
    data = json.loads(content_file.read_text(encoding="utf-8"))

    assert data["canvas_config"]["width"] == 1080
    assert data["canvas_config"]["height"] == 1920
    # 動画の尺はnarration_wav(5秒)にそのまま合わせる。
    assert data["duration"] == pytest.approx(5_000_000, abs=1000)

    track_types = {t["type"] for t in data["tracks"]}
    assert track_types == {"video", "audio", "text"}

    audio_track = next(t for t in data["tracks"] if t["type"] == "audio")
    assert len(audio_track["segments"]) == 1
    assert audio_track["segments"][0]["target_timerange"]["duration"] == pytest.approx(
        5_000_000, abs=1000
    )

    video_track = next(t for t in data["tracks"] if t["type"] == "video")
    assert len(video_track["segments"]) >= 1
    # 主動画トラックは0sから開始していなければならない
    assert video_track["segments"][0]["target_timerange"]["start"] == 0

    text_track = next(t for t in data["tracks"] if t["type"] == "text")
    assert len(text_track["segments"]) == 2  # 「。」で2文に分割される

    assert data["materials"]["audios"][0]["path"] == str(narration_wav.resolve())


def test_build_capcut_draft_allows_replace_on_rerun(tmp_path, synthetic_clip_paths, narration_wav):
    first = build_capcut_draft(7, synthetic_clip_paths, narration_wav, "テスト。", draft_root=tmp_path)
    second = build_capcut_draft(7, synthetic_clip_paths, narration_wav, "テスト。", draft_root=tmp_path)

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["draft_path"] == second["draft_path"]
