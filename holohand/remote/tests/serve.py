"""Start an isolated browser-test agent; never use the phone's pairing state."""

import os, sys, json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
state = Path.home() / ".cache/holohand-browser-tests"
state.mkdir(mode=0o700, parents=True, exist_ok=True)
(state / "config.json").write_text(json.dumps({"origin": "http://localhost:8766"}))
os.environ.update(HOLOHAND_STATE=str(state), HOLOHAND_PORT="8766")
sys.path.insert(0, str(root))
from aiohttp import web
from app import create_app

web.run_app(create_app(), host="127.0.0.1", port=8766, access_log=None)
