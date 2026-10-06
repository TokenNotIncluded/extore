"""Bounded declarative task graphs; no executable or credential selection.

File values here are only opaque references.  Their ownership, MIME type and
availability must be checked by the attachment service before accepting data.
"""

import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

MAX_NODES = 64
MAX_TRANSITIONS = 256
MAX_DEFINITION_BYTES = 100000
MAX_SNAPSHOT_BYTES = 200000
MAX_VALUES_BYTES = 200000
MAX_FIELDS = 30
MAX_CASES = 32
MAX_OPTIONS = 50
MAX_IMAGES = 20
FIELD_TYPES = {
    "text",
    "textarea",
    "email",
    "url",
    "number",
    "file",
    "image",
    "images",
    "select",
    "boolean",
}
_NODE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_FIELD_KEY = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_FILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_FIELD_KEYS = {
    "key",
    "label",
    "description",
    "collapsed",
    "required",
    "type",
    "options",
    "max_items",
    "sensitive",
    "sensitive_ttl_seconds",
}
_COMMON = {"id", "kind", "label"}
_SNAPSHOT_PRODUCT_KEYS = {
    "id",
    "name",
    "mode",
    "delivery",
    "view_policy",
    "parameters",
    "outputs",
    "allow_retry",
    "max_attempts",
    "processor_id",
    "webhook_url",
    "webhook_secret",
    "shop_id",
}
_NODE_KEYS = {
    "input": _COMMON
    | {
        "prompt",
        "question",
        "fields",
        "start_policy",
        "show_from",
        "timeout_seconds",
        "timeout_next",
        "next",
    },
    "process": _COMMON
    | {"inputs", "outputs", "timeout_seconds", "timeout_next", "failure_next", "next"},
    "display": _COMMON
    | {"content", "show_from", "result", "timeout_seconds", "timeout_next", "next"},
    "end": _COMMON | {"state", "result", "message", "retryable", "needs_review"},
}


def _invalid(message="定义无效"):
    raise ValueError(f"任务流程{message}")


def _encoded_size(value):
    try:
        return len(
            json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _invalid("定义编码无效")


def _keys(value, allowed, *, required=()):
    if (
        not isinstance(value, dict)
        or set(value) - allowed
        or set(required) - set(value)
    ):
        _invalid("字段无效或缺少必填项")


def _i18n(value, *, limit=10000, required=False):
    if not isinstance(value, dict) or len(value) > 20 or (required and not value):
        _invalid("多语言文本无效")
    result = {}
    for locale, text in value.items():
        if (
            not isinstance(locale, str)
            or not locale.strip()
            or len(locale) > 40
            or _CONTROL.search(locale)
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > limit
            or "\0" in text
        ):
            _invalid("多语言文本无效或过长")
        try:
            locale.encode("utf-8")
            text.encode("utf-8")
        except UnicodeError:
            _invalid("多语言文本编码无效")
        result[locale] = text
    return result


def _boolean(value):
    if type(value) is not bool:
        _invalid("布尔配置必须是布尔值")
    return value


def _fields(value, *, allow_sensitive=True):
    if not isinstance(value, list) or len(value) > MAX_FIELDS:
        _invalid("字段数量无效")
    result, seen = [], set()
    for field in value:
        _keys(field, _FIELD_KEYS, required=("key", "label"))
        key, kind = field["key"], field.get("type", "text")
        if not isinstance(key, str) or not _FIELD_KEY.fullmatch(key) or key in seen:
            _invalid("字段代码无效或重复")
        if not isinstance(kind, str) or kind not in FIELD_TYPES:
            _invalid("字段类型无效")
        seen.add(key)
        normalized = {
            "key": key,
            "label": _i18n(field["label"], limit=200, required=True),
            "description": _i18n(field.get("description", {})),
            "collapsed": _boolean(field.get("collapsed", True)),
            "required": _boolean(field.get("required", True)),
            "type": kind,
        }
        if kind == "select":
            options = field.get("options")
            if not isinstance(options, list) or not 1 <= len(options) <= MAX_OPTIONS:
                _invalid("选择项数量无效")
            choices, option_values = [], set()
            for option in options:
                _keys(option, {"value", "label"}, required=("value", "label"))
                text = option["value"]
                if (
                    not isinstance(text, str)
                    or not text
                    or text != text.strip()
                    or len(text) > 100
                    or _CONTROL.search(text)
                    or text in option_values
                ):
                    _invalid("选择项代码无效或重复")
                option_values.add(text)
                choices.append(
                    {
                        "value": text,
                        "label": _i18n(option["label"], limit=200, required=True),
                    }
                )
            normalized["options"] = choices
        elif "options" in field:
            _invalid("只有选择字段允许选择项")
        if kind == "images":
            maximum = field.get("max_items", 10)
            if type(maximum) is not int or not 1 <= maximum <= MAX_IMAGES:
                _invalid("图片数量上限无效")
            normalized["max_items"] = maximum
        elif "max_items" in field:
            _invalid("只有图片列表允许数量上限")
        sensitive = _boolean(field.get("sensitive", False))
        if sensitive:
            if not allow_sensitive or kind != "text":
                _invalid("敏感字段只能是输入文本")
            lifetime = field.get("sensitive_ttl_seconds", 120)
            if type(lifetime) is not int or not 1 <= lifetime <= 600:
                _invalid("敏感字段有效期无效")
            normalized["sensitive"] = True
            normalized["sensitive_ttl_seconds"] = lifetime
        elif "sensitive_ttl_seconds" in field:
            _invalid("只有敏感字段允许有效期")
        result.append(normalized)
    return result


def _valid_url(value):
    try:
        parsed = urlsplit(value)
        parsed.port
        return bool(
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and "\\" not in value
            and not re.search(r"[\s\x00-\x1f\x7f]", value)
        )
    except ValueError:
        return False


def validate_values(fields, values, *, output=False):
    """Canonicalize typed string values without asserting attachment rights."""
    fields = _fields(fields, allow_sensitive=not output)
    if not isinstance(values, dict) or set(values) - {field["key"] for field in fields}:
        _invalid("提交了未定义的字段")
    if any(not isinstance(value, str) for value in values.values()):
        _invalid("字段值必须是文本")
    if _encoded_size(values) > MAX_VALUES_BYTES:
        _invalid("字段内容超过编码大小上限")
    maximum = 100000 if output else 10000
    if output and sum(len(value) for value in values.values()) > 100000:
        _invalid("输出内容过长")
    result = {}
    for field in fields:
        key, kind = field["key"], field["type"]
        value = values.get(key, "")
        if len(value) > maximum or "\0" in value:
            _invalid("字段内容过长或无效")
        try:
            value.encode("utf-8")
        except UnicodeError:
            _invalid("字段文本编码无效")
        if not output or kind not in ("text", "textarea"):
            value = value.strip()
        if kind == "images":
            if not value:
                items = []
            else:
                try:
                    items = json.loads(value)
                except (ValueError, TypeError, RecursionError):
                    _invalid("图片列表必须是文件代码的 JSON 列表")
            if (
                not isinstance(items, list)
                or len(items) > field["max_items"]
                or any(
                    not isinstance(item, str) or not _FILE_ID.fullmatch(item)
                    for item in items
                )
                or len(items) != len(set(items))
            ):
                _invalid("图片列表无效或超过数量上限")
            if field["required"] and not items:
                _invalid("缺少必填字段")
            value = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        else:
            if field["required"] and not value.strip():
                _invalid("缺少必填字段")
            if value:
                if kind == "email" and not re.fullmatch(
                    r"[^\s@]+@[^\s@]+\.[^\s@]+", value
                ):
                    _invalid("邮箱格式不正确")
                if kind == "url" and not _valid_url(value):
                    _invalid("链接格式不正确")
                if kind == "number":
                    try:
                        if not Decimal(value).is_finite():
                            _invalid("数字必须有限")
                    except InvalidOperation:
                        _invalid("数字格式不正确")
                if kind == "boolean":
                    value = value.lower()
                    if value not in ("true", "false"):
                        _invalid("布尔字段必须是 true 或 false")
                if kind == "select" and value not in {
                    option["value"] for option in field["options"]
                }:
                    _invalid("选择项不在允许的范围内")
                if kind in ("file", "image") and not _FILE_ID.fullmatch(value):
                    _invalid("文件必须是有效的文件代码")
        result[key] = value
    if _encoded_size(result) > MAX_VALUES_BYTES:
        _invalid("字段内容超过编码大小上限")
    return result


def _target(value):
    if not isinstance(value, str) or not _NODE_ID.fullmatch(value):
        _invalid("节点目标代码无效")
    return value


def _reference(value):
    _keys(value, {"node", "field"}, required=("node", "field"))
    node, field = _target(value["node"]), value["field"]
    if not isinstance(field, str) or not _FIELD_KEY.fullmatch(field):
        _invalid("引用字段代码无效")
    return {"node": node, "field": field}


def _mapping(value):
    if not isinstance(value, dict) or len(value) > MAX_FIELDS:
        _invalid("字段映射无效")
    result = {}
    for key, reference in value.items():
        if not isinstance(key, str) or not _FIELD_KEY.fullmatch(key):
            _invalid("映射字段代码无效")
        result[key] = _reference(reference)
    return result


def _next(value):
    if isinstance(value, str):
        return _target(value)
    _keys(value, {"cases", "default"}, required=("cases", "default"))
    cases = value["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        _invalid("条件分支数量无效")
    result = {"cases": [], "default": _target(value["default"])}
    for case in cases:
        _keys(case, {"when", "to"}, required=("when", "to"))
        when = case["when"]
        _keys(when, {"source", "op", "value"}, required=("source", "op"))
        operation = when["op"]
        if not isinstance(operation, str) or operation not in ("eq", "in", "exists"):
            _invalid("条件操作无效")
        normalized = {"source": _reference(when["source"]), "op": operation}
        if operation == "exists":
            if "value" in when:
                _invalid("存在性条件不接受比较值")
        elif operation == "eq":
            if not isinstance(when.get("value"), str):
                _invalid("相等条件必须有文本比较值")
            normalized["value"] = when["value"]
        else:
            values = when.get("value")
            if (
                not isinstance(values, list)
                or not 1 <= len(values) <= MAX_OPTIONS
                or any(not isinstance(v, str) for v in values)
            ):
                _invalid("集合条件必须有文本比较值列表")
            normalized["value"] = list(values)
        result["cases"].append({"when": normalized, "to": _target(case["to"])})
    return result


def _timeout(node, *, process=False):
    seconds = node.get("timeout_seconds", 3600 if process else None)
    if seconds is None:
        if process or "timeout_next" in node:
            _invalid("超时路径配置无效")
        return {"timeout_seconds": None}
    if (
        type(seconds) is not int
        or not 1 <= seconds <= 86400
        or "timeout_next" not in node
    ):
        _invalid("超时时限或路径配置无效")
    return {"timeout_seconds": seconds, "timeout_next": _target(node["timeout_next"])}


def _compatible(source, destination):
    if source["type"] != destination["type"]:
        return False
    if source["type"] == "select":
        return {option["value"] for option in source["options"]}.issubset(
            option["value"] for option in destination["options"]
        )
    if source["type"] == "images":
        return source["max_items"] <= destination["max_items"]
    return True


def _edges(node):
    targets = []
    if node["kind"] != "end":
        route = node["next"]
        if isinstance(route, str):
            targets.append(route)
        else:
            targets.extend(case["to"] for case in route["cases"])
            targets.append(route["default"])
    for key in ("timeout_next", "failure_next"):
        if key in node:
            targets.append(node[key])
    return targets


def validate_definition(value, product):
    """Validate and return a copied canonical graph; ``None`` keeps the old path."""
    if value is None:
        return None
    _keys(value, {"version", "entry", "nodes"}, required=("version", "entry", "nodes"))
    if _encoded_size(value) > MAX_DEFINITION_BYTES:
        _invalid("定义超过大小上限")
    if type(value["version"]) is not int or value["version"] != 1:
        _invalid("版本无效")
    supplied = value["nodes"]
    if not isinstance(supplied, list) or not 1 <= len(supplied) <= MAX_NODES:
        _invalid("节点数量无效")
    if not isinstance(product, dict) or product.get("mode", "manual") not in (
        "manual",
        "script",
        "webhook",
    ):
        _invalid("商品处理方式无效")
    parameters = {
        field["key"]: field for field in _fields(product.get("parameters", []))
    }
    outputs = {
        field["key"]: field
        for field in _fields(product.get("outputs", []), allow_sensitive=False)
    }
    nodes = {}
    for node in supplied:
        if (
            not isinstance(node, dict)
            or not isinstance(node.get("kind"), str)
            or node["kind"] not in _NODE_KEYS
        ):
            _invalid("节点类型无效")
        kind = node["kind"]
        _keys(node, _NODE_KEYS[kind], required=("id", "kind"))
        node_id = _target(node["id"])
        if node_id in nodes:
            _invalid("节点代码重复")
        normalized = {"id": node_id, "kind": kind}
        if "label" in node:
            normalized["label"] = _i18n(node["label"], limit=200)
        if kind == "input":
            _keys(node, _NODE_KEYS[kind], required=("fields", "next"))
            start_policy = node.get("start_policy", "confirm")
            if start_policy not in ("confirm", "automatic"):
                _invalid("开始策略无效")
            normalized.update(
                {
                    "prompt": _i18n(node.get("prompt", {})),
                    "question": _i18n(node.get("question", {})),
                    "fields": _fields(node["fields"]),
                    "start_policy": start_policy,
                    "show_from": _mapping(node.get("show_from", {})),
                    "next": _next(node["next"]),
                    **_timeout(node),
                }
            )
        elif kind == "process":
            _keys(
                node,
                _NODE_KEYS[kind],
                required=("inputs", "outputs", "next", "failure_next", "timeout_next"),
            )
            normalized.update(
                {
                    "inputs": _mapping(node["inputs"]),
                    "outputs": _fields(node["outputs"], allow_sensitive=False),
                    "next": _next(node["next"]),
                    "failure_next": _target(node["failure_next"]),
                    **_timeout(node, process=True),
                }
            )
        elif kind == "display":
            _keys(node, _NODE_KEYS[kind], required=("next",))
            if "result" in node and "show_from" in node:
                _invalid("展示字段映射存在歧义")
            normalized.update(
                {
                    "content": _i18n(node.get("content", {})),
                    "show_from": _mapping(
                        node.get("show_from", node.get("result", {}))
                    ),
                    "next": _target(node["next"]),
                    **_timeout(node),
                }
            )
        else:
            state = node.get("state")
            if state not in ("succeeded", "failed", "rejected"):
                _invalid("终态无效")
            result = _mapping(node.get("result", {}))
            if state != "succeeded" and result:
                _invalid("失败或拒绝终态不能保留结果映射")
            if state == "succeeded" and (
                set(result) - set(outputs)
                or {key for key, field in outputs.items() if field["required"]}
                - set(result)
            ):
                _invalid("成功终态输出不符合商品定义")
            normalized.update(
                {
                    "state": state,
                    "result": result,
                    "message": _i18n(node.get("message", {})),
                    "retryable": _boolean(node.get("retryable", False)),
                    "needs_review": _boolean(node.get("needs_review", False)),
                }
            )
        nodes[node_id] = normalized
    entry = _target(value["entry"])
    if entry not in nodes or nodes[entry]["kind"] not in ("input", "display"):
        _invalid("入口必须是输入或展示节点")
    for node in nodes.values():
        if any(target not in nodes for target in _edges(node)):
            _invalid("引用了不存在的节点目标")

    fields = {
        node_id: {
            field["key"]: field for field in node.get("fields", node.get("outputs", []))
        }
        for node_id, node in nodes.items()
    }

    reference_cache = {}

    def reference_origin(reference):
        visited, order = set(), []
        while True:
            node_id, key = reference["node"], reference["field"]
            identity = (node_id, key)
            if node_id not in nodes or identity in visited:
                _invalid("字段引用不存在或形成无来源的循环")
            if identity in reference_cache:
                origin = reference_cache[identity]
                break
            visited.add(identity)
            order.append(identity)
            node = nodes[node_id]
            if node["kind"] in ("input", "process"):
                if key not in fields[node_id]:
                    _invalid("字段引用不存在")
                origin = node["kind"], fields[node_id][key]
                break
            mapping = (
                node.get("show_from", {})
                if node["kind"] == "display"
                else node.get("result", {})
            )
            if key not in mapping:
                _invalid("字段引用不存在")
            reference = mapping[key]
        for identity in order:
            reference_cache[identity] = origin
        return origin

    def reference_field(reference):
        return reference_origin(reference)[1]

    for node in nodes.values():
        kind = node["kind"]
        if kind in ("input", "display"):
            for key, reference in node["show_from"].items():
                source = reference_field(reference)
                if source.get("sensitive"):
                    _invalid("敏感字段只能传入处理节点")
        if kind == "process":
            for key, reference in node["inputs"].items():
                source = reference_field(reference)
                if product.get("mode") == "script" and (
                    key not in parameters or not _compatible(source, parameters[key])
                ):
                    _invalid("处理器输入与商品定义不兼容")
            if product.get("mode") == "script":
                if {
                    key for key, field in parameters.items() if field["required"]
                } - set(node["inputs"]):
                    _invalid("处理器缺少商品必填输入")
                if set(fields[node["id"]]) != set(outputs) or any(
                    not _compatible(field, outputs[key])
                    or not _compatible(outputs[key], field)
                    or field["required"] != outputs[key]["required"]
                    for key, field in fields[node["id"]].items()
                ):
                    _invalid("处理器输出与商品定义不兼容")
        if kind in ("input", "process") and isinstance(node["next"], dict):
            for case in node["next"]["cases"]:
                when = case["when"]
                source = reference_field(when["source"])
                if source.get("sensitive"):
                    _invalid("条件分支不能引用敏感字段")
                if when["op"] != "exists":
                    values = [when["value"]] if when["op"] == "eq" else when["value"]
                    comparison = {**source, "required": False}
                    cleaned = [
                        validate_values([comparison], {source["key"]: item})[
                            source["key"]
                        ]
                        for item in values
                    ]
                    when["value"] = cleaned[0] if when["op"] == "eq" else cleaned
        if kind == "end" and node["state"] == "succeeded":
            for key, reference in node["result"].items():
                source_kind, source = reference_origin(reference)
                if source.get("sensitive"):
                    _invalid("最终输出不能引用敏感字段")
                if (
                    outputs[key]["type"] in ("file", "image", "images")
                    and source_kind != "process"
                ):
                    _invalid("最终附件必须来自处理节点的输出")
                if not _compatible(source, outputs[key]):
                    _invalid("最终输出字段类型不兼容")
    reachable, pending = set(), [entry]
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(_edges(nodes[node_id]))
    if not any(nodes[node_id]["kind"] == "end" for node_id in reachable):
        _invalid("必须存在从入口可达的结束节点")
    canonical = {"version": 1, "entry": entry, "nodes": list(nodes.values())}
    if _encoded_size(canonical) > MAX_DEFINITION_BYTES:
        _invalid("规范化定义超过大小上限")
    projected_product = {
        key: product[key] for key in _SNAPSHOT_PRODUCT_KEYS if key in product
    }
    missing_identity = any(key not in projected_product for key in ("id", "shop_id"))
    for key in ("id", "shop_id"):
        projected_product.setdefault(key, "00000000-0000-4000-8000-000000000000")
    reserve = 128 if missing_identity else 0
    if (
        _encoded_size({"product": projected_product, "definition": canonical}) + reserve
        > MAX_SNAPSHOT_BYTES
    ):
        _invalid("商品与流程快照超过大小上限")
    return deepcopy(canonical)
