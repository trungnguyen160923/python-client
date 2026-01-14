# Python ADB Client Tool

Tool này là một client chạy nền, kết nối tới server điều khiển và thực thi lệnh ADB trên các thiết bị Android được nối vào máy tính.

## Chức năng chính

- Đọc `room_hash` từ file `config.txt` (hoặc yêu cầu người dùng nhập lần đầu), dùng để định danh "phòng" trên server.
- Định kỳ báo cáo danh sách thiết bị Android đang kết nối ADB lên API:
  - Tự động lấy danh sách từ lệnh `adb devices`.
  - Gửi `serial`, `status` (device/unauthorized/offline/...), `device_type` (android).
- Định kỳ gọi API subscribe để nhận danh sách lệnh cho từng thiết bị:
  - Lệnh start game (chạy `nat.myc.test/androidx.test.runner.AndroidJUnitRunner`) được chạy nền, tự restart nếu crash, một game/thiết bị.
  - Lệnh stop game (`force-stop nat.myc.test`) dừng tiến trình game hiện tại rồi thực thi lệnh stop một lần.
  - Các lệnh ADB khác được chạy một lần, gom kết quả và log lỗi vào `log_error.txt`.
- In định kỳ thông tin trạng thái số thread và số process game đang chạy.
- Tự động clear màn hình console sau một khoảng thời gian.

## Yêu cầu

- Python 3.8+ đã cài trên máy.
- ADB đã cài và có trong `PATH` (có thể dùng từ Android SDK Platform Tools).
- Kết nối internet tới server API.

## Cài đặt & chạy bằng Python

Khuyến nghị dùng virtualenv trong thư mục project.

```powershell
cd D:\CODE\Du_An_Tool_Ads\python-client

# Tạo venv (nếu chưa có)
python -m venv .venv
# hoặc
py -3 -m venv .venv

# Kích hoạt venv
.\.venv\Scripts\Activate.ps1

# Cài dependencies
pip install -r requirements.txt
# Nếu chưa có requirements.txt, tối thiểu là:
# pip install requests

# Chạy tool
python main.py
# Hoặc
python -m android_agent.main
```

Lần đầu chạy, chương trình sẽ hỏi `room_hash` và lưu vào file `config.txt` cạnh `main.py`.

## Build file EXE (Windows)

Project đã có sẵn script build bằng PyInstaller.

### 1. Cài PyInstaller trong venv

```powershell
.\.venv\Scripts\Activate.ps1
pip install pyinstaller
```

### 2. Build

```powershell
cd D:\CODE\Du_An_Tool_Ads\python-client
./build.cmd
```
hoặc
```powershell
pyinstaller --onefile --name main --hidden-import=psutil android_agent\main.py --clean
```
Script `build.cmd` sẽ tạo file:

- `dist\main.exe`

Bạn có thể copy `main.exe` sang các máy khác (kèm ADB và cấu hình cần thiết) để chạy mà không cần Python.

## File cấu hình & log

- `config.txt`
  - Chỉ chứa một dòng là `room_hash`.
  - Nếu xóa file, lần chạy sau tool sẽ hỏi lại và lưu mới.
- `log_error.txt`
  - Ghi lại các lỗi khi chạy lệnh ADB (stderr hoặc thông tin mã thoát), kèm thời gian và serial thiết bị.

## Lưu ý

- Đảm bảo tất cả thiết bị Android đã bật chế độ USB debugging và cấp quyền ADB.
- Khi muốn dừng tool, dùng `Ctrl + C` trong console; tool sẽ gửi tín hiệu dừng tới các thread nền và thoát an toàn.
