"""OCR 失败终态、续查与显式重提的回归测试，不调用供应商。"""
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.common.exceptions import BusinessError
from app.modules.audit.artifact.model import StoredArtifact
from app.modules.audit.artifact.service import artifacts


class OcrLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.task = SimpleNamespace(task_no="task-test")
        self.upload = SimpleNamespace()
        self.record = StoredArtifact(
            artifact_no="artifact-test", status="PENDING",
            external_job_id="original-job",
            deadline_at=datetime.now(UTC) + timedelta(minutes=10),
            metadata_json={},
        )

    def test_provider_failure_persists_terminal_state_before_propagation(self):
        failure = BusinessError("OCR_JOB_FAILED", "provider 500", status_code=502,
                                details={"provider_error": "internal 500", "provider_code": None})
        port = MagicMock()
        port.poll_ocr_job.side_effect = failure
        with patch.object(artifacts, "document_service", return_value=port):
            with self.assertRaises(BusinessError) as raised:
                artifacts.AuditArtifactArtifactsService(self.db)._complete_ocr_job( self.task, self.upload, self.record)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.record.status, "FAILED")
        self.assertEqual(self.record.external_job_id, "original-job")
        self.assertEqual(self.record.metadata_json["provider_error"], "internal 500")
        self.db.commit.assert_called_once()

    def test_network_timeout_retains_job_for_later_query(self):
        port = MagicMock()
        port.poll_ocr_job.side_effect = BusinessError("EXTERNAL_SERVICE_TIMEOUT", "network timeout")
        with patch.object(artifacts, "document_service", return_value=port):
            with self.assertRaises(BusinessError):
                artifacts.AuditArtifactArtifactsService(self.db)._complete_ocr_job( self.task, self.upload, self.record)
        self.assertEqual(self.record.status, "PENDING")
        self.assertEqual(self.record.external_job_id, "original-job")
        self.db.commit.assert_not_called()

    def test_failed_job_is_not_polled_or_resubmitted_by_ordinary_execution(self):
        self.record.status = "FAILED"
        with patch.object(artifacts.AuditArtifactArtifactsService, "_artifact_for_upload", return_value=self.record), \
             patch.object(artifacts, "document_service") as port:
            with self.assertRaises(BusinessError) as raised:
                artifacts.AuditArtifactArtifactsService(self.db)._submit_ocr_job( self.task, self.upload, MagicMock())
        self.assertEqual(raised.exception.code, "OCR_JOB_FAILED")
        port.assert_not_called()

    def test_ready_artifact_requires_no_provider_request(self):
        self.record.status = "READY"
        with patch.object(artifacts, "document_service") as port:
            result = artifacts.AuditArtifactArtifactsService(self.db)._complete_ocr_job( self.task, self.upload, self.record)
        self.assertIs(result, self.record)
        port.assert_not_called()


if __name__ == "__main__":
    unittest.main()
