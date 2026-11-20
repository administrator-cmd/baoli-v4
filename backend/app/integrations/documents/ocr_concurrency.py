"""Limit active PaddleOCR jobs shared by preprocessing workers in one process."""

from contextlib import contextmanager
from threading import BoundedSemaphore
from typing import Iterator

from app.core.config import get_settings


class OcrConcurrencyLimiter:
    """Limit local OCR submit-and-poll flows in one process.

    A local timeout releases the slot even if the provider may still be processing remotely.
    """

    def __init__(self, max_concurrent_jobs: int) -> None:
        if max_concurrent_jobs < 1:
            raise ValueError("max_concurrent_jobs must be at least 1")
        self.max_concurrent_jobs = max_concurrent_jobs
        self._semaphore = BoundedSemaphore(max_concurrent_jobs)

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self._semaphore:
            yield


OCR_MAX_CONCURRENT_JOBS = get_settings().ocr_max_concurrent_jobs
OCR_CONCURRENCY_LIMITER = OcrConcurrencyLimiter(OCR_MAX_CONCURRENT_JOBS)
