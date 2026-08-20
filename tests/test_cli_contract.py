from __future__ import annotations

import io
import json
import unittest

from adt_video_publisher.cli import main
from adt_video_publisher.contracts import EXIT_CODE_DESCRIPTIONS, SCHEMA_FILES, ExitCode


class CliContractTests(unittest.TestCase):
    def test_contract_json_is_machine_readable(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = main(["contract", "--json"], stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, ExitCode.SUCCESS)
        self.assertEqual(stderr.getvalue(), "")
        document = json.loads(stdout.getvalue())
        self.assertEqual(document["schema_version"], "1.0")
        self.assertEqual(document["tool"]["name"], "high2min")
        self.assertTrue(document["guarantees"]["ui_optional"])
        self.assertTrue(document["guarantees"]["originals_immutable"])
        statuses = {command["name"]: command["status"] for command in document["commands"]}
        for command in ("inspect", "plan", "compress", "verify", "resume"):
            self.assertEqual(statuses[command], "available")
        self.assertEqual(statuses["ui"], "available")
        self.assertEqual(statuses["publish"], "available")

    def test_json_flag_is_accepted_before_the_command(self) -> None:
        stdout = io.StringIO()
        exit_code = main(["--json", "contract"], stdout=stdout, stderr=io.StringIO())
        self.assertEqual(exit_code, ExitCode.SUCCESS)
        self.assertEqual(json.loads(stdout.getvalue())["schema_version"], "1.0")

    def test_usage_errors_are_structured_when_json_is_requested(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()

        exit_code = main(["unknown", "--json"], stdout=stdout, stderr=stderr)

        self.assertEqual(exit_code, ExitCode.USAGE_ERROR)
        self.assertEqual(stderr.getvalue(), "")
        error = json.loads(stdout.getvalue())["error"]
        self.assertEqual(error["code"], ExitCode.USAGE_ERROR)
        self.assertEqual(error["name"], "USAGE_ERROR")

    def test_public_exit_codes_are_unique(self) -> None:
        codes = [item.code for item in EXIT_CODE_DESCRIPTIONS]
        names = [item.name for item in EXIT_CODE_DESCRIPTIONS]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(codes), {int(code) for code in ExitCode})

    def test_every_public_schema_can_be_printed(self) -> None:
        for schema_name in SCHEMA_FILES:
            with self.subTest(schema=schema_name):
                stdout = io.StringIO()
                exit_code = main(["schema", schema_name], stdout=stdout, stderr=io.StringIO())
                self.assertEqual(exit_code, ExitCode.SUCCESS)
                self.assertEqual(json.loads(stdout.getvalue())["type"], "object")


if __name__ == "__main__":
    unittest.main()
