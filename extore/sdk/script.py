import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable

from ..variants import default_variant, validate_attributes
from .instructions import WorkInstructions

MAX_INPUT_BYTES = 200000
MAX_ENVIRONMENT_FIELDS = 128
MAX_ENVIRONMENT_VALUE_BYTES = 8192
MAX_ENVIRONMENT_BYTES = 65536


def _freeze_environment(value):
    """Copy server-owned workflow values without including them in errors."""
    if not isinstance(value, Mapping) or len(value) > MAX_ENVIRONMENT_FIELDS:
        raise ValueError("invalid task environment")
    result = {}
    total_bytes = 0
    for name, content in value.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name)
            or not isinstance(content, str)
            or "\x00" in content
        ):
            raise ValueError("invalid task environment")
        try:
            content_bytes = len(content.encode("utf-8"))
        except UnicodeError:
            raise ValueError("invalid task environment") from None
        total_bytes += content_bytes
        if (
            content_bytes > MAX_ENVIRONMENT_VALUE_BYTES
            or total_bytes > MAX_ENVIRONMENT_BYTES
        ):
            raise ValueError("task environment too large")
        result[name] = content
    return MappingProxyType(result)


def _steps(value, *, allow_empty=False):
    if (
        not isinstance(value, (list, tuple))
        or not (0 if allow_empty else 1) <= len(value) <= 30
    ):
        raise ValueError("steps must contain 1..30 definitions")
    result = []
    for step in value:
        if not isinstance(step, Mapping) or set(step) - {"id", "label", "done"}:
            raise ValueError("invalid step definition")
        sid = step.get("id")
        labels = step.get("label")
        if not isinstance(sid, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]{0,39}", sid
        ):
            raise ValueError("invalid step ID")
        if (
            not isinstance(labels, Mapping)
            or not 1 <= len(labels) <= 20
            or any(
                not isinstance(locale, str)
                or not locale.strip()
                or len(locale) > 40
                or not isinstance(label, str)
                or not label.strip()
                or len(label) > 200
                for locale, label in labels.items()
            )
        ):
            raise ValueError("invalid step labels")
        result.append({"id": sid, "label": dict(labels)})
    if len({step["id"] for step in result}) != len(result):
        raise ValueError("duplicate step IDs")
    return result


def _completed(value, plan, previous=()):
    if (
        not isinstance(value, (list, tuple))
        or len(value) > 30
        or any(
            not isinstance(sid, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", sid)
            for sid in value
        )
        or len(set(value)) != len(value)
    ):
        raise ValueError("invalid completed step IDs")
    identifiers = [step["id"] for step in plan]
    if not set(value) <= set(identifiers) or not set(previous) <= set(value):
        raise ValueError("completed steps must belong to the plan and cannot regress")
    return tuple(sid for sid in identifiers if sid in value)


def _freeze_steps(plan):
    return tuple(
        MappingProxyType(
            {"id": step["id"], "label": MappingProxyType(dict(step["label"]))}
        )
        for step in plan
    )


def _freeze_delivery_metadata(value, *, depth=0):
    """Keep server-owned context immutable without exposing values in errors."""
    if depth > 5:
        raise ValueError("invalid delivery context")
    if isinstance(value, Mapping):
        if len(value) > 100 or any(not isinstance(key, str) for key in value):
            raise ValueError("invalid delivery context")
        return MappingProxyType(
            {
                key: _freeze_delivery_metadata(content, depth=depth + 1)
                for key, content in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        if len(value) > 1001:
            raise ValueError("invalid delivery context")
        return tuple(_freeze_delivery_metadata(item, depth=depth + 1) for item in value)
    if value is None or type(value) in (str, bool, int, float):
        if type(value) is float:
            import math

            if not math.isfinite(value):
                raise ValueError("invalid delivery context")
        return value
    raise ValueError("invalid delivery context")


@dataclass(frozen=True)
class ShopContext:
    """Server-owned routing metadata; credentials belong in configuration."""

    shop_id: str | None = None
    profile_id: str | None = None
    revision: int | None = None

    def __post_init__(self):
        if any(
            value is not None
            and (not isinstance(value, str) or not 1 <= len(value) <= 100)
            for value in (self.shop_id, self.profile_id)
        ):
            raise ValueError("invalid shop context identifiers")
        if self.revision is not None and (
            type(self.revision) is not int or self.revision < 1
        ):
            raise ValueError("invalid shop context revision")


@dataclass(frozen=True)
class Task:
    id: str
    product_id: str
    attempt: int
    params: dict[str, str]
    configuration: dict[str, str] = field(default_factory=dict)
    variant: dict = field(default_factory=default_variant)
    steps: tuple = field(default_factory=tuple)
    completed_steps: tuple = field(default_factory=tuple)
    shop_context: ShopContext = field(default_factory=ShopContext)
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    instructions: WorkInstructions | None = field(default=None, repr=False)
    card_attributes: Mapping = field(default_factory=dict)
    entitlements: Mapping | None = None
    revision: Mapping = field(
        default_factory=lambda: {"current": 0, "message": "", "is_revision": False}
    )
    deliveries: tuple = field(default_factory=tuple)
    last_delivery: Mapping | None = None

    def __post_init__(self):
        plan = _steps(self.steps, allow_empty=True)
        completed = _completed(self.completed_steps, plan)
        context = self.shop_context
        if isinstance(context, dict):
            if set(context) - {"shop_id", "profile_id", "revision"}:
                raise ValueError("invalid shop context fields")
            context = ShopContext(**context)
        elif not isinstance(context, ShopContext):
            raise ValueError("invalid shop context")
        instructions = self.instructions
        if isinstance(instructions, Mapping):
            instructions = WorkInstructions.from_dict(
                instructions,
                product_id=self.product_id,
                shop_id=context.shop_id,
            )
        elif instructions is not None:
            if not isinstance(instructions, WorkInstructions):
                raise ValueError("invalid work instructions")
            instructions = WorkInstructions.from_dict(
                instructions.as_dict(),
                product_id=self.product_id,
                shop_id=context.shop_id,
            )
        object.__setattr__(self, "steps", _freeze_steps(plan))
        object.__setattr__(self, "completed_steps", completed)
        object.__setattr__(self, "shop_context", context)
        object.__setattr__(self, "environment", _freeze_environment(self.environment))
        object.__setattr__(self, "instructions", instructions)
        attributes = (
            validate_attributes(dict(self.card_attributes))
            if isinstance(self.card_attributes, Mapping)
            else validate_attributes(self.card_attributes)
        )
        if (
            not isinstance(self.revision, Mapping)
            or type(self.revision.get("current")) is not int
            or not 0 <= self.revision["current"] <= 2**53 - 1
            or not isinstance(self.revision.get("message", ""), str)
            or len(self.revision.get("message", "")) > 10000
            or type(self.revision.get("is_revision", False)) is not bool
            or self.entitlements is not None
            and not isinstance(self.entitlements, Mapping)
            or not isinstance(self.deliveries, (list, tuple))
            or self.last_delivery is not None
            and not isinstance(self.last_delivery, Mapping)
        ):
            raise ValueError("invalid delivery context")
        for name, value in (
            ("card_attributes", attributes),
            ("entitlements", self.entitlements),
            ("revision", self.revision),
            ("deliveries", self.deliveries),
            ("last_delivery", self.last_delivery),
        ):
            object.__setattr__(self, name, _freeze_delivery_metadata(value))

    @property
    def idempotency_key(self):
        # Stable over retries: downstream fulfillment must deduplicate by task ID.
        return self.id

    @property
    def delivery_idempotency_key(self):
        """One content version; stable across technical retries of that version."""
        return f"{self.id}:revision:{self.revision['current']}"

    def define_steps(self, plan, message=""):
        """Initialize an empty task plan once; a saved plan cannot be replaced."""
        if self.steps or self.completed_steps:
            raise ValueError("the task step plan is already frozen")
        plan = _steps(plan)
        if not isinstance(message, str) or len(message) > 1000:
            raise ValueError("progress message must be text of at most 1000 characters")
        object.__setattr__(self, "steps", _freeze_steps(plan))
        print(
            json.dumps(
                {
                    "kind": "progress",
                    "progress_steps": plan,
                    "progress": 0,
                    "completed_steps": [],
                    "message": message,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    def progress(
        self,
        percent: int | None = None,
        message: str = "",
        *,
        completed_steps: list[str] | None = None,
    ):
        if percent is not None and (type(percent) is not int or not 0 <= percent <= 99):
            raise ValueError("progress must be 0..99")
        if not isinstance(message, str) or len(message) > 1000:
            raise ValueError("progress message must be text of at most 1000 characters")
        if percent is None and completed_steps is None:
            raise ValueError("provide progress or completed_steps")
        payload = {"kind": "progress", "message": message}
        if percent is not None:
            payload["progress"] = percent
        if completed_steps is not None:
            completed = _completed(completed_steps, self.steps, self.completed_steps)
            payload["completed_steps"] = list(completed)
            object.__setattr__(self, "completed_steps", completed)
        print(
            json.dumps(
                payload,
                ensure_ascii=False,
            ),
            flush=True,
        )


@dataclass(frozen=True)
class Result:
    state: str
    content: str | None = None
    message: str = ""
    retryable: bool = False
    output: dict[str, str] | None = None
    completed_steps: list[str] | None = None

    @classmethod
    def success(
        cls, content=None, message="处理完成", *, output=None, completed_steps=None
    ):
        return cls(
            "succeeded",
            content,
            message,
            output=output,
            completed_steps=completed_steps,
        )

    @classmethod
    def failure(cls, message, retryable=False, *, completed_steps=None):
        return cls("failed", None, message, retryable, completed_steps=completed_steps)


def run(handler: Callable[[Task], Result]):
    try:
        source = getattr(sys.stdin, "buffer", sys.stdin)
        raw = source.read(MAX_INPUT_BYTES + 1)
        if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_INPUT_BYTES:
            raise ValueError("input too large")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("invalid task envelope")
        task = Task(**data)
        result = handler(task)
        if not isinstance(result, Result):
            raise TypeError("handler must return Result")
        print(
            json.dumps({"kind": "result", **result.__dict__}, ensure_ascii=False),
            flush=True,
        )
    except Exception:
        # Exceptions can contain customer secrets; do not expose them in the UI/logs.
        print(
            json.dumps(
                {
                    "kind": "result",
                    "state": "failed",
                    "message": "脚本异常，需商家核实是否已交付",
                    "retryable": False,
                }
            ),
            flush=True,
        )
