"""Carlos's versioned integration surface; observations never imply tested actions."""

from pathlib import Path
import asyncio, json, os, time, shutil, sys
from importlib.metadata import version, PackageNotFoundError
from .identity import identity, STATE_NAMES


class HoloSystem:
    def __init__(self, core):
        self.core = core
        self.started = time.monotonic()
        self._cache = {}
        self._checked = 0.0
        self._probe_lock = asyncio.Lock()

    async def capabilities(self):
        async with self._probe_lock:
            return await self._probe_capabilities()

    async def _probe_capabilities(self):
        if time.monotonic() - self._checked < 5:
            return self._cache
        c = self.core
        s = c.snapshot()
        v = s["voice"]
        rows = {}

        def add(name, state, evidence):
            rows[name] = {"state": state, "evidence": evidence, "checked_at": time.time()}

        add("Core", "AVAILABLE", "Served by the running Carlos Core process")
        add(
            "Voice",
            (
                "MUTED"
                if v.get("privacy_mode")
                else "AVAILABLE" if v.get("stt_available") else "UNAVAILABLE"
            ),
            "Current voice manager snapshot; acoustic performance requires measurement",
        )
        add(
            "STT",
            "AVAILABLE" if v.get("stt_available") else "UNAVAILABLE",
            "Local speech adapter availability; recognition quality separately tested",
        )
        add(
            "TTS",
            "AVAILABLE" if v.get("tts_available") else "UNAVAILABLE",
            "Local synthesis adapter availability; acoustic playback separately tested",
        )
        add("Memory", "RAM_ONLY" if c.privacy.ephemeral else "AVAILABLE", "Current storage policy")
        presence = s.get("presence", {})
        add(
            "Presence",
            presence.get("presence", "UNKNOWN"),
            "Identity remains " + presence.get("person_identity", "UNVERIFIED"),
        )
        health = s.get("health", {}).get("Local AI", {})
        add(
            "LocalAI",
            health.get("state", "UNVERIFIED"),
            "Owned model health supervisor; successful task performance is separate",
        )
        add("Wake", "ACTIVE" if v.get("wake_active") else "IDLE", "Current capture worker status")
        add(
            "LocalReasoning",
            (
                "AVAILABLE"
                if s["provider"].get("active")
                in ("offline", "local_hybrid", "local_agent", "carlos_router")
                else "IDLE"
            ),
            "Selected provider: " + str(s["provider"].get("active")),
        )
        local = c.config["providers"]["local_llama"]
        add(
            "LocalModel",
            (
                "INSTALLED"
                if Path(local["model_path"]).is_file() and Path(local["binary"]).is_file()
                else "UNAVAILABLE"
            ),
            "Model and runtime files; no throughput claim",
        )
        add(
            "CloudReasoning",
            (
                "CONNECTED"
                if s["provider"].get("connected")
                and s["provider"].get("active")
                not in ("offline", "local_hybrid", "local_agent", "carlos_router")
                else "UNVERIFIED"
            ),
            "Latest provider transport evidence; no unsolicited cloud probe",
        )
        add(
            "DesktopControl",
            "AVAILABLE" if s["desktop"].get("available") else "UNVERIFIED",
            "KWin bridge status; per-action verification remains required",
        )
        codex = await asyncio.to_thread(c.coding_agent.status)
        add(
            "Codex",
            "AVAILABLE" if codex.get("available") else "UNAVAILABLE",
            "Installed Codex gateway capability; no job started",
        )
        from .tools.holohand import status as hand_status
        from .remote_health import mobile_ready

        hand, remote = await asyncio.gather(
            hand_status({}, None), mobile_ready(), return_exceptions=True
        )
        add(
            "HoloHand",
            hand.get("state", "UNVERIFIED") if isinstance(hand, dict) else "UNVERIFIED",
            "Private running-instance status; gesture quality requires separate measurement",
        )
        add(
            "Remote",
            "READY" if remote is True else "UNAVAILABLE",
            "Local mobile backend health; cellular reachability and desktop streaming require separate tests",
        )
        vision = c.vision.status()
        add(
            "Vision",
            "AVAILABLE" if vision.get("available") else "UNAVAILABLE",
            "On-demand capture; visual reasoning: "
            + str(vision.get("visual_reasoning", "UNVERIFIED")),
        )
        add("Sentinel", "NOT_INSTALLED", "No independent Sentinel node has been provisioned")
        add(
            "RemoteWake",
            "UNVERIFIED",
            "Requires configured NIC/firmware and an independent always-on wake sender",
        )
        try:
            p = await asyncio.create_subprocess_exec(
                "tailscale",
                "status",
                "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                out, _ = await asyncio.wait_for(p.communicate(), 3)
            except BaseException:
                if p.returncode is None:
                    p.kill()
                    await p.wait()
                raise
            t = json.loads(out)
            add(
                "Tailscale",
                "CONNECTED" if t.get("BackendState") == "Running" else "UNAVAILABLE",
                "Local Tailscale daemon status",
            )
        except (OSError, ValueError, TimeoutError):
            add("Tailscale", "UNVERIFIED", "Status probe unavailable or timed out")
        self._cache = {"api_version": 1, "capabilities": rows}
        self._checked = time.monotonic()
        return self._cache

    def status(self):
        c = self.core
        from .readiness import project_readiness

        readiness = project_readiness(
            c.snapshot(), self._cache, getattr(c, "startup_ipc_seconds", None)
        )
        return {
            **identity(),
            "state": STATE_NAMES.get(c.state.current.value, c.state.current.value),
            "pid": os.getpid(),
            "uptime_seconds": round(time.monotonic() - c.started_monotonic, 3),
            "privacy": c.config.get("carlos", {}).get("privacy_mode", "NORMAL"),
            "mode": c.config.get("carlos", {}).get("mode", "DAILY"),
            "plugins": getattr(c, "plugins", []),
            "capabilities": self._cache.get("capabilities", {}),
            "readiness": readiness,
        }

    def support(self):
        # Deliberate allowlist: no conversations, window titles, clipboard,
        # config dumps, paths, raw logs, provider keys, or pairing credentials.
        s = self.core.snapshot()
        dependencies = {"python": ".".join(map(str, sys.version_info[:3]))}
        for package in ("aiohttp", "psutil", "numpy", "sherpa-onnx", "piper-tts"):
            try:
                dependencies[package] = version(package)
            except PackageNotFoundError:
                dependencies[package] = "NOT_INSTALLED"
        safe_types = {
            "health.repair_started",
            "health.repair_finished",
            "health.repair_failed",
            "tts.completed",
            "tts.failed",
            "tts.cancelled",
            "ai.first_token",
            "voice.transcription_complete",
            "voice.barge_in",
            "core.state_changed",
        }
        events = [
            {
                key: event[key]
                for key in ("type", "timestamp", "sequence", "duration_ms")
                if key in event
            }
            for event in self.core.bus.history()
            if event.get("type") in safe_types
        ][-80:]
        health = {
            name: {
                key: value
                for key, value in row.items()
                if key
                in {
                    "state",
                    "observed_at",
                    "recovered",
                    "attempt",
                    "repairs_in_window",
                    "temperature_celsius",
                    "resume_below_celsius",
                }
            }
            for name, row in s.get("health", {}).items()
        }
        return {
            "identity": identity(),
            "generated_at": time.time(),
            "core": self.status(),
            "bundle_version": 1,
            "component_version_scope": "Core Python environment only; isolated speech workers have separate dependencies",
            "component_versions": dependencies,
            "health": health,
            "events": events,
            "latency_samples": [
                {"started_at": r.get("started_at"), "stages_ms": r.get("stages_ms", {})}
                for r in self.core.bus.latency_report().get("recent", [])
            ],
            "tool_count": s["tools"]["registered"],
            "pending_confirmation_count": len(s["tools"]["pending_confirmations"]),
            "voice": {
                k: s["voice"].get(k)
                for k in (
                    "wake_active",
                    "stt_available",
                    "tts_available",
                    "privacy_mode",
                    "capture_active",
                )
            },
            "latency_policy": "Unmeasured stages remain null; use the local latency report for detailed task evidence.",
        }
