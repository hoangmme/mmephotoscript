# LL Photobooth - Sync Client

Bộ công cụ đồng bộ ảnh tự động (Sync Client) dành riêng cho các máy chụp hình (Client PC) của hệ thống LL Photobooth.

Repository này chứa các script cực nhẹ (chỉ bao gồm logic đồng bộ ảnh, không chứa giao diện Web) nhằm tối ưu tài nguyên cho máy chụp.

## 🚀 Tính Năng Chính
- **Tự động theo dõi thư mục (Watch Folder):** Phát hiện ảnh mới ngay khi phần mềm chụp hình (DSLR Booth, v.v.) vừa lưu file.
- **File Stability Check:** Chỉ bắt đầu xử lý khi file ảnh đã được ghi hoàn tất xuống ổ cứng (chống lỗi file do chưa lưu xong).
- **Nén & Tối ưu:** Tự động nén ảnh gốc sang định dạng `WebP` ngay trên RAM để tối ưu tốc độ mạng.
- **Đồng bộ thời gian thực:** Upload ảnh siêu tốc lên VPS.
- **Chạy ngầm (Background Service):** Khởi động cùng Windows, chạy ẩn không gây vướng víu giao diện cho khách.

---

## 🛠 Hướng Dẫn Cài Đặt (Cho máy Client)

Mở cửa sổ **PowerShell** (nhấn chuột phải vào Start chọn Windows PowerShell) và chạy dòng lệnh sau để cài đặt tự động:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force; iex ((New-Object System.Net.WebClient).DownloadString('https://raw.githubusercontent.com/hoangmme/mmephotoscript/main/install.ps1'))
```

## ⌨️ Cẩm Nang Lệnh (CLI)

Sau khi cài đặt xong, bạn có thể mở `cmd` hoặc `PowerShell` ở bất kỳ đâu và sử dụng lệnh `mmephoto` để điều khiển:

- `mmephoto setup` : Mở giao diện để nhập Mã Cài Đặt (Setup Code) và cấu hình thư mục ảnh.
- `mmephoto start` : Bật tiến trình đồng bộ ảnh chạy ngầm.
- `mmephoto stop` : Tắt tiến trình đồng bộ ảnh chạy ngầm.
- `mmephoto update` : Cập nhật phiên bản code đồng bộ mới nhất từ Github.
- `mmephoto reset` : Xóa cấu hình hiện tại để đăng ký lại Phòng khác.
- `mmephoto help` : Xem danh sách lệnh.
