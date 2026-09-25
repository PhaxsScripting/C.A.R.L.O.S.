"""Fast explicit personal, editing and settings requests; no keyword guessing."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote_plus


def handsfree_action(clean):
    from .commands import Action, _spoken_percent

    text = clean.strip()

    def match(pattern):
        return re.fullmatch(pattern, text, re.I)

    prefixes = {"note": "notes", "task": "tasks", "bookmark": "bookmarks", "snippet": "snippets"}
    # Literal saved content is consumed as a whole before action splitting.
    if m := match(
        r"(?:take|save|create|write|add) (?:a )?(note|snippet) (?:called |named )?(.+?)\s*(?::|\bsaying\b|\bwith text\b)\s*(.+)"
    ):
        return Action(
            prefixes[m[1].lower()] + ".create", {"title": m[2].strip(), "content": m[3].strip()}
        )
    if m := match(r"(?:add|create|save) (?:a |the )?(?:task|todo|to-do)(?: (?:called|to))? (.+)"):
        return Action("tasks.create", {"title": m[1]})
    if m := match(r"add (.+) to (?:my |the )?(?:task|todo|to-do) list"):
        return Action("tasks.create", {"title": m[1]})
    if m := match(r"(?:save|add|create) (?:a )?bookmark (?:called |named )?(.+?) (?:for|to) (\S+)"):
        return Action("bookmarks.create", {"title": m[1], "content": m[2]})
    if m := match(
        r"(?:list|show|show me|what are)(?: my| the)?(?: (completed|done|archived|all))? (notes|tasks|todos|to-dos|bookmarks|snippets)"
    ):
        prefix = {"todos": "tasks", "to-dos": "tasks"}.get(m[2].lower(), m[2].lower())
        return Action(
            prefix + ".list",
            {
                "state": {"completed": "done"}.get(
                    (m[1] or "active").lower(), (m[1] or "active").lower()
                )
            },
        )
    if m := match(
        r"(?:search|find)(?: my| the)? (notes|tasks|bookmarks|snippets) (?:for|about|containing) (.+)"
    ):
        return Action(m[1].lower() + ".list", {"query": m[2]})
    if m := match(
        r"(?:read|show)(?: me)?(?: my| the)? (note|task|bookmark|snippet) (?:called |named )?(.+)"
    ):
        return Action(prefixes[m[1].lower()] + ".read", {"identifier": m[2]})
    if m := match(r"(?:archive|restore) (?:my |the )?(note|task|bookmark|snippet) (.+)"):
        return Action(prefixes[m[1].lower()] + "." + text.split()[0].lower(), {"identifier": m[2]})
    if m := match(r"(complete|finish|check off|reopen) (?:my |the )?task (.+)"):
        return Action(
            "tasks." + ("reopen" if m[1].lower() == "reopen" else "complete"), {"identifier": m[2]}
        )
    if m := match(r"mark (?:my |the )?task (.+) (?:as )?(?:done|complete)"):
        return Action("tasks.complete", {"identifier": m[1]})
    if m := match(r"(?:append|add) (.+) to (?:my |the )?note (.+)"):
        return Action("notes.append", {"identifier": m[2], "content": m[1]})
    if m := match(
        r"open (?:my |the )?bookmark (.+?)(?: in (firefox|chrome|chromium|brave|vivaldi|edge|opera|librewolf))?"
    ):
        return Action(
            "bookmarks.open", {"identifier": m[1], "browser": (m[2] or "default").lower()}
        )
    if m := match(r"copy (?:my |the )?snippet (.+?)(?: to (?:the |my )?clipboard)?"):
        return Action("snippets.copy", {"identifier": m[1]})
    if m := match(
        r"snooze (?:the |my )?(?:reminder|timer|alarm) (.+) (?:for|by) (\d+|one|two|five|ten|fifteen|thirty|sixty) (seconds?|minutes?|hours?)"
    ):
        amount = _spoken_percent(m[2])
        if amount:
            return Action(
                "reminders.snooze",
                {
                    "identifier": m[1],
                    "seconds": amount
                    * {"second": 1, "minute": 60, "hour": 3600}[m[3].lower().rstrip("s")],
                },
            )
    if m := match(r"(?:calculate|compute|what is|what's) (.+)"):
        expression = m[1]
        if re.search(r"\d", expression) and re.fullmatch(
            r"[\d\s.+*/%()^,eπ-]+|[\d\sa-z.+*/%()^,-]+", expression, re.I
        ):
            # Do not divert arbitrary questions that merely contain a number.
            words = set(re.findall(r"[a-z]+", expression.lower()))
            if words <= {
                "pi",
                "e",
                "sqrt",
                "abs",
                "round",
                "plus",
                "minus",
                "times",
                "multiplied",
                "divided",
                "by",
                "percent",
                "of",
                "modulo",
            }:
                return Action("utility.calculate", {"expression": expression})
    if m := match(r"convert (-?\d+(?:\.\d+)?) (.+?) (?:to|into|in) (.+)"):
        return Action("utility.convert", {"value": float(m[1]), "source": m[2], "target": m[3]})
    if m := match(r"(?:what(?:'s| is) (?:the )?time|what time is it) in (.+)"):
        return Action("utility.world_clock", {"identifier": m[1]})
    if match(
        r"(?:count (?:the )?(?:words|characters)(?: in (?:my |the )?clipboard)?|clipboard (?:word count|statistics|stats))"
    ):
        return Action("utility.clipboard_stats", {})
    if m := match(
        r"(?:show|list|find)(?: me)? (?:my |the )?(?:recent|newest|latest) (?:files|downloads)(?: in (.+))?"
    ):
        location = m[1] or "downloads"
        names = {
            "downloads": "Downloads",
            "documents": "Documents",
            "desktop": "Desktop",
            "pictures": "Pictures",
            "home": "",
        }
        path = (
            str(Path.home() / names[location.casefold()])
            if location.casefold() in names
            else location
        )
        return Action("files.recent", {"path": path, "limit": 10})
    if m := match(
        r"(?:switch|go)(?: me)? to (?:the )?(next|previous) (?:workspace|virtual desktop)"
    ):
        return Action("desktop.workspace.switch", {"description": m[1].lower()})
    if m := match(r"(?:switch|go)(?: me)? to (?:workspace|virtual desktop) (.+)"):
        return Action("desktop.workspace.switch", {"description": m[1]})
    if m := match(
        r"(?:search|look up)(?: for)? (.+) on (youtube|wikipedia|github|reddit|google|duckduckgo)"
    ):
        sites = {
            "youtube": "https://www.youtube.com/results?search_query=",
            "wikipedia": "https://en.wikipedia.org/w/index.php?search=",
            "github": "https://github.com/search?q=",
            "reddit": "https://www.reddit.com/search/?q=",
            "google": "https://www.google.com/search?q=",
            "duckduckgo": "https://duckduckgo.com/?q=",
        }
        return Action(
            "browser.open_url",
            {"url": sites[m[2].lower()] + quote_plus(m[1]), "browser": "default"},
        )
    shortcuts = {
        "show downloads": "downloads",
        "show browser history": "history",
        "show browser bookmarks": "bookmarks",
        "bookmark this page": "bookmark_page",
        "focus address bar": "address_bar",
        "stop loading": "stop_loading",
        "go to last tab": "last_tab",
        "open a private window": "private_window",
    }
    if text.lower() in shortcuts:
        return Action(
            "browser.shortcut", {"description": "current window", "action": shortcuts[text.lower()]}
        )
    if m := match(r"(?:go|switch) to tab ([1-8]|one|two|three|four|five|six|seven|eight)"):
        index = _spoken_percent(m[1])
        return Action(
            "browser.shortcut", {"description": "current window", "action": f"tab_{index}"}
        )
    edits = {
        "select all": ("a", ["ctrl"]),
        "copy selection": ("c", ["ctrl"]),
        "copy that": ("c", ["ctrl"]),
        "cut selection": ("x", ["ctrl"]),
        "paste": ("v", ["ctrl"]),
        "paste that": ("v", ["ctrl"]),
        "undo typing": ("z", ["ctrl"]),
        "redo typing": ("z", ["ctrl", "shift"]),
        "delete last word": ("backspace", ["ctrl"]),
        "select previous word": ("left", ["ctrl", "shift"]),
        "select next word": ("right", ["ctrl", "shift"]),
        "next field": ("tab", []),
        "previous field": ("tab", ["shift"]),
        "cancel dialog": ("escape", []),
        "save document": ("s", ["ctrl"]),
    }
    if text.lower() in edits:
        key, modifiers = edits[text.lower()]
        return Action(
            "editing.key", {"description": "current window", "key": key, "modifiers": modifiers}
        )
    if m := match(r"(?:show|list)(?: me)? (?:the )?(buttons|controls|links)(?: (?:in|on) (.+))?"):
        target = m[2] or "current window"
        if target.lower() in {"screen", "my screen", "the screen", "this window", "my window"}:
            target = "current window"
        return Action(
            "controls.list",
            {
                "description": target,
                "role": {"buttons": "button", "links": "link", "controls": ""}[m[1].lower()],
            },
        )
    return settings_action(text)


def settings_action(text):
    from .commands import Action, _spoken_percent

    def match(pattern):
        return re.fullmatch(pattern, text, re.I)

    if match(r"(?:check|show|read)(?: me)? (?:my |the |current )?(?:computer |desktop )?settings"):
        return Action("settings.overview", {})
    if match(r"(?:what(?:'s| is)(?: my| the)?|check|show) (?:screen )?brightness"):
        return Action("settings.brightness.get", {})
    if m := match(
        r"(?:set|change|turn|put) (?:my |the )?(?:screen )?brightness(?: up| down)? (?:to|at) (.+?)(?: percent|%)?"
    ):
        value = _spoken_percent(m[1])
        if value is not None:
            return Action("settings.brightness.set", {"percent": value})
    if m := match(
        r"(?:(?:turn|bring) )?(?:my |the )?(?:screen )?brightness (up|down)(?: by (.+?)(?: percent|%)?)?"
    ):
        value = _spoken_percent(m[2]) if m[2] else 10
        if value is not None:
            return Action(
                "settings.brightness.adjust", {"delta": value if m[1].lower() == "up" else -value}
            )
    if m := match(r"(?:make|turn) (?:my |the )?screen (brighter|dimmer|darker)"):
        return Action(
            "settings.brightness.adjust", {"delta": 10 if m[1].lower() == "brighter" else -10}
        )
    if match(
        r"(?:check|show|what(?:'s| is))(?: my| the)? (?:wifi|wi-fi|bluetooth|radio)(?: status)?"
    ):
        return Action("settings.radios.status", {})
    if m := match(r"(?:turn|switch) (wifi|wi-fi|bluetooth) (on|off)"):
        return Action(
            "settings.radio.set",
            {"radio": m[1].lower().replace("-", ""), "enabled": m[2].lower() == "on"},
        )
    if m := match(r"(?:turn|switch) (on|off) (wifi|wi-fi|bluetooth)"):
        return Action(
            "settings.radio.set",
            {"radio": m[2].lower().replace("-", ""), "enabled": m[1].lower() == "on"},
        )
    if match(r"(?:show|list|check)(?: my| the)? (?:app|application) (?:audio|volumes|sound)"):
        return Action("settings.audio.apps", {})
    if match(r"(?:show|list|check)(?: my| the)? (?:audio|sound) devices"):
        return Action("settings.audio.devices", {})
    if m := match(
        r"(?:set|change|switch) (?:my |the )?(?:audio |sound )?(input|output)(?: device)? to (.+)"
    ):
        return Action("settings.audio.select", {"direction": m[1].lower(), "name": m[2]})
    if m := match(r"use (.+) (?:as|for) (?:my |the )?(microphone|audio output|sound output)"):
        return Action(
            "settings.audio.select",
            {"direction": "input" if m[2].lower() == "microphone" else "output", "name": m[1]},
        )
    if m := match(r"(?:set|change) (?:the )?(.+?)(?:'s)? volume to (.+?)(?: percent|%)?"):
        value = _spoken_percent(m[2])
        if value is not None and m[1].lower() not in {
            "mic",
            "microphone",
            "my",
            "speaker",
            "speakers",
            "headphones",
        }:
            return Action("settings.audio.app_volume", {"application": m[1], "percent": value})
    if m := match(r"(mute|unmute) (firefox|spotify|discord)"):
        return Action(
            "settings.audio.app_mute", {"application": m[2], "muted": m[1].lower() == "mute"}
        )
    if m := match(r"(?:open|show)(?: me)? (?:my |the )?(.+?) (?:settings|preferences)"):
        aliases = {
            "screen": "display",
            "monitor": "display",
            "sound": "audio",
            "volume": "audio",
            "wi-fi": "wifi",
            "battery": "power",
            "keyboard shortcuts": "shortcuts",
            "nightlight": "night light",
            "defaults": "default apps",
            "theme": "colors",
            "date": "date and time",
            "time": "date and time",
            "user": "users",
        }
        page = aliases.get(m[1].lower(), m[1].lower())
        # Kept here instead of importing the tool module (avoids a registry cycle).
        if page in {
            "display",
            "audio",
            "bluetooth",
            "network",
            "wifi",
            "power",
            "keyboard",
            "shortcuts",
            "mouse",
            "touchpad",
            "notifications",
            "accessibility",
            "default apps",
            "night light",
            "screen lock",
            "date and time",
            "language",
            "printers",
            "users",
            "wallpaper",
            "colors",
            "fonts",
            "icons",
            "startup",
            "virtual desktops",
            "about",
        }:
            return Action("settings.open", {"page": page})
    return None
