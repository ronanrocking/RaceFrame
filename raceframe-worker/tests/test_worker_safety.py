from __future__ import annotations

import io
import unittest
from types import SimpleNamespace

from PIL import Image

import worker


def face_result(*, width: int = 200, height: int = 200, score: float = 0.95) -> worker.FaceResult:
    return worker.FaceResult(
        face_index=0,
        embedding=[1.0] + [0.0] * 511,
        bounding_box_json={"left": 0, "top": 0, "right": width, "bottom": height, "width": width, "height": height},
        detection_score=score,
        quality_score=score,
    )


class FaceProbeSafetyTests(unittest.TestCase):
    def test_search_probe_requires_exactly_one_face(self) -> None:
        processor = worker.FaceProcessor()
        processor._scan_with_detection_count = lambda _data: ([], 0)  # type: ignore[method-assign]
        with self.assertRaises(worker.PermanentJobError):
            processor.enroll_single_search_face(b"ignored")

        processor._scan_with_detection_count = lambda _data: ([face_result(), face_result()], 2)  # type: ignore[method-assign]
        with self.assertRaises(worker.PermanentJobError):
            processor.enroll_single_search_face(b"ignored")

        # A second detection must not disappear merely because its embedding was invalid.
        processor._scan_with_detection_count = lambda _data: ([face_result()], 2)  # type: ignore[method-assign]
        with self.assertRaises(worker.PermanentJobError):
            processor.enroll_single_search_face(b"ignored")

    def test_search_probe_rejects_small_or_low_quality_face(self) -> None:
        processor = worker.FaceProcessor()
        processor._scan_with_detection_count = lambda _data: ([face_result(width=40)], 1)  # type: ignore[method-assign]
        with self.assertRaises(worker.PermanentJobError):
            processor.enroll_single_search_face(b"ignored")

        processor._scan_with_detection_count = lambda _data: ([face_result(score=0.1)], 1)  # type: ignore[method-assign]
        with self.assertRaises(worker.PermanentJobError):
            processor.enroll_single_search_face(b"ignored")

    def test_header_guard_accepts_single_frame_and_rejects_animation(self) -> None:
        still = io.BytesIO()
        Image.new("RGB", (100, 100)).save(still, format="PNG")
        self.assertEqual(worker.validate_image_header(still.getvalue()), (100, 100))

        animated = io.BytesIO()
        frames = [Image.new("RGB", (100, 100), color=color) for color in ("red", "blue")]
        frames[0].save(animated, format="GIF", save_all=True, append_images=frames[1:])
        with self.assertRaises(worker.PermanentJobError):
            worker.validate_image_header(animated.getvalue())

    def test_download_session_never_contains_worker_authorization(self) -> None:
        client = worker.BackendClient()
        try:
            self.assertIn("Authorization", client._api.headers)
            self.assertNotIn("Authorization", client._downloads.headers)
        finally:
            client.close()

    def test_ocr_diagnostics_never_duplicate_provider_response_text(self) -> None:
        image_bytes = io.BytesIO()
        Image.new("RGB", (100, 80)).save(image_bytes, format="PNG")
        response = SimpleNamespace(
            error=SimpleNamespace(message="", code=0),
            full_text_annotation=SimpleNamespace(pages=[], text="sensitive full OCR response"),
            text_annotations=[],
        )

        class FakeVisionClient:
            def document_text_detection(self, **_kwargs):
                return response

        processor = worker.OCRProcessor()
        processor._client = FakeVisionClient()
        detections, diagnostics = processor.scan(image_bytes.getvalue())
        self.assertEqual(detections, [])
        self.assertEqual(diagnostics["image_width"], 100)
        self.assertEqual(diagnostics["image_height"], 80)
        self.assertEqual(diagnostics["detection_count"], 0)
        self.assertNotIn("sensitive", str(diagnostics))
        self.assertLess(len(str(diagnostics)), 1_000)

    def test_presence_payload_contains_only_opaque_operational_identity(self) -> None:
        presence = worker.WorkerPresenceHeartbeat()
        self.assertEqual(
            set(presence.payload()),
            {"worker_id", "worker_version", "started_at", "status", "current_job_id", "current_job_type"},
        )
        self.assertEqual(presence.payload()["status"], "idle")
        self.assertNotIn("hostname", presence.payload())
        self.assertNotIn("ip", presence.payload())

        presence.set_active({"id": "b180db26-1879-478d-abd2-1a248c8547fc", "job_type": "ocr"})
        self.assertEqual(presence.payload()["status"], "active")
        self.assertEqual(presence.payload()["current_job_type"], "ocr")
        presence.set_idle()
        self.assertIsNone(presence.payload()["current_job_id"])


if __name__ == "__main__":
    unittest.main()
