# PROJECT: LL Photobooth Sync Client | Tự động đồng bộ ảnh photobooth lên VPS

- **Strategic Decisions:**
  - [Reversible] Quản lý đơn tiến trình (Single-Instance) & IPC: Tiếp tục sử dụng Socket Mutex trên cổng localhost `127.0.0.1:49512`, bổ sung IPC server nhẹ để gửi PID, HWND giữa các instance.
  - [Reversible] Điều khiển cửa sổ Terminal: Dùng Win32 API thuần qua `ctypes` (`user32`, `kernel32`) trên Windows và AppleScript (`osascript`) trên macOS để tìm và đưa cửa sổ tiến trình cũ lên foreground khi chạy lần 2. Không cài thêm dependency ngoài (`pywin32`).

- **Operational Constraints:**
  - Máy client photobooth chạy Windows 10/11 (CMD, PowerShell, Windows Terminal, hoặc chạy ẩn qua VBS). Cần hỗ trợ tốt cả trường hợp cửa sổ đang bị Minimize hoặc đang ẩn.
  - Giữ script nhẹ và chạy mượt mà, không giật lag phần mềm chụp ảnh của booth.

- **Status:**
  - Done: Triển khai thành công tính năng "chạy lần 2 tự đưa cửa sổ terminal của instance cũ lên trước màn hình" trong `sync_client.py` (commit `9814b61`).
  - Done: Sửa lỗi `mmephoto stop` bị lặp vô tận trên Windows (commit `eb9fd83`): thêm `.gitattributes` ép CRLF cho file `.bat`, sửa lệnh PowerShell lọc theo tên tiến trình (`python.exe`/`wscript.exe`), và cách ly tiến trình `git pull` khi update.
  - Current Focus: Kiểm thử và theo dõi thực tế trên máy Client Windows khi vận hành.
  - Lưu ý: Instance chạy ẩn (VBS) sẽ được hiển thị khi chạy lần 2 -> đóng cửa sổ này sẽ dừng đồng bộ (đã bổ sung cảnh báo hướng dẫn nhân viên thu nhỏ/minimize thay vì đóng). Lần chạy ngầm thứ 2 (Startup/`mmephoto start`) không tự kích hoạt cửa sổ để tránh đè lên app chụp ảnh.

- **Flags (Drift/Critical/Entropy):**
  - Không có.

- **Cost/Impact Alerts:**
  - Không có (Thay đổi gói gọn trong hàm xử lý đơn tiến trình của `sync_client.py`, [Reversible], không thay đổi format config hay data).

- **Registry & Recovery:**
  - `sync_client.py`: Script đồng bộ ảnh client.
  - `mmephoto.bat`: Menu và lệnh CLI quản trị trên Windows.
  - `install.ps1`: Script cài đặt tự động PowerShell.
