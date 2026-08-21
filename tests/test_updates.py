from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from adt_video_publisher.updates import (
    LATEST_RELEASE_PAGE,
    UpdateCheckError,
    check_for_update,
)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self.payload


class UpdateTests(unittest.TestCase):
    def test_newer_release_is_reported_and_success_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "update.json"
            calls: list[object] = []

            def opener(request: object, **_kwargs: object) -> FakeResponse:
                calls.append(request)
                return FakeResponse(
                    {
                        "tag_name": "v0.9.0",
                        "html_url": (
                            "https://github.com/adolfalfred/"
                            "High2Min-Video-Compressor/releases/tag/v0.9.0"
                        ),
                    }
                )

            result = check_for_update(
                current_version="0.8.5",
                cache_file=cache,
                now=1000,
                opener=opener,
            )
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.latest_version, "0.9.0")
            self.assertEqual(result.current_version, "0.8.5")
            self.assertEqual(len(calls), 1)
            self.assertEqual(json.loads(cache.read_text(encoding="utf-8"))["checked_at"], 1000)

            throttled = check_for_update(
                current_version="0.8.5",
                cache_file=cache,
                now=1001,
                opener=lambda *_args, **_kwargs: self.fail("network should be throttled"),
            )
            self.assertIsNone(throttled)

    def test_force_bypasses_recent_successful_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "update.json"
            cache.write_text('{"checked_at": 1000}\n', encoding="utf-8")
            result = check_for_update(
                current_version="0.8.5",
                force=True,
                cache_file=cache,
                now=1001,
                opener=lambda *_args, **_kwargs: FakeResponse(
                    {"tag_name": "v0.8.5", "html_url": LATEST_RELEASE_PAGE}
                ),
            )
            self.assertIsNone(result)

    def test_untrusted_release_url_falls_back_to_known_github_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = check_for_update(
                current_version="0.8.5",
                cache_file=Path(temporary) / "update.json",
                now=1000,
                opener=lambda *_args, **_kwargs: FakeResponse(
                    {"tag_name": "v0.8.6", "html_url": "https://example.com/fake-update"}
                ),
            )
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.release_url, LATEST_RELEASE_PAGE)

    def test_invalid_release_data_does_not_create_a_success_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "update.json"
            with self.assertRaises(UpdateCheckError):
                check_for_update(
                    current_version="0.8.5",
                    cache_file=cache,
                    now=1000,
                    opener=lambda *_args, **_kwargs: FakeResponse(
                        {"tag_name": "not-a-version", "html_url": LATEST_RELEASE_PAGE}
                    ),
                )
            self.assertFalse(cache.exists())

    def test_older_release_is_not_offered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = check_for_update(
                current_version="0.8.5",
                cache_file=Path(temporary) / "update.json",
                now=1000,
                opener=lambda *_args, **_kwargs: FakeResponse(
                    {"tag_name": "v0.8.4", "html_url": LATEST_RELEASE_PAGE}
                ),
            )
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
