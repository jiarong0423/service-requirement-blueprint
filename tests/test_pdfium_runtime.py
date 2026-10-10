from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

from app.services import pdfium_runtime


def test_pdfium_rendering_is_serialized_and_resources_are_closed(monkeypatch) -> None:
    state_lock = threading.Lock()
    start_barrier = threading.Barrier(2)
    state = {
        "active_documents": 0,
        "maximum_active_documents": 0,
        "documents_closed": 0,
        "pages_closed": 0,
        "bitmaps_closed": 0,
    }

    class FakeBitmap:
        def to_pil(self):
            return Image.new("RGB", (4, 4), "white")

        def close(self) -> None:
            with state_lock:
                state["bitmaps_closed"] += 1

    class FakePage:
        def render(self, *, scale: float):
            assert scale == 1.0
            time.sleep(0.005)
            return FakeBitmap()

        def close(self) -> None:
            with state_lock:
                state["pages_closed"] += 1

    class FakeDocument:
        def __init__(self, raw: bytes) -> None:
            assert raw == b"synthetic-pdf"
            with state_lock:
                state["active_documents"] += 1
                state["maximum_active_documents"] = max(
                    state["maximum_active_documents"], state["active_documents"]
                )

        def __len__(self) -> int:
            return 1

        def __getitem__(self, index: int):
            assert index == 0
            return FakePage()

        def close(self) -> None:
            with state_lock:
                state["active_documents"] -= 1
                state["documents_closed"] += 1

    monkeypatch.setattr(pdfium_runtime.pdfium, "PdfDocument", FakeDocument)

    def render_repeatedly() -> None:
        start_barrier.wait()
        for _ in range(10):
            images = pdfium_runtime.render_pdf_images(b"synthetic-pdf", scale=1.0)
            assert len(images) == 1
            assert images[0].size == (4, 4)
            images[0].close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(render_repeatedly) for _ in range(2)]
        for future in futures:
            future.result()

    assert state == {
        "active_documents": 0,
        "maximum_active_documents": 1,
        "documents_closed": 20,
        "pages_closed": 20,
        "bitmaps_closed": 20,
    }


def test_pdfium_rendering_closes_prior_outputs_when_a_later_page_fails(monkeypatch) -> None:
    state = {
        "documents_closed": 0,
        "pages_closed": 0,
        "bitmaps_closed": 0,
        "source_images_closed": 0,
        "output_images_closed": 0,
    }

    class FakeOutputImage:
        def close(self) -> None:
            state["output_images_closed"] += 1

    class FakeSourceImage:
        def convert(self, mode: str):
            assert mode == "RGB"
            return FakeOutputImage()

        def close(self) -> None:
            state["source_images_closed"] += 1

    class FakeBitmap:
        def to_pil(self):
            return FakeSourceImage()

        def close(self) -> None:
            state["bitmaps_closed"] += 1

    class FakePage:
        def __init__(self, index: int) -> None:
            self.index = index

        def render(self, *, scale: float):
            assert scale == 2.0
            if self.index == 1:
                raise RuntimeError("synthetic second-page failure")
            return FakeBitmap()

        def close(self) -> None:
            state["pages_closed"] += 1

    class FakeDocument:
        def __init__(self, raw: bytes) -> None:
            assert raw == b"two-page-pdf"

        def __len__(self) -> int:
            return 2

        def __getitem__(self, index: int):
            return FakePage(index)

        def close(self) -> None:
            state["documents_closed"] += 1

    monkeypatch.setattr(pdfium_runtime.pdfium, "PdfDocument", FakeDocument)

    try:
        pdfium_runtime.render_pdf_images(b"two-page-pdf", scale=2.0)
    except RuntimeError as error:
        assert str(error) == "synthetic second-page failure"
    else:
        raise AssertionError("The injected second-page failure was not raised")

    assert state == {
        "documents_closed": 1,
        "pages_closed": 2,
        "bitmaps_closed": 1,
        "source_images_closed": 1,
        "output_images_closed": 1,
    }
