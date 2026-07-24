import os
import time
import requests
import json
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from PIL import Image
import io
import threading
import shutil
import uuid

# --- CONFIGURATION ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

default_config = {
    "server_url": "https://photo.llphotobooth.vn",
    "branch_id": "CN01",
    "room_id": "ROOM_01",
    "watch_folder": "./photos",
    "api_key": "YOUR_SECRET_API_KEY",
    "compress_quality": 80,
    "max_width": 1200
}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        print("=== CÀI ĐẶT LẦN ĐẦU ===")
        server_url = "https://photo.llphotobooth.vn"
        setup_code = input("Nhập Mã Cài Đặt (Setup Code) do Admin cấp: ").strip()
        watch_folder = input("Nhập đường dẫn thư mục gốc (Nhấn Enter để dùng './photos'): ").strip()
        
        if not watch_folder: watch_folder = "./photos"
        
        print("\nĐang xác thực Mã Cài Đặt với Server...")
        try:
            res = requests.post(f"{server_url}/api/setup-room", json={"setupCode": setup_code})
            if res.status_code == 200:
                data = res.json()
                print(f"[OK] Xác thực thành công! Chi nhánh: {data['branchId']}")
                print(f"Các phòng thuộc chi nhánh: {', '.join(data.get('rooms', []))}")
                
                room_id = input("Bạn đang cài đặt máy này cho Phòng nào? (Nhập đúng Tên phòng ở trên): ").strip()
                if room_id not in data.get('rooms', []):
                    print("[LỖI] Tên phòng không hợp lệ!")
                    exit(1)
                    
                # Tạo thư mục Archive (lưu trữ bản sao) tại thư mục CÀI ĐẶT
                archive_folder = os.path.join(BASE_DIR, data['branchId'], room_id)
                if not os.path.exists(archive_folder):
                    os.makedirs(archive_folder)
                
                print(f"[OK] Đã cấu hình theo dõi thư mục gốc: {watch_folder}")
                print(f"[OK] Ảnh gốc sẽ được copy sao lưu vào: {archive_folder}")
                
                config = {
                    "server_url": server_url,
                    "branch_id": data["branchId"],
                    "password": data["password"],
                    "room_id": room_id,
                    "watch_folder": watch_folder,
                    "compress_quality": 80,
                    "max_width": 1200
                }
            else:
                print(f"[LỖI] {res.json().get('error')}")
                print("Vui lòng chạy lại script và nhập đúng Mã Cài Đặt.")
                exit(1)
        except Exception as e:
            print(f"[LỖI] Không thể kết nối tới server: {e}")
            exit(1)
            
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
        print(f"[*] Đã lưu cấu hình vào {CONFIG_FILE}.")
        return config

    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

config = load_config()

SERVER_URL = config["server_url"].rstrip('/')
BRANCH_ID = config["branch_id"]
ROOM_ID = config.get("room_id", "")
WATCH_FOLDER = config["watch_folder"]
PASSWORD = config.get("password", "")
QUALITY = config["compress_quality"]
MAX_WIDTH = config["max_width"]
ARCHIVE_FOLDER = os.path.join(BASE_DIR, BRANCH_ID, ROOM_ID)

if not os.path.exists(WATCH_FOLDER):
    os.makedirs(WATCH_FOLDER)
    print(f"[*] Đã tạo thư mục theo dõi: {WATCH_FOLDER}")

print(f"==================================================")
print(f" LL PHOTOBOOTH - PC SYNC CLIENT (WEBP ONLY)")
print(f" Chi nhánh: {BRANCH_ID}")
print(f" Phòng: {ROOM_ID}")
print(f" Thư mục theo dõi: {WATCH_FOLDER}")
print(f" Máy chủ: {SERVER_URL}")
print(f"==================================================\n")

current_session = {}

def wait_for_file_stable(file_path, stable_seconds=2.0, poll_interval=0.5, max_wait=30):
    """Wait until file size stops changing for stable_seconds.
    Returns True if file is stable, False if timed out or file disappeared."""
    filename = os.path.basename(file_path)
    start = time.time()
    last_size = -1
    stable_since = None

    while time.time() - start < max_wait:
        try:
            if not os.path.exists(file_path):
                return False
            current_size = os.path.getsize(file_path)
        except OSError:
            time.sleep(poll_interval)
            continue

        if current_size == last_size and current_size > 0:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= stable_seconds:
                return True
        else:
            stable_since = None
            last_size = current_size

        time.sleep(poll_interval)

    print(f"    [CẢNH BÁO] File {filename} chờ quá {max_wait}s mà chưa ổn định.")
    return False


def process_and_upload(file_path, room_id, session_id):
    filename = os.path.basename(file_path)
    print(f"[>] Phát hiện file mới: {filename} — đang chờ ghi xong...")

    # ── BƯỚC 1: Chờ file ghi xong (File Stability Check) ──
    if not wait_for_file_stable(file_path):
        print(f"    [LỖI] File {filename} không ổn định hoặc đã biến mất. Bỏ qua.")
        return

    file_size_kb = os.path.getsize(file_path) / 1024
    print(f"    [OK] File ổn định ({file_size_kb:.0f} KB). Bắt đầu xử lý...")

    # ── BƯỚC 2: Đọc & nén ảnh thành WebP (retry nếu lỗi) ──
    max_retries = 5
    img = None
    for attempt in range(max_retries):
        try:
            img = Image.open(file_path)
            img.load()  # Force đọc toàn bộ pixel data để phát hiện file bị cắt
            break
        except (PermissionError, OSError, Exception) as e:
            if attempt < max_retries - 1:
                print(f"    [Retry {attempt+1}/{max_retries}] Chưa đọc được: {e}")
                time.sleep(1)
            else:
                print(f"    [LỖI] Không thể đọc file {filename} sau {max_retries} lần: {e}")
                return

    if img is None:
        return

    try:
        # Resize nếu ảnh quá to
        if img.width > MAX_WIDTH:
            ratio = MAX_WIDTH / img.width
            new_height = int(img.height * ratio)
            img = img.resize((MAX_WIDTH, new_height), Image.Resampling.LANCZOS)

        # Convert to RGB nếu cần
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        byte_arr = io.BytesIO()
        img.save(byte_arr, format='WEBP', quality=QUALITY)
        byte_arr.seek(0)

        webp_size_kb = byte_arr.getbuffer().nbytes / 1024
        print(f"    [OK] Nén WebP thành công: {file_size_kb:.0f} KB → {webp_size_kb:.0f} KB")

        # ── BƯỚC 3: Upload lên VPS (retry nếu lỗi mạng) ──
        upload_url = f"{SERVER_URL}/api/stream-upload/{BRANCH_ID}/{room_id}/{session_id}"

        files = {
            'image': (f"{os.path.splitext(filename)[0]}.webp", byte_arr, 'image/webp')
        }
        headers = {}
        if PASSWORD:
            headers['Authorization'] = f"Bearer {PASSWORD}"

        upload_success = False
        for attempt in range(3):
            try:
                byte_arr.seek(0)
                print(f"    -> Đang upload WebP lên VPS... (lần {attempt+1})")
                start_time = time.time()
                response = requests.post(upload_url, files=files, headers=headers, timeout=30)

                if response.status_code == 200:
                    print(f"    [OK] Upload thành công ({time.time() - start_time:.2f}s)")
                    upload_success = True
                    break
                else:
                    print(f"    [LỖI] Upload thất bại (HTTP {response.status_code}): {response.text}")
            except requests.exceptions.RequestException as e:
                print(f"    [LỖI] Lỗi mạng (lần {attempt+1}/3): {e}")
                if attempt < 2:
                    time.sleep(2)

        # ── BƯỚC 4: Lưu bản nén WebP sang Archive ──
        if upload_success:
            try:
                session_archive_dir = os.path.join(ARCHIVE_FOLDER, session_id)
                if not os.path.exists(session_archive_dir):
                    os.makedirs(session_archive_dir)

                webp_filename = f"{os.path.splitext(filename)[0]}.webp"
                dest_path = os.path.join(session_archive_dir, webp_filename)

                byte_arr.seek(0)
                with open(dest_path, "wb") as f:
                    f.write(byte_arr.read())

                print(f"    [OK] Đã lưu bản nén WebP vào: {dest_path}")
            except Exception as e:
                print(f"    [CẢNH BÁO] Không thể lưu bản WebP {filename}: {str(e)}")

    except Exception as e:
        print(f"    [LỖI] Xử lý file {filename} thất bại: {str(e)}")


def scan_existing_files():
    print(f"[*] Đang quét tất cả thư mục & thư mục con trong {WATCH_FOLDER}...")
    for root, dirs, files in os.walk(WATCH_FOLDER):
        if root.startswith(ARCHIVE_FOLDER):
            continue
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in ['.jpg', '.jpeg', '.png', '.cr2', '.raw']:
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, WATCH_FOLDER)
                parts = rel_path.split(os.sep)
                if len(parts) >= 2:
                    session_id = parts[0]
                else:
                    session_id = "default"
                t = threading.Thread(target=process_and_upload, args=(file_path, ROOM_ID, session_id))
                t.start()

class PhotoHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
            
        file_path = event.src_path
        
        # Bỏ qua nếu file nằm trong thư mục Archive (chống lặp vô tận nếu cài chung ổ)
        if file_path.startswith(ARCHIVE_FOLDER):
            return
            
        filename = os.path.basename(file_path)
        ext = os.path.splitext(filename)[1].lower()
        
        if ext in ['.jpg', '.jpeg', '.png', '.cr2', '.raw']:
            rel_path = os.path.relpath(file_path, WATCH_FOLDER)
            parts = rel_path.split(os.sep)
            
            # Supports structure: WATCH_FOLDER / SESSION_ID / SUBDIR / file.jpg
            if len(parts) >= 2:
                session_id = parts[0]
            else:
                session_id = "default"
                
            # Chạy thread riêng để không block file system event
            t = threading.Thread(target=process_and_upload, args=(file_path, ROOM_ID, session_id))
            t.start()

if __name__ == "__main__":
    scan_existing_files()
    event_handler = PhotoHandler()
    observer = Observer()
    observer.schedule(event_handler, path=WATCH_FOLDER, recursive=True)
    observer.start()
    
    print(f"[*] Đang lắng nghe ảnh mới tại {WATCH_FOLDER}...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
