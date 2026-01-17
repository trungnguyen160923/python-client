lệnh chạy server
python -m android_agent.main

Lệnh build exe
pyinstaller --noconfirm --onefile --console --add-data ".env;." --paths . --name "AndroidAgent" android_agent/main.py


