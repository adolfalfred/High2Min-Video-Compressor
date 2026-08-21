from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from release.gui_entrypoint import _log_directory


class GuiEntrypointTests(unittest.TestCase):
    def test_windows_logs_use_local_application_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch("sys.platform", "win32"), patch.dict(
            os.environ, {"LOCALAPPDATA": temporary}
        ):
            self.assertEqual(
                _log_directory(),
                Path(temporary) / "High2Min Video Compressor" / "Logs",
            )

    def test_macos_logs_use_the_standard_user_log_directory(self) -> None:
        with patch("sys.platform", "darwin"), patch("pathlib.Path.home", return_value=Path("/Users/test")):
            self.assertEqual(
                _log_directory(),
                Path("/Users/test/Library/Logs/High2Min Video Compressor"),
            )


if __name__ == "__main__":
    unittest.main()
