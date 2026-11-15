#!/usr/bin/env python3
"""Local test for context_stats.py without Open WebUI or pydantic.

pydantic is only used for the Valves model, so a two-class stub is enough. Everything else -
the strip round-trip, the rendering, the cost arithmetic - runs unchanged.
"""
import asyncio
import importlib.util
import json
import os
import sys
import types

# --- minimal pydantic stand-in -------------------------------------------------
pyd = types.ModuleType("pydantic")


class _Field:
    def __init__(self, default=None, description=""):
        self.default = default


def Field(default=None, description="", **kwargs):
    return _Field(default, description)


def field_validator(*args, **kwargs):
    """pydantic's decorator is a no-op in this stub; the validator body is still callable."""
    def decorator(function):
        return function
    return decorator


class BaseModel:
    def __init__(self, **kw):
        self._klass = type(self)
        for name in dir(self._klass):
            if isinstance(getattr(self._klass, name), _Field):
                setattr(self, name, getattr(self._klass, name).default)
        for k, v in kw.items():
            setattr(self, k, v)


pyd.Field = Field
pyd.field_validator = field_validator
pyd.BaseModel = BaseModel
sys.modules["pydantic"] = pyd

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "context_stats.py")
spec = importlib.util.spec_from_file_location("context_stats", PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

ADMIN = {"id": "u-admin", "email": "owner@example", "role": "admin"}
PLAIN = {"id": "u-plain", "email": "member@example", "role": "user"}

FAILED = []


def check(name, got, want):
    ok = got == want
    if not ok:
        FAILED.append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name)
    if not ok:
        print("        got : %r" % (got,))
        print("        want: %r" % (want,))


USAGE_LOCAL = {
    "prompt_tokens": 125266,
    "completion_tokens": 96,
    "cached_tokens": 0,
    "prompt_n": 125266,
    "predicted_n": 96,
    "prompt_per_second": 1583.8,
    "predicted_per_second": 99.9,
    "draft_n": 128,
    "draft_n_accepted": 79,
}

USAGE_WARM = {
    "prompt_tokens": 40000,
    "completion_tokens": 200,
    "cached_tokens": 38400,
    "cache_restore_ms": 1069.5,
    "prompt_per_second": 1776.0,
    "predicted_per_second": 55.0,
}

print("== rendering ==")
f = mod.Filter()
line = f._line(USAGE_LOCAL, "local-model", 0)
print("  line:", line)
check("renders prefill rate", "prefill 1,584 tok/s" in line, True)
check("renders prefill count", "125,266 tok" in line, True)
check("renders decode rate", "decode 99.9 tok/s" in line, True)
check("renders cache 0%", "0% cached" in line, True)
check("renders draft 62%", "draft 62%" in line, True)
check("local model costs zero", line.rstrip().endswith("$0.00"), True)

warm = f._line(USAGE_WARM, "local-model", 0)
print("  warm:", warm)
check("warm shows 96% cached", "96% cached" in warm, True)
check("warm shows restore ms", "restored 1069 ms" in warm, True)

print("== cost table ==")
f.valves.prices = json.dumps(
    {
        "local-model": {"in": 0, "out": 0},
        "anthropic/claude-sonnet-5": {"in": 3.0, "cached": 0.3, "out": 15.0},
        "gpt-image-1": {"image": 0.04},
    }
)
check("listed local model is $0.00", f._money(f._cost("local-model", USAGE_LOCAL, 0)[0]), "$0.00")

# 125,266 in @ $3/Mtok + 96 out @ $15/Mtok = 0.375798 + 0.00144 = 0.377238
d, est = f._cost("anthropic/claude-sonnet-5", USAGE_LOCAL, 0)
check("paid model is an estimate", est, True)
check("paid model arithmetic", round(d, 6), round(125266 / 1e6 * 3.0 + 96 / 1e6 * 15.0, 6))

# cached split: 38,400 cached @ $0.30/Mtok + 1,600 uncached @ $3/Mtok
d2, _ = f._cost("anthropic/claude-sonnet-5", USAGE_WARM, 0)
want2 = 1600 / 1e6 * 3.0 + 38400 / 1e6 * 0.30 + 200 / 1e6 * 15.0
check("cached rate applied to reused tokens", round(d2, 9), round(want2, 9))

check("image priced per image", round(f._cost("gpt-image-1", {}, 2)[0], 4), 0.08)

provider = dict(USAGE_LOCAL)
provider["cost"] = 0.0123
check("provider-reported cost wins", f._cost("anthropic/claude-sonnet-5", provider, 0), (0.0123, False))

check("unlisted model is free", f._cost("mystery-model", USAGE_LOCAL, 0), (0.0, False))

# A provider-reported cost is exact, so it must not carry the estimate tilde; a table-derived
# figure must keep it. Both are rendered through the same _money() call.
check("_money marks only estimates", (f._money(0.5, True), f._money(0.5, False)), ("~$0.5000", "$0.5000"))

provider_line = f._line(provider, "anthropic/claude-sonnet-5", 0)
print("  provider:", provider_line)
check("provider cost renders exact amount", provider_line.rstrip().endswith("$0.0123"), True)
check("provider cost has no tilde", "~$" in provider_line, False)

estimate_line = f._line(USAGE_LOCAL, "anthropic/claude-sonnet-5", 0)
print("  estimate:", estimate_line)
check("table estimate keeps tilde", "~$" in estimate_line, True)

print("== strip round-trip ==")
body = {
    "messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "answer one\n\n---\n" + mod.SENTINEL + " prefill 100 tok/s \u00b7 $0.00"},
    ]
}
cleaned = asyncio.run(f.inlet(body))
check("strip removes the line", cleaned["messages"][2]["content"], "answer one")
check("strip leaves innocents alone", cleaned["messages"][1]["content"], "hello")

# idempotent: a second pass must be a no-op
again = asyncio.run(f.inlet(cleaned))
check("strip is idempotent", again["messages"][2]["content"], "answer one")

print("== outlet appends, inlet removes ==")
body2 = {"model": "local-model", "messages": [{"role": "user", "content": "hi"},
                                             {"role": "assistant", "content": "the answer", "usage": USAGE_LOCAL}]}
out = asyncio.run(f.outlet(body2, None, {"id": "local-model"}, ADMIN))
written = out["messages"][1]["content"]
check("outlet appended a line", mod.SENTINEL in written, True)
back = asyncio.run(f.inlet(out))
check("inlet restores the original text", back["messages"][1]["content"], "the answer")

print("== robustness ==")
check("no usage -> unchanged", asyncio.run(f.outlet({"messages": [{"role": "assistant", "content": "x"}]}, None, None, ADMIN))["messages"][0]["content"], "x")
check("no assistant -> unchanged", asyncio.run(f.outlet({"messages": [{"role": "user", "content": "x"}]}, None, None, ADMIN))["messages"][0]["content"], "x")
check("empty usage dict -> unchanged", asyncio.run(f.outlet({"messages": [{"role": "assistant", "content": "x", "usage": {}}]}, None, None, ADMIN))["messages"][0]["content"], "x")
check("multimodal content untouched",
      asyncio.run(f.outlet({"messages": [{"role": "assistant", "content": [{"type": "text", "text": "x"}], "usage": USAGE_LOCAL}]}, None, None, ADMIN))["messages"][0]["content"],
      [{"type": "text", "text": "x"}])
check("garbage usage does not raise", isinstance(asyncio.run(f.outlet({"messages": [{"role": "assistant", "content": "x", "usage": {"prompt_tokens": "abc", "prompt_per_second": None}}]}, None, None, ADMIN)), dict), True)
check("bad prices json does not raise", f._prices() is not None, True)
f.valves.prices = "not json"
check("unparseable prices -> free", f._cost("x", USAGE_LOCAL, 0), (0.0, False))

print("== per-user access ==")
f.valves.prices = "{}"


def settings_for(user, policy=None, groups=None):
    f.valves.access_policy = json.dumps(policy) if policy is not None else '{"default": false, "admins": true}'
    if groups is not None:
        async def fake_group_keys(user_id):
            return set(groups)
        f._group_keys = fake_group_keys
    else:
        async def fake_group_keys(user_id):
            return set()
        f._group_keys = fake_group_keys
    return asyncio.run(f._settings_for(user))


check("admin is on by default", settings_for(ADMIN)["visible"], True)
check("plain user is off by default", settings_for(PLAIN)["visible"], False)
check("no user context is off", settings_for(None)["visible"], False)

check("policy can enable a user by email",
      settings_for(PLAIN, {"default": False, "users": {"member@example": True}})["visible"], True)
check("policy can enable a user by id",
      settings_for(PLAIN, {"default": False, "users": {"u-plain": True}})["visible"], True)
check("policy can disable an admin",
      settings_for(ADMIN, {"default": False, "admins": False})["visible"], False)

obj = settings_for(PLAIN, {"default": False, "users": {"member@example": {"visible": True, "show_cost": False}}})
check("policy object enables but drops cost", (obj["visible"], obj["show_cost"]), (True, False))
check("policy object keeps the other metrics", obj["show_prefill"], True)
check("enabling a member keeps the admin's base template",
      settings_for(PLAIN, {"default": True})["show_cost"], True)

check("a group can be matched by name",
      settings_for(PLAIN, {"default": False, "groups": {"Family": True}}, groups=["Family"])["visible"], True)
check("a group can be matched by id",
      settings_for(PLAIN, {"default": False, "groups": {"gid-9": True}}, groups=["gid-9"])["visible"], True)
check("a non-matching group changes nothing",
      settings_for(PLAIN, {"default": False, "groups": {"Family": True}}, groups=["Other"])["visible"], False)
check("a user rule beats a group rule",
      settings_for(PLAIN, {"default": False, "groups": {"Family": True}, "users": {"u-plain": False}},
                   groups=["Family"])["visible"], False)

own_on = dict(PLAIN, valves=mod.Filter.UserValves(visible=True))
check("own valve turns it on over an off policy", settings_for(own_on, {"default": False})["visible"], True)
own_cost = dict(ADMIN, valves=mod.Filter.UserValves(show_cost=False))
check("own valve can silence just the cost", settings_for(own_cost, {"default": False, "admins": True})["show_cost"], False)
own_other = dict(ADMIN, valves=mod.Filter.UserValves(show_decode=False))
check("own valve leaves unsaid metrics alone", settings_for(own_other, {"default": False, "admins": True})["show_prefill"], True)
check("unset user valves inherit instead of forcing",
      settings_for(dict(PLAIN, valves=mod.Filter.UserValves()), {"default": False})["visible"], False)

f.valves.access_policy = "not json"
check("malformed policy fails closed for a member", asyncio.run(f._settings_for(PLAIN))["visible"], False)
check("malformed policy still lets the admin in", asyncio.run(f._settings_for(ADMIN))["visible"], True)

print("== outlet honours the policy ==")
f.valves.prices = "{}"
hidden = {"model": "local-model", "messages": [{"role": "user", "content": "hi"},
                                              {"role": "assistant", "content": "the answer", "usage": USAGE_LOCAL}]}
f.valves.access_policy = '{"default": false, "admins": true}'
check("member gets no line", asyncio.run(f.outlet(hidden, None, {"id": "local-model"}, PLAIN))["messages"][1]["content"], "the answer")
shown = asyncio.run(f.outlet({"model": "local-model", "messages": [dict(m) for m in hidden["messages"]]},
                             None, {"id": "local-model"}, ADMIN))["messages"][1]["content"]
check("admin gets the line", mod.SENTINEL in shown, True)
nocost_user = dict(ADMIN, valves=mod.Filter.UserValves(show_cost=False))
nocost = asyncio.run(f.outlet({"model": "local-model", "messages": [dict(m) for m in hidden["messages"]]},
                              None, {"id": "local-model"}, nocost_user))["messages"][1]["content"]
check("cost can be hidden while the rest shows", ("$" in nocost, "prefill" in nocost), (False, True))
f.valves.access_policy = '{"default": false, "admins": true}'

print("== the Default/Custom toggle's empty string ==")
coerce = mod.Filter.UserValves._blank_means_custom
check("empty string becomes an explicit False, not inherit", coerce(""), False)
check("whitespace is also Custom", coerce("  "), False)
check("'null'/'none'/'inherit' become inherit", (coerce("null"), coerce("none"), coerce("inherit")), (None, None, None))
check("' Default ' still means inherit", coerce(" Default "), None)
check("textual true is accepted", (coerce("true"), coerce(" on "), coerce("1")), (True, True, True))
check("textual false is accepted", (coerce("false"), coerce("off"), coerce("0")), (False, False, False))
check("real booleans pass through untouched", (coerce(True), coerce(False)), (True, False))
check("None stays None", coerce(None), None)
check("genuine junk is left for pydantic to reject", coerce("banana"), "banana")

print()
if FAILED:
    print("FAILED: %d -> %s" % (len(FAILED), ", ".join(FAILED)))
    sys.exit(1)
print("all checks passed")
