@echo off
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name Phantom --icon icon.ico --add-data "icon.ico;." --hidden-import pystray._win32 --collect-all customtkinter phantom.py
echo.
echo Done: dist\Phantom.exe
pause
