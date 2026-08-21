from __future__ import annotations

import unittest
from unittest.mock import patch

from adt_video_publisher.processes import hidden_process_options


class HiddenProcessTests(unittest.TestCase):
    def test_windows_children_use_create_no_window(self) -> None:
        with patch("adt_video_publisher.processes.sys.platform", "win32"):
            options = hidden_process_options()
        self.assertEqual(options, {"creationflags": 0x08000000})

    def test_other_platforms_do_not_receive_windows_flags(self) -> None:
        with patch("adt_video_publisher.processes.sys.platform", "linux"):
            self.assertEqual(hidden_process_options(), {})


if __name__ == "__main__":
    unittest.main()
