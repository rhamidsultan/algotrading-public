@echo off
cd /d "%~dp0"
start "" /b cmd /c "timeout /t 5 >nul & start http://localhost:8501"
python -m streamlit run dashboard.py --server.port 8501
pause
