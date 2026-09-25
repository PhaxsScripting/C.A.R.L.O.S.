"""Session presence evidence, without camera frames or claims of identity."""

import asyncio
import time


class PresenceMonitor:
    def __init__(self, bus, config=None):
        self.bus = bus
        self.config = config if config is not None else {}
        self.state = {"session": "UNKNOWN", "person_identity": "UNVERIFIED", "camera_used": False}
        self.previous_lock = None
        self.sleep_offset = None
        self.last_addressed = 0.0
        self.locked_since = None
        self.last_greeting = 0.0
        self.idle_supported = None
        self.state.update(presence="UNKNOWN", attention="DORMANT", confidence=0.0)

    def consume(self, event):
        now = time.monotonic()
        if event.type in {"wake.detected", "command.received", "voice.transcription_complete"}:
            self.last_addressed = now
            self.state.update(
                attention="ENGAGED",
                presence="ENGAGED",
                confidence=0.9,
                evidence="Explicit assistant interaction; person identity unverified",
            )
        elif event.type == "voice.listening_started":
            self.state["attention"] = "WAITING_FOR_USER"
        elif event.type in {"voice.conversation_ended", "carlos.privacy_changed"}:
            self.last_addressed = 0.0
            self.state.update(
                attention="DORMANT",
                presence="UNKNOWN",
                confidence=0.0,
                evidence="Previous interaction context cleared",
            )
        elif event.type == "voice.barge_in":
            self.state["attention"] = "INTERRUPTED"

    def observe_lock(self, locked, now=None):
        now = time.monotonic() if now is None else now
        self.state.update(
            session="LOCKED" if locked else "UNLOCKED", observed_at=time.time(), camera_used=False
        )
        if locked:
            if self.locked_since is None:
                self.locked_since = now
            self.state.update(
                presence="LIKELY_ABSENT",
                confidence=0.55,
                evidence="Screen locked; physical absence is not proven",
                attention="DORMANT",
            )
        else:
            away = now - self.locked_since if self.locked_since is not None else 0
            if (
                self.config.get("greetings", True)
                and self.previous_lock is True
                and away >= 300
                and now - self.last_greeting >= 1800
            ):
                self.bus.publish(
                    "presence.returned",
                    "presence",
                    {
                        "away_seconds": round(away),
                        "greeting": "Welcome back.",
                        "delivery": "HUD_ONLY",
                        "identity": "UNVERIFIED",
                    },
                )
                self.last_greeting = now
            self.locked_since = None
            engaged = bool(self.last_addressed and now - self.last_addressed < 30)
            self.state.update(
                presence="ENGAGED" if engaged else "UNKNOWN",
                confidence=0.9 if engaged else 0.2,
                evidence=(
                    "Recent explicit interaction"
                    if engaged
                    else "Unlocked session alone does not prove desk occupancy"
                ),
            )
            if not engaged:
                self.state["attention"] = "DORMANT"
        if locked != self.previous_lock:
            self.bus.publish("presence.session_changed", "presence", dict(self.state))
        self.previous_lock = locked

    def observe_hand(self, status, now=None):
        """Use only fresh metadata from the existing tracker; never open a camera."""
        now = time.monotonic() if now is None else now
        if not self.config.get("hand_presence", True) or self.state.get("session") != "UNLOCKED":
            return
        if self.last_addressed and now - self.last_addressed < 30:
            return
        tracking = status.get("tracking", {})
        if (
            status.get("state") == "READY"
            and tracking.get("hand_visible") is True
            and isinstance(tracking.get("confidence"), (int, float))
            and tracking["confidence"] >= 0.8
            and isinstance(tracking.get("age_ms"), (int, float))
            and 0 <= tracking["age_ms"] <= 1000
        ):
            idle = self.state.get("idle_seconds")
            active = isinstance(idle, (int, float)) and 0 <= idle <= 60
            self.state.update(
                presence="AT_DESK" if active else "PRESENT",
                confidence=0.75 if active else 0.65,
                camera_used=True,
                evidence="Fresh hand metadata from the already-running HoloHand tracker; identity unverified",
            )
        else:
            self.state.update(
                presence="UNKNOWN",
                confidence=0.2,
                camera_used=False,
                evidence="No fresh desk evidence; absence is not proven",
            )

    async def run(self):
        while True:
            p = None
            try:
                offset = time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
                if self.sleep_offset is not None and offset - self.sleep_offset > 2:
                    self.bus.publish(
                        "system.resume_observed",
                        "presence",
                        {
                            "suspended_seconds": round(offset - self.sleep_offset, 2),
                            "detection_interval_seconds": 5,
                        },
                    )
                self.sleep_offset = offset
                p = await asyncio.create_subprocess_exec(
                    "qdbus6",
                    "org.freedesktop.ScreenSaver",
                    "/ScreenSaver",
                    "org.freedesktop.ScreenSaver.GetActive",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await asyncio.wait_for(p.communicate(), 2)
                value = out.strip()
                if p.returncode == 0 and value in {b"true", b"false"}:
                    locked = value == b"true"
                    self.observe_lock(locked)
                    if self.idle_supported is not False:
                        p = await asyncio.create_subprocess_exec(
                            "qdbus6",
                            "org.freedesktop.ScreenSaver",
                            "/ScreenSaver",
                            "org.freedesktop.ScreenSaver.GetSessionIdleTime",
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        idle, _ = await asyncio.wait_for(p.communicate(), 2)
                        self.idle_supported = p.returncode == 0 and idle.strip().isdigit()
                        self.state["idle_seconds"] = (
                            int(idle.strip()) if self.idle_supported else None
                        )
                        self.state["idle_supported"] = self.idle_supported
                    if not locked and self.config.get("hand_presence", True):
                        from .tools.holohand import status

                        try:
                            self.observe_hand(await status({}, None))
                        except (OSError, ValueError):
                            self.observe_hand({})
                else:
                    self.state["session"] = "UNKNOWN"
            except (OSError, TimeoutError):
                self.state["session"] = "UNKNOWN"
            finally:
                if p is not None and p.returncode is None:
                    p.kill()
                    await p.wait()
            await asyncio.sleep(5)
