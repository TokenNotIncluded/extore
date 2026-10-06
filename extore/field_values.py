"""String-compatible product fields and bounded opaque attachment references."""

import json
import re

ATTACHMENT_TYPES = frozenset({"file", "image", "images"})
IMAGE_TYPES = frozenset({"image", "images"})
IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
DEFAULT_MAX_IMAGES = 10
MAX_IMAGES = 20
_FILE_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def attachment_ids(field, value):
    """Return references in order, never interpreting URLs or embedded files."""
    if field.get("type") not in ATTACHMENT_TYPES:
        return []
    if not isinstance(value, str):
        raise ValueError("附件字段必须是文本")
    if not value:
        return []
    if field["type"] != "images":
        return [value]
    try:
        values = json.loads(value)
    except (ValueError, RecursionError):
        raise ValueError("图片集合必须是文件 ID 的 JSON 数组") from None
    maximum = field.get("max_items", DEFAULT_MAX_IMAGES)
    if (
        type(maximum) is not int
        or not 1 <= maximum <= MAX_IMAGES
        or not isinstance(values, list)
        or len(values) > maximum
        or any(
            not isinstance(fid, str) or not _FILE_ID.fullmatch(fid) for fid in values
        )
        or len(set(values)) != len(values)
    ):
        raise ValueError("图片集合包含无效、重复或过多的文件 ID")
    return values


def normalize_rich_value(field, value):
    """Canonicalize new types while preserving the existing string API."""
    if not isinstance(value, str):
        raise ValueError("字段值必须是文本")
    kind = field.get("type", "text")
    if kind == "images":
        ids = attachment_ids(field, value)
        if field.get("required", True) and not ids:
            raise ValueError("请至少提供一张图片")
        return json.dumps(ids, separators=(",", ":"))
    if kind == "image" and value and not _FILE_ID.fullmatch(value):
        raise ValueError("图片字段必须是已上传的文件 ID")
    if kind == "boolean" and value not in ("", "true", "false"):
        raise ValueError("是／否字段只能填写 true 或 false")
    if (
        kind == "select"
        and value
        and value not in {option["value"] for option in field.get("options", [])}
    ):
        raise ValueError("请选择商品定义的选项")
    return value
