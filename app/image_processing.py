from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Literal

from PIL import Image, ImageEnhance, ImageOps


Direction = Literal["auto", "landscape", "portrait"]
FitMode = Literal["scale", "cut"]

PALETTE_RGB: list[tuple[int, int, int]] = [
    (0, 0, 0),  # black
    (255, 255, 255),  # white
    (255, 255, 0),  # yellow
    (255, 0, 0),  # red
    (0, 0, 255),  # blue
    (0, 255, 0),  # green
]

# Pillow uses the palette RGB values as color-distance anchors during
# quantization. Real 6-color e-paper colors are much lighter than pure RGB, so
# matching against pure primaries tends to send pale photo colors to white. This
# palette keeps the same index order but uses softer anchors to pull more image
# detail into the available color inks.
QUANTIZE_PALETTE_RGB: list[tuple[int, int, int]] = [
    (18, 18, 18),  # black
    (255, 255, 255),  # white
    (238, 220, 72),  # yellow
    (224, 82, 72),  # red
    (76, 120, 220),  # blue
    (72, 178, 82),  # green
]

# Wire format consumed by ESP32:
# each byte stores two palette indexes, high nibble first, low nibble second.
FORMAT_NAME = "epd4bit-indexed-v1"
RAW_IMAGE_SUFFIXES = {".dng", ".DNG"}


@dataclass(frozen=True)
class ConvertedImage:
    width: int
    height: int
    preview_bmp: bytes
    epd_data: bytes


def convert_image_file(
    image_path: Path,
    *,
    direction: Direction = "auto",
    mode: FitMode = "scale",
    dither: bool = True,
) -> ConvertedImage:
    if image_path.suffix in RAW_IMAGE_SUFFIXES:
        image = _open_raw_image(image_path)
        return convert_image(image, direction=direction, mode=mode, dither=dither)

    with Image.open(image_path) as image:
        return convert_image(image, direction=direction, mode=mode, dither=dither)


def convert_image(
    image: Image.Image,
    *,
    direction: Direction = "auto",
    mode: FitMode = "scale",
    dither: bool = True,
) -> ConvertedImage:
    source = image.convert("RGB")
    target_width, target_height = _target_size(source, direction)
    resized = _resize_to_screen(source, target_width, target_height, mode)
    enhanced = _prepare_for_epaper(resized)
    indexed = _quantize_to_palette(enhanced, dither=dither)

    # Keep a BMP preview on the server so remote testing can verify the image
    # without needing physical access to the ESP32 or e-paper display.
    preview_rgb = indexed.convert("RGB")
    preview_buffer = BytesIO()
    preview_rgb.save(preview_buffer, format="BMP")

    return ConvertedImage(
        width=target_width,
        height=target_height,
        preview_bmp=preview_buffer.getvalue(),
        epd_data=_pack_4bit_pixels(indexed),
    )


def _open_raw_image(image_path: Path) -> Image.Image:
    try:
        import rawpy
    except ImportError as exc:
        raise RuntimeError("DNG/RAW support is not installed") from exc

    with rawpy.imread(str(image_path)) as raw:
        rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
    return Image.fromarray(rgb, mode="RGB")


def unpack_4bit_pixels(data: bytes, pixel_count: int) -> list[int]:
    pixels: list[int] = []
    for byte in data:
        pixels.append((byte >> 4) & 0x0F)
        if len(pixels) == pixel_count:
            break
        pixels.append(byte & 0x0F)
        if len(pixels) == pixel_count:
            break
    return pixels


def _target_size(image: Image.Image, direction: Direction) -> tuple[int, int]:
    if direction == "landscape":
        return 800, 480
    if direction == "portrait":
        return 480, 800
    width, height = image.size
    return (800, 480) if width >= height else (480, 800)


def _resize_to_screen(
    image: Image.Image,
    target_width: int,
    target_height: int,
    mode: FitMode,
) -> Image.Image:
    if mode == "cut":
        return ImageOps.pad(
            image,
            size=(target_width, target_height),
            method=Image.Resampling.LANCZOS,
            color=(255, 255, 255),
            centering=(0.5, 0.5),
        )

    width, height = image.size
    ratio = max(target_width / width, target_height / height)
    resized_size = (max(1, int(width * ratio)), max(1, int(height * ratio)))
    resized = image.resize(resized_size, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (target_width, target_height), (255, 255, 255))
    left = (target_width - resized.width) // 2
    top = (target_height - resized.height) // 2
    canvas.paste(resized, (left, top))
    return canvas


def _prepare_for_epaper(image: Image.Image) -> Image.Image:
    contrasted = ImageOps.autocontrast(image, cutoff=1)
    contrasted = ImageEnhance.Contrast(contrasted).enhance(1.28)
    contrasted = ImageEnhance.Color(contrasted).enhance(1.65)
    return ImageEnhance.Sharpness(contrasted).enhance(1.08)


def _quantize_to_palette(image: Image.Image, *, dither: bool) -> Image.Image:
    # Pillow palette indexes become the protocol indexes sent to the ESP32.
    # Do not reorder PALETTE_RGB without updating the firmware mapping.
    palette_image = Image.new("P", (1, 1))
    palette: list[int] = []
    for red, green, blue in QUANTIZE_PALETTE_RGB:
        palette.extend([red, green, blue])
    palette.extend([0, 0, 0] * (256 - len(QUANTIZE_PALETTE_RGB)))
    palette_image.putpalette(palette)

    dither_mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    indexed = image.quantize(palette=palette_image, dither=dither_mode)
    indexed = indexed.point([index if index < len(PALETTE_RGB) else 0 for index in range(256)])
    output_palette: list[int] = []
    for red, green, blue in PALETTE_RGB:
        output_palette.extend([red, green, blue])
    output_palette.extend([0, 0, 0] * (256 - len(PALETTE_RGB)))
    indexed.putpalette(output_palette)
    return indexed


def _pack_4bit_pixels(indexed: Image.Image) -> bytes:
    # 800x480 and 480x800 both become 384000 pixels / 2 = 192000 bytes.
    # The firmware should unpack byte >> 4 first, then byte & 0x0F.
    raw = indexed.tobytes()
    packed = bytearray((len(raw) + 1) // 2)
    for index in range(0, len(raw), 2):
        first = raw[index] & 0x0F
        second = raw[index + 1] & 0x0F if index + 1 < len(raw) else 0
        packed[index // 2] = (first << 4) | second
    return bytes(packed)
