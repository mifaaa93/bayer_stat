import io
import wave
from unittest.mock import Mock, patch

from voice_transcription import VoiceTranscriber


def pcm_wav(seconds=0.05, rate=8000):
    frames = b"\x00\x00" * int(seconds * rate)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)
    return output.getvalue()


def test_voice_is_always_converted_to_16khz_wav():
    transcriber = VoiceTranscriber("key", "https://example.test/v1")
    converted = transcriber.convert_to_wav(pcm_wav(), ".oga")
    assert converted[:4] == b"RIFF"
    with wave.open(io.BytesIO(converted), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16000
        assert wav.getsampwidth() == 2


def test_transcription_uses_expected_model_and_multipart():
    transcriber = VoiceTranscriber("key", "https://example.test/v1")
    response = Mock()
    response.json.return_value = {"text": "статистика за вчера"}
    response.raise_for_status.return_value = None
    with patch.object(transcriber, "convert_to_wav", return_value=b"RIFF audio") as convert:
        with patch("voice_transcription.requests.post", return_value=response) as request:
            assert transcriber.transcribe(b"telegram oga") == "статистика за вчера"
    convert.assert_called_once_with(b"telegram oga", ".oga")
    assert request.call_args.args[0].endswith("/audio/transcriptions")
    assert request.call_args.kwargs["data"]["model"] == "gpt-4o-transcribe"
    assert request.call_args.kwargs["files"]["file"][0] == "voice.wav"


def test_empty_transcription_is_rejected():
    transcriber = VoiceTranscriber("key", "https://example.test/v1")
    response = Mock()
    response.json.return_value = {"text": " "}
    response.raise_for_status.return_value = None
    with patch.object(transcriber, "convert_to_wav", return_value=b"RIFF audio"):
        with patch("voice_transcription.requests.post", return_value=response):
            try:
                transcriber.transcribe(b"audio")
                assert False, "expected RuntimeError"
            except RuntimeError as exc:
                assert "пустой" in str(exc)
