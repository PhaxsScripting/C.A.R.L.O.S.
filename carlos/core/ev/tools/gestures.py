"""Atomic gestures with scoped holds, never persistent model-owned input state."""

from ..permissions import Permission
from .base import ToolSpec
from .builtin import object_schema


def register_gesture_tools(registry):
    window = {"type": "string", "minLength": 1, "maxLength": 100}
    coordinate = {"type": "number", "minimum": -32768, "maximum": 32768}

    async def gesture(arguments, context):
        return await context.desktop.input.gesture(arguments["window_id"], arguments["events"])

    async def relative(arguments, context):
        return await context.desktop.input.move_relative(
            arguments["window_id"], arguments["dx"], arguments["dy"]
        )

    async def drag(arguments, context):
        events = [
            {"type": "move", "x": arguments["start_x"], "y": arguments["start_y"]},
            {"type": "button", "button": arguments.get("button", "left"), "state": "press"},
        ]
        for index in range(1, 9):
            events.extend(
                [
                    {
                        "type": "move",
                        "x": arguments["start_x"]
                        + (arguments["end_x"] - arguments["start_x"]) * index / 8,
                        "y": arguments["start_y"]
                        + (arguments["end_y"] - arguments["start_y"]) * index / 8,
                    },
                    {"type": "pause", "milliseconds": 25},
                ]
            )
        events.append(
            {"type": "button", "button": arguments.get("button", "left"), "state": "release"}
        )
        return await context.desktop.input.gesture(arguments["window_id"], events)

    registry.register(
        ToolSpec(
            "desktop.input.gesture",
            "DESKTOP",
            "Send 1-64 events to one exact focused window. Event types: {type:move,x,y}, {type:relative,dx,dy}, {type:button,button:left/right/middle,state:press/release}, {type:key,key,state:press/release}, {type:pause,milliseconds:1..250}. Holds are scoped to this call and always released on completion/error/cancellation. Supports drag, modifier holds and hover. Does not verify application effects or cross-window drops.",
            Permission.SENSITIVE,
            object_schema(
                {
                    "window_id": window,
                    "events": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 64,
                        "items": {"type": "object"},
                    },
                },
                ["window_id", "events"],
            ),
            gesture,
            verification="Focus and point checks per event; input delivery only",
        )
    )
    registry.register(
        ToolSpec(
            "desktop.pointer.move_relative",
            "DESKTOP",
            "Move relative to the freshly observed cursor within an exact focused window; verify the final KWin position.",
            Permission.LOW_RISK,
            object_schema(
                {"window_id": window, "dx": coordinate, "dy": coordinate}, ["window_id", "dx", "dy"]
            ),
            relative,
        )
    )
    registry.register(
        ToolSpec(
            "desktop.pointer.drag",
            "DESKTOP",
            "Drag between global logical coordinates inside one exact focused unobscured window. Always release the button. Read back the app's content separately; mouse delivery is not successful drag/drop proof.",
            Permission.SENSITIVE,
            object_schema(
                {
                    "window_id": window,
                    "start_x": coordinate,
                    "start_y": coordinate,
                    "end_x": coordinate,
                    "end_y": coordinate,
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                },
                ["window_id", "start_x", "start_y", "end_x", "end_y"],
            ),
            drag,
        )
    )
