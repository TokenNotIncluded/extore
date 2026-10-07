"""Scoped merchant work briefs, kept separate from customer-supplied inputs."""

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass

SCHEMA = "extore.work-instructions.v1"
MAX_SLOGAN_CHARACTERS = 4000
_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_REVISION = re.compile(r"[0-9a-f]{64}\Z")


def validate_slogan(value):
    """Validate text without interpreting Markdown, commands, or links."""
    if (
        not isinstance(value, str)
        or len(value) > MAX_SLOGAN_CHARACTERS
        or any(
            unicodedata.category(char) == "Cc" and char not in "\n\r\t"
            for char in value
        )
    ):
        raise ValueError("invalid work instructions")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("invalid work instructions") from None
    return value


@dataclass(frozen=True)
class WorkInstructions:
    """A server-issued factory and workshop brief; it never grants permission."""

    schema: str
    shop_id: str
    product_id: str
    factory_slogan: str
    workshop_slogan: str
    revision: str

    def __post_init__(self):
        if (
            self.schema != SCHEMA
            or not isinstance(self.shop_id, str)
            or not _ID.fullmatch(self.shop_id)
            or not isinstance(self.product_id, str)
            or not _ID.fullmatch(self.product_id)
            or not isinstance(self.revision, str)
            or not _REVISION.fullmatch(self.revision)
        ):
            raise ValueError("invalid work instructions")
        validate_slogan(self.factory_slogan)
        validate_slogan(self.workshop_slogan)

    @classmethod
    def from_dict(cls, value, *, product_id=None, shop_id=None):
        if not isinstance(value, Mapping) or set(value) != {
            "schema",
            "shop_id",
            "product_id",
            "factory_slogan",
            "workshop_slogan",
            "revision",
        }:
            raise ValueError("invalid work instructions")
        result = cls(**dict(value))
        if (
            product_id is not None
            and result.product_id != product_id
            or shop_id is not None
            and result.shop_id != shop_id
        ):
            raise ValueError("work instructions belong to a different scope")
        return result

    def as_dict(self):
        return asdict(self)
