# PROJECT: LL Photobooth Sync Client | Tự động đồng bộ ảnh photobooth lên VPS

- **Strategic Decisions:**
  - [Reversible] Quản lý đơn tiến trình (Single-Instance) & IPC: Tiếp tục sử dụng Socket Mutex trên cổng localhost `127.0.0.1:49512`, bổ sung IPC server nhẹ để gửi PID, HWND giữa các instance.
  - [Reversible] Điều khiển cửa sổ Terminal: Dùng Win32 API thuần qua `ctypes` (`user32`, `kernel32`) trên Windows và AppleScript (`osascript`) trên macOS để tìm và đưa cửa sổ tiến trình cũ lên foreground khi chạy lần 2. Không cài thêm dependency ngoài (`pywin32`).

- **Operational Constraints:**
  - Máy client photobooth chạy Windows 10/11 (CMD, PowerShell, Windows Terminal, hoặc chạy ẩn qua VBS). Cần hỗ trợ tốt cả trường hợp cửa sổ đang bị Minimize hoặc đang ẩn.
  - Giữ script nhẹ và chạy mượt mà, không giật lag phần mềm chụp ảnh của booth.

- **Status:**
  - Done: Pull code mới nhất (`1ac3c78`). Triển khai "lần chạy 2 đưa terminal cũ lên trước" trong `sync_client.py` (IPC qua cổng 49512, Win32 ctypes, fallback tìm theo tiêu đề, macOS Terminal.app). Khoá đơn tiến trình chuyển lên trước `load_config`. Đã test IPC trên macOS.
  - Current Focus: Chờ test thực tế trên máy Windows (CMD, Windows Terminal, cửa sổ minimize, chạy ẩn VBS).
  - Next: Commit/push khi người dùng xác nhận.
  - Lưu ý: Instance chạy ẩn (VBS) sẽ bị hiện cửa sổ khi gọi lên -> đóng cửa sổ = dừng đồng bộ (đã in cảnh báo trong log). Lần chạy ẩn thứ 2 (Startup/`mmephoto start`) KHÔNG bật cửa sổ để không đè phần mềm chụp.

- **Flags (Drift/Critical/Entropy):**
  - Không có.

- **Cost/Impact Alerts:**
  - Không có (Thay đổi gói gọn trong hàm xử lý đơn tiến trình của `sync_client.py`, [Reversible], không thay đổi format config hay data).

- **Registry & Recovery:**
  - `sync_client.py`: Script đồng bộ ảnh client.
  - `mmephoto.bat`: Menu và lệnh CLI quản trị trên Windows.
  - `install.ps1`: Script cài đặt tự động PowerShell.
