"""Bounded image decoding; originals and their metadata never become media assets."""
from hashlib import sha256
from io import BytesIO
from threading import BoundedSemaphore
import warnings

from django.core.exceptions import ValidationError
from PIL import Image, ImageOps, UnidentifiedImageError


MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 16_000_000
MAX_DIMENSION = 10_000
MIN_DIMENSION = 32
DISPLAY_DIMENSION = 1600
ALLOWED_FORMATS = ('JPEG', 'PNG', 'WEBP')
INVALID_PHOTO = 'Use a complete, static JPEG, PNG or WebP photo within the supported size limits.'
_decode_slot = BoundedSemaphore(1)


class _BoundedOutput(BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_OUTPUT_BYTES:
            raise ValidationError(INVALID_PHOTO)
        return super().write(data)


def _read_bounded(upload):
    upload.seek(0)
    chunks, size = [], 0
    while True:
        chunk = upload.read(min(64 * 1024, MAX_INPUT_BYTES - size + 1))
        if not isinstance(chunk, bytes):
            raise ValidationError(INVALID_PHOTO)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_INPUT_BYTES:
            raise ValidationError(INVALID_PHOTO)
        chunks.append(chunk)
    if not size:
        raise ValidationError(INVALID_PHOTO)
    return b''.join(chunks)


def _check_geometry(image):
    width, height = image.size
    if (min(width, height) < MIN_DIMENSION
            or max(width, height) > MAX_DIMENSION
            or width * height > MAX_PIXELS
            or image.format not in ALLOWED_FORMATS
            or getattr(image, 'n_frames', 1) != 1
            or getattr(image, 'is_animated', False)):
        raise ValidationError(INVALID_PHOTO)


def normalize_photo(upload):
    # Pixel buffers are much larger than compressed uploads. Bound decoding
    # independently of the web worker's thread count; wait only briefly and
    # never hold database participant locks while waiting for this slot.
    if not _decode_slot.acquire(timeout=2):
        raise ValidationError('Photo processing is busy. Please try again shortly.')
    try:
        return _normalize_photo(upload)
    finally:
        _decode_slot.release()


def _normalize_photo(upload):
    """Return one metadata-free JPEG; declared MIME, extension and size are ignored.

    UploadedFile objects are seekable, and the actual stream is bounded before
    Pillow sees it. Geometry is checked before pixel allocation. A new RGB
    canvas prevents inherited EXIF/GPS, ICC, XMP, comments or filenames from
    reaching storage. No global Pillow limits or decoder settings are changed.
    """
    try:
        raw = _read_bounded(upload)
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw), formats=ALLOWED_FORMATS) as probe:
                _check_geometry(probe)
                probe.verify()
            with Image.open(BytesIO(raw), formats=ALLOWED_FORMATS) as image:
                _check_geometry(image)
                image.load()
                ImageOps.exif_transpose(image, in_place=True)
                image.thumbnail((DISPLAY_DIMENSION, DISPLAY_DIMENSION), Image.Resampling.LANCZOS)
                with image.convert('RGBA') as rgba:
                    with Image.new('RGB', rgba.size, 'white') as canvas:
                        canvas.paste(rgba, mask=rgba.getchannel('A'))
                        output = _BoundedOutput()
                        canvas.save(output, format='JPEG', quality=82, optimize=True)
                        data, width, height = output.getvalue(), canvas.width, canvas.height
        return {
            'data': data, 'width': width, 'height': height,
            'sha256': sha256(data).hexdigest(), 'mime': 'image/jpeg',
        }
    except (AttributeError, OSError, TypeError, ValueError, SyntaxError,
            UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        # Decoder errors can contain filenames and user-supplied metadata.
        raise ValidationError(INVALID_PHOTO) from None
