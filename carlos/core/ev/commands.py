"""Small, composable intent grammar for explicit desktop requests.

Only the user's request enters here. Explanations, negations, hypothetical
requests, and quoted examples never acquire authority to execute an action.
"""

from __future__ import annotations

import re
import datetime as dt
from urllib.parse import urlsplit, urlunsplit, quote_plus
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Action:
    tool: str
    arguments: dict[str, Any]


def clock_request(text: str) -> str | None:
    """Recognize a current local clock question, including a final correction.

    Only reads time/date. Quoted examples and explanations are not clock reads.
    Never rewrite 'timer' into 'time' or use this for setting the system clock.
    """
    clean = text.strip().replace("’", "'")
    if re.search(r'["“”`]', clean) or re.match(
        r"^(?:explain|teach|write|type|imagine|suppose|if)\b", clean, re.I
    ):
        return None
    clean = re.split(r"[.!?]\s+", clean)[-1].strip(" .!?")
    clean = re.sub(r"^(?:(?:please|just|actually|hey|no)[,\s]+)+", "", clean, flags=re.I)
    clean = re.sub(r"\b(?:the\s+(?:fuck|hell)|fucking|damn)\s+", "", clean, flags=re.I)
    clean = re.sub(r"(?:[,\s]+please|\s+(?:right\s+)?now)$", "", clean, flags=re.I).strip()
    clean = re.sub(r"^(?:(?:can|could|would|will)\s+you\s+|please\s+)+", "", clean, flags=re.I)
    if re.fullmatch(
        r"(?:what\s+time\s+is\s+it|what(?:'s|\s+is)\s+(?:the\s+)?(?:current\s+)?time|(?:tell|give|show)\s+me\s+(?:(?:the|current)\s+)*(?:time|time\s+of\s+day)|(?:tell|give|show)\s+me\s+what\s+time\s+it\s+is|do\s+you\s+know\s+what\s+time\s+it\s+is|time)",
        clean,
        re.I,
    ):
        return "time"
    if re.fullmatch(
        r"(?:what(?:'s|\s+is)\s+(?:the\s+)?(?:date|day)(?:\s+today)?|what\s+(?:day|date)\s+is\s+it|(?:tell|give|show)\s+me\s+(?:the\s+)?(?:date|day)|what(?:'s|\s+is)\s+today's\s+date)",
        clean,
        re.I,
    ):
        return "date"
    return None


def strip_command_asides(text: str) -> str:
    if re.search(r'["“”`]|https?://', text, re.I):
        return text
    candidate = re.sub(r"^(?:(?:hey|bro|dude|man)[,\s]+)+", "", text, flags=re.I)
    if re.match(
        r"^(?:play|start)\s+(?:(?:my|the)\s+)?(?:music|spotify)\b", candidate, re.I
    ) and not re.search(r"\b(?:song|playlist|called|named|titled)\b", candidate, re.I):
        candidate = re.sub(
            r"(?:[,;!.?\s]+(?:bro|dude|man|please|thanks|thank you|already|for me|what the (?:fuck|hell)|wtf))+[.!?]*$",
            "",
            candidate,
            flags=re.I,
        )
        return candidate.strip()
    if not re.match(
        r"^(?:(?:please|just|actually|can you|could you|would you|will you)\s+)*(?:unpause|pause|resume|stop|skip|next|previous|turn|set|lower|raise|increase|decrease|mute|unmute|minimize|maximize|restore|open|close|launch)\b",
        candidate,
        re.I,
    ):
        return text
    candidate = re.sub(
        r"(?:[,;!.?\s]+(?:bro|dude|man|please|thanks|thank you|already|for me|what the (?:fuck|hell)|for fuck'?s sake|wtf))+[.!?]*$",
        "",
        candidate,
        flags=re.I,
    )
    return candidate.strip()


def request_text(text: str) -> str | None:
    text = text.strip().replace("’", "'")
    text = strip_command_asides(text)
    # Check negation again after stripping filler. Keep capability questions
    # as questions, even if they start with just or please.
    for _ in range(8):
        unwrapped = re.sub(
            r"^(?:(?:please|just|actually|go\s+ahead\s+and|do\s+me\s+a\s+favor\s+and)[,\s]+)+",
            "",
            text,
            flags=re.I,
        )
        unwrapped = re.sub(
            r"^((?:can|could|would|will)\s+you)\s+(?:(?:just|actually|please)[,\s]+)+",
            r"\1 ",
            unwrapped,
            flags=re.I,
        )
        if unwrapped == text:
            break
        text = unwrapped
    polite_audio = re.fullmatch(
        r"(?:can|could|would|will)\s+you\s+(make\s+it\s+(?:louder|quieter|softer))[.!?]*",
        text,
        re.I,
    )
    if polite_audio:
        return polite_audio[1]
    general_question = re.match(
        r"^(?:is|are|does|should|what\s+(?:makes|causes)|how\s+(?:can|does))\b", text, re.I
    )
    local_reference = re.search(r"\b(?:my|this|current|currently|our|these|those)\b", text, re.I)
    status_question = re.search(
        r"\b(?:running|open|muted|playing|paused|connected|enabled|disabled|installed)[.!?]*$",
        text,
        re.I,
    )
    if general_question and not local_reference and not status_question:
        return None
    if re.fullmatch(r"why\s+(?:can(?:not|'t)|could(?:not|'t))\s+you\s+find\s+.+", text, re.I):
        return text  # A bounded catalog lookup, not an application launch.
    if re.match(
        r"^(?:(?:please|can you|could you)\s+)*(?:create|build|design|develop|make)\b", text, re.I
    ) and re.search(
        r"\b(?:security|cybersecurity|cyber\s+sec)\b.*\b(?:tools?|scripts?|programs?|scanner)\b",
        text,
        re.I,
    ):
        return None  # Creating/discussing a tool is not running a local scan.
    # Talking about an app isn't permission to control it.
    if re.match(
        r"^(?:why\b|(?:do|did|does|should|would|could|can|am|are|is)\s+(?:i|we|it|they|a|an)\b|"
        r"(?:what|how)\s+(?:does|did|do|would|should)\b|what\s+(?:is|are)\s+(?:a|an)\b|"
        r"(?:can|could)\s+you\s+(?:create|build|write|design|develop|make|help|explain|teach|discuss|talk|understand|think)\b|"
        r"(?:are\s+you|do\s+you\s+(?:know|think|like|understand|support))\b|"
        r"(?:i\s+(?:was|am|feel|think|wonder|like|love|hate)|i\s+(?:need|want)\s+help|let's\s+(?:talk|discuss)|tell\s+me\s+(?:about|why|something|a\s+(?:story|joke))|"
        r"what(?:'s|\s+is)\s+your\s+(?:opinion|view|favorite)|how\s+are\s+you|"
        r"what\s+(?:is|are)\s+(?:firewalls?|security|ssh|cybersecurity|vision|accessibility|coding|python))\b)",
        text,
        re.I,
    ):
        return None
    if re.match(r"^how\s+much\b", text, re.I) and re.search(
        r"\b(?:need|recommend|buy|cost|should)\b", text, re.I
    ):
        return None
    if re.match(
        r"^(?:how\s+(?:(?:do|can|would|should)\s+(?:i|we|someone|you)\b|to\b)|what\s+(?:would|happens|is\s+the\s+way)|explain\b|tell\s+me\s+how|show\s+me\s+how|if\b|suppose\b|imagine\b)",
        text,
        re.I,
    ):
        return None
    text = re.sub(
        r"^(?:(?:can|could|would|will)\s+you\s+|(?:i\s+(?:want|need)\s+you\s+to\s+)|please\s+)+",
        "",
        text,
        flags=re.I,
    )
    if re.match(r"^(?:don't|dont|do\s+not|never|avoid)\b", text, re.I):
        return None
    if re.match(
        r"(?:take|save|create|write|add) (?:a )?(?:note|snippet) .+?(?::|\bsaying\b|\bwith text\b)\s*\S",
        text,
        re.I,
    ):
        return text  # Saved content is literal, including terminal punctuation.
    # Preserve punctuation inside a literal terminal URL (query/fragment/path).
    if re.search(r"https?://\S+$", text, re.I):
        return text.strip()
    return re.sub(r"(?:,?\s+please)?[.!?]*$", "", text, flags=re.I).strip()


def media_request(text: str) -> Action | None:
    clean = request_text(text)
    if not clean or re.search(r'["“”`]', clean):
        return None
    clean = re.sub(r"\s+(?:right\s+)?now$", "", clean, flags=re.I)
    clean = re.sub(
        r"^get\s+(?:my\s+|the\s+)?spotify\s+(?:playing|going)(?:\s+again)?$",
        "resume Spotify",
        clean,
        flags=re.I,
    )
    clean = re.sub(
        r"^(?:put|turn)\s+(?:(?:my|the)\s+)?music\s+back\s+on$",
        "resume my music",
        clean,
        flags=re.I,
    )
    clean = re.sub(
        r"^(?:put|turn)\s+(?:(?:my|the)\s+)?spotify\s+(?:back\s+)?on$",
        "resume Spotify",
        clean,
        flags=re.I,
    )
    clean = re.sub(
        r"^(?:pause|stop)\s+(?:the\s+)?(?:playback\s+)?(?:on|in)\s+(?:my\s+)?spotify$",
        "pause Spotify",
        clean,
        flags=re.I,
    )
    clean = re.sub(
        r"^(?:resume|unpause)\s+(?:the\s+)?(?:playback\s+)?(?:on|in)\s+(?:my\s+)?spotify$",
        "resume Spotify",
        clean,
        flags=re.I,
    )
    target = r"(?:(?:my|the|this|current)\s+)?(?:music|song|track|playback|spotify(?:\s+music)?)"
    player = r"(?:\s+(?:on|in|from|off|using)\s+(?:my\s+)?spotify)?"
    patterns = (
        (rf"(?:pause|stop)\s+{target}{player}|turn\s+{target}\s+off", "pause"),
        (
            rf"(?:play|resume|unpause|un\s+pause|start(?:\s+playing)?)\s+{target}{player}|turn\s+{target}\s+back\s+on",
            "play",
        ),
        (rf"(?:skip|next)(?:\s+(?:(?:to\s+)?(?:the\s+)?next\s+)?(?:song|track))?{player}", "next"),
        (
            rf"(?:previous|last)\s+(?:song|track){player}|go\s+back\s+(?:a\s+|one\s+)?(?:song|track){player}",
            "previous",
        ),
    )
    for pattern, action in patterns:
        if re.fullmatch(pattern, clean, re.I):
            arguments = {"action": action}
            if re.search(r"\bspotify\b", clean, re.I):
                arguments["player"] = "spotify"
            return Action("audio.media", arguments)
    return None


def direct_action(text: str) -> Action | None:
    clean = request_text(text)
    if not clean:
        return None
    if action := website_request(clean):
        return action
    if action := audio_request(clean):
        return action
    if action := media_request(clean):
        return action
    from .handsfree_commands import handsfree_action

    if action := handsfree_action(clean):
        return action
    clean = re.sub(r"\s+(?:right\s+)?now$", "", clean, flags=re.I)
    if re.fullmatch(
        r"(?:check|inspect|show)(?:\s+me)?\s+(?:my\s+|the\s+)?(?:boot(?:\s+status)?|firmware(?:\s+status)?)",
        clean,
        re.I,
    ):
        return Action("system.boot.status", {})
    if re.fullmatch(
        r"(?:check|inspect|show|list)(?:\s+me)?\s+(?:my\s+|the\s+)?(?:startup|autostart)\s+(?:apps|applications|programs|entries)",
        clean,
        re.I,
    ):
        return Action("system.startup.list", {})
    video = r"(?:(?:my|the|this|current)\s+)?(?:youtube(?:\s+video)?|video)"
    for pattern, action in (
        (
            rf"(?:full\s*screen)\s+{video}|(?:make|put)\s+{video}\s+(?:in\s+)?full\s*screen(?:\s+mode)?",
            "fullscreen",
        ),
        (
            rf"(?:exit|leave)\s+full\s*screen\s+(?:on|in|for)\s+{video}|unfullscreen\s+{video}",
            "exit_fullscreen",
        ),
        (rf"(?:play|resume)\s+{video}", "play"),
        (rf"pause\s+{video}", "pause"),
        (rf"mute\s+{video}", "mute"),
        (rf"unmute\s+{video}", "unmute"),
        (rf"(?:put|make)\s+{video}\s+(?:in\s+)?theat(?:er|re)\s+mode", "theater"),
        (rf"(?:exit|leave)\s+theat(?:er|re)\s+mode\s+(?:on|in|for)\s+{video}", "default_view"),
    ):
        if re.fullmatch(pattern, clean, re.I):
            return Action(
                "browser.video",
                {
                    "description": "YouTube" if "youtube" in clean.casefold() else "current window",
                    "action": action,
                },
            )
    for pattern, action in (
        (r"zoom in(?:\s+(?:on|in)\s+(?:the\s+)?(?:browser|page))?", "zoom_in"),
        (r"zoom out(?:\s+(?:on|in)\s+(?:the\s+)?(?:browser|page))?", "zoom_out"),
        (r"reset (?:the )?(?:browser |page )?zoom", "reset_zoom"),
        (r"scroll to (?:the )?(?:top|start)(?: of (?:the )?page)?", "top"),
        (r"scroll to (?:the )?(?:bottom|end)(?: of (?:the )?page)?", "bottom"),
        (r"(?:find|search) (?:text )?(?:on|in) (?:this|the|my) page", "find"),
    ):
        if re.fullmatch(pattern, clean, re.I):
            return Action("browser.shortcut", {"description": "current window", "action": action})
    machine = r"(?:(?:my|this|the)\s+)?(?:computer|pc|laptop|machine|system)"
    for pattern, action in [
        (rf"(?:shut\s*down|power\s*off)(?:\s+{machine})?", "shutdown"),
        (rf"turn\s+(?:off\s+{machine}|{machine}\s+off)", "shutdown"),
        (rf"(?:restart|reboot)(?:\s+{machine})?", "reboot"),
        (rf"(?:suspend|sleep)(?:\s+{machine})?|put\s+{machine}\s+to\s+sleep", "suspend"),
        (rf"lock(?:\s+(?:{machine}|(?:(?:my|the)\s+)?screen))?", "lock"),
        (r"log\s*(?:out|off)(?:\s+(?:me|my\s+session))?", "logout"),
    ]:
        if re.fullmatch(pattern, clean, re.I):
            return Action("system.power", {"action": action})
    if re.fullmatch(
        r"(?:cancel|abort|stop)\s+(?:the\s+)?(?:shutdown|shut\s*down|reboot|restart|logout|power\s+action)",
        clean,
        re.I,
    ):
        return Action("system.power.cancel", {})
    if re.fullmatch(r"(?:restore|show)\s+all\s+windows", clean, re.I):
        return Action("desktop.show_desktop", {"show": False})
    match = re.fullmatch(
        r"(minimi[sz]e|maximi[sz]e|restore|fullscreen|unfullscreen|unminimi[sz]e)\s+(.+)",
        clean,
        re.I,
    )
    if match:
        action = match[1].lower().replace("sise", "size")
        action = {
            "minimise": "minimize",
            "maximise": "maximize",
            "unminimize": "restore",
            "unminimise": "restore",
            "unfullscreen": "restore",
        }.get(action, action)
        target = match[2]
        if re.fullmatch(r"(?:(?:my|the|this|current|active)\s+)?window", target, re.I):
            target = "current window"
        return Action("window.state", {"description": target, "action": action})
    match = re.fullmatch(
        r"(?:make|put)\s+(.+?)\s+(?:in\s+)?(?:full\s*screen|full\s*screen\s+mode)", clean, re.I
    )
    if match:
        return Action("window.state", {"description": match[1], "action": "fullscreen"})
    if re.fullmatch(r"(?:show\s+(?:me\s+)?(?:the|my)\s+desktop|hide\s+all\s+windows)", clean, re.I):
        return Action("desktop.show_desktop", {"show": True})
    if re.fullmatch(
        r"(?:inspect|read|check|show)(?:\s+me)?\s+(?:my\s+)?(?:live|runtime)\s+firewall(?:\s+rules)?(?:\s+(?:as\s+admin|with\s+admin\s+access))?",
        clean,
        re.I,
    ):
        return Action(
            "security.firewall.runtime", {"authorize": bool(re.search(r"\badmin\b", clean, re.I))}
        )
    duration = r"(\d+(?:\.\d+)?|one|two|three|four|five|ten|fifteen|twenty|thirty|sixty|an?|half)\s*(seconds?|minutes?|hours?|days?)"
    reminder = re.fullmatch(
        rf"remind\s+me\s+(?:to\s+)?(.+?)\s+(in|every)\s+{duration}", clean, re.I
    )
    timer = re.fullmatch(
        rf"(?:set\s+|start\s+)?(?:a\s+)?(?:timer\s+(?:for\s+)?{duration}|{duration}\s+timer)(?:\s+(?:called|named)\s+(.+))?",
        clean,
        re.I,
    )
    if reminder or timer:
        words = {
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "ten": 10,
            "fifteen": 15,
            "twenty": 20,
            "thirty": 30,
            "sixty": 60,
            "a": 1,
            "an": 1,
            "half": 0.5,
        }
        if reminder:
            label, repeat, number, unit = reminder.groups()
        else:
            assert timer
            first, unit1, second, unit2, label = timer.groups()
            number, unit, repeat = first or second, unit1 or unit2, "in"
            label = label or f"{number} {unit} timer"
        value = words.get(number.lower())
        seconds = (value if value is not None else float(number)) * {
            "second": 1,
            "minute": 60,
            "hour": 3600,
            "day": 86400,
        }[unit.lower().rstrip("s")]
        return Action(
            "reminders.create",
            {
                "label": label,
                "seconds": seconds,
                "repeat_seconds": seconds if repeat.lower() == "every" else 0,
            },
        )
    alarm = re.fullmatch(
        r"(?:set\s+)?(?:an?\s+)?alarm\s+(?:for\s+|at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s+(tomorrow))?(?:\s+(?:called|named)\s+(.+))?",
        clean,
        re.I,
    )
    if alarm:
        hour, minute, meridiem, tomorrow, label = alarm.groups()
        h, m = int(hour), int(minute or 0)
        if (meridiem and not 1 <= h <= 12) or h > 23 or m > 59:
            return None
        if meridiem:
            h = h % 12 + (12 if meridiem.lower() == "pm" else 0)
        now = dt.datetime.now()
        when = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if tomorrow or when <= now:
            when += dt.timedelta(days=1)
        return Action(
            "reminders.create",
            {
                "label": label or f"Alarm at {h:02}:{m:02}",
                "seconds": when.timestamp() - now.timestamp(),
            },
        )
    if re.fullmatch(
        r"(?:list|show|check)(?:\s+me)?\s+(?:my\s+)?(?:reminders|timers|alarms)", clean, re.I
    ):
        return Action("reminders.list", {})
    match = re.fullmatch(
        r"(?:cancel|delete|remove)\s+(?:the\s+)?(?:reminder|timer|alarm)\s+(.+)", clean, re.I
    )
    if match:
        return Action("reminders.cancel", {"identifier": match[1]})
    match = re.fullmatch(
        r"(?:set|save)\s+(?:app\s+)?alias\s+(.+?)\s+(?:to|as|for)\s+(.+)", clean, re.I
    )
    if match:
        return Action("applications.alias.save", {"name": match[1], "target": match[2]})
    if re.fullmatch(r"(?:list|show)(?:\s+my)?\s+(?:app\s+)?aliases", clean, re.I):
        return Action("applications.alias.list", {})
    match = re.fullmatch(r"(?:remove|forget|delete)\s+(?:app\s+)?alias\s+(.+)", clean, re.I)
    if match:
        return Action("applications.alias.remove", {"name": match[1]})
    match = re.fullmatch(r"(?:save|create)\s+(?:a\s+)?routine\s+([^:]+):\s*(.+)", clean, re.I)
    if match:
        return Action(
            "routines.save",
            {
                "name": match[1],
                "commands": [
                    item.strip()
                    for item in re.split(r";|\s+then\s+", match[2], flags=re.I)
                    if item.strip()
                ],
            },
        )
    if re.fullmatch(r"(?:list|show)(?:\s+my)?\s+routines", clean, re.I):
        return Action("routines.list", {})
    match = re.fullmatch(r"(?:run|start)\s+(?:my\s+)?routine\s+(.+)", clean, re.I)
    if match:
        return Action("routines.run", {"name": match[1]})
    match = re.fullmatch(r"(?:delete|remove)\s+(?:my\s+)?routine\s+(.+)", clean, re.I)
    if match:
        return Action("routines.remove", {"name": match[1]})
    if re.fullmatch(
        r"(?:list|show)(?:\s+me)?\s+(?:my\s+)?(?:wallpapers|scenes|themes)", clean, re.I
    ):
        return Action("scenes.list", {})
    match = re.fullmatch(
        r"(?:set|change|switch)(?:\s+my)?\s+(?:wallpaper|scene|theme)\s+to\s+(.+)|go\s+([a-z][\w -]+)",
        clean,
        re.I,
    )
    if match:
        return Action("scenes.apply", {"name": match[1] or match[2]})
    match = re.fullmatch(r"(?:search|find)\s+(.+?)\s+(?:on|in)\s+spotify", clean, re.I)
    if match:
        return Action("spotify.search", {"query": match[1]})
    playlist = re.fullmatch(
        r"(?:play|shuffle)\s+(?:(?:a\s+)?(?:(random|any)\s+)?(?:song|track|music)\s+(?:from|on|in)\s+)?(?:my\s+)(.+?)\s+playlist(?:\s+(?:on|in)\s+spotify)?",
        clean,
        re.I,
    )
    if not playlist:
        playlist = re.fullmatch(
            r"(?:play|shuffle)\s+()my\s+playlist\s+(.+?)(?:\s+(?:on|in)\s+spotify)?", clean, re.I
        )
    if playlist:
        return Action(
            "spotify.play",
            {
                "query": playlist[2],
                "kind": "playlist",
                "own_playlist": True,
                "random": bool(playlist[1]) or clean.lower().startswith("shuffle"),
            },
        )
    match = re.fullmatch(
        r"play\s+(?:(track|song|album|playlist|artist)\s+)?(.+?)\s+(?:on|in)\s+spotify", clean, re.I
    )
    if match:
        return Action(
            "spotify.play",
            {
                "query": match[2],
                "kind": "track" if match[1] in {None, "song"} else match[1].lower(),
            },
        )
    if re.fullmatch(r"(?:list|show)(?:\s+my)?\s+spotify\s+playlists", clean, re.I):
        return Action("spotify.playlists", {})
    if re.fullmatch(r"(?:check|diagnose|test)\s+(?:my\s+)?spotify(?:\s+connection)?", clean, re.I):
        return Action("spotify.diagnose", {})
    match = re.fullmatch(r"(?:preview\s+)?organi[sz]e\s+(?:files\s+in\s+)?(.+)", clean, re.I)
    if match:
        return Action("files.organize.preview", {"path": match[1].strip('"')})
    match = re.fullmatch(r"(apply|undo)\s+organization\s+([a-f0-9]{32})", clean, re.I)
    if match:
        return Action("files.organize." + match[1].lower(), {"preview_id": match[2].lower()})
    return None


_BROWSERS = r"firefox|chromium|chrome|brave|vivaldi|edge|opera|librewolf"
_SITES = {
    "youtube": "youtube.com",
    "google": "google.com",
    "github": "github.com",
    "reddit": "reddit.com",
    "twitch": "twitch.tv",
    "wikipedia": "wikipedia.org",
}


def normalize_web_url(value: str) -> str:
    value = value.strip().strip('"“”')
    if re.search(r"[\x00-\x20\x7f\\]", value) or len(value) > 2000:
        raise ValueError("Specify one HTTP or HTTPS URL without spaces or control characters")
    value = _SITES.get(value.casefold(), value)
    if not re.match(r"^[a-z][a-z0-9+.-]*:", value, re.I):
        value = "https://" + value
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(
            "Only HTTP/HTTPS websites without embedded login credentials are supported"
        )
    hostname = parsed.hostname.encode("idna").decode("ascii")
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", hostname) or (
        "." not in hostname and hostname != "localhost"
    ):
        raise ValueError("Specify a complete website address")
    port = parsed.port
    return urlunsplit(
        (
            parsed.scheme.lower(),
            hostname + (f":{port}" if port else ""),
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def website_request(clean: str) -> Action | None:
    target = "default"
    clean = clean.strip()
    # Do not let punctuation cleanup silently strip a URL fragment or query.
    suffix = re.search(
        rf"\s+(?:in|on|using|with)\s+(?:(?:my|the)\s+)?({_BROWSERS})(?:\s+browser)?$", clean, re.I
    )
    if suffix:
        target, clean = suffix[1].lower(), clean[: suffix.start()]
    clean = re.sub(r"\s+for\s+me$", "", clean, flags=re.I)
    prefix = re.match(
        rf"^(?:open|launch|start)\s+({_BROWSERS})\s*(?:and\s+(?:then\s+)?|then\s+|,\s*)(.+)$",
        clean,
        re.I,
    )
    if prefix:
        target, clean = prefix[1].lower(), prefix[2]
    else:
        browser_destination = re.fullmatch(
            rf"(?:open|launch|start)\s+({_BROWSERS})\s+(?:to|at|with)\s+(.+)", clean, re.I
        )
        if browser_destination:
            target, clean = browser_destination[1].lower(), "open " + browser_destination[2]
        prefixed = re.match(rf"^(?:in|using|on)\s+({_BROWSERS})[,\s]+(.+)$", clean, re.I)
        if prefixed:
            target, clean = prefixed[1].lower(), prefixed[2]
    search = re.fullmatch(r"(?:search(?:\s+for)?|look\s+up)\s+(.+)", clean, re.I)
    if search and target != "default":
        return Action(
            "browser.open_url",
            {"url": "https://www.google.com/search?q=" + quote_plus(search[1]), "browser": target},
        )
    navigation = re.fullmatch(
        r"(?:open|visit|navigate\s+to|go\s+to|browse\s+to|take\s+me\s+to)\s+(?:(?:the\s+)?(?:website|url|site)\s+)?(.+)",
        clean,
        re.I,
    )
    if not navigation:
        return None
    candidate = navigation[1].strip()
    candidate = re.sub(r"^(?:the\s+)", "", candidate, flags=re.I)
    candidate = re.sub(r"\s+dot\s+", ".", candidate, flags=re.I)
    try:
        url = normalize_web_url(candidate)
    except (ValueError, UnicodeError):
        return None
    return Action("browser.open_url", {"url": url, "browser": target})


def _spoken_percent(text: str) -> int | None:
    text = text.lower().replace("-", " ").strip()
    if text.isdecimal():
        return int(text)
    ones = dict(
        zip(
            "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
            range(20),
        )
    )
    tens = dict(
        zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10))
    )
    if text in ones:
        return ones[text]
    if text in {"hundred", "a hundred", "one hundred", "max", "maximum", "full"}:
        return 100
    if text in {"half", "halfway"}:
        return 50
    parts = text.split()
    if (
        parts
        and parts[0] in tens
        and (len(parts) == 1 or len(parts) == 2 and parts[1] in ones and ones[parts[1]] < 10)
    ):
        return tens[parts[0]] + (ones[parts[1]] if len(parts) == 2 else 0)
    return None


def audio_request(clean: str) -> Action | None:
    clean = clean.casefold().strip()
    clean = re.sub(r"\s+for\s+me$", "", clean)
    if re.search(
        r"\b(?:mic|microphone|spotify|firefox|youtube|discord|headphones?|speakers?)\b", clean
    ):
        return None  # Do not redirect an app/device-specific operation to global output.
    if re.fullmatch(
        r"(?:what(?:'s| is)(?: my| the| current)? (?:volume|sound)(?: level)?|how loud is (?:it|my (?:volume|sound))|check (?:my |the )?volume)",
        clean,
    ):
        return Action("audio.get_volume", {})
    if re.fullmatch(
        r"(?:unmute(?: (?:my |the )?(?:audio|sound|volume))?|(?:turn|put) (?:the |my )?(?:sound|audio) on)",
        clean,
    ):
        return Action("audio.set_mute", {"muted": False})
    if re.fullmatch(
        r"(?:mute(?: (?:my |the )?(?:audio|sound|volume))?|(?:turn|put) (?:the |my )?(?:sound|audio) off)",
        clean,
    ):
        return Action("audio.set_mute", {"muted": True})
    # Exact target levels take precedence over 'up/down' words.
    absolute = re.fullmatch(
        r"(?:(?:set|turn|put|change|make) )?(?:my |the )?(?:volume|sound)(?: level)?(?: (?:up|down))? (?:to |at )?(.+?)(?:\s*(?:%|percent))?",
        clean,
    )
    if absolute and not (
        re.search(r"\b(?:up|down)\b", clean) and not re.search(r"\b(?:to|at)\b", clean)
    ):
        percent = _spoken_percent(absolute[1])
        if percent is not None and 0 <= percent <= 100:
            return Action("audio.set_volume", {"percent": percent})
    relative = re.fullmatch(
        r"(?:(?:turn|bring) )?(?:my |the )?(?:volume|sound) (up|down)(?: (?:by )?(.+?)(?:\s*(?:%|percent))?)?",
        clean,
    )
    if not relative:
        relative = re.fullmatch(
            r"(increase|decrease|raise|lower) (?:my |the )?(?:volume|sound)(?: (?:by )?(.+?)(?:\s*(?:%|percent))?)?",
            clean,
        )
    if not relative:
        relative = re.fullmatch(
            r"(increase|decrease|raise|lower) it(?: (?:by )?(.+?)(?:\s*(?:%|percent))?)?", clean
        )
    if relative:
        amount = _spoken_percent(relative[2]) if relative[2] else 10
        if amount is not None and 1 <= amount <= 100:
            return Action(
                "audio.adjust_volume",
                {"delta": amount if relative[1] in {"up", "increase", "raise"} else -amount},
            )
    shorthand = re.fullmatch(r"(?:(?:make|turn) it )?(louder|quieter|softer)", clean)
    if shorthand:
        return Action("audio.adjust_volume", {"delta": 10 if shorthand[1] == "louder" else -10})
    short_direction = re.fullmatch(
        r"(?:turn|bring)\s+(?:it\s+(up|down)|(up|down)\s+(?:the\s+|my\s+)?(?:volume|sound))(?:\s+(?:a\s+)?(?:bit|little))?",
        clean,
    )
    if short_direction:
        return Action(
            "audio.adjust_volume",
            {"delta": 10 if (short_direction[1] or short_direction[2]) == "up" else -10},
        )
    return None


def browser_input(text: str) -> dict[str, Any] | None:
    clean = request_text(text)
    if not clean:
        return None
    target = "browser"
    suffix = re.search(
        r"\s+(?:in|on)\s+(firefox|chromium|chrome|brave|vivaldi|edge|opera)(?:\s+browser)?$",
        clean,
        re.I,
    )
    if suffix:
        target, clean = suffix[1], clean[: suffix.start()]
    key = None
    for pattern, shortcut in (
        (r"(?:open|make|create)\s+(?:a\s+)?new\s+tab", ("t", ["ctrl"])),
        (r"(?:close|shut)\s+(?:the\s+|this\s+|current\s+)?tab", ("w", ["ctrl"])),
        (r"(?:reopen|restore)\s+(?:(?:the|last|closed)\s+)*tab", ("t", ["ctrl", "shift"])),
        (r"(?:refresh|reload)(?:\s+(?:this|the|my|current))?(?:\s+(?:page|tab))?", ("r", ["ctrl"])),
        (r"go\s+back(?:\s+(?:a\s+)?page)?", ("left", ["alt"])),
        (r"go\s+forward(?:\s+(?:a\s+)?page)?", ("right", ["alt"])),
        (r"(?:switch\s+to\s+)?(?:the\s+)?next\s+tab", ("tab", ["ctrl"])),
        (r"(?:switch\s+to\s+)?(?:the\s+)?previous\s+tab", ("tab", ["ctrl", "shift"])),
        (r"(?:focus|select)\s+(?:the\s+)?address\s+bar", ("l", ["ctrl"])),
    ):
        if re.fullmatch(pattern, clean, re.I):
            key = shortcut
            break
    if key:
        return {
            "actions": [
                {
                    "window": target,
                    "tool": "desktop.keyboard.key",
                    "arguments": {"key": key[0], "modifiers": key[1]},
                }
            ]
        }
    navigation = re.fullmatch(r"(?:navigate|go|browse)\s+to\s+(https?://\S+)", clean, re.I)
    if navigation and len(navigation[1]) <= 2000:
        return {
            "actions": [
                {
                    "window": target,
                    "tool": "desktop.keyboard.key",
                    "arguments": {"key": "l", "modifiers": ["ctrl"]},
                },
                {
                    "window": target,
                    "tool": "desktop.keyboard.type_text",
                    "arguments": {"text": navigation[1]},
                },
                {
                    "window": target,
                    "tool": "desktop.keyboard.key",
                    "arguments": {"key": "enter", "modifiers": []},
                },
            ]
        }
    return None
