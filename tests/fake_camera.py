"""A real, whole JPEG for tests that stand in for a camera.

The capture path now refuses anything that does not decode end to end, so a
placeholder like ``b"jpeg"`` would be thrown away exactly like the truncated
picture a parent once received. Tests that only care about which bytes came
back use this instead.
"""

import io

from PIL import Image


def jpeg(width: int = 1920, height: int = 1080) -> bytes:
    """A full-size picture, so the capture path treats it as a real camera's."""
    # Coloured, so a daytime picture is not mistaken for a night-mode one.
    img = Image.merge(
        "RGB",
        [
            Image.effect_noise((width, height), 30).point(
                lambda v, base=base: (v // 4) + base
            )
            for base in (150, 90, 40)
        ],
    )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


JPEG = jpeg()
