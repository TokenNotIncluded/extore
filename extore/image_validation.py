"""Bounded decoding of supported raster uploads without trusting MIME labels."""

import warnings

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_PIXELS = 16_000_000
_FORMATS = ("PNG", "JPEG", "WEBP")
_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_INVALID = "图片必须是可解码的 PNG、JPEG 或 WebP 静态图片，且不超过 1600 万像素"


def _dimensions(width, height):
    if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError(_INVALID)


def _preflight_webp(stream):
    """Bound WebP canvas/bitstream dimensions before its native decoder opens."""
    stream.seek(0)
    header = stream.read(12)
    if header[:4] != b"RIFF" or header[8:] != b"WEBP":
        stream.seek(0)
        return
    stream.seek(0, 2)
    size = stream.tell()
    if int.from_bytes(header[4:8], "little") + 8 != size:
        raise ValueError(_INVALID)
    offset = 12
    chunks = 0
    while offset < size:
        chunks += 1
        if chunks > 1024 or size - offset < 8:
            raise ValueError(_INVALID)
        stream.seek(offset)
        chunk = stream.read(8)
        kind = chunk[:4]
        length = int.from_bytes(chunk[4:8], "little")
        end = offset + 8 + length + (length & 1)
        if end > size:
            raise ValueError(_INVALID)
        if kind in (b"ANIM", b"ANMF"):
            raise ValueError(_INVALID)
        if kind == b"VP8X":
            if length != 10:
                raise ValueError(_INVALID)
            data = stream.read(10)
            if data[0] & 2:
                raise ValueError(_INVALID)
            _dimensions(
                int.from_bytes(data[4:7], "little") + 1,
                int.from_bytes(data[7:10], "little") + 1,
            )
        elif kind == b"VP8 ":
            if length < 10:
                raise ValueError(_INVALID)
            data = stream.read(10)
            if data[0] & 1 or data[3:6] != b"\x9d\x01\x2a":
                raise ValueError(_INVALID)
            _dimensions(
                int.from_bytes(data[6:8], "little") & 0x3FFF,
                int.from_bytes(data[8:10], "little") & 0x3FFF,
            )
        elif kind == b"VP8L":
            if length < 5:
                raise ValueError(_INVALID)
            data = stream.read(5)
            if data[0] != 0x2F:
                raise ValueError(_INVALID)
            bits = int.from_bytes(data[1:5], "little")
            _dimensions((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        offset = end
    stream.seek(0)


def _complete_container(stream, format):
    # Pillow's PNG verify accepts an incomplete IEND CRC. Check the terminal
    # container too so a mostly decodable, truncated upload cannot pass.
    position = stream.tell()
    try:
        stream.seek(0, 2)
        size = stream.tell()
        if format == "PNG":
            if size < 12:
                raise ValueError(_INVALID)
            stream.seek(-12, 2)
            if stream.read(12) != b"\x00\x00\x00\x00IEND\xaeB\x60\x82":
                raise ValueError(_INVALID)
        elif format == "JPEG":
            if size < 2:
                raise ValueError(_INVALID)
            stream.seek(-2, 2)
            if stream.read(2) != b"\xff\xd9":
                raise ValueError(_INVALID)
        elif format == "WEBP":
            stream.seek(0)
            header = stream.read(12)
            if (
                len(header) != 12
                or header[:4] != b"RIFF"
                or header[8:] != b"WEBP"
                or int.from_bytes(header[4:8], "little") + 8 != size
            ):
                raise ValueError(_INVALID)
    finally:
        stream.seek(position)


def image_content_type(stream):
    """Check decodability and resource bounds, returning a trusted raster MIME.

    Use the bounded upload spool or SQLite BLOB without making our own compressed
    copy. Pillow's WebP codec reads the compressed bytes internally. Dimensions
    are checked before load, including WebP's canvas before native allocation.
    Animated PNG/WebP are rejected. JPEG's native codec may recover a scan ending
    early with EOI: decodability does not guarantee the producer's intended pixels.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _preflight_webp(stream)
            with Image.open(stream, formats=_FORMATS) as image:
                _dimensions(*image.size)
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError(_INVALID)
                content_type = _MIME[image.format]
                _complete_container(stream, image.format)
                image.verify()
            # verify checks container structure without loading pixels. Decode
            # separately to reject data the native codec cannot safely materialize.
            stream.seek(0)
            with Image.open(stream, formats=_FORMATS) as image:
                image.load()
        return content_type
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Warning,
        OSError,
        ValueError,
        SyntaxError,
        EOFError,
        KeyError,
    ):
        raise ValueError(_INVALID) from None
    finally:
        stream.seek(0)
