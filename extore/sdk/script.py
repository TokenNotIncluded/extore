import json
import sys
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Task:
    id: str
    product_id: str
    attempt: int
    params: dict[str, str]
    configuration: dict[str, str] = field(default_factory=dict)

    @property
    def idempotency_key(self):
        # Stable over retries: downstream fulfillment must deduplicate by task ID.
        return self.id

    def progress(self, percent: int, message: str = ""):
        if not 0 <= percent <= 99:
            raise ValueError("progress must be 0..99")
        print(
            json.dumps(
                {"kind": "progress", "progress": percent, "message": message},
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

    @classmethod
    def success(cls, content=None, message="处理完成", *, output=None):
        return cls("succeeded", content, message, output=output)

    @classmethod
    def failure(cls, message, retryable=False):
        return cls("failed", None, message, retryable)


def run(handler: Callable[[Task], Result]):
    data = json.load(sys.stdin)
    task = Task(**data)
    try:
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
