"""Local speech-to-text for the chat box (optional `voice` extra: faster-whisper).

Audio is recorded in the browser, posted to the local food-ui server and transcribed on this
machine. It never leaves the computer and is never stored. The text is placed in the chat box
for the user to check; nothing is sent or ordered by voice alone.

Install with `uv sync --extra voice`. The model is downloaded once (from Hugging Face) on the
first transcription and cached in ~/.cache/huggingface.
"""
from __future__ import annotations

import importlib.util
import io
import os
import re
import threading
import time
from collections import Counter
from typing import Any, Iterable

MODEL_ENV = "FOOD_CONSOLE_WHISPER_MODEL"
DEVICE_ENV = "FOOD_CONSOLE_WHISPER_DEVICE"
LANGUAGE_ENV = "FOOD_CONSOLE_WHISPER_LANGUAGE"
DEFAULT_MODEL = "small"
SAMPLE_RATE = 16_000
MAX_AUDIO_SECONDS = 60
MAX_AUDIO_BYTES = 2_000_000

# Words Whisper often mishears in this app. Passed as the initial prompt, which biases
# spelling toward these words without forcing them into the transcript.
BASE_VOCABULARY = (
    "Zomato, Swiggy, Instamart, Dineout, District, Zepto, Blinkit. "
    "Paneer, biryani, dal makhani, butter chicken, masala dosa, idli, sambar, vada, naan, roti, "
    "paratha, tikka, kebab, momos, chole bhature, rajma chawal, poha, upma, thali, lassi, chai, "
    "kcal, protein, UPI."
)
MAX_HINTS = 30


def available() -> bool:
    """True when the optional dependency is installed."""
    return importlib.util.find_spec("faster_whisper") is not None


def model_name() -> str:
    return os.environ.get(MODEL_ENV) or DEFAULT_MODEL


def language() -> str | None:
    """English by default, so Hindi dish names come out in Latin script; 'auto' detects."""
    value = (os.environ.get(LANGUAGE_ENV) or "en").strip().lower()
    return None if value == "auto" else value


def hints_from_orders(orders: Iterable[dict[str, Any]], limit: int = MAX_HINTS) -> list[str]:
    """The user's most frequent restaurants and dishes, so Whisper spells them right."""
    seen: Counter[str] = Counter()
    for order in orders:
        merchant = str(order.get("merchant") or "").strip()
        if merchant:
            seen[merchant] += 2  # one merchant name covers many orders; rank it a bit higher
        for item in order.get("items") or []:
            name = str((item or {}).get("name") or "").strip()
            if name:
                seen[name] += 1
    clean = []
    for name, _ in seen.most_common():
        name = re.sub(r"\s+", " ", re.sub(r"[^\w&' .-]", " ", name)).strip()[:40]
        if name and name.lower() not in {c.lower() for c in clean}:
            clean.append(name)
        if len(clean) >= limit:
            break
    return clean


def build_prompt(hints: Iterable[str] = ()) -> str:
    extra = ", ".join(hints)
    return BASE_VOCABULARY + (f" {extra}." if extra else "")


def decode(audio: bytes):
    """Any browser recording to 16 kHz mono float32, stopping early past the length limit.

    Done here with PyAV directly: faster_whisper.decode_audio passes an argument that
    newer PyAV releases no longer accept.
    """
    import av
    import numpy as np

    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    chunks, total, limit = [], 0, (MAX_AUDIO_SECONDS + 2) * SAMPLE_RATE
    with av.open(io.BytesIO(audio), mode="r") as container:
        stream = next(s for s in container.streams if s.type == "audio")
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunk = out.to_ndarray().reshape(-1)
                chunks.append(chunk)
                total += chunk.size
            if total > limit:
                break
        for out in resampler.resample(None):  # flush
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


class Transcriber:
    """Loads the Whisper model on first use and transcribes one clip at a time."""

    def __init__(self, name: str | None = None, *, device: str | None = None, cpu_threads: int | None = None):
        self.name = name or model_name()
        self.device = device or os.environ.get(DEVICE_ENV) or "cpu"
        self.cpu_threads = cpu_threads or min(8, os.cpu_count() or 4)
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            # int8 on CPU: fast on Apple Silicon and x86, small memory footprint.
            compute = "int8" if self.device == "cpu" else "default"
            self._model = WhisperModel(self.name, device=self.device, compute_type=compute, cpu_threads=self.cpu_threads)
        return self._model

    def transcribe(self, audio: bytes, hints: Iterable[str] = ()) -> dict[str, Any]:
        """Decode any browser recording format (WebM/Opus, MP4/AAC, WAV) and transcribe it."""
        if not audio:
            raise ValueError("empty recording")
        if len(audio) > MAX_AUDIO_BYTES:
            raise ValueError("recording too large")
        try:
            samples = decode(audio)
        except Exception as exc:  # PyAV raises its own error types for undecodable input
            raise ValueError("could not decode the recording") from exc
        duration = len(samples) / SAMPLE_RATE
        if duration > MAX_AUDIO_SECONDS + 1:
            raise ValueError(f"recording longer than {MAX_AUDIO_SECONDS} seconds")
        if duration < 0.3:
            return {"text": "", "duration": round(duration, 1), "seconds": 0.0}
        started = time.monotonic()
        with self._lock:
            model = self._load()
            segments, _info = model.transcribe(
                samples,
                language=language(),
                beam_size=5,
                vad_filter=True,  # drop silence: avoids "Thank you." style hallucinations
                condition_on_previous_text=False,
                initial_prompt=build_prompt(hints),
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        return {"text": text, "duration": round(duration, 1), "seconds": round(time.monotonic() - started, 2)}
