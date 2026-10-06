"""Adapter for the reviewed, version-pinned processor catalog."""


def catalog():
    from extore_processors import catalog as official_catalog

    return official_catalog()


def processor_catalog():
    return catalog()


def specification(processor_id):
    from extore_processors import get_spec

    return get_spec(processor_id)


def normalize_processor_product(config, *, strict_schema=True, allow_incomplete=True):
    """Processor code is authoritative for customer inputs and delivered outputs."""
    if config.get("mode") != "script":
        return config
    from extore_processors import validate_configuration

    processor_id = config.get("processor_id", "")
    spec = specification(processor_id)
    if strict_schema:
        for name in ("parameters", "outputs"):
            declared = config.get(name)
            if declared and _fields(declared) != _fields(spec[name]):
                raise ValueError("顾客填写项和输出字段由商品处理器代码定义")
    supplied = config.get("processor_config", {})
    configuration = validate_configuration(
        processor_id,
        supplied,
        allow_incomplete=allow_incomplete,
    )
    return {
        **config,
        "script": "",
        "delivery": spec["delivery"],
        "parameters": spec["parameters"],
        "outputs": spec["outputs"],
        # Keep omitted keys distinguishable from explicitly cleared text. The
        # credential store applies defaults after merging an existing binding.
        "processor_config": {key: configuration[key] for key in supplied},
    }


def _fields(fields):
    result = []
    for field in fields:
        normalized = {
            "description": {},
            "collapsed": True,
            "required": True,
            "type": "text",
            **field,
        }
        # Defaults for extension fields must not alter legacy catalog schemas.
        if normalized["type"] != "select":
            normalized.pop("options", None)
        if normalized["type"] == "images":
            normalized.setdefault("max_items", 10)
        else:
            normalized.pop("max_items", None)
        if not normalized.get("sensitive"):
            normalized.pop("sensitive", None)
            normalized.pop("sensitive_ttl_seconds", None)
        else:
            normalized.setdefault("sensitive_ttl_seconds", 120)
        result.append(normalized)
    return result


def normalize_product(config, *, allow_incomplete=True):
    return normalize_processor_product(
        config, strict_schema=False, allow_incomplete=allow_incomplete
    )


def configuration_fields(processor_id):
    """Only processor code may classify a configuration field as readable."""
    spec = specification(processor_id)
    fields = {}
    for field in spec.get("configuration", []):
        key = field.get("key") if isinstance(field, dict) else None
        if not isinstance(key, str):
            continue
        # Conflicting or duplicate declarations remain secret rather than
        # allowing a later declaration to broaden an earlier classification.
        if key in fields:
            fields[key] = {**field, "secret": True}
        else:
            fields[key] = field
    return fields


def editable_configuration(processor_id, values):
    """Return explicitly non-secret fields for an already authorized owner."""
    if not isinstance(values, dict):
        return {}
    fields = configuration_fields(processor_id)
    return {
        key: value
        for key, value in values.items()
        if key in fields
        and fields[key].get("secret") is False
        and isinstance(value, str)
    }


def public_configuration(config):
    """Shared product exports and management links never contain shop settings."""
    return {}
