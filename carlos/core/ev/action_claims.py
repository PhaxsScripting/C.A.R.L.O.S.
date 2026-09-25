"""Conservative guard for explicit action claims lacking execution receipts.

This is not an intent router, a semantic verifier, or an execution mechanism.
It catches common misleading acknowledgements without blocking explanations.
"""

import re


def claims_computer_action(text: str) -> bool:
    text = text.strip().replace("’", "'")
    text = re.sub(
        r"^(?:(?:okay|ok|sure|alright|all right|yes|yep|certainly)[,!.:\s]+)+", "", text, flags=re.I
    )
    if re.fullmatch(r"(?:done|all done|completed)[.!\s]*", text, re.I):
        return True
    past = r"opened|closed|deleted|created|changed|executed|launched|paused|unpaused|resumed|muted|unmuted|saved|moved|minimized|maximized|installed|removed|typed|clicked"
    action = r"open|close|delete|create|change|execute|launch|pause|unpause|resume|mute|unmute|save|move|minimize|maximize|install|remove|type|click"
    ongoing = r"doing|opening|closing|deleting|creating|changing|executing|launching|pausing|unpausing|resuming|muting|saving|moving|minimizing|maximizing|installing|typing|clicking"
    # Do not reject conditional offers or explicit explanations of inability.
    if re.search(r"\b(?:if|would|could|cannot|can't|will not|won't)\b", text, re.I):
        return False
    if re.match(rf"^I(?:'ve| have| just)?\s+(?:{past})\b", text, re.I):
        return True
    if re.match(rf"^I(?:'m| am)\s+(?:(?:now|just)\s+)?(?:{ongoing})\b", text, re.I):
        return True
    if re.match(rf"^I(?:'ll| will)\s+(?:(?:now|just)\s+)?(?:{action})\b", text, re.I):
        return True
    if re.match(rf"^I(?:'m| am)\s+going to\s+(?:{action})\b", text, re.I):
        return True
    # Bare progress captions, not prose such as 'Opening files requires...'.
    return bool(
        re.fullmatch(
            rf"(?:{ongoing})\s+[^\n]{{1,180}}(?:\.\.\.|…|\bright now[.!]?|\bnow[.!]?)", text, re.I
        )
    )
