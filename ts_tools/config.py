import os

DEFAULT_TSSPLITTER_EXE = r"C:\DTV\TSsplitter\TsSplitter.exe"
DEFAULT_RPLSINFO_EXE = r"C:\DTV\rplsinfo152\rplsinfo.exe"

SUPPORTED_EXTS = (".ts", ".m2ts")

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
