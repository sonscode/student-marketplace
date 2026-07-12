import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault(
    "SQLITE_DATABASE_PATH",
    os.path.join(tempfile.gettempdir(), f"azison_pytest_{os.getpid()}.db"),
)
