"""Keeps every command listed in programs.txt running, restarting any that exit.

One command per line, run from this folder. Lines starting with # are ignored.
The file is re-read every 30s, so you can add/remove programs without restarting.
Each program's output goes to logs/<name>.log; the runner itself logs to logs/runner.log.
"""
import ctypes
import logging
import os
import shlex
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROGRAMS_FILE = os.path.join(BASE_DIR, "programs.txt")
LOG_DIR = os.path.join(BASE_DIR, "logs")
CHECK_INTERVAL = 30

os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(LOG_DIR, "runner.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logging.getLogger().addHandler(logging.StreamHandler())

# Prevent Windows from sleeping while the runner is alive (ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
if os.name == "nt":
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)


def read_programs():
    try:
        with open(PROGRAMS_FILE, encoding="utf-8") as f:
            lines = [line.strip() for line in f]
    except FileNotFoundError:
        return []
    return [line for line in lines if line and not line.startswith("#")]


def log_name(command):
    safe = "".join(c if c.isalnum() else "_" for c in command)
    return os.path.join(LOG_DIR, safe[:60] + ".log")


def start(command):
    args = shlex.split(command, posix=False)
    # Use this runner's Python for "python ..." lines, so the venv is respected
    if args and args[0].lower() in ("python", "python.exe"):
        args[0] = sys.executable
    log = open(log_name(command), "a", encoding="utf-8")
    # PYTHONUNBUFFERED so print() output reaches the log file immediately
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(args, cwd=BASE_DIR, stdout=log, stderr=subprocess.STDOUT, env=env)
    logging.info(f"Started (pid {proc.pid}): {command}")
    return proc


def main():
    running = {}  # command -> Popen
    while True:
        wanted = read_programs()

        # Stop programs removed from the list
        for command in list(running):
            if command not in wanted:
                logging.info(f"Stopping (removed from list): {command}")
                running.pop(command).terminate()

        # Start new programs and restart dead ones
        for command in wanted:
            proc = running.get(command)
            if proc is not None and proc.poll() is None:
                continue
            if proc is not None:
                logging.warning(f"Exited with code {proc.returncode}, restarting: {command}")
            try:
                running[command] = start(command)
            except Exception as e:
                logging.error(f"Could not start {command}: {e!r}")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
