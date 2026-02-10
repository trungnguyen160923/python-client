# (Tuỳ chọn) Tạo virtualenv
python -m venv .venv
.venv\\Scripts\\activate

lệnh chạy server
python -m android_agent.main

Lệnh build exe
pyinstaller --noconfirm --onefile --console --add-data ".env;." --paths . --name "AndroidAgent" android_agent/main.py
