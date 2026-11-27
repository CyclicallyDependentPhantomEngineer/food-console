"""Voice input: vocabulary hints, audio decoding limits and the transcription endpoint.

No Whisper model is downloaded here: the endpoint tests use a fake transcriber, and the
decoding tests need only PyAV (skipped when the optional `voice` extra is not installed).
"""
import argparse
import base64
import io
import math
import struct
import wave

import pytest
from starlette.testclient import TestClient

from food_console import voice
from food_console.web import MAX_REQUEST_BYTES, AppState, build_app

PORT = 8765


def wav(seconds: float, rate: int = 16_000) -> bytes:
    """A mono 16-bit sine tone."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(int(seconds * rate))))
    return buf.getvalue()


def test_hints_rank_dedupe_and_sanitise():
    orders = [
        {"merchant": "Behrouz Biryani", "items": [{"name": "Chicken Biryani"}, {"name": "Raita"}]},
        {"merchant": "behrouz biryani", "items": [{"name": "Chicken Biryani"}]},
        {"merchant": "Meghana <script>", "items": [{"name": None}, {}]},
        {"merchant": "", "items": None},
    ]
    hints = voice.hints_from_orders(orders)
    assert hints[0] == "Behrouz Biryani"
    assert [h.lower() for h in hints].count("behrouz biryani") == 1
    assert "Chicken Biryani" in hints and "Raita" in hints
    assert all("<" not in h and ">" not in h for h in hints)
    assert len(voice.hints_from_orders([{"merchant": f"Place {i}"} for i in range(100)])) == voice.MAX_HINTS


def test_prompt_includes_base_vocabulary_and_hints():
    assert voice.build_prompt().startswith("Zomato, Swiggy")
    assert voice.build_prompt(["Behrouz Biryani"]).endswith("Behrouz Biryani.")


def test_language_defaults_to_english(monkeypatch):
    monkeypatch.delenv(voice.LANGUAGE_ENV, raising=False)
    assert voice.language() == "en"
    monkeypatch.setenv(voice.LANGUAGE_ENV, "auto")
    assert voice.language() is None


needs_av = pytest.mark.skipif(not voice.available(), reason="voice extra not installed")


@needs_av
def test_decode_resamples_to_16k_mono():
    samples = voice.decode(wav(1.0, rate=44_100))
    assert abs(len(samples) - 16_000) < 400
    assert samples.dtype.name == "float32" and float(abs(samples).max()) <= 1.0


@needs_av
def test_transcriber_rejects_bad_input_without_loading_a_model():
    t = voice.Transcriber("not-a-real-model")
    with pytest.raises(ValueError, match="empty"):
        t.transcribe(b"")
    with pytest.raises(ValueError, match="too large"):
        t.transcribe(b"0" * (voice.MAX_AUDIO_BYTES + 1))
    with pytest.raises(ValueError, match="decode"):
        t.transcribe(b"definitely not audio")
    with pytest.raises(ValueError, match="longer than"):
        t.transcribe(wav(voice.MAX_AUDIO_SECONDS + 5, rate=8_000))
    assert t.transcribe(wav(0.1))["text"] == ""  # too short to be speech
    assert not t.loaded


class FakeTranscriber:
    name, loaded = "fake", True

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, hints=()):
        self.calls.append((audio, list(hints)))
        if audio == b"bad":
            raise ValueError("could not decode the recording")
        return {"text": "find paneer tikka near me", "duration": 2.0, "seconds": 0.5}


@pytest.fixture()
def client():
    state = AppState(argparse.Namespace(port=PORT, profile=None, history_limit=5))
    state.transcriber = FakeTranscriber()
    with TestClient(build_app(state), base_url=f"http://127.0.0.1:{PORT}") as c:
        assert c.get(f"/login?t={state.login_token}", follow_redirects=False).status_code == 303
        c.headers["X-Food-Console"] = "1"
        c.state = state
        yield c


def post_audio(client, audio: bytes):
    return client.post("/api/voice/transcribe", json={"audio": base64.b64encode(audio).decode()})


def test_transcribe_returns_text(client):
    r = post_audio(client, b"clip")
    assert r.status_code == 200 and r.json()["text"] == "find paneer tikka near me"
    assert client.state.transcriber.calls[0][0] == b"clip"


def test_transcribe_requires_session_and_header(client):
    anon = TestClient(client.app, base_url=f"http://127.0.0.1:{PORT}")
    assert anon.post("/api/voice/transcribe", json={"audio": ""}, headers={"X-Food-Console": "1"}).status_code == 401
    assert client.post("/api/voice/transcribe", json={"audio": ""}, headers={"X-Food-Console": ""}).status_code == 403
    assert client.state.transcriber.calls == []


def test_transcribe_rejects_bad_payloads(client):
    assert client.post("/api/voice/transcribe", json={"audio": "not base64!!"}).status_code == 400
    r = post_audio(client, b"bad")
    assert r.status_code == 400 and "decode" in r.json()["error"]


def test_voice_route_allows_clips_larger_than_other_routes(client):
    big = b"x" * 1_000_000  # ~1.3 MB once base64-encoded: over the normal limit, under the voice one
    assert len(base64.b64encode(big)) > MAX_REQUEST_BYTES
    assert post_audio(client, big).status_code == 200
    assert client.post("/api/chat", json={"message": "x" * (MAX_REQUEST_BYTES + 10)}).status_code == 413
    assert post_audio(client, b"x" * 2_500_000).status_code == 413


def test_status_and_unavailable(client):
    assert client.get("/api/voice").json()["available"] is True
    client.state.transcriber = None
    status = client.get("/api/voice").json()
    assert status["available"] is False and status["install"] == "uv sync --extra voice"
    assert post_audio(client, b"clip").status_code == 503
