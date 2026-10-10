from __future__ import annotations

import threading

import pypdfium2 as pdfium
from PIL import Image


_PDFIUM_LOCK = threading.Lock()


def render_pdf_images(raw: bytes, *, scale: float) -> tuple[Image.Image, ...]:
    """Render PDF pages while honoring PDFium's single-threaded runtime contract."""
    images: list[Image.Image] = []
    try:
        with _PDFIUM_LOCK:
            document = pdfium.PdfDocument(raw)
            try:
                for index in range(len(document)):
                    page = document[index]
                    try:
                        bitmap = page.render(scale=scale)
                        try:
                            source_image = bitmap.to_pil()
                            try:
                                images.append(source_image.convert("RGB"))
                            finally:
                                source_image.close()
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
            finally:
                document.close()
    except BaseException:
        for image in images:
            try:
                image.close()
            except Exception:
                pass
        raise
    return tuple(images)
