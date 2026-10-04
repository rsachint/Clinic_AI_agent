import base64
import os

from sarvamai import SarvamAI

# Bulbul's language set is narrower than Saaras's -- it has no Urdu, Assamese,
# Nepali, Konkani, or Kashmiri, all of which Saaras accepts as ASR input.
# Fall back to Hindi rather than erroring for those.
_SUPPORTED = {
    "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN",
    "ml-IN", "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN",
}


def synthesize(text, language_code="hi-IN", model="bulbul:v3", speaker="priya"):
    """Returns raw WAV bytes."""
    lang = language_code if language_code in _SUPPORTED else "hi-IN"
    client = SarvamAI(api_subscription_key=os.environ["SARVAM_API_KEY"])
    response = client.text_to_speech.convert(
        text=text,
        language_code=lang,
        model=model,
        speaker=speaker,
    )
    return base64.b64decode(response.audios[0])
