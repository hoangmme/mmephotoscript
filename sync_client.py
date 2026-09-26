# -*- coding: utf-8 -*-
import os
import sys
import time
import socket
import requests
import json
from queue import Queue
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from PIL import Image
import io
import threading
import shutil
import uuid
from urllib.parse import quote

# --- SINGLE-INSTANCE LOCK (SOCKET MUTEX) ---
_lock_socket = None

def ensure_single_instance(port=49512):
    """Đảm bảo chỉ có duy nhất 1 tiến trình sync_client chạy ngầm.
    Tránh trường hợp người dùng click mở nhiều lần gây đơ máy và xung đột file."""
    global _lock_socket
    try:
        _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock_socket.bind(("127.0.0.1", port))
    except (socket.error, OSError):
        print("\n[CẢNH BÁO] Đã có một tiến trình sync_client.py đang chạy ngầm trên máy này!")
        print("           Không khởi động thêm tiến trình mới để tránh quá tải và đơ máy.\n")
        sys.exit(0)

def set_low_process_priority():
    """Hạ độ ưu tiên CPU trên Windows (BELOW_NORMAL) để không làm giật chuột hay đơ phần mềm chụp ảnh."""
    if os.name == 'nt':
        try:
            import ctypes
            # BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
            ctypes.windll.kernel32.SetPriorityClass(
                ctypes.windll.kernel32.GetCurrentProcess(),
                0x00004000
            )
        except Exception:
            pass

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
# Đường dẫn tương đối tính theo thư mục cài đặt, không phụ thuộc thư mục đang đứng khi chạy
if not os.path.isabs(WATCH_FOLDER):
    WATCH_FOLDER = os.path.normpath(os.path.join(BASE_DIR, WATCH_FOLDER))
PASSWORD = config.get("password", "")
QUALITY = config.get("compress_quality", 80)
MAX_WIDTH = config.get("max_width", 1200)
ARCHIVE_FOLDER = os.path.join(BASE_DIR, BRANCH_ID, ROOM_ID)
# Số luồng upload song song (vừa đủ nhanh, không làm nghẽn mạng/CPU máy chụp)
UPLOAD_WORKERS = max(1, int(config.get("upload_workers", 3)))
# Chu kỳ quét lại thư mục (giây): bắt các file watchdog bỏ sót và thử lại file upload lỗi
RESCAN_INTERVAL = max(10, int(config.get("rescan_interval", 30)))
# Chỉ đồng bộ ảnh trình duyệt hiển thị được. File RAW (.cr2/.nef/.arw/.raw...) KHÔNG upload:
# web không hiển thị được (ô ảnh hỏng) và server ký URL upload với kiểu image/jpeg.
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp')
LOG_FILE = os.path.join(BASE_DIR, "sync_client.log")

class _Tee:
    """Ghi log ra cả console và file (script chạy ẩn nên không xem được console)."""
    def __init__(self, stream, path):
        self.stream = stream
        self.lock = threading.Lock()
        try:
            if os.path.exists(path) and os.path.getsize(path) > 5 * 1024 * 1024:
                os.replace(path, path + ".old")
            self.file = open(path, 'a', encoding='utf-8')
        except Exception:
            self.file = None

    def write(self, data):
        with self.lock:
            if self.stream:
                try:
                    self.stream.write(data)
                except Exception:
                    pass
            if self.file:
                try:
                    self.file.write(data)
                    self.file.flush()
                except Exception:
                    pass

    def flush(self):
        if self.stream:
            try:
                self.stream.flush()
            except Exception:
                pass

if __name__ == "__main__":
    sys.stdout = _Tee(sys.stdout, LOG_FILE)
    sys.stderr = sys.stdout

if not os.path.exists(WATCH_FOLDER):
    os.makedirs(WATCH_FOLDER)
    print(f"[*] Đã tạo thư mục theo dõi: {WATCH_FOLDER}")

print(f"==================================================")
print(f" LL PHOTOBOOTH - PC SYNC CLIENT (OPTIMIZED QUEUE)")
print(f" Chi nhánh: {BRANCH_ID}")
print(f" Phòng: {ROOM_ID}")
print(f" Thư mục theo dõi: {WATCH_FOLDER}")
print(f" Máy chủ: {SERVER_URL}")
print(f"==================================================\n")

def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)

# --- AN TOÀN ĐƯỜNG DẪN & DATABASE FILE ĐÃ XỬ LÝ ---
PROCESSED_DB_FILE = os.path.join(BASE_DIR, "processed_files.json")

def norm_path(path):
    return os.path.normcase(os.path.abspath(path))

def is_inside_folder(child_path, parent_path):
    """Kiểm tra child_path có nằm trong parent_path không (chuẩn hóa an toàn trên Windows/Linux)."""
    try:
        child = norm_path(child_path)
        parent = norm_path(parent_path)
        return os.path.commonpath([child, parent]) == parent
    except Exception:
        return False

def load_processed_files():
    for path in (PROCESSED_DB_FILE, PROCESSED_DB_FILE + ".bak"):
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return set(norm_path(p) for p in json.load(f))
        except Exception as e:
            print(f"[CẢNH BÁO] Không đọc được {path}: {e}")
    return set()

def save_processed_files():
    """Ghi atomic (file tạm + replace) để file DB không bao giờ bị hỏng giữa chừng.
    Phải gọi khi đang giữ processed_files_lock."""
    tmp_path = PROCESSED_DB_FILE + ".tmp"
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(sorted(processed_files), f, indent=2)
        if os.path.exists(PROCESSED_DB_FILE):
            shutil.copyfile(PROCESSED_DB_FILE, PROCESSED_DB_FILE + ".bak")
        os.replace(tmp_path, PROCESSED_DB_FILE)
    except Exception as e:
        print(f"[CẢNH BÁO] Không thể lưu processed_files.json: {e}")

processed_files = load_processed_files()
processed_files_lock = threading.Lock()

def prune_processed_files():
    """Bỏ các file đã bị xoá khỏi máy khỏi danh sách đã xử lý, để file này không phình mãi theo thời gian."""
    with processed_files_lock:
        missing = [p for p in processed_files if not os.path.exists(p)]
        if missing:
            processed_files.difference_update(missing)
            save_processed_files()
    return len(missing)

# File đang nằm trong hàng đợi hoặc đang upload (chống xếp hàng trùng)
queued_files = set()
# File upload lỗi: abs_path -> (số lần lỗi, thời điểm được thử lại)
failed_files = {}
queue_state_lock = threading.Lock()

def is_file_processed(file_path):
    with processed_files_lock:
        return norm_path(file_path) in processed_files

def mark_processed(abs_path):
    with processed_files_lock:
        processed_files.add(abs_path)
        save_processed_files()

def wait_for_file_stable(file_path, stable_seconds=2.0, poll_interval=0.5, max_wait=120):
    """Chờ file ghi xong hoàn toàn trước khi đọc."""
    filename = os.path.basename(file_path)
    try:
        st = os.stat(file_path)
        # Trên Windows, st_ctime là thời điểm TẠO file. Khi copy file, mtime được giữ nguyên
        # từ file gốc (cũ) nhưng ctime là lúc copy -> phải dùng cả hai, nếu không sẽ đọc file đang copy dở.
        last_change = max(st.st_mtime, st.st_ctime)
        if st.st_size > 0 and (time.time() - last_change) >= stable_seconds:
            return True
    except OSError:
        return False

    start = time.time()
    last_sig = None
    stable_since = None

    while time.time() - start < max_wait:
        try:
            st = os.stat(file_path)
            sig = (st.st_size, st.st_mtime)
        except OSError:
            if not os.path.exists(file_path):
                return False
            time.sleep(poll_interval)
            continue

        if sig == last_sig and st.st_size > 0:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= stable_seconds:
                return True
        else:
            stable_since = None
            last_sig = sig

        time.sleep(poll_interval)

    log(f"    [CẢNH BÁO] {filename}: chờ quá {max_wait}s mà file chưa ghi xong.")
    return False

# --- HTTP ---
_thread_local = threading.local()

def http():
    """Mỗi luồng một Session riêng để tái sử dụng kết nối (keep-alive), nhanh hơn tạo kết nối mới mỗi request."""
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        _thread_local.session = s
    return s

def url_segment(value):
    """Mã hoá 1 đoạn đường dẫn URL. Tên thư mục có '#', '?', '%' mà không mã hoá sẽ làm URL bị cắt
    (vd 'khach #3' thành 'khach ') -> ảnh lên R2 nhưng server ghi nhận sai phiên, web hiện ảnh hỏng."""
    return quote(str(value), safe='')

def server_headers():
    return {'Authorization': f"Bearer {PASSWORD}"} if PASSWORD else {}

def post_json_with_retry(url, payload, label, attempts=3, timeout=15):
    """POST tới server, retry khi lỗi mạng / HTTP 5xx / 429. Trả về response hoặc None."""
    for attempt in range(1, attempts + 1):
        try:
            r = http().post(url, json=payload, headers=server_headers(), timeout=timeout)
            if r.ok:
                return r
            log(f"    [LỖI] {label}: HTTP {r.status_code} (lần {attempt}/{attempts}): {r.text[:200]}")
            if r.status_code < 500 and r.status_code != 429:
                return None  # Lỗi 4xx (sai dữ liệu) thì thử lại cũng vô ích
        except requests.exceptions.RequestException as e:
            log(f"    [LỖI] {label}: lỗi mạng (lần {attempt}/{attempts}): {e}")
        if attempt < attempts:
            time.sleep(2 * attempt)
    return None

def upload_to_r2(put_url, filename, data=None, file_path=None, content_type='application/octet-stream'):
    """PUT lên R2 qua pre-signed URL. Nếu truyền file_path thì stream thẳng từ ổ cứng, không nạp cả file vào RAM.
    Không gửi header Authorization (sẽ làm hỏng chữ ký của pre-signed URL)."""
    if not put_url:
        log(f"    [LỖI R2] {filename}: server không trả về URL upload.")
        return False
    for attempt in range(1, 4):
        try:
            if file_path:
                with open(file_path, 'rb') as f:
                    r = http().put(put_url, data=f, headers={'Content-Type': content_type}, timeout=(10, 120))
            else:
                r = http().put(put_url, data=data, headers={'Content-Type': content_type}, timeout=(10, 120))
            if r.ok:
                return True
            log(f"    [LỖI R2] {filename}: HTTP {r.status_code} (lần {attempt}/3): {r.text[:200]}")
        except (requests.exceptions.RequestException, OSError) as e:
            log(f"    [LỖI R2] {filename}: lỗi mạng (lần {attempt}/3): {e}")
        if attempt < 3:
            time.sleep(2 * attempt)
    return False

def make_thumbnail(file_path):
    from PIL import ImageOps
    with Image.open(file_path) as img:
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        # Đảm bảo hệ màu RGB/RGBA để tương thích với WebP
        if img.mode not in ('RGB', 'RGBA'):
            img = img.convert('RGB')
        if img.width > MAX_WIDTH:
            new_h = int(img.height * MAX_WIDTH / float(img.width))
            resample_filter = getattr(getattr(Image, 'Resampling', Image), 'BICUBIC', Image.BICUBIC)
            img = img.resize((MAX_WIDTH, new_h), resample_filter)
        thumb_io = io.BytesIO()
        # method=3: cân bằng giữa tốc độ nén và CPU
        img.save(thumb_io, format="WEBP", quality=QUALITY, method=3)
        return thumb_io.getvalue()

def process_and_upload(file_path, room_id, session_id, seq=None):
    """Trả về True nếu file đã được xử lý xong (hoặc không cần xử lý nữa), False nếu cần thử lại sau."""
    abs_path = norm_path(file_path)
    if is_file_processed(file_path):
        return True

    filename = os.path.basename(file_path)
    tag = f"[{session_id}/{filename}]"
    log(f"[>] {tag} Phát hiện file — đang kiểm tra ghi file...")

    # ── BƯỚC 1: Chờ file ghi xong ──
    if not wait_for_file_stable(file_path):
        if not os.path.exists(file_path):
            log(f"    {tag} File đã biến mất. Bỏ qua.")
            return True
        return False

    try:
        file_size_kb = os.path.getsize(file_path) / 1024
    except OSError:
        return not os.path.exists(file_path)

    ext = os.path.splitext(filename)[1].lower()
    mime_type = {'.png': 'image/png', '.webp': 'image/webp',
                 '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}.get(ext, 'application/octet-stream')

    # ── BƯỚC 2: Tạo thumbnail ──
    thumb_bytes = None
    thumb_name = f"{os.path.splitext(filename)[0]}_thumb.webp"
    if not filename.startswith('00_frame') and ext in ('.jpg', '.jpeg', '.png', '.webp'):
        try:
            thumb_bytes = make_thumbnail(file_path)
        except Exception as e:
            log(f"    [CẢNH BÁO] {tag} Không thể tạo thumbnail: {e}")

    # ── BƯỚC 3: Xin pre-signed URL ──
    filenames_to_request = [filename] + ([thumb_name] if thumb_bytes else [])
    res = post_json_with_retry(f"{SERVER_URL}/api/get-presigned-urls", {
        "branch": BRANCH_ID,
        "room": room_id,
        "session": session_id,
        "filenames": filenames_to_request
    }, f"{tag} Lấy URL chữ ký")
    if res is None:
        return False
    try:
        urls_data = res.json().get('urls', {}) or {}
    except ValueError:
        log(f"    [LỖI] {tag} Server trả về dữ liệu không hợp lệ: {res.text[:200]}")
        return False

    # ── BƯỚC 4: Upload lên R2 ──
    log(f"    {tag} Đang đẩy ảnh gốc ({file_size_kb:.0f} KB) lên R2...")
    if not upload_to_r2(urls_data.get(filename), filename, file_path=file_path, content_type=mime_type):
        return False

    thumb_ok = False
    if thumb_bytes and urls_data.get(thumb_name):
        thumb_ok = upload_to_r2(urls_data.get(thumb_name), thumb_name, data=thumb_bytes, content_type='image/webp')
        if not thumb_ok:
            log(f"    [CẢNH BÁO] {tag} Upload thumbnail thất bại (ảnh gốc vẫn OK).")

    # ── BƯỚC 5: Báo server. Nếu server không ghi nhận, ảnh sẽ KHÔNG hiện trên web -> phải thử lại ──
    if seq is not None:
        order_wait_turn(session_id, seq)  # giữ đúng thứ tự ảnh trên web
    notify_url = f"{SERVER_URL}/api/notify-r2-upload/{url_segment(BRANCH_ID)}/{url_segment(room_id)}/{url_segment(session_id)}"
    if post_json_with_retry(notify_url, {"filename": filename}, f"{tag} Thông báo server") is None:
        return False
    if thumb_ok:
        post_json_with_retry(notify_url, {"filename": thumb_name}, f"{tag} Thông báo thumbnail")

    # ── BƯỚC 6: Lưu bản sao ảnh gốc sang Archive ──
    try:
        session_archive_dir = os.path.join(ARCHIVE_FOLDER, session_id)
        os.makedirs(session_archive_dir, exist_ok=True)
        shutil.copy2(file_path, os.path.join(session_archive_dir, filename))
    except Exception as e:
        log(f"    [CẢNH BÁO] {tag} Không thể lưu bản sao: {e}")

    # Chỉ đánh dấu hoàn thành khi đã upload + server ghi nhận thành công
    mark_processed(abs_path)
    log(f"    [OK] {tag} Hoàn tất.")
    return True

# --- GIỮ ĐÚNG THỨ TỰ ẢNH ---
# Upload lên R2 chạy song song (nhanh), nhưng bước báo server (quyết định thứ tự ảnh trên web)
# chạy theo đúng thứ tự file được phát hiện trong từng phiên. Không có bước này, 3 luồng song song
# làm ảnh hiện lộn xộn (01, 03, 02...). Chờ tối đa ORDER_WAIT giây để 1 file lỗi không chặn cả phiên.
ORDER_WAIT = 60
_order_cv = threading.Condition()
_order_next_seq = {}     # session -> số thứ tự sẽ cấp tiếp
_order_pending = {}      # session -> các số thứ tự chưa xong

def order_ticket(session_id):
    with _order_cv:
        seq = _order_next_seq.get(session_id, 0)
        _order_next_seq[session_id] = seq + 1
        _order_pending.setdefault(session_id, set()).add(seq)
        return seq

def order_wait_turn(session_id, seq):
    deadline = time.time() + ORDER_WAIT
    with _order_cv:
        while True:
            pending = _order_pending.get(session_id, set())
            if not any(s < seq for s in pending):
                return
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            _order_cv.wait(timeout=min(remaining, 1.0))

def order_done(session_id, seq):
    with _order_cv:
        _order_pending.get(session_id, set()).discard(seq)
        _order_cv.notify_all()

# --- HÀNG ĐỢI + NHIỀU WORKER ---
upload_queue = Queue()

def session_for(file_path):
    parts = os.path.normpath(os.path.relpath(file_path, WATCH_FOLDER)).split(os.sep)
    return parts[0] if len(parts) >= 2 else "default"

def enqueue_file(file_path):
    """Thêm file vào hàng đợi nếu chưa xử lý, chưa nằm trong hàng đợi và không đang trong thời gian chờ thử lại."""
    if os.path.splitext(file_path)[1].lower() not in IMAGE_EXTS:
        return False
    abs_path = norm_path(file_path)
    if is_inside_folder(abs_path, ARCHIVE_FOLDER):
        return False
    with processed_files_lock:
        if abs_path in processed_files:
            return False
    with queue_state_lock:
        if abs_path in queued_files:
            return False
        fail = failed_files.get(abs_path)
        if fail and time.time() < fail[1]:
            return False
        queued_files.add(abs_path)
    session_id = session_for(file_path)
    upload_queue.put((file_path, ROOM_ID, session_id, order_ticket(session_id)))
    return True

def worker_loop():
    while True:
        item = upload_queue.get()
        if item is None:
            break
        file_path, room_id, session_id, seq = item
        abs_path = norm_path(file_path)
        ok = False
        try:
            ok = process_and_upload(file_path, room_id, session_id, seq)
        except Exception as e:
            log(f"    [LỖI] Xử lý thất bại {file_path}: {e}")
        finally:
            order_done(session_id, seq)
            with queue_state_lock:
                queued_files.discard(abs_path)
                if ok:
                    failed_files.pop(abs_path, None)
                else:
                    # Backoff tăng dần: 15s, 30s, 60s ... tối đa 5 phút. Lần quét định kỳ sau sẽ tự thử lại.
                    count = failed_files.get(abs_path, (0, 0))[0] + 1
                    delay = min(15 * (2 ** (count - 1)), 300)
                    failed_files[abs_path] = (count, time.time() + delay)
                    log(f"    [THỬ LẠI] {os.path.basename(file_path)}: lỗi lần {count}, sẽ thử lại sau {delay}s.")
            upload_queue.task_done()

def scan_existing_files(verbose=True):
    if verbose:
        log(f"[*] Đang quét tất cả thư mục & thư mục con trong {WATCH_FOLDER}...")
    count_queued = 0
    for root, dirs, files in os.walk(WATCH_FOLDER):
        if is_inside_folder(root, ARCHIVE_FOLDER):
            dirs[:] = []
            continue
        dirs.sort()
        for file in sorted(files):
            if enqueue_file(os.path.join(root, file)):
                count_queued += 1

    if count_queued > 0:
        log(f"[*] Đã thêm {count_queued} file cần đồng bộ vào hàng đợi.")
    elif verbose:
        log(f"[*] Không có file mới nào cần xử lý trong {WATCH_FOLDER}.")

def rescan_loop():
    """Watchdog trên Windows có thể RỚT sự kiện khi nhiều file được ghi/copy cùng lúc (tràn buffer).
    Quét lại định kỳ đảm bảo không file nào bị bỏ sót, đồng thời thử lại các file upload lỗi."""
    while True:
        time.sleep(RESCAN_INTERVAL)
        try:
            scan_existing_files(verbose=False)
        except Exception as e:
            log(f"[LỖI] Quét định kỳ thất bại: {e}")

class PhotoHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            enqueue_file(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            enqueue_file(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            enqueue_file(event.dest_path)

if __name__ == "__main__":
    # 1. Khóa đơn tiến trình (ngăn chặn chạy 2-3 instance song song)
    ensure_single_instance()

    # 2. Hạ độ ưu tiên CPU để máy tính không bị đơ giật ứng dụng chụp ảnh
    set_low_process_priority()

    # 3. Khởi chạy các worker upload song song
    for i in range(UPLOAD_WORKERS):
        threading.Thread(target=worker_loop, name=f"upload-{i+1}", daemon=True).start()

    # 4. Theo dõi thư mục ảnh mới (bật TRƯỚC khi quét để không lọt file tạo ra trong lúc quét)
    observer = Observer()
    observer.schedule(PhotoHandler(), path=WATCH_FOLDER, recursive=True)
    observer.start()

    # 5. Quét các file đã có sẵn + quét lại định kỳ
    removed = prune_processed_files()
    if removed:
        log(f"[*] Đã dọn {removed} file không còn trên máy khỏi processed_files.json.")
    scan_existing_files()
    threading.Thread(target=rescan_loop, name="rescan", daemon=True).start()

    log(f"[*] Đang lắng nghe ảnh mới tại {WATCH_FOLDER} ({UPLOAD_WORKERS} luồng upload, quét lại mỗi {RESCAN_INTERVAL}s)...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
