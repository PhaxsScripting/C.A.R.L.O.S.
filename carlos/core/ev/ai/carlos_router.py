"""Local-first routing. A failed continuation never silently replays tools."""

from .base import Provider, ProviderError
from .base import ProviderTurn
import re
from .local_llama import LocalHybridProvider


class CarlosRouter(Provider):
    name = "carlos_router"

    def __init__(self, local, cloud, mode):
        self.local, self.cloud, self.mode = local, cloud, mode
        self.last_route = "local"

    @property
    def model(self):
        return self.local.model

    @property
    def available(self):
        return True, "Local command router available; model capability checked per request"

    def set_personality(self, personality):
        self.local.set_personality(personality)

    async def prewarm(self):
        await self.local.prewarm()

    async def begin(self, user_text, context, memories, tools):
        self.last_route = "local"
        # Social acknowledgments contain no machine-state claims or actions.
        # They should not wait for a general-purpose model to evaluate history.
        if re.fullmatch(
            r"\s*(?:(?:hey|hi|hello|yo)(?:[, ]+carlos)?|carlos)[.!?\s]*", user_text, re.I
        ):
            return ProviderTurn("carlos_social", self.model, "Yeah, I'm here.")
        if re.fullmatch(r"\s*(?:thanks|thank you)(?:[, ]+carlos)?[.!?\s]*", user_text, re.I):
            return ProviderTurn("carlos_social", self.model, "Anytime.")
        # Explicit opt-in can select cloud reasoning. Normal requests stay local,
        # including loss of Internet. Privacy is checked at the actual call site.
        cloud_request = user_text.casefold().startswith("use cloud: ")
        if cloud_request:
            if self.mode() != "NORMAL" or self.cloud is None:
                raise ProviderError("Cloud reasoning is disabled by the current privacy policy")
            self.last_route = "cloud"
            return await self.cloud.begin(user_text[11:], context, memories, tools)
        return await self.local.begin(user_text, context, memories, tools)

    async def continue_with_tools(self, turn, outputs, tools):
        if self.cloud is not None and turn.provider == self.cloud.name:
            if self.mode() != "NORMAL":
                raise ProviderError("Cloud continuation blocked by privacy policy")
            return await self.cloud.continue_with_tools(turn, outputs, tools)
        return await self.local.continue_with_tools(turn, outputs, tools)

    async def close(self):
        await self.local.close()
        close = getattr(self.cloud, "close", None)
        if close:
            await close()
