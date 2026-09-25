"""Owned PipeWire echo filter; never changes the system's default devices."""

import asyncio
import json
import re


class EchoCancel:
    source = "carlos.aec.source"

    def __init__(self, enabled=False):
        self.enabled = enabled
        self.process = None
        self.input = ""
        self.output = ""
        self.state = "DISABLED"

    @property
    def active(self):
        return (
            self.state == "ACTIVE" and self.process is not None and self.process.returncode is None
        )

    async def _command(self, *args):
        p = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        try:
            out, _ = await asyncio.wait_for(p.communicate(), 2)
            return out.decode().strip() if p.returncode == 0 else ""
        finally:
            if p.returncode is None:
                p.kill()
                await p.wait()

    async def ensure(self, physical_source):
        if not self.enabled:
            return physical_source
        try:
            sink = await self._command("pactl", "get-default-sink")
            # Bluetooth output is independently clocked and buffered. Do not
            # attach an acoustic reference filter to its playback graph.
            # Keep explicit wake interruption; ordinary AEC barge-in stays off.
            if sink.startswith("bluez_output."):
                await self.close()
                self.state = "BYPASSED_BLUETOOTH"
                return physical_source
            if self.active and physical_source == self.input and sink == self.output:
                return self.source
            await self.close()
            if (
                not all(re.fullmatch(r"[\w.:-]+", x) for x in (physical_source, sink))
                or physical_source == self.source
            ):
                self.state = "UNAVAILABLE"
                return physical_source
            args = (
                "{ library.name = aec/libspa-aec-webrtc monitor.mode = true audio.channels = 1 audio.position = [ MONO ] "
                'capture.props = { node.name = carlos.aec.capture node.passive = true target.object = "'
                + physical_source
                + '" } '
                "source.props = { node.name = "
                + self.source
                + ' node.description = "Carlos Microphone" priority.session = 0 } '
                'sink.props = { node.name = carlos.aec.reference target.object = "' + sink + '" } }'
            )
            self.process = await asyncio.create_subprocess_exec(
                "pw-cli",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self.process.stdin.write(
                ("load-module libpipewire-module-echo-cancel " + args + "\n").encode()
            )
            await self.process.stdin.drain()
            for _ in range(10):
                rows = json.loads(
                    await self._command("pactl", "-f", "json", "list", "sources") or "[]"
                )
                if any(x.get("name") == self.source for x in rows):
                    self.input, self.output, self.state = physical_source, sink, "ACTIVE"
                    return self.source
                await asyncio.sleep(0.1)
        except (OSError, ValueError, TimeoutError):
            pass
        await self.close()
        self.state = "UNAVAILABLE"
        return physical_source

    async def close(self):
        p, self.process = self.process, None
        if p is not None and p.returncode is None:
            p.terminate()
            try:
                await asyncio.wait_for(p.wait(), 2)
            except TimeoutError:
                p.kill()
                await p.wait()
        self.state = "STOPPED" if self.enabled else "DISABLED"
