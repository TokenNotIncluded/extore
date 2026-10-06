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
    configuration = validate_configuration(
        processor_id,
        config.get("processor_config", {}),
        allow_incomplete=allow_incomplete,
    )
    return {
        **config,
        "script": "",
        "delivery": spec["delivery"],
        "parameters": spec["parameters"],
        "outputs": spec["outputs"],
        "processor_config": configuration,
    }


def _fields(fields):
    return [
        {
            "description": {},
            "collapsed": True,
            "required": True,
            "type": "text",
            **field,
        }
        for field in fields
    ]


def normalize_product(config, *, allow_incomplete=True):
    return normalize_processor_product(
        config, strict_schema=False, allow_incomplete=allow_incomplete
    )


def public_configuration(config):
    """Metadata-only configuration view for links lacking fulfillment access."""
    if config.get("mode") != "script" or not config.get("processor_id"):
        return {}
    spec = specification(config["processor_id"])
    secret_keys = {
        field["key"] for field in spec["configuration"] if field.get("secret")
    }
    return {
        key: value
        for key, value in config.get("processor_config", {}).items()
        if key not in secret_keys
    }
