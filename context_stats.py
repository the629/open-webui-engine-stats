"""
title: Context stats and cost
author: homelab
version: 1.1.0
type: filter
active: true
global: true
description: Show per-message engine stats - prompt processing (prefill) rate and token count, generation rate, prefix-cache reuse, speculative-draft acceptance - plus a cost: the exact figure a provider reports when it reports one, otherwise an estimate from the price table, and 0.00 for any model not listed. Access is per user and off by default for non-admins; the admin turns it on for everyone, for named groups, or for individual users, and each metric including cost can be overridden per user. The line is appended to the assistant message for display and stripped again on the way upstream, so it never enters the model's context or the prefix cache.
"""

import json
import re
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# The sentinel makes the injected line both recognisable and removable. It is the only part of the
# rendered line that may never change without also changing STRIP_RE, because inlet() rewrites the
# conversation from it before every request.
SENTINEL = "\u26a1 "  # lightning mark
STRIP_RE = re.compile(r"\n*(?:^|\n)-{2,}\n" + re.escape(SENTINEL) + r"[^\n]*\n?\Z")


def _fmt_int(n):
    try:
        return "{:,}".format(int(n))
    except (TypeError, ValueError):
        return str(n)


def _fmt_rate(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return "{:,.0f}".format(f) if f >= 100 else "{:,.1f}".format(f)


def _usage_of(message):
    """Usage may sit on the message itself or under 'info' depending on OWUI version."""
    if not isinstance(message, dict):
        return {}
    for candidate in (message.get("usage"), (message.get("info") or {}).get("usage")):
        if isinstance(candidate, dict) and candidate:
            return candidate
    return {}


def _text_of(message):
    """Plain text of a message, or None when its content is a list of parts."""
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else None


def _count_images(messages):
    total = 0
    for m in messages or []:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("image_url", "input_image"):
                    total += 1
    return total


METRIC_FLAGS = ("show_prefill", "show_decode", "show_cache", "show_draft", "show_cost")


def _fold(settings, layer):
    """Apply one policy layer over the running settings. Later layers win."""
    if layer is None:
        return settings
    if isinstance(layer, bool):
        settings["visible"] = layer
        return settings
    if isinstance(layer, dict):
        if "visible" in layer:
            settings["visible"] = bool(layer["visible"])
        for flag in METRIC_FLAGS:
            if flag in layer:
                settings[flag] = bool(layer[flag])
    return settings


def _own_layer(own):
    """A user's own UserValves as a plain layer, dropping every unset (None) field."""
    if own is None:
        return None
    if isinstance(own, dict):
        data = dict(own)
    else:
        data = {name: getattr(own, name, None) for name in ("visible",) + METRIC_FLAGS}
    layer = {name: value for name, value in data.items() if value is not None}
    return layer or None


class Filter:
    class Valves(BaseModel):
        prices: str = Field(
            default="{}",
            description=(
                "JSON price table, dollars per MILLION tokens, keyed by model id. Anything not "
                "listed is free, which is how local models show 0.00 with no configuration. "
                'Example: {"local-model": {"in": 0, "out": 0}, "gpt-image-1": {"image": 0.04}, '
                '"anthropic/claude-sonnet-5": {"in": 3.0, "cached": 0.3, "out": 15.0}}'
            ),
        )
        precision: int = Field(
            default=4, description="Decimal places for a non-zero cost. Zero always renders as $0.00."
        )
        show_prefill: bool = Field(default=True, description="Base template: show prompt-processing rate and tokens.")
        show_decode: bool = Field(default=True, description="Base template: show generation rate and tokens.")
        show_cache: bool = Field(default=True, description="Base template: show prefix-cache reuse percentage.")
        show_draft: bool = Field(default=True, description="Base template: show speculative-draft acceptance.")
        show_cost: bool = Field(default=True, description="Base template: show estimated cost.")
        access_policy: str = Field(
            default='{"default": true}',
            description=(
                "Who sees the line, as JSON. On by default. Values are true, false, or an object overriding "
                'individual metrics, e.g. {"visible": true, "show_cost": false}. Keys, applied lowest first: '
                '"default" (everyone not matched below), "groups" (matched by id or name, in the order '
                'written), "admins" (role == admin), "users" (matched by user id or email). A user\'s own '
                'UserValves always win over all of this. To keep engine internals to yourself, use '
                '{"default": false, "admins": true}. Example: {"default": false, "admins": true, '
                '"groups": {"Family": true}, "users": {"someone@x": {"visible": true, "show_cost": false}}}'
            ),
        )
        emit_status: bool = Field(
            default=True, description="Also emit a transient status event while rendering."
        )
        debug: bool = Field(
            default=False,
            description="Log the usage keys and the resolved per-user settings to the container log. "
            "Leave off; it is for proving what actually arrives.",
        )

    class UserValves(BaseModel):
        """Per-user overrides. Unset (Default) means "inherit the admin policy".

        Every field is Optional so "Default" can mean inherit, but each one also declares
        `json_schema_extra={"type": "boolean"}`. Without that marker pydantic emits
        `anyOf: [boolean, null]` with no top-level type, Open WebUI's renderer misses its
        `type === 'boolean'` branch, and the Custom state renders as a text box you have to type
        "true" or "false" into. With it you get the switch.
        """

        visible: Optional[bool] = Field(
            default=None,
            description=(
                "Show the stats line at all. Click Custom for this user's own choice, then flip the "
                "switch; click Custom again to go back to Default and inherit the admin policy."
            ),
            json_schema_extra={"type": "boolean"},
        )
        show_prefill: Optional[bool] = Field(
            default=None, description="Override: prompt-processing rate.",
            json_schema_extra={"type": "boolean"},
        )
        show_decode: Optional[bool] = Field(
            default=None, description="Override: generation rate.",
            json_schema_extra={"type": "boolean"},
        )
        show_cache: Optional[bool] = Field(
            default=None, description="Override: cache reuse percentage.",
            json_schema_extra={"type": "boolean"},
        )
        show_draft: Optional[bool] = Field(
            default=None, description="Override: draft acceptance.",
            json_schema_extra={"type": "boolean"},
        )
        show_cost: Optional[bool] = Field(
            default=None, description="Override: estimated cost.",
            json_schema_extra={"type": "boolean"},
        )

        @field_validator("*", mode="before")
        @classmethod
        def _blank_means_custom(cls, value):
            """Absorb what Open WebUI's Default/Custom toggle actually sends.

            `common/Valves.svelte:52` initialises a custom value with `propertySpec.default ?? ''`; for a
            nullable boolean the schema default is null, so `null ?? ''` hands us an empty string, which
            pydantic rejects as a bool (the save fails with a 400) and which means "the user has just
            switched this field to Custom". Map it to False, NOT to None: None is the reply that would make
            the form re-render the row back to Default and take the switch away before it can be flipped.
            Switching back to Default sends a real null, which still means inherit.
            """
            if isinstance(value, str):
                text = value.strip().lower()
                if text == "":
                    return False
                if text in ("default", "inherit", "null", "none"):
                    return None
                if text in ("true", "on", "yes", "1"):
                    return True
                if text in ("false", "off", "no", "0"):
                    return False
            return value

    def __init__(self):
        self.valves = self.Valves()

    # ------------------------------------------------------- who sees what

    def _policy(self):
        """The admin access policy as a dict. A malformed value fails closed: default off, admins on."""
        try:
            policy = json.loads(self.valves.access_policy or "{}")
        except (ValueError, TypeError):
            return {"default": False, "admins": True}
        return policy if isinstance(policy, dict) else {"default": False, "admins": True}

    async def _group_keys(self, user_id):
        """The ids and names of this user's groups, or an empty set when membership is unknown."""
        if not user_id:
            return set()
        try:
            from open_webui.models.groups import Groups

            groups = await Groups.get_groups_by_member_id(user_id)
        except Exception:
            return set()
        keys = set()
        for group in groups or []:
            for attribute in ("id", "name"):
                value = group.get(attribute) if isinstance(group, dict) else getattr(group, attribute, None)
                if value:
                    keys.add(value)
        return keys

    async def _settings_for(self, user):
        """Resolve the effective display settings for one user.

        Layers are folded lowest first, each overriding only the keys it names:
        policy "default" -> matching "groups" (in the order written) -> "admins" for role == admin ->
        "users" by id or email -> the user's own UserValves. So a user's own choice always wins, and an
        unset user valve inherits instead of forcing a value.
        """
        settings = {flag: bool(getattr(self.valves, flag, True)) for flag in METRIC_FLAGS}
        settings["visible"] = True
        user = user or {}
        policy = self._policy()

        layers = [policy.get("default", False)]

        group_rules = policy.get("groups")
        if isinstance(group_rules, dict) and group_rules:
            members = await self._group_keys(user.get("id"))
            for key, layer in group_rules.items():  # insertion order == the order written
                if key in members:
                    layers.append(layer)

        if user.get("role") == "admin":
            layers.append(policy.get("admins", True))

        user_rules = policy.get("users")
        if isinstance(user_rules, dict):
            for key in (user.get("id"), user.get("email")):
                if key and key in user_rules:
                    layers.append(user_rules[key])

        layers.append(_own_layer(user.get("valves")))

        for layer in layers:
            settings = _fold(settings, layer)
        return settings

    # ------------------------------------------------------------------ cost

    def _prices(self):
        try:
            table = json.loads(self.valves.prices or "{}")
            return table if isinstance(table, dict) else {}
        except (ValueError, TypeError):
            return {}

    def _cost(self, model_id, usage, images):
        """Return (dollars, is_estimate) or (None, False) when the model is not priced."""
        # A provider that reports its own cost is authoritative and wins outright.
        for key in ("cost", "total_cost"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                return float(value), False

        table = self._prices()
        row = table.get(model_id) or table.get(str(model_id).split("/")[-1])
        if not isinstance(row, dict):
            return 0.0, False  # unlisted model == free (local)

        def rate(key):
            try:
                return float(row.get(key, 0) or 0)
            except (TypeError, ValueError):
                return 0.0

        prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        cached = usage.get("cached_tokens") or usage.get("cache_n") or 0
        try:
            prompt, completion, cached = int(prompt), int(completion), int(cached)
        except (TypeError, ValueError):
            prompt = completion = cached = 0
        # Cached prompt tokens are billed at their own (usually much lower) rate when one is given,
        # so the uncached remainder is what pays the full input price.
        uncached = max(prompt - cached, 0) if rate("cached") else prompt

        dollars = (
            uncached / 1e6 * rate("in")
            + (cached / 1e6 * rate("cached") if rate("cached") else 0.0)
            + completion / 1e6 * rate("out")
            + max(images, 0) * rate("image")
        )
        return dollars, True

    def _money(self, dollars, estimated=False):
        if not dollars:
            return "$0.00"
        digits = max(2, min(int(self.valves.precision), 8))
        # A provider-reported figure is exact, so only a price-table result gets the tilde.
        return ("~$" if estimated else "$") + (("%%.%df" % digits) % dollars)

    # ------------------------------------------------------------- rendering

    def _line(self, usage, model_id, images, settings=None):
        settings = settings or {flag: True for flag in METRIC_FLAGS}
        parts = []
        if settings.get("show_prefill"):
            rate = _fmt_rate(usage.get("prompt_per_second"))
            count = usage.get("prompt_n") or usage.get("prompt_tokens")
            if rate:
                parts.append("prefill %s tok/s" % rate + (" (%s tok)" % _fmt_int(count) if count else ""))
            elif count:
                parts.append("prefill %s tok" % _fmt_int(count))
        if settings.get("show_decode"):
            rate = _fmt_rate(usage.get("predicted_per_second") or usage.get("completion_tokens_per_second"))
            count = usage.get("predicted_n") or usage.get("completion_tokens")
            if rate:
                parts.append("decode %s tok/s" % rate + (" (%s tok)" % _fmt_int(count) if count else ""))
            elif count:
                parts.append("%s tok" % _fmt_int(count))
        if settings.get("show_cache"):
            prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            cached = usage.get("cached_tokens") or usage.get("cache_n") or 0
            try:
                if int(prompt) > 0:
                    parts.append("%d%% cached" % round(100.0 * int(cached) / int(prompt)))
            except (TypeError, ValueError, ZeroDivisionError):
                pass
            restore = usage.get("cache_restore_ms")
            try:
                if restore and float(restore) > 0:
                    parts.append("restored %d ms" % int(float(restore)))
            except (TypeError, ValueError):
                pass
        if settings.get("show_draft"):
            accepted = usage.get("draft_n_accepted") or usage.get("draft_tokens_accepted")
            proposed = usage.get("draft_n") or usage.get("draft_tokens")
            try:
                if proposed and int(proposed) > 0:
                    parts.append("draft %d%%" % round(100.0 * int(accepted or 0) / int(proposed)))
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        if settings.get("show_cost"):
            dollars, estimated = self._cost(model_id, usage, images)
            parts.append(self._money(dollars, estimated))
        if not parts:
            return None
        return SENTINEL + " \u00b7 ".join(parts)

    # ----------------------------------------------------------- filter hooks

    async def inlet(self, body: dict, __user__: Optional[dict] = None) -> dict:
        """Remove a previously rendered stats line so it never reaches the model.

        This is the load-bearing half. The line is written into assistant messages for display, and
        assistant messages are resent on every later turn: left in place it would change the prefix
        of the conversation and defeat the engine's checkpoint reuse on every turn after the first.
        """
        try:
            for m in body.get("messages") or []:
                text = _text_of(m)
                if text and SENTINEL in text:
                    m["content"] = STRIP_RE.sub("", text)
        except Exception:
            # Never break a request over cosmetics.
            pass
        return body

    async def outlet(
        self,
        body: dict,
        __event_emitter__=None,
        __model__: Optional[dict] = None,
        __user__: Optional[dict] = None,
    ) -> dict:
        try:
            messages = body.get("messages") or []
            if self.valves.debug:
                print("[context_stats] OUTLET msgs=%d roles=%s top=%s assistant_message=%s"
                      % (len(messages),
                         [m.get("role") for m in messages if isinstance(m, dict)][-4:],
                         sorted(k for k in body.keys() if k != "messages"),
                         isinstance(body.get("assistant_message"), dict)))
            target = body.get("assistant_message")
            if not isinstance(target, dict) or target.get("role") != "assistant":
                target = next(
                    (m for m in reversed(messages) if isinstance(m, dict) and m.get("role") == "assistant"),
                    None,
                )
            if target is None:
                return body

            usage = _usage_of(target)
            if not usage and isinstance(body.get("usage"), dict):
                usage = body["usage"]
            if self.valves.debug:
                print("[context_stats] usage keys: %s" % sorted(usage.keys()))
            if not usage:
                return body

            model_id = ""
            if isinstance(__model__, dict):
                model_id = __model__.get("id") or ""
            if not model_id:
                model_id = str(body.get("model") or "")

            settings = await self._settings_for(__user__)
            if self.valves.debug:
                print("[context_stats] user=%s role=%s settings=%s"
                      % ((__user__ or {}).get("email"),
                         (__user__ or {}).get("role"),
                         {k: v for k, v in settings.items() if k == "visible" or v}))
            if not settings.get("visible"):
                return body  # this user has not been given access to the line

            text = _text_of(target)
            if text is None:
                return body  # multimodal/structured reply: leave it alone rather than corrupt it

            line = self._line(usage, model_id, _count_images(messages), settings)
            if not line:
                return body

            target["content"] = STRIP_RE.sub("", text).rstrip() + "\n\n---\n" + line

            if self.valves.emit_status and __event_emitter__ is not None:
                try:
                    await __event_emitter__(
                        {"type": "status", "data": {"description": line, "done": True}}
                    )
                except Exception:
                    pass
        except Exception:
            # A filter that raises kills the reply; emit a bare answer rather than nothing.
            pass
        return body
