import os
import sys
import datetime
import wave
import time
import subprocess
import socket
import threading
import queue
os.environ["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"

import av
import cv2
import pyaudio

# --- THE ADB HUNTER ---
def setup_adb():
    print("Searching for ADB...")
    adb_paths = ["/opt/homebrew/bin/adb", "/usr/local/bin/adb", os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"), "adb"]
    valid_adb = next((path for path in adb_paths if subprocess.run([path, "version"], capture_output=True).returncode == 0), None)
            
    if not valid_adb:
        print("❌ CRITICAL ERROR: Could not find ADB.")
        return False
        
    print("Configuring USB Bridges...")
    try:
        subprocess.run([valid_adb, "-d", "forward", "tcp:8080", "tcp:8080"], check=True)
        subprocess.run([valid_adb, "-d", "forward", "tcp:8081", "tcp:8081"], check=True)
        subprocess.run([valid_adb, "-d", "forward", "tcp:8082", "tcp:8082"], check=True)
        print("✅ USB Bridges established!\n")
        return True
    except Exception as e:
        return False

if not setup_adb():
    sys.exit(1)

# --- QUEUES & GLOBALS ---
frame_queue = queue.Queue(maxsize=2)
latest_frame = None  # Holds the absolute freshest frame
is_recording = False
ffmpeg_process = None
audio_writer = None
frame_width, frame_height = 0, 0
vid_path = ""
aud_path = ""
current_quality = "MAX" # UI Tracker

def receive_video():
    global latest_frame
    while True:
        try:
            container = av.open('tcp://127.0.0.1:8080', format='h264', options={'fflags': 'nobuffer', 'flags': 'low_delay', 'strict': 'experimental'})
            for frame in container.decode(video=0):
                img = frame.to_ndarray(format='bgr24')
                latest_frame = img # Always cache the freshest image
                
                if frame_queue.full():
                    try: frame_queue.get_nowait() 
                    except queue.Empty: pass
                frame_queue.put(img)
        except Exception:
            time.sleep(1)

def receive_audio():
    global is_recording, audio_writer
    p = pyaudio.PyAudio()
    stream = p.open(format=pyaudio.paInt16, channels=2, rate=44100, output=True)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(('127.0.0.1', 8081))
        while True:
            data = sock.recv(4096)
            if not data: break
            stream.write(data)
            
            writer = audio_writer
            if is_recording and writer is not None:
                try: writer.writeframes(data)
                except Exception: pass
    except Exception:
        pass

# --- NEW: THE OBS-STYLE METRONOME THREAD ---
def recording_metronome():
    global is_recording, ffmpeg_process, latest_frame
    frame_duration = 1.0 / 60.0 # Perfect 60 FPS clock
    
    while True:
        if not is_recording:
            time.sleep(0.05)
            continue
            
        start_time = time.time()
        
        # Write exactly one frame per tick. If the network lags, 
        # it just writes the last known frame, perfectly preserving the timeline!
        proc = ffmpeg_process
        if proc is not None and proc.stdin and latest_frame is not None:
            try: proc.stdin.write(latest_frame.tobytes())
            except Exception: pass
                
        # Wait for the exact microsecond of the next tick
        elapsed = time.time() - start_time
        sleep_time = frame_duration - elapsed
        if sleep_time > 0:
            time.sleep(sleep_time)

threading.Thread(target=receive_video, daemon=True).start()
threading.Thread(target=receive_audio, daemon=True).start()
threading.Thread(target=recording_metronome, daemon=True).start() # Start the Metronome!

# --- RECORDING & UI LOGIC ---
def toggle_recording():
    global is_recording, ffmpeg_process, audio_writer, frame_width, frame_height
    global vid_path, aud_path
    
    if not is_recording:
        if frame_width == 0: return 
        desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        vid_path = os.path.join(desktop_path, f"Nexas_{timestamp}_temp.mp4")
        aud_path = os.path.join(desktop_path, f"Nexas_{timestamp}_temp.wav")
        
        cmd = [
            "ffmpeg", "-y", "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{frame_width}x{frame_height}", "-pix_fmt", "bgr24", "-r", "60",
            "-i", "-", "-c:v", "h264_videotoolbox", "-b:v", "30M", vid_path
        ]
        ffmpeg_process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        audio_writer = wave.open(aud_path, 'wb')
        audio_writer.setnchannels(2)
        audio_writer.setsampwidth(2)
        audio_writer.setframerate(44100)
        
        is_recording = True
        print(f"\n🔴 RECORDING: {timestamp}")
    else:
        is_recording = False
        if ffmpeg_process:
            ffmpeg_process.stdin.close()
            ffmpeg_process.wait()
            ffmpeg_process = None
        if audio_writer: 
            audio_writer.close()
            audio_writer = None
        
        print("\n🔄 Muxing pristine files... Please wait...")
        final_path = vid_path.replace("_temp.mp4", "_FINAL.mp4")
        try:
            subprocess.run(["ffmpeg", "-y", "-i", vid_path, "-i", aud_path, "-c:v", "copy", "-c:a", "aac", final_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            os.remove(vid_path)
            os.remove(aud_path)
            print(f"✅ SUCCESS! Hardware-accelerated video saved to Desktop!")
        except Exception as e: print(f"⚠️ Error merging files: {e}")

def send_quality(level):
    global current_quality
    current_quality = level
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(('127.0.0.1', 8082))
        s.sendall((level + "\n").encode())
        s.close()
        print(f"📡 Changed stream quality to: {level}")
    except: pass

def mouse_click(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:
        if 20 <= y <= 60:
            if 20 <= x <= 140: toggle_recording()
        elif 80 <= y <= 110:
            if 20 <= x <= 80: send_quality("MAX")
            elif 90 <= x <= 150: send_quality("MED")
            elif 160 <= x <= 220: send_quality("LOW")

cv2.namedWindow('Android USB Mirror')
cv2.setMouseCallback('Android USB Mirror', mouse_click)

timeout_counter = 0
has_connected = False

while True:
    try:
        latest_img = frame_queue.get(timeout=1)
        has_connected = True 
        timeout_counter = 0  
        frame_height, frame_width, _ = latest_img.shape
            
        display_img = latest_img.copy()
        
        # RECORD UI
        if is_recording:
            cv2.rectangle(display_img, (20, 20), (140, 60), (0, 0, 255), -1)
            cv2.putText(display_img, "STOP", (45, 47), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            cv2.circle(display_img, (170, 40), 10, (0, 0, 255), -1)
        else:
            cv2.rectangle(display_img, (20, 20), (140, 60), (0, 200, 0), -1)
            cv2.putText(display_img, "REC", (50, 47), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
            
        # QUALITY UI (Colors update dynamically based on what you clicked!)
        c_max = (0, 200, 0) if current_quality == "MAX" else (100, 100, 100)
        c_med = (0, 200, 0) if current_quality == "MED" else (100, 100, 100)
        c_low = (0, 200, 0) if current_quality == "LOW" else (100, 100, 100)
        
        cv2.rectangle(display_img, (20, 80), (80, 110), c_max, -1)
        cv2.putText(display_img, "MAX", (30, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        
        cv2.rectangle(display_img, (90, 80), (150, 110), c_med, -1)
        cv2.putText(display_img, "MED", (105, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        
        cv2.rectangle(display_img, (160, 80), (220, 110), c_low, -1)
        cv2.putText(display_img, "LOW", (170, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            
        cv2.imshow('Android USB Mirror', display_img)
        
    except queue.Empty:
        if has_connected:
            timeout_counter += 1
            if timeout_counter >= 5:
                if not is_recording:
                    print("\n📱 Phone disconnected! Auto-closing window safely...")
                    break
        continue 

    if cv2.waitKey(1) & 0xFF == ord('q'): 
        break

if is_recording: toggle_recording() 
cv2.destroyAllWindows()
print("Mirroring closed cleanly.")