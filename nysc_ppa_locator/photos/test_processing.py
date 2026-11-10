"""Real decoder inputs, metadata removal and hard resource bounds."""
from hashlib import sha256
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import struct
from threading import Event, Lock
from unittest.mock import patch
import zlib

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image, PngImagePlugin

from . import processing


def image_bytes(format='JPEG', size=(64, 96), mode='RGB', color='green', **save_options):
    output = BytesIO()
    with Image.new(mode, size, color) as image:
        image.save(output, format=format, **save_options)
    return output.getvalue()


class PhotoProcessingTests(SimpleTestCase):
    def normalize(self, data, name='photo.jpg', content_type='image/jpeg'):
        return processing.normalize_photo(SimpleUploadedFile(name, data, content_type=content_type))

    def test_real_static_supported_formats_become_new_jpegs_with_checked_dimensions(self):
        for format in ('JPEG', 'PNG', 'WEBP'):
            with self.subTest(format=format):
                normalized = self.normalize(image_bytes(format))
                self.assertEqual(set(normalized), {'data', 'width', 'height', 'sha256', 'mime'})
                self.assertEqual((normalized['width'], normalized['height']), (64, 96))
                self.assertEqual(normalized['sha256'], sha256(normalized['data']).hexdigest())
                self.assertEqual(normalized['mime'], 'image/jpeg')
                with Image.open(BytesIO(normalized['data'])) as image:
                    self.assertEqual(image.format, 'JPEG')
                    self.assertEqual(image.mode, 'RGB')
                    image.load()

    def test_actual_decode_not_filename_mime_or_claimed_size_determines_format(self):
        upload = SimpleUploadedFile('looks-like-svg.svg', image_bytes('PNG'), content_type='image/svg+xml')
        upload.size = processing.MAX_INPUT_BYTES * 100
        normalized = processing.normalize_photo(upload)
        self.assertEqual(normalized['mime'], 'image/jpeg')
        with self.assertRaises(ValidationError):
            self.normalize(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', name='looks-like-jpeg.jpg')

    def test_orientation_is_applied_and_exif_gps_icc_are_removed(self):
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = 'PRIVATE ORIGINAL COMMENT'
        exif[34853] = {1: 'N', 2: (1.0, 2.0, 3.0), 3: 'E', 4: (4.0, 5.0, 6.0)}
        data = image_bytes(exif=exif, icc_profile=b'PRIVATE ORIGINAL ICC')
        normalized = self.normalize(data, name='PRIVATE ORIGINAL FILENAME.jpg')
        self.assertEqual((normalized['width'], normalized['height']), (96, 64))
        self.assertNotIn(b'PRIVATE ORIGINAL', normalized['data'])
        with Image.open(BytesIO(normalized['data'])) as image:
            self.assertEqual(dict(image.getexif()), {})
            self.assertNotIn('exif', image.info)
            self.assertNotIn('icc_profile', image.info)

    def test_png_text_xmp_comments_and_transparency_do_not_survive(self):
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text('XML:com.adobe.xmp', 'PRIVATE EXACT LOCATION')
        metadata.add_text('Comment', 'PRIVATE CONTACT')
        data = image_bytes('PNG', mode='RGBA', color=(255, 0, 0, 0), pnginfo=metadata)
        normalized = self.normalize(data)
        self.assertNotIn(b'PRIVATE', normalized['data'])
        with Image.open(BytesIO(normalized['data'])) as image:
            self.assertTrue(all(channel >= 250 for channel in image.getpixel((0, 0))))
            self.assertNotIn('XML:com.adobe.xmp', image.info)
            self.assertNotIn('Comment', image.info)

    def test_polyglot_trailing_content_is_discarded_and_never_returned(self):
        payload = b'<script>PRIVATE_EXECUTABLE_PAYLOAD</script>'
        normalized = self.normalize(image_bytes() + payload)
        self.assertNotIn(payload, normalized['data'])
        self.assertTrue(normalized['data'].startswith(b'\xff\xd8'))
        self.assertTrue(normalized['data'].endswith(b'\xff\xd9'))

    def test_animation_and_unsupported_images_are_rejected(self):
        frames = [Image.new('RGB', (64, 64), color) for color in ('red', 'blue')]
        try:
            for format in ('GIF', 'PNG', 'WEBP'):
                with self.subTest(format=format):
                    output = BytesIO()
                    frames[0].save(output, format=format, save_all=True, append_images=frames[1:], duration=100, loop=0)
                    with self.assertRaises(ValidationError):
                        self.normalize(output.getvalue())
        finally:
            for frame in frames:
                frame.close()
        with self.assertRaises(ValidationError):
            self.normalize(image_bytes('BMP'))

    def test_blank_truncated_and_invalid_inputs_have_generic_errors(self):
        for data in (b'', b'not an image', image_bytes()[:-40]):
            with self.subTest(size=len(data)), self.assertRaisesMessage(ValidationError, processing.INVALID_PHOTO):
                self.normalize(data)

    def test_geometry_is_checked_before_pixel_decode(self):
        original = image_bytes('PNG', size=(32, 32))
        for width, height in ((10001, 32), (4001, 4000)):
            chunk = b'IHDR' + struct.pack('>II', width, height) + original[24:29]
            forged = original[:16] + chunk[4:] + struct.pack('>I', zlib.crc32(chunk)) + original[33:]
            with self.subTest(size=(width, height)), patch.object(Image.Image, 'load', side_effect=AssertionError('Geometry must be checked before loading pixels.')):
                with self.assertRaises(ValidationError):
                    self.normalize(forged)

    def test_small_photos_are_rejected_and_large_photos_are_downscaled(self):
        with self.assertRaises(ValidationError):
            self.normalize(image_bytes(size=(31, 100)))
        normalized = self.normalize(image_bytes(size=(2000, 1000)))
        self.assertEqual((normalized['width'], normalized['height']), (1600, 800))
        self.assertLessEqual(len(normalized['data']), processing.MAX_OUTPUT_BYTES)

    def test_actual_stream_size_is_bounded_even_when_upload_size_lies(self):
        class TrackingFile(BytesIO):
            size = 1
            name = 'PRIVATE PHOTO.jpg'
            def __init__(self, value):
                super().__init__(value)
                self.returned = 0
            def read(self, size):
                result = super().read(size)
                self.returned += len(result)
                return result
        upload = TrackingFile(b'x' * (processing.MAX_INPUT_BYTES + 100_000))
        with self.assertRaises(ValidationError):
            processing.normalize_photo(upload)
        self.assertEqual(upload.returned, processing.MAX_INPUT_BYTES + 1)

    def test_output_encoding_is_bounded(self):
        with patch.object(processing, 'MAX_OUTPUT_BYTES', 32):
            with self.assertRaises(ValidationError):
                self.normalize(image_bytes())

    def test_decoder_warning_fails_without_changing_global_pillow_limits(self):
        initial = Image.MAX_IMAGE_PIXELS
        with patch.object(Image, 'MAX_IMAGE_PIXELS', 4000):
            with self.assertRaises(ValidationError):
                self.normalize(image_bytes())
        self.assertEqual(Image.MAX_IMAGE_PIXELS, initial)

    def test_input_failure_does_not_echo_private_filename_or_decoder_error(self):
        class BadFile(BytesIO):
            def read(self, size):
                raise OSError('PRIVATE FILENAME and metadata')
        with self.assertRaises(ValidationError) as error:
            processing.normalize_photo(BadFile())
        self.assertNotIn('PRIVATE', str(error.exception))

    def test_parallel_uploads_do_not_decode_pixel_buffers_at_the_same_time(self):
        entered, overlap, release = Event(), Event(), Event()
        guard = Lock()
        active = 0
        original = processing._normalize_photo

        def decode(upload):
            nonlocal active
            with guard:
                active += 1
                if active > 1:
                    overlap.set()
            entered.set()
            try:
                if not release.wait(timeout=5):
                    raise AssertionError('Decoder test timed out.')
                return original(upload)
            finally:
                with guard:
                    active -= 1

        with patch.object(processing, '_normalize_photo', side_effect=decode), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.normalize, image_bytes())
            try:
                self.assertTrue(entered.wait(timeout=5))
                second = pool.submit(self.normalize, image_bytes('PNG'))
                self.assertFalse(overlap.wait(timeout=0.05))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5)['mime'], 'image/jpeg')
            self.assertEqual(second.result(timeout=5)['mime'], 'image/jpeg')
            self.assertFalse(overlap.is_set())
