from __future__ import annotations

import json
import unittest

from adt_video_publisher.contracts import CONTRACT_SCHEMA_VERSION, SCHEMA_FILES, schema_resource


class SchemaTests(unittest.TestCase):
    def test_schemas_are_valid_json_with_unique_ids(self) -> None:
        schema_ids: set[str] = set()

        for public_name in SCHEMA_FILES:
            with self.subTest(schema=public_name):
                with schema_resource(public_name).open("r", encoding="utf-8") as schema_file:
                    schema = json.load(schema_file)

                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema["additionalProperties"])
                self.assertNotIn(schema["$id"], schema_ids)
                schema_ids.add(schema["$id"])

    def test_all_documents_use_the_same_initial_contract_version(self) -> None:
        self.assertEqual(CONTRACT_SCHEMA_VERSION, "1.0")
        for public_name in SCHEMA_FILES:
            with schema_resource(public_name).open("r", encoding="utf-8") as schema_file:
                schema = json.load(schema_file)
            self.assertEqual(schema["properties"]["schema_version"]["const"], CONTRACT_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
