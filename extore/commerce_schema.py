"""Public structural schemas for commerce import v1, independent of any shop.

Authorization, identifier relationships, reference-price precision, quota sums,
time-relative expiry and idempotency are still checked by the server. Returning
a fresh object prevents one consumer from modifying the public contract.
"""

from .commerce_models import MAX_ISSUE_COUNT, MAX_LIMITS, MAX_PRODUCTS, SCOPES

SCHEMA_ID = "https://extore.lmm.best/api/integrations/commerce/schema"


def _ref(name):
    return {"$ref": "#/$defs/" + name}


def _object(properties, required=(), *, request=False):
    result = {"type": "object", "properties": properties}
    if required:
        result["required"] = list(required)
    if request:
        result["additionalProperties"] = False
    return result


def _localized(limit):
    return {
        "oneOf": [
            {"type": "string", "maxLength": limit},
            {
                "type": "object",
                "propertyNames": {
                    "pattern": "^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*(?![\\s\\S])"
                },
                "additionalProperties": {"type": "string", "maxLength": limit},
            },
        ]
    }


def commerce_schema():
    identity = {
        "type": "string",
        "minLength": 1,
        "maxLength": 100,
        "pattern": "^[^\\u0000-\\u001f\\u007f]+(?![\\s\\S])",
    }
    pid = {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,100}(?![\\s\\S])"}
    variant_id = {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{0,39}(?![\\s\\S])"}
    timestamp = {"type": "number", "exclusiveMinimum": 0}
    field_schema = _object(
        {
            "key": {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,39}(?![\\s\\S])"},
            "label": _localized(200),
            "description": _localized(10000),
            "type": {
                "enum": [
                    "text",
                    "email",
                    "url",
                    "textarea",
                    "number",
                    "file",
                    "select",
                    "boolean",
                    "image",
                    "images",
                ]
            },
            "required": {"type": "boolean"},
            "collapsed": {"type": "boolean"},
            "options": {
                "type": "array",
                "maxItems": 100,
                "items": _object(
                    {
                        "value": {
                            "type": "string",
                            "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(?![\\s\\S])",
                        },
                        "label": _localized(200),
                    },
                    ("value",),
                ),
            },
            "max_items": {"type": "integer", "minimum": 1, "maximum": 20},
        }
    )
    variant = _object(
        {
            "id": variant_id,
            "name": {"type": "string", "maxLength": 120},
            "description": {"type": "string", "maxLength": 10000},
            "price": _ref("ReferencePrice"),
            "currency": {"type": "string", "pattern": "^[A-Z]{3,5}(?![\\s\\S])"},
            "attributes": {
                "type": "object",
                "maxProperties": 20,
                "propertyNames": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 100,
                    "not": {"enum": ["__proto__", "constructor", "prototype"]},
                },
                "additionalProperties": {
                    "oneOf": [
                        {"type": "null"},
                        {"type": "boolean"},
                        {"type": "string", "maxLength": 1000},
                        {
                            "type": "number",
                            "minimum": -9007199254740991,
                            "maximum": 9007199254740991,
                        },
                    ]
                },
            },
            "enabled": {"type": "boolean"},
        },
        ("price", "attributes"),
    )
    product = _object(
        {
            "name": _localized(120),
            "description": _localized(20000),
            "logo": {
                "type": "string",
                "format": "uri",
                "maxLength": 2000,
                "pattern": "^https://",
            },
            "image": {
                "type": "string",
                "format": "uri",
                "maxLength": 2000,
                "pattern": "^https://",
            },
            "public": {"type": "boolean"},
            "mode": {"enum": ["manual", "webhook", "script", "stock"]},
            "delivery": {"enum": ["content", "service"]},
            "view_policy": {"enum": ["repeat", "once"]},
            "parameters": {"type": "array", "maxItems": 30, "items": _ref("Field")},
            "outputs": {"type": "array", "maxItems": 30, "items": _ref("Field")},
            "progress_steps": {
                "type": "array",
                "maxItems": 30,
                "items": _object({"id": variant_id, "label": _localized(200)}, ("id",)),
            },
            "support_email": {"type": "string", "maxLength": 254},
            "revision_policy": {
                "oneOf": [
                    {"type": "null"},
                    _object(
                        {
                            "attribute_key": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 100,
                            },
                            "label": _localized(200),
                        },
                        ("attribute_key",),
                    ),
                ]
            },
        },
        ("parameters", "outputs", "progress_steps"),
    )
    issue_properties = {
        "product_id": pid,
        "variant_id": variant_id,
        "count": {"type": "integer", "minimum": 1, "maximum": MAX_ISSUE_COUNT},
        "label": {
            "type": "string",
            "maxLength": 100,
            "pattern": "^[^\\u0000-\\u001f\\u007f]*(?![\\s\\S])",
        },
        "expires": {"oneOf": [{"type": "null"}, timestamp]},
        "expected_revision": {"oneOf": [{"type": "null"}, _ref("Revision")]},
    }
    limit = _object(
        {
            "product_id": pid,
            "variant_id": variant_id,
            "max_count": {"type": "integer", "minimum": 1, "maximum": 10000},
        },
        ("product_id", "variant_id", "max_count"),
        request=True,
    )
    definitions = {
        "Identity": identity,
        "Revision": {"type": "string", "pattern": "^[0-9a-f]{64}(?![\\s\\S])"},
        "IdempotencyKey": {
            "type": "string",
            "minLength": 8,
            "maxLength": 200,
            "pattern": "^[!-~]+(?![\\s\\S])",
        },
        "ReferencePrice": {
            "description": "Original decimal text; reference price only, never the actual selling price. Null is unknown. Numeric JSON prices are not accepted.",
            "oneOf": [
                {"type": "null"},
                {
                    "type": "string",
                    "maxLength": 100,
                    "pattern": "^[0-9]+(?:\\.[0-9]{1,6})?(?![\\s\\S])",
                },
            ],
        },
        "Field": field_schema,
        "Variant": variant,
        "Product": product,
        "Listing": _object(
            {
                "schema": {"const": "extore.product-listing.v1"},
                "id": identity,
                "shop_id": identity,
                "revision": _ref("Revision"),
                "redemption_url": {"type": "string", "format": "uri"},
                "semantics": _object(
                    {
                        "price": {"const": "reference"},
                        "inventory": {"const": "not_exported"},
                        "payment": {"const": "external_sales_platform"},
                        "redemption": {"const": "extore"},
                    },
                    ("price", "inventory", "payment", "redemption"),
                ),
                "product": _ref("Product"),
                "variants": {
                    "type": "array",
                    "maxItems": 100,
                    "items": _ref("Variant"),
                },
            },
            (
                "schema",
                "id",
                "shop_id",
                "revision",
                "redemption_url",
                "semantics",
                "product",
                "variants",
            ),
        ),
        "Catalog": _object(
            {
                "schema": {"const": "extore.commerce-catalog.v1"},
                "issuer": {"type": "string", "format": "uri"},
                "shop": _object(
                    {"id": identity, "name": {"type": "string"}}, ("id", "name")
                ),
                "grant_id": identity,
                "products": {
                    "type": "array",
                    "maxItems": MAX_PRODUCTS,
                    "items": _ref("Listing"),
                },
            },
            ("schema", "issuer", "shop", "grant_id", "products"),
        ),
        "CardIssueRequest": _object(
            issue_properties, ("product_id", "variant_id", "count"), request=True
        ),
        "CLIStockRequest": _object(
            {
                **issue_properties,
                "idempotency_key": _ref("IdempotencyKey"),
                "expected_revision": _ref("Revision"),
            },
            (
                "product_id",
                "variant_id",
                "count",
                "idempotency_key",
                "expected_revision",
            ),
            request=True,
        ),
        "CardBatch": _object(
            {
                "schema": {"const": "extore.card-batch.v1"},
                "grant_id": identity,
                "product_id": pid,
                "variant_id": variant_id,
                "batch_id": identity,
                "count": {"type": "integer", "minimum": 1, "maximum": MAX_ISSUE_COUNT},
                "codes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_ISSUE_COUNT,
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "minLength": 16,
                        "maxLength": 4096,
                        "pattern": "^[!-~]+(?![\\s\\S])",
                    },
                },
                "created_at": timestamp,
                "recovery_expires": timestamp,
                "quota": _object(
                    {
                        "max_count": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10000,
                        },
                        "issued_count": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 10000,
                        },
                        "remaining": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 10000,
                        },
                    },
                    ("max_count", "issued_count", "remaining"),
                ),
                "variant": _ref("Variant"),
            },
            (
                "schema",
                "grant_id",
                "product_id",
                "variant_id",
                "batch_id",
                "count",
                "codes",
                "created_at",
                "recovery_expires",
                "quota",
                "variant",
            ),
        ),
        "CardLimit": limit,
        "Approval": _object(
            {
                "shop_id": identity,
                "review_digest": _ref("Revision"),
                "scopes": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 2,
                    "uniqueItems": True,
                    "items": {"enum": list(SCOPES)},
                },
                "product_ids": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_PRODUCTS,
                    "uniqueItems": True,
                    "items": pid,
                },
                "card_limits": {
                    "type": "array",
                    "maxItems": MAX_LIMITS,
                    "items": _ref("CardLimit"),
                },
                "grant_expires": timestamp,
            },
            ("shop_id", "review_digest", "scopes", "product_ids", "grant_expires"),
            request=True,
        ),
        "TokenResponse": _object(
            {
                "access_token": {"type": "string", "minLength": 16, "maxLength": 4096},
                "refresh_token": {"type": "string", "minLength": 16, "maxLength": 4096},
                "token_type": {"const": "Bearer"},
                "expires_in": {"type": "integer", "minimum": 1, "maximum": 900},
                "scope": {
                    "enum": [
                        "products.read",
                        "cards.issue",
                        "products.read cards.issue",
                    ]
                },
                "grant_id": identity,
                "grant_expires": timestamp,
            },
            (
                "access_token",
                "refresh_token",
                "token_type",
                "expires_in",
                "scope",
                "grant_id",
                "grant_expires",
            ),
        ),
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "Extore commerce import protocol v1",
        "description": "Structural validation only. Requests reject unknown fields; responses permit additive extensions. Never log CardBatch or TokenResponse data.",
        "oneOf": [
            _ref(name)
            for name in (
                "Catalog",
                "Listing",
                "CardIssueRequest",
                "CardBatch",
                "Approval",
                "TokenResponse",
            )
        ],
        "$defs": definitions,
    }
