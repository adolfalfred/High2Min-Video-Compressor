from __future__ import annotations

import subprocess
import sys
import time
import unittest

from adt_video_publisher.browser_validation import (
    _run_browser_command,
    find_chromium,
    run_browser_contract_tests,
)


class BrowserValidationTests(unittest.TestCase):
    def test_complete_dump_is_recovered_when_browser_does_not_exit(self) -> None:
        script = (
            "import time; "
            "print('<body data-high2min-browser-result=\"{}\"></body>', flush=True); "
            "time.sleep(60)"
        )
        started = time.monotonic()
        completed = _run_browser_command([sys.executable, "-c", script], timeout=3)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("data-high2min-browser-result", completed.stdout)
        self.assertLess(time.monotonic() - started, 6)

    def test_incomplete_dump_still_times_out(self) -> None:
        with self.assertRaises(subprocess.TimeoutExpired):
            _run_browser_command(
                [sys.executable, "-c", "import time; print('pending', flush=True); time.sleep(60)"],
                timeout=0.2,
            )

    @unittest.skipUnless(find_chromium(), "Chrome, Chromium, or Edge is not installed")
    def test_accessibility_assets_pass_mobile_browser_contract(self) -> None:
        result = run_browser_contract_tests(viewports=((320, 640),))
        self.assertTrue(result.passed)
        checks = result.viewports[0].checks
        self.assertTrue(checks["handControlVisible"])
        self.assertTrue(checks["touchDragMoved"])
        self.assertTrue(checks["audioDoesNotPauseVideo"])
        self.assertTrue(checks["videoDoesNotPauseAudio"])
        self.assertTrue(checks["toolbarSingleRow"])


if __name__ == "__main__":
    unittest.main()
