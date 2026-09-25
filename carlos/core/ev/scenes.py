"""Named assistant scenes; explicit plans reuse the verified routine executor."""

import re
import time
from .commands import request_text
from .permissions import Permission
from .tools.base import ToolSpec
from .tools.builtin import object_schema

DEFAULTS = {
    "homecoming": {
        "description": "Return to the desktop and inspect Carlos status",
        "hud": "CARLOS",
        "quiet": False,
    },
    "coding": {"description": "Project and build attention", "hud": "PROJECT", "quiet": False},
    "gaming": {
        "description": "Keep background assistant notifications quiet",
        "hud": "SYSTEM",
        "quiet": True,
    },
    "night": {"description": "Quiet assistant interaction", "hud": "CARLOS", "quiet": True},
    "leaving": {
        "description": "Remote status and quiet assistant notifications",
        "hud": "REMOTE",
        "quiet": True,
    },
    "focus": {
        "description": "Keep background success messages out of the conversation",
        "hud": "PROJECT",
        "quiet": True,
    },
    "movie": {
        "description": "Require explicit addressing during media",
        "hud": "MEDIA",
        "quiet": True,
    },
    "remote": {"description": "Remote connection context", "hud": "REMOTE", "quiet": False},
}


class SceneEngine:
    def __init__(self, core):
        self.core = core
        self.current = {"name": None, "state": "IDLE"}

    def definitions(self):
        custom = self.core.daily.records("carlos_scene")
        return {name: {**value, "commands": []} for name, value in DEFAULTS.items()} | custom

    def resolve(self, text):
        clean = request_text(text)
        if not clean:
            return None
        match = re.fullmatch(
            r"(?:activate|start|switch to|enter)\s+(?:the\s+)?([\w -]{1,50})\s+(?:scene|mode)[.!?]*",
            clean,
            re.I,
        )
        if match:
            return match[1].strip().casefold()
        # This grammar sees only explicit user commands after the wake/attention gate.
        if re.fullmatch(r"wake up[, ]+daddy's home[.!?]*", clean, re.I):
            return "homecoming"
        return None

    def activate(self, name, *, running=False):
        definition = self.definitions().get(name)
        if definition is None:
            raise ValueError("Unknown scene")
        self.current = {
            "name": name,
            "state": "RUNNING" if running else "ACTIVE",
            "activated_at": time.time(),
            "hud": definition["hud"],
            "quiet": definition["quiet"],
        }
        self.core.bus.publish("carlos.scene_changed", "scenes", dict(self.current))
        return definition

    def finish(self, activation, status):
        # An older cancelled run must not overwrite a newer selected scene.
        if self.current is not activation:
            return
        self.current["state"] = {"completed": "ACTIVE", "cancelled": "CANCELLED"}.get(
            status, "FAILED"
        )
        self.current["finished_at"] = time.time()
        self.core.bus.publish("carlos.scene_changed", "scenes", dict(self.current))

    def register(self, registry):
        name = {"type": "string", "minLength": 1, "maxLength": 50, "pattern": "[A-Za-z0-9 _-]+"}
        registry.register(
            ToolSpec(
                "carlos.scenes.list",
                "SCENES",
                "List assistant scenes and their explicit command plans; separate from wallpaper themes.",
                Permission.SAFE,
                object_schema({}, []),
                lambda a, c: {"scenes": self.definitions(), "active": dict(self.current)},
                read_only=True,
            )
        )

        def save(a, c):
            for command in a["commands"]:
                if request_text(command) is None or self.resolve(command):
                    raise ValueError(
                        "Scene commands must be explicit and cannot recursively activate scenes"
                    )
            return c.daily.save(
                "carlos_scene",
                a["name"].strip().casefold(),
                {
                    "description": a.get("description", "Custom scene"),
                    "commands": a["commands"],
                    "hud": a["hud"],
                    "quiet": a["quiet"],
                },
            )

        registry.register(
            ToolSpec(
                "carlos.scenes.save",
                "SCENES",
                "Save a named assistant scene; does not execute commands. Running still requires normal permissions and verification.",
                Permission.LOW_RISK,
                object_schema(
                    {
                        "name": name,
                        "description": {"type": "string", "maxLength": 200},
                        "commands": {
                            "type": "array",
                            "maxItems": 8,
                            "items": {"type": "string", "minLength": 1, "maxLength": 500},
                        },
                        "hud": {
                            "type": "string",
                            "enum": ["CARLOS", "PROJECT", "SYSTEM", "MEDIA", "REMOTE"],
                        },
                        "quiet": {"type": "boolean"},
                    },
                    ["name", "commands", "hud", "quiet"],
                ),
                save,
            )
        )
