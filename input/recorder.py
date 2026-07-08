import pyaudio
import wave

def record_audio(filename="test_output.wav", duration=10):
    CHUNK = 1024
    FORMAT = pyaudio.paInt16
    CHANNELS = 1
    RATE = 44100

    logger.info(
        "Audio recording started | file=%s | duration=%ss | rate=%s | channels=%s",
        filename, duration, RATE, CHANNELS
    )

    try:
        p = pyaudio.PyAudio()

        stream = p.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=RATE,
            input=True,
            frames_per_buffer=CHUNK
        )

        frames = []

        for _ in range(int(RATE / CHUNK * duration)):
            data = stream.read(CHUNK)
            frames.append(data)

        logger.info("Audio recording completed | frames_collected=%s", len(frames))

        stream.stop_stream()
        stream.close()

        # Save file
        wf = wave.open(filename, 'wb')
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(p.get_sample_size(FORMAT))
        wf.setframerate(RATE)
        wf.writeframes(b''.join(frames))
        wf.close()

        logger.info("Audio file saved successfully | path=%s", filename)

        p.terminate()

    except Exception as e:
        logger.exception("Audio recording failed | file=%s", filename)