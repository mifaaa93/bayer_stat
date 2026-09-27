"""Telegram voice conversion and transcription through the configured provider."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import requests


class VoiceTranscriber:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str = "gpt-4o-transcribe",
        ffmpeg_bin: str = "ffmpeg",
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.ffmpeg_bin = ffmpeg_bin
        if ffmpeg_bin == "ffmpeg":
            try:
                import imageio_ffmpeg

                self.ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
            except (ImportError, RuntimeError):
                pass

    def convert_to_wav(self, audio: bytes, suffix: str = ".oga") -> bytes:
        """Always convert Telegram audio to mono 16 kHz PCM WAV."""
        with tempfile.TemporaryDirectory(prefix="tg-voice-") as folder:
            source = Path(folder) / f"input{suffix}"
            target = Path(folder) / "converted.wav"
            source.write_bytes(audio)
            try:
                subprocess.run(
                    [
                        self.ffmpeg_bin, "-hide_banner", "-loglevel", "error",
                        "-y", "-i", str(source),
                        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                        str(target),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
            except FileNotFoundError as exc:
                raise RuntimeError(
                    "ffmpeg не найден. Установите ffmpeg и укажите FFMPEG_BIN."
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Конвертация голосового сообщения превысила 120 секунд") from exc
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or b"").decode("utf-8", "replace")[-500:]
                raise RuntimeError(f"ffmpeg не смог конвертировать аудио: {detail}") from exc
            return target.read_bytes()

    def transcribe(self, audio: bytes, suffix: str = ".oga") -> str:
        wav = self.convert_to_wav(audio, suffix)
        if len(wav) > 25 * 1024 * 1024:
            raise RuntimeError("Конвертированное голосовое больше лимита 25 MB")
        response = requests.post(
            self.base_url + "/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            files={"file": ("voice.wav", wav, "audio/wav")},
            data={
                "model": self.model,
                "response_format": "json",
                "language": "ru",
            },
            timeout=180,
        )
        response.raise_for_status()
        payload = response.json()
        text = str(payload.get("text") or "").strip()
        if not text:
            raise RuntimeError("Модель транскрибации вернула пустой текст")
        return text
