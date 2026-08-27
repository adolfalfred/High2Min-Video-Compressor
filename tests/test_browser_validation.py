from __future__ import annotations

import unittest

from adt_video_publisher.browser_validation import (
    find_chromium,
    run_browser_contract_tests,
)


class BrowserValidationTests(unittest.TestCase):
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
