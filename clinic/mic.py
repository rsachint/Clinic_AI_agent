import subprocess
from pathlib import Path

_BEEP_PATH = Path(__file__).parent / "assets" / "beep.wav"


def play_beep():
    """Fire-and-forget audible cue for exactly when recording starts --
    relying on a visual countdown alone assumes someone is looking at the
    screen/terminal, which a voice-first product shouldn't assume."""
    subprocess.Popen(
        ["afplay", str(_BEEP_PATH)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# Placeholder for the real appliance's push-to-talk hardware capture (§5 of the
# plan). On this dev Mac we shell out to ffmpeg against the built-in mic.
def record(path, seconds, device_index=0, sample_rate=16000):
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "avfoundation",
            "-i", ":{}".format(device_index),
            "-t", str(seconds),
            "-ar", str(sample_rate),
            "-ac", "1",
            path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
