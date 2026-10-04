import os
from collections import namedtuple

from sarvamai import SarvamAI

# language_probability is only populated by Saaras when language_code is
# omitted/"unknown" -- passing a pinned code (e.g. "hi-IN") gets that same
# code echoed back with no real detection confidence behind it.
TranscriptResult = namedtuple("TranscriptResult", ["text", "language_code", "language_probability"])


def transcribe(audio_path, language_code="unknown", model="saaras:v4", input_audio_codec=None):
    # input_audio_codec is optional -- the mic path (WAV) has always worked
    # without specifying it. WhatsApp voice notes are OGG/Opus containers;
    # Sarvam's SDK accepts "opus"/"ogg" directly (verified against the
    # installed SDK's parameter list), so the WhatsApp audio path passes
    # input_audio_codec="opus" explicitly rather than relying on inference.
    api_key = os.environ["SARVAM_API_KEY"]
    client = SarvamAI(api_subscription_key=api_key)
    kwargs = {"model": model, "language_code": language_code}
    if input_audio_codec:
        kwargs["input_audio_codec"] = input_audio_codec
    with open(audio_path, "rb") as f:
        response = client.speech_to_text.transcribe(file=f, **kwargs)
    return TranscriptResult(response.transcript, response.language_code, response.language_probability)
