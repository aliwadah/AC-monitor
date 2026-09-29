import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

try:
    from config_secrets import SECRETS
    for k, v in SECRETS.items():
        if v:
            os.environ.setdefault(k, str(v))
except ImportError:
    pass

from server import app as application