@echo off
rem The CME signal database: updates every source, prints the views and opens the page (cme --help).
"%~dp0.venv\Scripts\python.exe" "%~dp0cme_challenge\signal_db.py" %*
