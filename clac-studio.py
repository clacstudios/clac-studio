import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import wave
import struct
import threading
import math
import os
import io
import time
import queue

try:
    import pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False

# ================================
# BIT STREAM
# ================================
class BitStream:
    def __init__(self, filename=None, mode='rb', header_bytes=0, data_bytes=None):
        self.buffer = 0
        self.bits_in_buffer = 0
        self.file = None
        self.data_bytes = data_bytes
        self.byte_idx = 0
        if filename:
            self.file = open(filename, mode)
            if header_bytes > 0 and mode == 'rb':
                self.file.read(header_bytes)

    def write_bit(self, bit):
        self.buffer = (self.buffer << 1) | (bit & 1)
        self.bits_in_buffer += 1
        if self.bits_in_buffer == 8:
            if self.file:
                self.file.write(bytes([self.buffer]))
            self.buffer = 0
            self.bits_in_buffer = 0

    def write_bits(self, value, count):
        for i in range(count - 1, -1, -1):
            self.write_bit((value >> i) & 1)

    def flush(self):
        if self.bits_in_buffer > 0:
            self.buffer <<= (8 - self.bits_in_buffer)
            if self.file:
                self.file.write(bytes([self.buffer]))
            self.buffer = 0
            self.bits_in_buffer = 0

    def read_bit(self):
        if self.bits_in_buffer == 0:
            if self.file:
                byte = self.file.read(1)
            elif self.data_bytes and self.byte_idx < len(self.data_bytes):
                byte = bytes([self.data_bytes[self.byte_idx]])
                self.byte_idx += 1
            else:
                return None
            if not byte:
                return None
            self.buffer = byte[0]
            self.bits_in_buffer = 8
        self.bits_in_buffer -= 1
        return (self.buffer >> self.bits_in_buffer) & 1

    def read_bits(self, count):
        value = 0
        for _ in range(count):
            bit = self.read_bit()
            if bit is None:
                return None
            value = (value << 1) | bit
        return value

    def close(self):
        if self.file:
            self.file.close()

# ================================
# CLAC CODEC
# ================================
class CLACCodec:
    MAGIC = b'CLAC'
    HEADER_SIZE = 20
    BLOCK_SIZE = 4096
    STREAM_CHUNK_SAMPLES = 4096

    def encode(self, input_wav, output_clac, progress_callback=None):
        with wave.open(input_wav, 'rb') as wav_in:
            n_channels = wav_in.getnchannels()
            framerate = wav_in.getframerate()
            raw_data = wav_in.readframes(wav_in.getnframes())
        samples = list(struct.unpack(f"<{len(raw_data)//2}h", raw_data))
        total = len(samples)
        with open(output_clac, 'wb') as f:
            f.write(self.MAGIC)
            f.write(struct.pack('<I', framerate))
            f.write(struct.pack('<H', n_channels))
            f.write(struct.pack('<H', 16))
            f.write(struct.pack('<Q', total))
        bs = BitStream(filename=output_clac, mode='ab')
        last = 0
        for i in range(0, total, self.BLOCK_SIZE):
            block = samples[i:i + self.BLOCK_SIZE]
            self._encode_block(bs, block, last)
            last = block[-1] if block else last
            if progress_callback:
                progress_callback(((i + len(block)) / total) * 100)
        bs.flush()
        bs.close()
        orig = os.path.getsize(input_wav)
        comp = os.path.getsize(output_clac)
        return ((orig - comp) / orig) * 100 if orig > 0 else 0

    def _encode_block(self, bs, samples, last):
        if not samples: return
        residuals = []
        for s in samples:
            residuals.append(s - last)
            last = s
        avg = sum(abs(r) for r in residuals) / len(residuals)
        k = 0 if avg < 1 else max(0, min(15, int(math.log2(avg))))
        bs.write_bits(k, 4)
        bs.write_bits(len(residuals), 16)
        for res in residuals:
            ures = ((res << 1) ^ (res >> 31)) & 0xFFFFFFFF
            q, r = ures >> k, ures & ((1 << k) - 1)
            for _ in range(q): bs.write_bit(1)
            bs.write_bit(0)
            if k > 0: bs.write_bits(r, k)

    def decode(self, input_clac, output_wav=None, progress_callback=None, return_bytes=False):
        with open(input_clac, 'rb') as f:
            if f.read(4) != self.MAGIC: raise ValueError("Invalid .clac file")
            framerate = struct.unpack('<I', f.read(4))[0]
            n_channels = struct.unpack('<H', f.read(2))[0]
            f.read(2)
            total = struct.unpack('<Q', f.read(8))[0]
            data = f.read()
        bs = BitStream(data_bytes=data)
        samples, last = [], 0
        while len(samples) < total:
            k = bs.read_bits(4)
            if k is None: break
            block_len = bs.read_bits(16)
            if block_len is None: break
            for _ in range(block_len):
                if len(samples) >= total: break
                q = 0
                while bs.read_bit() == 1: q += 1
                r = bs.read_bits(k) if k > 0 else 0
                ures = (q << k) | r
                res = (ures >> 1) ^ -(ures & 1)
                samples.append(max(-32768, min(32767, last + res)))
                last = samples[-1]
            if progress_callback: progress_callback((len(samples) / total) * 100)
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as w:
            w.setnchannels(n_channels); w.setsampwidth(2); w.setframerate(framerate)
            w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        wav_bytes = wav_buffer.getvalue()
        if return_bytes: return wav_bytes
        elif output_wav:
            with open(output_wav, 'wb') as f: f.write(wav_bytes)
            return True
        return None

    def decode_stream(self, input_clac, chunk_callback, progress_callback=None, stop_flag=None):
        with open(input_clac, 'rb') as f:
            if f.read(4) != self.MAGIC: raise ValueError("Invalid .clac file")
            framerate = struct.unpack('<I', f.read(4))[0]
            n_channels = struct.unpack('<H', f.read(2))[0]
            f.read(2)
            total = struct.unpack('<Q', f.read(8))[0]
            data = f.read()
        bs = BitStream(data_bytes=data)
        chunk_samples, last, decoded = [], 0, 0
        while decoded < total:
            if stop_flag and stop_flag.is_set(): break
            k = bs.read_bits(4)
            if k is None: break
            block_len = bs.read_bits(16)
            if block_len is None: break
            for _ in range(block_len):
                if decoded >= total or (stop_flag and stop_flag.is_set()): break
                q = 0
                while bs.read_bit() == 1: q += 1
                r = bs.read_bits(k) if k > 0 else 0
                ures = (q << k) | r
                res = (ures >> 1) ^ -(ures & 1)
                chunk_samples.append(max(-32768, min(32767, last + res)))
                last = chunk_samples[-1]
                decoded += 1
                if len(chunk_samples) >= self.STREAM_CHUNK_SAMPLES:
                    chunk_callback(struct.pack(f"<{len(chunk_samples)}h", *chunk_samples))
                    chunk_samples = []
            if progress_callback: progress_callback((decoded / total) * 100)
        if chunk_samples:
            chunk_callback(struct.pack(f"<{len(chunk_samples)}h", *chunk_samples))
        return framerate, n_channels, total

    def verify(self, wav1, wav2):
        with wave.open(wav1, 'rb') as w1, wave.open(wav2, 'rb') as w2:
            return w1.getparams() == w2.getparams() and w1.readframes(w1.getnframes()) == w2.readframes(w2.getnframes())

# ================================
# PYAUDIO PLAYER
# ================================
class PyAudioPlayer:
    def __init__(self):
        self.pa = pyaudio.PyAudio()
        self.stream = None
        self.audio_queue = queue.Queue(maxsize=200)
        self.is_playing = False
        self.is_paused = False
        self.stop_flag = threading.Event()
        self.decode_done = threading.Event()
        self.playback_thread = None
        self.framerate = 44100
        self.channels = 2
        self.total_samples = 0
        self.volume = 1.0
        self._progress_lock = threading.Lock()
        self._samples_played_local = 0

    def _playback_worker(self):
        """Blocking playback thread"""
        while not self.stop_flag.is_set():
            if self.is_paused:
                time.sleep(0.01)
                continue
            try:
                chunk = self.audio_queue.get(timeout=0.1)
                if self.volume != 1.0:
                    samples = list(struct.unpack(f"<{len(chunk)//2}h", chunk))
                    samples = [max(-32768, min(32767, int(s * self.volume))) for s in samples]
                    chunk = struct.pack(f"<{len(samples)}h", *samples)
                self.stream.write(chunk, exception_on_underflow=False)
                with self._progress_lock:
                    self._samples_played_local += len(chunk) // 2
            except queue.Empty:
                if self.decode_done.is_set() and self.audio_queue.empty():
                    break
                continue
            except Exception:
                break

    def start_stream(self, framerate, channels):
        self.framerate, self.channels = framerate, channels
        if self.stream:
            self.stream.stop_stream()
            self.stream.close()
        self.stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=framerate,
            output=True,
            frames_per_buffer=4096
        )

    def feed_chunk(self, chunk_bytes):
        if not self.stop_flag.is_set():
            try:
                self.audio_queue.put(chunk_bytes, timeout=1.0)
            except queue.Full:
                pass

    def start(self):
        self.is_playing = True
        self.is_paused = False
        self.stop_flag.clear()
        self.decode_done.clear()
        with self._progress_lock:
            self._samples_played_local = 0
        self.playback_thread = threading.Thread(target=self._playback_worker, daemon=True)
        self.playback_thread.start()

    def pause(self):
        self.is_paused = True

    def unpause(self):
        self.is_paused = False

    def stop(self):
        self.stop_flag.set()
        self.decode_done.set()
        self.is_playing = False
        self.is_paused = False
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break
        if self.playback_thread and self.playback_thread.is_alive():
            self.playback_thread.join(timeout=2.0)
        if self.stream:
            self.stream.stop_stream()

    def signal_decode_complete(self):
        self.decode_done.set()

    def wait_for_playback(self, timeout=60):
        start = time.time()
        while time.time() - start < timeout:
            if self.stop_flag.is_set():
                return False
            if self.decode_done.is_set() and self.audio_queue.empty():
                if not self.playback_thread or not self.playback_thread.is_alive():
                    return True
            time.sleep(0.05)
        return False

    def set_volume(self, vol):
        self.volume = max(0.0, min(1.0, vol))

    def close(self):
        self.stop()
        if self.stream:
            self.stream.close()
        self.pa.terminate()

# ================================
# GUI APPLICATION
# ================================
class CLACApp:
    def __init__(self, root):
        self.root = root
        self.root.title("🎵 CLAC Studio")
        self.root.geometry("600x480")
        self.root.resizable(False, False)
        self.pyaudio_available = PYAUDIO_AVAILABLE
        self.current_file = tk.StringVar(value="No file loaded")
        self.file_info = tk.StringVar(value="")
        self.progress_var = tk.DoubleVar()
        self.status_var = tk.StringVar(value="Ready")
        self.playback_state = 'stopped'
        self.original_wav = None
        self.decoded_wav = None
        self.stream_player = PyAudioPlayer() if self.pyaudio_available else None
        self.decoder_thread = None
        self._setup_ui()

    def _setup_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        
        # File Section
        file_frame = ttk.LabelFrame(self.root, text="📁 File", padding=15)
        file_frame.pack(fill=tk.X, padx=15, pady=10)
        ttk.Label(file_frame, textvariable=self.current_file, wraplength=500).pack(anchor=tk.W)
        ttk.Label(file_frame, textvariable=self.file_info, foreground="#007ACC").pack(anchor=tk.W)
        
        # Buttons
        btn_frame = ttk.Frame(self.root, padding=15)
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="📂 Open", command=self.open_file).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="⬇ Encode", command=self.start_encode).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="⬆ Decode", command=self.start_decode).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="✓ Verify", command=self.verify_files).pack(side=tk.LEFT, padx=5)
        
        # Progress Bar (only for encode/decode, hidden during playback)
        prog_frame = ttk.Frame(self.root, padding=15)
        prog_frame.pack(fill=tk.X)
        self.progress_bar = ttk.Progressbar(prog_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X)
        self.status_label = ttk.Label(prog_frame, textvariable=self.status_var, font=('Arial', 9))
        self.status_label.pack(anchor=tk.W, pady=5)
        
        # Player Section
        player_frame = ttk.LabelFrame(self.root, text="🔊 Player", padding=15)
        player_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=10)
        
        mode_txt = "🚀 PyAudio Streaming" if self.pyaudio_available else "⚠️ Install PyAudio"
        mode_col = "#00AA00" if self.pyaudio_available else "#FF6600"
        ttk.Label(player_frame, text=f"Mode: {mode_txt}", foreground=mode_col, font=('Arial', 9, 'bold')).pack(anchor=tk.W, pady=5)
        
        ctrl_frame = ttk.Frame(player_frame)
        ctrl_frame.pack(fill=tk.X, pady=5)
        self.play_btn = ttk.Button(ctrl_frame, text="▶ Play", command=self.toggle_play)
        self.play_btn.pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl_frame, text="■ Stop", command=self.stop_play).pack(side=tk.LEFT, padx=5)
        
        vol_frame = ttk.Frame(player_frame)
        vol_frame.pack(fill=tk.X, pady=5)
        ttk.Label(vol_frame, text="Volume:").pack(side=tk.LEFT, padx=5)
        self.vol_scale = ttk.Scale(vol_frame, from_=0, to=1, orient=tk.HORIZONTAL, command=self.set_volume)
        self.vol_scale.set(0.8)
        self.vol_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

    def open_file(self):
        path = filedialog.askopenfilename(filetypes=[("Audio", "*.wav *.clac"), ("All", "*.*")])
        if path:
            self.current_file.set(path)
            self.file_info.set(f"Size: {os.path.getsize(path)/1024:.1f} KB")
            self.stop_play()
            self.status_var.set("File loaded")
            self.progress_var.set(0)
            self.progress_bar['mode'] = 'determinate'  # Show progress bar
            if path.endswith('.wav'):
                self.original_wav = path
                self.decoded_wav = None
            elif path.endswith('.clac'):
                self.original_wav = None
                self.decoded_wav = path.replace('.clac', '_decoded.wav')

    def _run_task(self, task, args, success_msg, on_done=None):
        def worker():
            try:
                def cb(p):
                    self.root.after(0, lambda: self.progress_var.set(p))
                result = task(*args, progress_callback=cb)
                if on_done:
                    self.root.after(0, lambda: on_done(result))
                else:
                    self.root.after(0, lambda: [self.status_var.set(success_msg), messagebox.showinfo("Done", success_msg)])
            except Exception as e:
                msg = str(e)
                self.root.after(0, lambda m=msg: [self.status_var.set(f"Error: {m}"), messagebox.showerror("Error", m)])
            finally:
                self.root.after(0, lambda: self.progress_var.set(0))
        threading.Thread(target=worker, daemon=True).start()

    def start_encode(self):
        path = self.current_file.get()
        if not path.endswith('.wav'):
            return messagebox.showwarning("Warning", "Open a .wav file first")
        out = path.replace('.wav', '.clac')
        self.status_var.set("Encoding...")
        self.progress_bar['mode'] = 'determinate'
        def on_done(r):
            msg = f"Encoded! {r:.1f}% smaller"
            self.file_info.set(msg)
            self.status_var.set(msg)
            messagebox.showinfo("Done", msg)
        self._run_task(CLACCodec().encode, (path, out), "Encoding complete", on_done)

    def start_decode(self):
        path = self.current_file.get()
        if not path.endswith('.clac'):
            return messagebox.showwarning("Warning", "Open a .clac file first")
        out = path.replace('.clac', '_decoded.wav')
        self.status_var.set("Decoding...")
        self.progress_bar['mode'] = 'determinate'
        self._run_task(CLACCodec().decode, (path, out), "Decoded!", lambda r: self._set_decoded(out))

    def _set_decoded(self, path):
        self.current_file.set(path)
        self.file_info.set(f"Size: {os.path.getsize(path)/1024:.1f} KB")
        self.decoded_wav = path

    def verify_files(self):
        if not self.original_wav or not self.decoded_wav:
            return messagebox.showinfo("Verify", "Encode then decode first")
        if not os.path.exists(self.original_wav):
            return messagebox.showerror("Error", "Original not found")
        self.status_var.set("Verifying...")
        self.progress_bar['mode'] = 'determinate'
        try:
            if CLACCodec().verify(self.original_wav, self.decoded_wav):
                messagebox.showinfo("✅ Verified", "Bit-perfect identical!")
                self.status_var.set("Verification: PASSED ✓")
            else:
                messagebox.showerror("❌ Failed", "Files don't match")
                self.status_var.set("Verification: FAILED ✗")
        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda m=msg: messagebox.showerror("Error", m))
        finally:
            self.progress_var.set(0)

    def _stream_decoder_thread(self, path):
        try:
            def chunk_cb(chunk):
                if self.stream_player:
                    self.stream_player.feed_chunk(chunk)
            codec = CLACCodec()
            fr, ch, total = codec.decode_stream(path, chunk_cb, None, self.stream_player.stop_flag if self.stream_player else None)
            if self.stream_player:
                self.stream_player.total_samples = total
                self.stream_player.signal_decode_complete()
                self.stream_player.wait_for_playback()
        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda m=msg: self.status_var.set(f"Stream Error: {m}"))
        finally:
            self.root.after(0, lambda: self._on_stream_complete())

    def _on_stream_complete(self):
        if self.stream_player:
            self.stream_player.stop()
        self.playback_state = 'stopped'
        self.play_btn.config(text="▶ Play")
        self.status_var.set("Finished")
        self.progress_bar['mode'] = 'determinate'
        self.progress_var.set(0)

    def _update_playback_status(self):
        """Update status during playback (no progress bar)"""
        if self.playback_state == 'playing':
            self.root.after(500, self._update_playback_status)

    def toggle_play(self):
        if not self.pyaudio_available:
            return messagebox.showwarning("PyAudio Required", "pip install pyaudio")
        
        if self.playback_state == 'playing':
            # Pause
            if self.stream_player:
                self.stream_player.pause()
            self.playback_state = 'paused'
            self.play_btn.config(text="▶ Resume")
            self.status_var.set("⏸ Paused")
            return
        
        if self.playback_state == 'paused':
            # Resume
            if self.stream_player:
                self.stream_player.unpause()
            self.playback_state = 'playing'
            self.play_btn.config(text="⏸ Pause")
            self.status_var.set("🎵 Streaming...")
            self.progress_bar['mode'] = 'indeterminate'  # Hide progress, show activity
            self._update_playback_status()
            return
        
        # Start new playback
        path = self.current_file.get()
        if path.endswith('.wav') or path.endswith('.clac'):
            self.status_var.set("⏳ Starting stream...")
            self.progress_bar['mode'] = 'indeterminate'  # Hide progress, show activity
            if self.stream_player:
                self.stream_player.stop()
                self.stream_player.total_samples = 0
                if path.endswith('.wav'):
                    self._start_wav_stream(path)
                else:
                    self.stream_player.start_stream(44100, 2)
                    self.stream_player.start()
                    self.decoder_thread = threading.Thread(target=self._stream_decoder_thread, args=(path,), daemon=True)
                    self.decoder_thread.start()
                    self.root.after(500, lambda: self.status_var.set("🎵 Streaming...") if self.playback_state == 'playing' else None)
            self.playback_state = 'playing'
            self.play_btn.config(text="⏸ Pause")
            self._update_playback_status()
        else:
            messagebox.showwarning("Warning", "No audio loaded")

    def _start_wav_stream(self, path):
        try:
            with wave.open(path, 'rb') as w:
                fr, ch = w.getframerate(), w.getnchannels()
                self.stream_player.start_stream(fr, ch)
                def feed_wav():
                    try:
                        while not self.stream_player.stop_flag.is_set():
                            data = w.readframes(4096)
                            if not data:
                                break
                            self.stream_player.feed_chunk(data)
                        if self.stream_player:
                            self.stream_player.signal_decode_complete()
                            self.stream_player.wait_for_playback()
                    finally:
                        self._on_stream_complete()
                self.decoder_thread = threading.Thread(target=feed_wav, daemon=True)
                self.decoder_thread.start()
        except Exception as e:
            msg = str(e)
            self.root.after(0, lambda m=msg: messagebox.showerror("Player Error", m))
            self._on_stream_complete()

    def stop_play(self):
        if self.stream_player:
            self.stream_player.stop()
        if self.decoder_thread and self.decoder_thread.is_alive():
            self.decoder_thread.join(timeout=1.0)
        self.playback_state = 'stopped'
        self.play_btn.config(text="▶ Play")
        self.status_var.set("Stopped")
        self.progress_bar['mode'] = 'determinate'
        self.progress_var.set(0)

    def set_volume(self, val):
        if self.stream_player:
            self.stream_player.set_volume(float(val))
        self.status_var.set(f"Volume: {int(float(val)*100)}%")

    def on_closing(self):
        self.stop_play()
        if self.stream_player:
            self.stream_player.close()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = CLACApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()