"""Windows audio recorder using WASAPI loopback (system audio) + microphone mix.

Requires pyaudiowpatch: pip install pyaudiowpatch
"""
from __future__ import annotations

import threading
import wave
from pathlib import Path


TARGET_RATE = 16000
CHANNELS = 1
CHUNK = 512


class AudioRecorderWin:
    """Records system audio + microphone to a WAV file using WASAPI loopback."""

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._wav_path: Path | None = None

    def start(self, wav_path: Path) -> None:
        if self._thread is not None:
            raise RuntimeError("already recording")
        self._stop_event.clear()
        self._wav_path = wav_path
        self._thread = threading.Thread(target=self._record_loop, args=(wav_path,), daemon=True)
        self._thread.start()

    def stop(self) -> Path:
        if self._thread is None:
            raise RuntimeError("not recording")
        self._stop_event.set()
        self._thread.join(timeout=10)
        self._thread = None
        path = self._wav_path
        self._wav_path = None
        return path

    def _record_loop(self, wav_path: Path) -> None:
        import pyaudiowpatch as pyaudio
        import numpy as np

        p = pyaudio.PyAudio()
        try:
            # --- Loopback device (system audio) ---
            wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_out_idx = wasapi_info["defaultOutputDevice"]
            default_out = p.get_device_info_by_index(default_out_idx)

            loopback_device = None
            if default_out.get("isLoopbackDevice"):
                loopback_device = default_out
            else:
                for lb in p.get_loopback_device_info_generator():
                    if default_out["name"] in lb["name"]:
                        loopback_device = lb
                        break

            lb_rate = int(loopback_device["defaultSampleRate"]) if loopback_device else TARGET_RATE
            lb_channels = int(loopback_device["maxInputChannels"]) if loopback_device else 2

            # --- Microphone device ---
            mic_idx = wasapi_info["defaultInputDevice"]
            mic_info = p.get_device_info_by_index(mic_idx)
            mic_rate = int(mic_info["defaultSampleRate"])
            mic_channels = int(mic_info["maxInputChannels"])

            lb_frames: list[bytes] = []
            mic_frames: list[bytes] = []
            lb_lock = threading.Lock()
            mic_lock = threading.Lock()

            def lb_callback(in_data, frame_count, time_info, status):
                with lb_lock:
                    lb_frames.append(in_data)
                return (None, pyaudio.paContinue)

            def mic_callback(in_data, frame_count, time_info, status):
                with mic_lock:
                    mic_frames.append(in_data)
                return (None, pyaudio.paContinue)

            streams = []

            if loopback_device is not None:
                streams.append(p.open(
                    format=pyaudio.paInt16,
                    channels=lb_channels,
                    rate=lb_rate,
                    input=True,
                    input_device_index=loopback_device["index"],
                    frames_per_buffer=CHUNK,
                    stream_callback=lb_callback,
                ))

            streams.append(p.open(
                format=pyaudio.paInt16,
                channels=mic_channels,
                rate=mic_rate,
                input=True,
                input_device_index=mic_idx,
                frames_per_buffer=CHUNK,
                stream_callback=mic_callback,
            ))

            for s in streams:
                s.start_stream()

            self._stop_event.wait()

            for s in streams:
                s.stop_stream()
                s.close()

            # --- Mix and resample to 16kHz mono ---
            def to_mono_16k(frames: list[bytes], src_rate: int, src_channels: int) -> np.ndarray:
                if not frames:
                    return np.zeros(0, dtype=np.float32)
                raw = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
                if src_channels > 1:
                    raw = raw.reshape(-1, src_channels).mean(axis=1)
                if src_rate != TARGET_RATE:
                    from scipy.signal import resample_poly
                    from math import gcd
                    g = gcd(TARGET_RATE, src_rate)
                    raw = resample_poly(raw, TARGET_RATE // g, src_rate // g)
                return raw

            with lb_lock:
                lb_data = list(lb_frames)
            with mic_lock:
                mic_data = list(mic_frames)

            lb_audio = to_mono_16k(lb_data, lb_rate, lb_channels)
            mic_audio = to_mono_16k(mic_data, mic_rate, mic_channels)

            # Align lengths
            length = max(len(lb_audio), len(mic_audio))
            if len(lb_audio) < length:
                lb_audio = np.pad(lb_audio, (0, length - len(lb_audio)))
            if len(mic_audio) < length:
                mic_audio = np.pad(mic_audio, (0, length - len(mic_audio)))

            mixed = np.clip((lb_audio + mic_audio) * 0.5, -1.0, 1.0)
            mixed_int16 = (mixed * 32767).astype(np.int16)

            wav_path.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(2)
                wf.setframerate(TARGET_RATE)
                wf.writeframes(mixed_int16.tobytes())

        finally:
            p.terminate()
