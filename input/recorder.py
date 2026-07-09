import audioop
import wave
import config
from utils.logger import get_logger

logger = get_logger(__name__)

import pyaudio

import config
from utils.logger import get_logger

logger = get_logger(__name__)


def record_audio(
    filename: str = config.TEMP_AUDIO_FILE,
    max_duration: int = config.AUDIO_DURATION,
    silence_threshold: int = config.SILENCE_THRESHOLD,
    silence_duration: float = config.SILENCE_DURATION,
) -> bytes:
    chunk    = config.AUDIO_CHUNK
    fmt      = pyaudio.paInt16
    channels = config.AUDIO_CHANNELS
    rate     = config.AUDIO_RATE

    logger.info(
        "Audio recording started | file=%s | max_duration=%ss | rate=%s | channels=%s",
        filename, max_duration, rate, channels,
    )

    p = pyaudio.PyAudio()
    stream = p.open(
        format=fmt,
        channels=channels,
        rate=rate,
        input=True,
        frames_per_buffer=chunk,
    )

    frames: list[bytes] = []
    silent_chunks = 0
    max_chunks    = int(rate / chunk * max_duration)
    silence_limit = int(rate / chunk * silence_duration)

    try:
        for _ in range(max_chunks):
            data = stream.read(chunk, exception_on_overflow=False)
            frames.append(data)

            rms = audioop.rms(data, 2)  # 2 bytes per sample for paInt16
            if rms < silence_threshold:
                silent_chunks += 1
            else:
                silent_chunks = 0

            if silent_chunks >= silence_limit:
                logger.info(
                    "Silence detected | silent_chunks=%d | stopping early", silent_chunks
                )
                break

        logger.info("Audio recording completed | frames_collected=%d", len(frames))

    except Exception:
        logger.exception("Audio recording failed | file=%s", filename)
        stream.stop_stream()
        stream.close()
        p.terminate()
        return b""

    stream.stop_stream()
    stream.close()

    audio_data = b"".join(frames)

    try:
        wf = wave.open(filename, "wb")
        wf.setnchannels(channels)
        wf.setsampwidth(p.get_sample_size(fmt))
        wf.setframerate(rate)
        wf.writeframes(audio_data)
        wf.close()
        logger.info("Audio file saved successfully | path=%s", filename)
    except Exception:
        logger.exception("Failed to save audio file | path=%s", filename)

    p.terminate()
    return audio_data
