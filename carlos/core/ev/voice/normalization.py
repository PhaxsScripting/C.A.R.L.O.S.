from __future__ import annotations

import re
from dataclasses import dataclass

from ..commands import request_text

_HESITATION = re.compile(r"(?<![\w'])\b(?:u+h+|u+m+|h+m+)\b(?![\w'])", re.IGNORECASE)
_LEADING_DISCOURSE = re.compile(
    r"^\s*(?:(?:well|okay|ok|so|basically)\b[\s,;:.-]*)+",
    re.IGNORECASE,
)
_TRAILING_DISCOURSE = re.compile(r"[\s,;:.-]+(?:okay|ok|so)\s*[.!?]*$", re.IGNORECASE)
_WAKE_PREFIX = re.compile(
    r"^\s*(?:(?:hey|yo)[\s,!.:-]+)?(?:carlos|e\s*[.\-]?\s*v\.?|v\s*[.\-]?\s*v\.?|t\s*[.\-]?\s*v\.?|eevee|evee|evie|eve)\b[\s,;:!.-]*",
    re.IGNORECASE,
)


def normalize_transcript(raw: str) -> str:
    """Conservatively remove wake/filler speech without changing semantics."""

    text = raw.strip()
    for _ in range(5):
        stripped = _WAKE_PREFIX.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    text = _LEADING_DISCOURSE.sub("", text, count=1) or text
    text = _HESITATION.sub(" ", text)
    # Whisper commonly spaces spoken computer acronyms. Join only the small,
    # explicit vocabulary used by deterministic system intents.
    text = re.sub(r"\br[.\s-]+a[.\s-]+m\b\.?", "RAM", text, flags=re.IGNORECASE)
    text = re.sub(r"\bc[.\s-]+p[.\s-]+u\b\.?", "CPU", text, flags=re.IGNORECASE)
    text = re.sub(r"\bg[.\s-]+p[.\s-]+u\b\.?", "GPU", text, flags=re.IGNORECASE)
    # Bounded repairs for repeatable application-name errors observed from the
    # local microphone. They only apply to a leading close command and do not
    # rewrite ordinary conversation.
    text = re.sub(
        r"^\s*closed?\s*[- ]\s*(?:fact\s*city|faxity|phacity)\b",
        "close phaxity",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^\s*closed\s+", "close ", text, count=1, flags=re.IGNORECASE)
    text = _TRAILING_DISCOURSE.sub("", text, count=1)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,;:]){2,}", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" \t,;:")
    return text


@dataclass(frozen=True)
class SpeechInterpretation:
    text: str
    repair: str = ""
    clarification: str = ""


def interpret_spoken_command(text: str, *, media_active: bool = False) -> SpeechInterpretation:
    clean = request_text(text)
    if not clean or re.search(r'["“”`]', text):
        return SpeechInterpretation(text)
    clean = re.sub(r"\s+", " ", clean.casefold().replace(",", " ")).strip()
    if clean == "pause spotify":
        return SpeechInterpretation(text)
    if re.fullmatch(
        r"(?:pause|plause|paws) (?:my |the )?(?:spotify|spot ify|spot a fi|spot for it|spawn for it)",
        clean,
    ):
        return SpeechInterpretation("pause Spotify", "confirmed_spotify_pause_mishearing")
    if re.fullmatch(r"(?:plause|paws) (?:my |the )?music", clean):
        return SpeechInterpretation("pause my music", "confirmed_pause_mishearing")
    if clean == "plasma music":
        if media_active:
            return SpeechInterpretation(
                "pause my music", "confirmed_pause_music_mishearing_with_playback"
            )
        return SpeechInterpretation(
            text, clarification="Did you mean pause your music? Say pause my music if so."
        )
    if re.fullmatch(r"(?:pause|plause|paws) (?:spot|spawn) .{1,24}", clean):
        return SpeechInterpretation(
            text, clarification="Which player should I pause? You can say pause Spotify."
        )
    return SpeechInterpretation(text)


def is_negative_action(text: str) -> bool:
    lowered = text.casefold().replace("’", "'")
    return bool(
        re.search(
            r"\b(?:do\s+not|don't|dont|never)\b(?!\s+(?:know|understand|remember)\b)[^.!?;]{0,36}\b(?:close|quit|exit|kill|stop|terminate|shut\s+down|open|launch|pause|unpause|resume|mute|unmute)\b",
            lowered,
        )
    )


def is_conversation_stop(text: str) -> bool:
    lowered = normalize_transcript(text).casefold().replace("’", "'").strip(" .,!?")
    if re.fullmatch(
        r"(?:(?:alright|all right|okay|ok)[, .!]+)?(?:enough|that's enough|thats enough|stop responding|stop answering)(?:[, .!]+(?:good|thanks|thank you|now|please))?",
        lowered,
    ):
        return True
    return bool(
        re.fullmatch(
            r"(?:stop(?:\s+(?:talking|listening|the\s+conversation))?|shut(?:\s+the\s+fuck)?\s+up|be\s+quiet|quiet|silence|cancel(?:\s+that)?|dismiss(?:\s+that)?|never\s+mind|nevermind|hold\s+(?:on|up)|wait(?:\s+(?:a\s+)?(?:second|moment))?|no\s+thanks|not\s+now|that's\s+(?:all|it)|thats\s+(?:all|it)|i(?:'m|\s+am)\s+done|we(?:'re|\s+are)\s+done|end(?:\s+(?:the\s+)?(?:conversation|chat))?|go\s+away|go\s+to\s+sleep)",
            lowered,
        )
    )


def spoken_response(text: str) -> str:
    """Keep code/links visible in chat without reciting markup or raw URLs."""
    text = re.split(r"\n\nSources:\s*", text, maxsplit=1)[0]
    text = re.sub(r"```[\s\S]*?```", "The code is in the chat.", text)
    text = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "the linked page", text)
    text = re.sub(r"(?m)^\s*(?:#{1,6}\s+|[-*]\s+)", "", text)
    text = re.sub(r"[*`]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 1900:
        short = text[:1850]
        boundary = max(short.rfind(". "), short.rfind("! "), short.rfind("? "))
        text = short[: boundary + 1] if boundary > 1000 else short.rsplit(" ", 1)[0] + "."
        text += " The rest is in the chat."
    return text
