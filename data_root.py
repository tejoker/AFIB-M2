"""Where the scripts keep downloaded data and the team's shared config (never committed).

AFIB_DATA_DIR wins, then the legacy BBG_DATA_DIR, then data/ in the repository. Every script
reads the folder from here so they all agree on which setting applies.
"""
import os
from pathlib import Path

CONFIGURED = os.environ.get("AFIB_DATA_DIR") or os.environ.get("BBG_DATA_DIR")
DATA = Path(CONFIGURED or Path(__file__).resolve().parent / "data")
