import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import openapi_capture


class OpenApiCaptureTests(unittest.TestCase):
    def test_valid_openapi_document_is_recognized(self):
        payload = json.dumps({
            "openapi": "3.1.0",
            "paths": {"/orders": {"get": {}}},
        }).encode()

        self.assertTrue(openapi_capture.valid_openapi_json(payload))
        self.assertFalse(openapi_capture.valid_openapi_json(b'{"paths": {}}'))
        self.assertFalse(openapi_capture.valid_openapi_json(b"not-json"))

    def test_static_discovery_ignores_capture_output_directory(self):
        document = json.dumps({
            "openapi": "3.1.0",
            "paths": {"/orders": {"get": {}}},
        }).encode()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "specs" / "revision.json"
            output.parent.mkdir()
            (output.parent / "swagger.json").write_bytes(b"stale")
            checked_in = root / "docs" / "openapi.json"
            checked_in.parent.mkdir()
            checked_in.write_bytes(document)

            payload, source = openapi_capture.find_static_document(
                [], [root], output)

        self.assertEqual(document, payload)
        self.assertTrue(source.endswith("docs/openapi.json"))

    def test_explicit_static_file_can_live_in_output_directory(self):
        document = json.dumps({
            "swagger": "2.0",
            "paths": {"/health": {"get": {}}},
        }).encode()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "specs" / "swagger.json"
            candidate.parent.mkdir()
            candidate.write_bytes(document)

            payload, source = openapi_capture.find_static_document(
                [candidate], [], root / "specs" / "revision.json")

        self.assertEqual(document, payload)
        self.assertEqual(candidate.resolve(), Path(source))

    def test_build_generation_uses_fresh_msbuild_cache(self):
        document = json.dumps({
            "openapi": "3.1.0",
            "paths": {"/health": {"get": {}}},
        }).encode()

        def fake_run(command, **_kwargs):
            output_arg = next(
                arg for arg in command
                if arg.startswith("-p:OpenApiDocumentsDirectory="))
            output = Path(output_arg.split("=", 1)[1])
            (output / "fixture.json").write_bytes(document)
            self.assertTrue(any(
                arg.startswith("-p:_OpenApiDocumentsCache=")
                for arg in command
            ))
            return mock.Mock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "Api.csproj"
            project.write_text("<Project />", encoding="utf-8")
            with mock.patch.object(
                    openapi_capture.subprocess, "run", side_effect=fake_run):
                payload, source, failures = (
                    openapi_capture.generate_from_projects(
                        [project], "OpenApi"))

        self.assertEqual(document, payload)
        self.assertEqual(f"build:{project.resolve()}", source)
        self.assertEqual([], failures)

    def test_runtime_404_falls_back_without_waiting(self):
        error = urllib.error.HTTPError(
            "http://localhost/openapi.json",
            404,
            "Not Found",
            {},
            None,
        )
        with mock.patch.object(
                openapi_capture.urllib.request,
                "urlopen",
                side_effect=error) as urlopen:
            payload, source = openapi_capture.fetch_runtime(
                ["http://localhost/openapi.json"],
                wait_seconds=180,
                request_timeout=1,
            )

        self.assertIsNone(payload)
        self.assertIsNone(source)
        urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
