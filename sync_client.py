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
import re
import unicodedata
import subprocess
from urllib.parse import quote

# --- SINGLE-INSTANCE LOCK (SOCKET MUTEX) + IPC GỌI CỬA SỔ CŨ LÊN ---
# Cổng khoá vừa để chặn chạy trùng, vừa là kênh để lần chạy sau hỏi bản đang chạy:
# "cửa sổ terminal của bạn là cái nào?" rồi đưa cửa sổ đó lên trước màn hình.
LOCK_PORT = 49512
WINDOW_TITLE_PREFIX = "LL PHOTOBOOTH SYNC CLIENT"
CONSOLE_TITLE = WINDOW_TITLE_PREFIX
_lock_socket = None

# ---- Win32 helpers (chỉ dùng ctypes có sẵn, không cần pywin32) ----
_win32 = None

def _get_win32():
    """Khai báo kiểu tham số Win32 một lần (bắt buộc trên Python 64-bit để HWND không bị cắt)."""
    global _win32
    if _win32 is not None or os.name != 'nt':
        return _win32
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    HWND = wintypes.HWND
    kernel32.GetConsoleWindow.restype = HWND
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
    for name, args, res in [
        ("IsWindow", [HWND], wintypes.BOOL),
        ("IsWindowVisible", [HWND], wintypes.BOOL),
        ("IsIconic", [HWND], wintypes.BOOL),
        ("ShowWindow", [HWND, ctypes.c_int], wintypes.BOOL),
        ("SetForegroundWindow", [HWND], wintypes.BOOL),
        ("BringWindowToTop", [HWND], wintypes.BOOL),
        ("GetForegroundWindow", [], HWND),
        ("GetWindow", [HWND, wintypes.UINT], HWND),
        ("GetWindowThreadProcessId", [HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
        ("AttachThreadInput", [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL),
        ("GetWindowTextLengthW", [HWND], ctypes.c_int),
        ("GetWindowTextW", [HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
        ("keybd_event", [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_void_p], None),
    ]:
        fn = getattr(user32, name)
        fn.argtypes = args
        fn.restype = res
    _win32 = (ctypes, wintypes, user32, kernel32)
    return _win32

def _get_console_hwnd():
    """HWND cửa sổ console của tiến trình hiện tại (0 nếu không có / không phải Windows)."""
    try:
        w = _get_win32()
        return (w[3].GetConsoleWindow() or 0) if w else 0
    except Exception:
        return 0

def _visible_console_window(hwnd):
    """Trả về cửa sổ thật người dùng nhìn thấy ứng với console hwnd.
    - CMD/PowerShell (conhost): chính là hwnd.
    - Windows Terminal: hwnd là cửa sổ giả (ẩn), cửa sổ thật là 'owner' của nó."""
    w = _get_win32()
    if not w or not hwnd:
        return 0
    user32 = w[2]
    if not user32.IsWindow(hwnd):
        return 0
    if not user32.IsWindowVisible(hwnd):
        owner = user32.GetWindow(hwnd, 4)  # GW_OWNER
        if owner and user32.IsWindowVisible(owner):
            return owner
    return hwnd

def _own_console_visible():
    """Lần chạy này có cửa sổ cho người dùng thấy không? (chạy ẩn qua run_hidden.vbs thì không)."""
    if os.name == 'nt':
        try:
            w = _get_win32()
            target = _visible_console_window(_get_console_hwnd())
            return bool(target and w[2].IsWindowVisible(target))
        except Exception:
            return False
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False

def set_console_title(title):
    """Đặt tiêu đề cửa sổ terminal để dễ nhận ra (và để tìm lại cửa sổ theo tiêu đề)."""
    if os.name == 'nt':
        try:
            _get_win32()[3].SetConsoleTitleW(title)
        except Exception:
            pass
    else:
        try:
            out = sys.__stdout__
            if out and out.isatty():
                out.write(f"\033]0;{title}\007")
                out.flush()
        except Exception:
            pass

def _find_windows_by_title(fragment, exclude=()):
    """Tìm các cửa sổ đang hiện có tiêu đề chứa `fragment` (dự phòng khi không lấy được HWND)."""
    w = _get_win32()
    if not w:
        return []
    ctypes, wintypes, user32, _ = w
    found = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        try:
            length = user32.GetWindowTextLengthW(hwnd)
            if length and user32.IsWindowVisible(hwnd) and hwnd not in exclude:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if fragment in buf.value:
                    found.append(hwnd)
        except Exception:
            pass
        return True

    ctypes.windll.user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found

def _bring_window_to_front_windows(hwnd):
    """Bung cửa sổ (nếu đang thu nhỏ/ẩn) và đưa lên trước màn hình.
    Windows chặn 'cướp focus', nên gắn tạm luồng nhập liệu vào cửa sổ đang foreground (AttachThreadInput)."""
    w = _get_win32()
    if not w or not hwnd:
        return False
    _, _, user32, kernel32 = w
    if not user32.IsWindow(hwnd):
        return False

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)   # SW_RESTORE
    elif not user32.IsWindowVisible(hwnd):
        user32.ShowWindow(hwnd, 5)   # SW_SHOW (cửa sổ đang bị ẩn do chạy ngầm qua VBS)

    def _try_focus():
        fg = user32.GetForegroundWindow()
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
        return user32.GetForegroundWindow() == hwnd

    if _try_focus():
        return True
    # Dự phòng: gõ phím Alt ảo để Windows cho phép đổi foreground. Chỉ làm khi cửa sổ của chính
    # lần chạy này đang ở trước, để không bấm nhầm Alt vào phần mềm chụp ảnh.
    if _own_console_visible():
        user32.keybd_event(0x12, 0, 0, None)   # VK_MENU down
        user32.keybd_event(0x12, 0, 2, None)   # VK_MENU up (KEYEVENTF_KEYUP)
        if _try_focus():
            return True
    # Không giành được focus thì ít nhất cửa sổ đã được bung ra / hiện lên
    return bool(user32.IsWindowVisible(hwnd))

def _bring_terminal_to_front_macos(pid):
    """macOS: tìm tab Terminal.app đang chạy PID cũ (theo tty) và đưa lên trước."""
    try:
        tty = subprocess.run(["ps", "-o", "tty=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        if not tty or tty.startswith("?"):
            return False
        tty_path = tty if tty.startswith("/dev/") else "/dev/" + tty
        script = f'''
if application "Terminal" is running then
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is "{tty_path}" then
          set selected of t to true
          set index of w to 1
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end tell
end if
return "notfound"
'''
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        return res.stdout.strip() == "ok"
    except Exception:
        return False

def _serve_focus_requests(sock):
    """Bản đang chạy: trả lời lần chạy sau thông tin cửa sổ terminal của mình."""
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            break
        try:
            with conn:
                conn.settimeout(2)
                req = conn.recv(64).strip()
                info = {"pid": os.getpid(), "hwnd": _get_console_hwnd() or 0, "title": CONSOLE_TITLE}
                conn.sendall(json.dumps(info).encode("utf-8"))
                if req == b"FOCUS":
                    _log = globals().get("log", print)
                    _log("[*] Có lần chạy mới -> đã đưa cửa sổ này lên trước. "
                         "Muốn ẩn thì THU NHỎ (minimize), ĐỪNG ĐÓNG: đóng cửa sổ sẽ dừng đồng bộ ảnh.")
        except Exception:
            pass

def _focus_existing_instance():
    """Lần chạy sau: hỏi bản đang chạy rồi đưa cửa sổ terminal của nó lên trước. Trả về True nếu thành công."""
    info = {}
    try:
        with socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=2) as c:
            c.settimeout(2)
            c.sendall(b"FOCUS\n")
            chunks = []
            while True:
                part = c.recv(4096)
                if not part:
                    break
                chunks.append(part)
        info = json.loads(b"".join(chunks).decode("utf-8") or "{}")
    except Exception:
        info = {}   # Bản cũ (chưa có IPC) không trả lời -> dùng tìm theo tiêu đề

    if os.name == 'nt':
        target = _visible_console_window(int(info.get("hwnd") or 0))
        if target and _bring_window_to_front_windows(target):
            return True
        own = _visible_console_window(_get_console_hwnd())
        for hwnd in _find_windows_by_title(WINDOW_TITLE_PREFIX, exclude=(own,)):
            if _bring_window_to_front_windows(hwnd):
                return True
        return False
    if sys.platform == 'darwin' and info.get("pid"):
        return _bring_terminal_to_front_macos(info["pid"])
    return False

def ensure_single_instance(port=LOCK_PORT):
    """Đảm bảo chỉ có duy nhất 1 tiến trình sync_client chạy ngầm.
    Tránh trường hợp người dùng click mở nhiều lần gây đơ máy và xung đột file.
    Lần chạy sau sẽ không chạy thêm mà đưa cửa sổ terminal của bản đang chạy lên trước màn hình."""
    global _lock_socket
    try:
        _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock_socket.bind(("127.0.0.1", port))
        _lock_socket.listen(5)
    except (socket.error, OSError):
        try:
            _lock_socket.close()
        except Exception:
            pass
        print("\n[CẢNH BÁO] Đã có một tiến trình sync_client.py đang chạy trên máy này!")
        print("           Không khởi động thêm tiến trình mới để tránh quá tải và đơ máy.")
        # Chạy ẩn (Startup / mmephoto start) thì không bật cửa sổ đè lên phần mềm chụp ảnh
        if not _own_console_visible():
            sys.exit(0)
        if _focus_existing_instance():
            print("[OK] Đã đưa cửa sổ của tiến trình đang chạy lên trước màn hình.\n")
            time.sleep(1)
        else:
            print("[!] Không tìm thấy cửa sổ của tiến trình đang chạy (có thể đang chạy ngầm từ bản cũ).")
            print("    Dùng 'mmephoto stop' rồi chạy lại nếu muốn xem log trực tiếp.\n")
            time.sleep(4)
        sys.exit(0)
    threading.Thread(target=_serve_focus_requests, args=(_lock_socket,),
                     name="focus-ipc", daemon=True).start()

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

if __name__ == "__main__":
    # Khóa đơn tiến trình NGAY ĐẦU (trước khi đọc config / ghi log): lần chạy sau chỉ
    # đưa cửa sổ của bản đang chạy lên trước rồi thoát, không đụng gì tới file.
    ensure_single_instance()

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
    CONSOLE_TITLE = f"{WINDOW_TITLE_PREFIX} - {BRANCH_ID} / {ROOM_ID}"
    set_console_title(CONSOLE_TITLE)
    sys.stdout = _Tee(sys.stdout, LOG_FILE)
    sys.stderr = sys.stdout

# Thư mục chụp có sẵn từ trước hay vừa được tạo mới (ổ ngoài/ổ mạng chưa kết nối -> tạo rỗng).
# Chỉ dọn processed_files.json khi thư mục có sẵn, tránh xoá nhầm cả danh sách rồi tải lại toàn bộ ảnh.
WATCH_FOLDER_EXISTED = os.path.isdir(WATCH_FOLDER)
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
    """Bỏ các file đã bị xoá khỏi máy khỏi danh sách đã xử lý, để file này không phình mãi theo thời gian.
    Chỉ dọn file nằm trong thư mục chụp, và chỉ khi thư mục chụp có sẵn từ trước khi script chạy."""
    if not WATCH_FOLDER_EXISTED:
        return 0
    with processed_files_lock:
        missing = [p for p in processed_files
                   if is_inside_folder(p, WATCH_FOLDER) and not os.path.exists(p)]
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
        # Với JPEG: giải mã thẳng ở 1/2, 1/4... độ phân giải (vẫn >= MAX_WIDTH mỗi chiều)
        # thay vì giải mã đủ 24MP rồi mới thu nhỏ -> nhanh ~3 lần, đỡ CPU máy chụp
        img.draft('RGB', (MAX_WIDTH, MAX_WIDTH))
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
    # Chuẩn hoá Unicode NFC: cùng một tên tiếng Việt gõ bằng bảng mã "dựng sẵn" hay "tổ hợp"
    # vẫn ra đúng một phiên trên server (server so sánh tên phiên theo từng byte)
    return unicodedata.normalize('NFC', parts[0]) if len(parts) >= 2 else "default"

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
        # Cấp số thứ tự và xếp hàng trong cùng một khoá: watchdog và luồng quét định kỳ có thể cùng thêm
        # file của một phiên; nếu tách rời, file số lớn có thể vào hàng trước file số nhỏ.
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

def natural_key(name):
    """'ảnh_2' đứng trước 'ảnh_10' (so số theo giá trị, không theo từng ký tự)."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', name)]

def capture_order_key(file_path):
    """Thứ tự chụp = thời điểm ảnh được ghi (mtime). Chép file sang thư mục khác vẫn giữ mtime gốc
    của máy ảnh. Trùng thời điểm (chụp liên tiếp trong cùng 1 giây) thì xếp theo số trong tên file."""
    try:
        mtime = int(os.path.getmtime(file_path))
    except OSError:
        mtime = 0
    return (mtime, natural_key(os.path.basename(file_path)))

def scan_existing_files(verbose=True):
    if verbose:
        log(f"[*] Đang quét tất cả thư mục & thư mục con trong {WATCH_FOLDER}...")
    found = []
    for root, dirs, files in os.walk(WATCH_FOLDER):
        if is_inside_folder(root, ARCHIVE_FOLDER):
            dirs[:] = []
            continue
        found.extend(os.path.join(root, f) for f in files)

    # Xếp theo phiên, rồi theo thứ tự chụp. Không sắp theo chữ cái: tên kiểu "_1, _2 ... _10"
    # sẽ ra "_1, _10, _11 ... _2".
    found.sort(key=lambda p: (session_for(p), capture_order_key(p)))
    count_queued = 0
    for file_path in found:
        if enqueue_file(file_path):
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
    # 1. Khóa đơn tiến trình: đã chạy ở đầu file (trước load_config)

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
