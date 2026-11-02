import pytest

from sobesnik_asr import AudioDecodeError, probe_audio, strip_video
from sobesnik_asr.samples import mp4_video_bytes, ogg_opus_bytes, wav_bytes


def test_wav() -> None:
    info = probe_audio(wav_bytes(1.5))
    assert info.format == "wav"
    assert info.mime == "audio/wav"
    assert info.duration_s == pytest.approx(1.5, abs=0.05)


def test_ogg_opus() -> None:
    info = probe_audio(ogg_opus_bytes(2.0))
    assert info.format == "ogg"
    assert info.mime == "audio/ogg"
    assert info.duration_s == pytest.approx(2.0, abs=0.1)


def test_strip_video() -> None:
    """Кружочек Telegram: видео отрезается, звук копируется как есть."""
    video = probe_audio(mp4_video_bytes(2.0))
    assert video.has_video and video.format == "m4a"
    audio = probe_audio(strip_video(mp4_video_bytes(2.0)))
    assert not audio.has_video
    assert audio.mime == "audio/mp4"
    assert audio.duration_s == pytest.approx(2.0, abs=0.1)
    with pytest.raises(AudioDecodeError):
        strip_video(wav_bytes(1.5))


@pytest.mark.parametrize(
    "data", [b"", b"hello, this is not audio" * 20, b"%PDF-1.4\n" + b"0" * 500]
)
def test_not_audio(data: bytes) -> None:
    with pytest.raises(AudioDecodeError):
        probe_audio(data)
