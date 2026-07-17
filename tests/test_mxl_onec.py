from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mxl_onec import (
    INFOBASE_MARKER,
    MANAGED_INFOBASE_USER,
    OneCRenderError,
    OneCRenderSettings,
    build_onec_command,
    ensure_renderer_infobase,
    render_mxl_batch_with_onec,
    render_mxl_with_onec,
    resolve_onec_settings,
)
from mxl_onec import _file_infobase_connection


class MxlOneCRenderTests(unittest.TestCase):
    def settings(self, root: Path) -> OneCRenderSettings:
        client = root / "1cv8c.exe"
        client.write_bytes(b"fake")
        infobase = root / "ib"
        infobase.mkdir()
        (infobase / INFOBASE_MARKER).write_bytes(b"fake")
        epf = root / "MxlToHtml.epf"
        epf.write_bytes(b"fake")
        return OneCRenderSettings(client, infobase, epf, "Service", "secret", 5)

    def test_builds_platform_command_with_file_infobase_and_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root)
            command = build_onec_command(
                settings, root / "job.json", root / "1c.log"
            )

        self.assertEqual(str(settings.client_exe), command[0])
        self.assertIn("ENTERPRISE", command)
        self.assertIn("/IBConnectionString", command)
        self.assertIn("/Execute", command)
        self.assertIn("/C", command)
        self.assertIn("/Out", command)
        self.assertIn("Service", command)
        self.assertIn("secret", command)

    def test_formats_create_infobase_connection_like_platform_cli(self):
        self.assertEqual(
            r"File=C:\Users\tester\MxlMerge\ib;",
            _file_infobase_connection(Path(r"C:\Users\tester\MxlMerge\ib")),
        )
        self.assertEqual(
            'File="C:\\Users\\Test User\\MxlMerge\\ib";',
            _file_infobase_connection(Path(r"C:\Users\Test User\MxlMerge\ib")),
        )

    def test_renders_and_validates_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root)
            source = root / "source.mxl"
            source.write_bytes(b"MOXCEL-test")
            target = root / "output" / "source.html"

            def fake_run(command, **kwargs):
                job_path = Path(command[command.index("/C") + 1])
                job = json.loads(job_path.read_text(encoding="utf-8"))
                Path(job["outputPath"]).write_text("<html>rendered</html>", encoding="utf-8")
                Path(job["statusPath"]).write_text(
                    json.dumps({"success": True, "error": ""}), encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("mxl_onec.subprocess.run", side_effect=fake_run):
                result = render_mxl_with_onec(source, target, settings)

        self.assertEqual(target.resolve(), result)

    def test_reports_platform_error_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root)
            source = root / "source.mxl"
            source.write_bytes(b"MOXCEL-test")

            def fake_run(command, **kwargs):
                job_path = Path(command[command.index("/C") + 1])
                job = json.loads(job_path.read_text(encoding="utf-8"))
                Path(job["statusPath"]).write_text(
                    json.dumps({"success": False, "error": "Cannot read MXL"}),
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("mxl_onec.subprocess.run", side_effect=fake_run):
                with self.assertRaisesRegex(OneCRenderError, "Cannot read MXL"):
                    render_mxl_with_onec(source, root / "source.html", settings)

    def test_renders_batch_in_one_platform_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.settings(root)
            items = {}
            for side in ("base", "local", "remote"):
                source = root / f"{side}.mxl"
                source.write_bytes(b"MOXCEL-test")
                items[side] = (source, root / f"{side}.html")

            def fake_run(command, **kwargs):
                job_path = Path(command[command.index("/C") + 1])
                job = json.loads(job_path.read_text(encoding="utf-8"))
                for item in job["items"]:
                    Path(item["outputPath"]).write_text(item["name"], encoding="utf-8")
                Path(job["statusPath"]).write_text(
                    json.dumps({"success": True, "error": ""}), encoding="utf-8"
                )
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch(
                "mxl_onec.subprocess.run", side_effect=fake_run
            ) as run_mock:
                outputs = render_mxl_batch_with_onec(items, settings)

        self.assertEqual(1, run_mock.call_count)
        self.assertEqual({"base", "local", "remote"}, set(outputs))

    def test_creates_empty_service_infobase_with_designer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = root / "8.3.27.1" / "bin" / "1cv8c.exe"
            client.parent.mkdir(parents=True)
            client.write_bytes(b"fake")
            designer = client.with_name("1cv8.exe")
            designer.write_bytes(b"fake")
            infobase = root / "runtime" / "ib"
            epf = root / "MxlToHtml.epf"
            epf.write_bytes(b"fake")
            settings = OneCRenderSettings(client, infobase, epf, timeout_seconds=5)

            template = root / "MxlRendererTemplate.dt"
            template.write_bytes(b"template")

            def fake_run(command, **kwargs):
                if "CREATEINFOBASE" in command:
                    (infobase / INFOBASE_MARKER).write_bytes(b"created")
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch("mxl_onec._managed_template_path", return_value=template), patch(
                "mxl_onec.subprocess.run", side_effect=fake_run
            ) as run_mock:
                result = ensure_renderer_infobase(settings)

            command = build_onec_command(
                settings, root / "job.json", root / "1c.log"
            )
            self.assertIn(MANAGED_INFOBASE_USER, command)

        self.assertEqual(infobase, result)
        self.assertEqual(2, run_mock.call_count)
        create_command = run_mock.call_args_list[0].args[0]
        restore_command = run_mock.call_args_list[1].args[0]
        self.assertEqual(str(designer), create_command[0])
        self.assertIn("CREATEINFOBASE", create_command)
        self.assertIn("/RestoreIB", restore_command)

    def test_resolves_default_infobase_from_client_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = root / "platform" / "8.3.27.1" / "bin" / "1cv8c.exe"
            client.parent.mkdir(parents=True)
            client.write_bytes(b"fake")
            epf = root / "MxlToHtml.epf"
            epf.write_bytes(b"fake")
            local_app_data = root / "LocalAppData"

            with patch("mxl_onec._git_config", return_value=None), patch.dict(
                "os.environ", {"LOCALAPPDATA": str(local_app_data)}, clear=True
            ):
                settings = resolve_onec_settings(
                    client_exe=str(client), epf=str(epf)
                )

        self.assertEqual(
            (local_app_data / "MxlMerge" / "renderer" / "8.3.27.1" / "ib").resolve(),
            settings.infobase,
        )


if __name__ == "__main__":
    unittest.main()
