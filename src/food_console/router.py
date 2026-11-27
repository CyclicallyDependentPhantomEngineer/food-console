"""Model router: pick the Claude model per task.

- Structured tasks have fixed routes (Pick, Stats refresh): no classifier call.
- Chat messages are classified by a small, cheap model (Claude Haiku 5.5) into a tier:
  light (lookups, status), standard (search, carts, orders, bookings) or complex (analysis,
  multi-service planning). Unsure means the higher tier.
- Within one conversation the tier only goes up. Switching to a smaller model mid-
  conversation drops the reasoning of earlier turns and restarts the prompt cache, while
  moving up keeps both usable (Haiku 5.5 -> Sonnet 5.5 -> Opus 5.5). A new conversation
  starts from the bottom again.
- The router changes only the model, never the tool list: changing tools mid-conversation
  invalidates earlier reasoning and can be rejected by the API.

Environment overrides: FOOD_ROUTER=off (always FOOD_MODEL / ZOMATO_CHAT_MODEL),
FOOD_MODEL_LIGHT / FOOD_MODEL_STANDARD / FOOD_MODEL_COMPLEX, FOOD_ROUTER_MODEL.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

TIERS = ("light", "standard", "complex")
DEFAULT_MODELS = {"light": "claude-haiku-5-5", "standard": "claude-sonnet-5-5", "complex": "claude-opus-5-5"}
# Effort per tier: the small model answers lookups at low effort; the others keep the default.
EFFORT = {"light": "low", "standard": "medium", "complex": "medium"}
# Tasks with a fixed tier (no classifier call).
TASK_TIERS = {"pick": "standard", "stats": "standard"}
ALIASES = {"auto": "auto", "haiku": "light", "sonnet": "standard", "opus": "complex",
           "light": "light", "standard": "standard", "complex": "complex"}

CLASSIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tier": {"type": "string", "enum": list(TIERS)},
        "reason": {"type": "string", "description": "A few words: what kind of task this is"},
    },
    "required": ["tier", "reason"],
    "additionalProperties": False,
}

CLASSIFY_SYSTEM = """You route requests for a food-delivery and grocery assistant (Zomato, Swiggy, \
District, Zepto, Blinkit) to a model tier. Reply with the tier and a few words of reason.

light - one lookup or status question that needs a few read-only tool calls and little \
reasoning: list saved addresses, show my cart, track an order, my last/recent orders, \
payment status, a simple fact from one service.
standard - routine multi-step work: search or compare restaurants, dishes or products; \
show menus with filters; build or change a cart; apply a coupon; place an order or \
booking; pay; reorder.
complex - needs real analysis or planning: spending or health analysis across history, \
planning meals or orders across several services or constraints, open-ended or ambiguous \
requests, anything where a wrong judgment would be costly.

If unsure between two tiers, choose the higher one."""


def tier_of(name: str | None) -> str | None:
    """'haiku' / 'light' / 'auto' ... -> tier name, 'auto', or None if unknown."""
    return ALIASES.get((name or "").strip().lower())


@dataclass
class Decision:
    tier: str
    model: str
    effort: str
    reason: str
    source: str  # router | task | fixed | ratchet | off | fallback
    usage: dict[str, Any] | None = None  # the classifier call, recorded with the run

    def as_event(self) -> dict[str, Any]:
        return {"type": "model", "tier": self.tier, "model": self.model, "reason": self.reason, "source": self.source}


@dataclass
class Router:
    enabled: bool = True
    models: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))
    classifier_model: str = DEFAULT_MODELS["light"]
    off_model: str = DEFAULT_MODELS["complex"]

    @classmethod
    def from_env(cls) -> "Router":
        models = {t: os.environ.get(f"FOOD_MODEL_{t.upper()}", m) for t, m in DEFAULT_MODELS.items()}
        off_model = os.environ.get("FOOD_MODEL") or os.environ.get("ZOMATO_CHAT_MODEL") or DEFAULT_MODELS["complex"]
        return cls(enabled=os.environ.get("FOOD_ROUTER", "auto").lower() not in ("off", "0", "false", "no"),
                   models=models, classifier_model=os.environ.get("FOOD_ROUTER_MODEL", models["light"]),
                   off_model=off_model)

    def _decide(self, tier: str, reason: str, source: str, usage: dict[str, Any] | None = None) -> Decision:
        return Decision(tier, self.models[tier], EFFORT[tier], reason, source, usage)

    async def choose(self, client: Any, *, kind: str, text: str, context: str = "", floor: str | None = None,
                     override: str | None = None) -> Decision:
        """Model for one run. `floor` is the conversation's current tier (only goes up)."""
        if not self.enabled:
            return Decision("complex", self.off_model, os.environ.get("ZOMATO_CHAT_EFFORT", "medium"),
                            "router off", "off")
        forced = tier_of(override)
        if forced and forced != "auto":
            # Your explicit choice wins, even below the conversation's tier (that turn then runs
            # without the larger model's earlier reasoning).
            return self._decide(forced, "chosen by you", "fixed")
        task = kind.split(":")[-1]  # "cli:pick" -> "pick"
        if task in TASK_TIERS:
            return self._decide(TASK_TIERS[task], f"{task} task", "task")
        tier, reason, usage = await self.classify(client, text, context)
        if floor and TIERS.index(floor) > TIERS.index(tier):
            return self._decide(floor, f"{reason}; staying on the conversation's model", "ratchet", usage)
        return self._decide(tier, reason, "router" if usage else "fallback", usage)

    async def classify(self, client: Any, text: str, context: str = "") -> tuple[str, str, dict[str, Any] | None]:
        prompt = (f"Earlier in this conversation:\n{context[-1200:]}\n\n" if context else "") + f"New request:\n{text[-2000:]}"
        try:
            r = await client.beta.messages.create(
                model=self.classifier_model, max_tokens=400, system=CLASSIFY_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": CLASSIFY_SCHEMA}},
            )
            data = json.loads(next(b.text for b in r.content if b.type == "text"))
            tier = data["tier"] if data.get("tier") in TIERS else "standard"
            u = r.usage
            usage = {"model": getattr(r, "model", None) or self.classifier_model, "input": u.input_tokens,
                     "output": u.output_tokens, "cache_read": u.cache_read_input_tokens or 0,
                     "cache_write": u.cache_creation_input_tokens or 0, "stop": "router", "router": True,
                     "tool_uses": []}
            return tier, str(data.get("reason") or "")[:120], usage
        except Exception as exc:  # classifier unavailable: never block the chat
            return "standard", f"router unavailable ({type(exc).__name__}); using the standard model", None

