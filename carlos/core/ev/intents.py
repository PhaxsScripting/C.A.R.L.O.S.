from __future__ import annotations

import re

APPLICATION_ALIASES = {
    "visual studio code": "code",
    "vs code": "code",
    "vscode": "code",
    "code": "code",
    "mozilla firefox": "firefox",
    "firefox": "firefox",
    "discord": "discord",
    "steam": "steam",
    "spotify": "spotify",
    "terminal": "konsole",
    "konsole": "konsole",
    "phaxity audio": "phaxity-neko-music",
    "phaxity neko music": "phaxity-neko-music",
    "neko music": "phaxity-neko-music",
    "nico music": "phaxity-neko-music",
    "tax and neko music": "phaxity-neko-music",
    "tax neko music": "phaxity-neko-music",
}


CLOSE_APPLICATION_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:close|quit|exit|terminate|kill|stop|shut\s+down)\s+(.+?)[.!?]?\s*$",
    re.IGNORECASE,
)

_ACTION_START = (
    r"(?:open|launch|start|close|quit|exit|terminate|kill|stop|shut\s+down|"
    r"play|resume|unpause|pause|skip|next|previous|go\s+back|set|turn|lower|raise|increase|decrease|mute|unmute|"
    r"find|show|check|what|how|remember|forget|focus|switch\s+to|bring|type|write|"
    r"enter|press|hit|send|click|double[- ]click|right[- ]click|middle[- ]click|"
    r"scroll|move\s+(?:the\s+)?(?:mouse|pointer|cursor))\b"
)
_EXECUTABLE_START = re.compile(
    r"^\s*(?:please\s+)?(?:open|launch|start|close|quit|exit|terminate|kill|stop|shut\s+down|"
    r"play|resume|unpause|pause|skip|next|previous|go\s+back|set|turn|lower|raise|increase|decrease|mute|unmute|find|show|check|"
    r"remember|forget|focus|switch\s+to|bring|type|write|enter|press|hit|send|click|"
    r"double[- ]click|right[- ]click|middle[- ]click|scroll|"
    r"move\s+(?:the\s+)?(?:mouse|pointer|cursor))\b",
    re.IGNORECASE,
)

# Whisper occasionally renders "then" as "Ben".  It is only repaired when
# it sits between two complete action clauses, never inside an app/file name.
_ACTION_SEPARATOR = re.compile(
    rf"\s*(?:[,;]\s*)?(?:(?:and\s+)?then|ben)(?:\s*,?\s*and)?\s+(?={_ACTION_START})|"
    rf"\s*(?:[,;]\s*|\s+and\s+)(?={_ACTION_START})",
    re.IGNORECASE,
)
_QUOTED_LITERAL = re.compile(r"""(?:"[^"\n]*"|'[^'\n]*'|“[^”\n]*”|‘[^’\n]*’)""")


def application_query(spoken: str) -> str:
    """Map a spoken app name to the stable private process-search identity."""

    query = spoken.casefold().strip(' \t.,!?"')
    query = re.sub(r"^(?:out\s+|the\s+|my\s+)+", "", query)
    query = re.sub(
        r"\s+(?:apps?|applications?|windows?|browser|client|programs?)$", "", query
    ).strip()
    query = re.sub(r"[,;:_-]+", " ", query)
    query = re.sub(r"\s+", " ", query).strip()
    query = re.sub(r"\b(?:fact\s*city|faxity|phacity)\b", "phaxity", query)
    query = re.sub(r"\bnico\s+music\b", "neko music", query)
    return APPLICATION_ALIASES.get(query, query)


def split_action_clauses(text: str) -> list[str]:
    """Split explicit actions and bounded shared-verb application closes."""

    original = text.strip()
    # Quoted payloads are data to type, never a second desktop command.
    masked = _QUOTED_LITERAL.sub(lambda match: "\ufffc" * len(match.group()), original)
    separators = list(_ACTION_SEPARATOR.finditer(masked))
    starts = [0, *(match.end() for match in separators)]
    ends = [*(match.start() for match in separators), len(original)]
    clauses = [original[start:end].strip(" \t,;.") for start, end in zip(starts, ends)]
    clauses = [part for part in clauses if part]
    expanded: list[str] = []
    for clause in clauses:
        # Do not guess whether 'and' belongs to an unknown application name,
        # quoted data, a negation, or commentary. Only known app identities
        # may inherit a close verb; stop remains playback control.
        from .commands import request_text

        clean = request_text(clause)
        match = re.fullmatch(r"(close|quit|exit)\s+(.+)", clean or "", re.I)
        if match and not re.search(r'["“”`]', clause):
            targets = re.split(r"\s+and\s+|,\s*(?:and\s+)?", match[2], flags=re.I)
            if 2 <= len(targets) <= 8 and all(
                application_query(target) in APPLICATION_ALIASES.values() for target in targets
            ):
                expanded.extend(f"{match[1]} {target.strip()}" for target in targets)
                continue
        expanded.append(clause)
    clauses = expanded
    if len(clauses) < 2 or sum(bool(_EXECUTABLE_START.match(part)) for part in clauses) < 2:
        return [original]
    return clauses


def extract_close_targets(text: str) -> tuple[str, ...]:
    """Return canonical explicit close targets for high-impact voice checks."""

    from .commands import media_request, request_text
    from .voice.normalization import is_conversation_stop

    targets: list[str] = []
    for clause in split_action_clauses(text):
        clause = request_text(clause)
        if not clause or is_conversation_stop(clause) or media_request(clause):
            continue
        match = CLOSE_APPLICATION_PATTERN.match(clause)
        if not match:
            continue
        target = application_query(match.group(1))
        # "stop Spotify/music" is playback control, while "close Spotify" is
        # an application close.  Keep the safety verifier aligned with routing.
        if clause.casefold().lstrip().startswith("stop ") and (
            target == "spotify" or re.search(r"\b(?:music|song|track)\b", target)
        ):
            continue
        targets.append(target)
    return tuple(targets)
