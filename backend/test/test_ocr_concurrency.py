"""PaddleOCR concurrency configuration and process-local slot limiter tests."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.documents.ocr_concurrency import OcrConcurrencyLimiter


class OcrConcurrencyTests(unittest.TestCase):
    def test_concurrency_limit_is_configurable_and_positive(self):
        self.assertEqual(Settings(_env_file=None).ocr_max_concurrent_jobs, 5)
        self.assertEqual(Settings(_env_file=None, ocr_max_concurrent_jobs=5).ocr_max_concurrent_jobs, 5)
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, ocr_max_concurrent_jobs=0)

    def test_limiter_caps_parallel_jobs(self):
        limiter = OcrConcurrencyLimiter(2)
        lock = Lock()
        two_jobs_entered = Event()
        release_jobs = Event()
        active = 0
        peak = 0

        def run_job():
            nonlocal active, peak
            with limiter.slot():
                with lock:
                    active += 1
                    peak = max(peak, active)
                    if active >= 2:
                        two_jobs_entered.set()
                try:
                    release_jobs.wait(timeout=2)
                finally:
                    with lock:
                        active -= 1

        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(run_job) for _ in range(8)]
            try:
                self.assertTrue(two_jobs_entered.wait(timeout=2))
            finally:
                release_jobs.set()
            for future in futures:
                future.result()

        self.assertEqual(peak, 2)


if __name__ == "__main__":
    unittest.main()
