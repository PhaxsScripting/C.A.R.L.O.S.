#!/usr/bin/env python3
"""Local-only lifecycle and pairing administration."""

import argparse, fcntl, json, os, secrets, signal, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from security import save, digest
from recovery import GiggleBudget

ROOT = Path(__file__).resolve().parents[1]
STATE = Path.home() / ".local/state/holohand-remote"
STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
PID = STATE / "supervisor.pid"


def alive():
    try:
        pid = int(PID.read_text())
        os.kill(pid, 0)
        if str(Path(__file__).resolve()) not in Path(f"/proc/{pid}/cmdline").read_text().replace(
            "\0", " "
        ):
            return None
        return pid
    except (OSError, ValueError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["start", "stop", "restart", "status", "pair", "devices", "supervise"]
    )
    a = parser.parse_args().action
    if a == "pair":
        code = f"{secrets.randbelow(1000000):06d}"
        save(STATE / "pairing.json", {"hash": digest(code), "expires": time.time() + 300})
        print("HoloHand pairing code:", code, "(expires in 5 minutes)")
        return
    if a == "devices":
        p = STATE / "devices.json"
        d = json.loads(p.read_text()) if p.exists() else {}
        print(
            json.dumps(
                [
                    {k: v for k, v in x.items() if k in ("name", "last_seen", "created")}
                    for x in d.values()
                ],
                indent=2,
            )
        )
        return
    if a == "status":
        print("Running" if alive() else "Stopped")
        return
    if a in ("stop", "restart"):
        pid = alive()
        if pid:
            os.kill(pid, signal.SIGTERM)
            for _ in range(100):
                if not alive():
                    break
                time.sleep(0.1)
        if a == "stop":
            return
    if a in ("start", "restart"):
        if alive():
            return
        with open(STATE / "supervisor.log", "ab") as log:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "supervise"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        return
    lock = open(STATE / "supervisor.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    PID.write_text(str(os.getpid()))
    stopping = False
    child = None

    def stop(*args):
        nonlocal stopping
        stopping = True
        if child and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    budget = GiggleBudget()
    try:
        while not stopping:
            # Rotate only between launches; active files stay owned by the current agent.
            for name in ["desktop.log", "supervisor.log"]:
                p = STATE / name
                if p.exists() and p.stat().st_size > 2 * 1024 * 1024:
                    p.replace(STATE / (name + ".1"))
            start = time.monotonic()
            child = subprocess.Popen(
                [sys.executable, str(ROOT / "app.py")], stdin=subprocess.DEVNULL
            )
            child.wait()
            if stopping:
                break
            delay = budget.retry_after(time.monotonic())
            if delay is None:
                print(
                    "Three crashes in ten minutes; recovery stopped. Inspect logs and explicitly restart.",
                    flush=True,
                )
                break
            for _ in range(int(delay * 10)):
                if stopping:
                    break
                time.sleep(0.1)
    finally:
        PID.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
