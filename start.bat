@echo off
cd /d "%~dp0"
if not exist .env (
  copy .env.example .env >nul
  echo Fill in .env (TELEGRAM_TOKEN, OPENROUTER_API_KEY) then run start.bat again.
  notepad .env
  exit /b
)
python -m pip install -q -r requirements.txt
python -m vinted_hunter
pause
