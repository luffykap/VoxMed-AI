import pyaudio
import wave

def record_audio(filename="test_output.wav", duration=10):
    CHUNK = 1024
    FORMAT = pyaudio.paInt16
    CHANNELS = 1
    RATE = 44100

    p = pyaudio.PyAudio()

    stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE,
                    input=True, frames_per_buffer=CHUNK)

    print("Recording...")

    frames = []

    for _ in range(int(RATE / CHUNK * duration)):
        data = stream.read(CHUNK)
        frames.append(data)

    print("Done!")

    stream.stop_stream()
    stream.close()

    # Save BEFORE terminating PyAudio
    wf = wave.open(filename, 'wb')
    wf.setnchannels(CHANNELS)
    wf.setsampwidth(p.get_sample_size(FORMAT))
    wf.setframerate(RATE)
    wf.writeframes(b''.join(frames))
    wf.close()

    p.terminate()

if __name__ == "__main__":
    record_audio()
