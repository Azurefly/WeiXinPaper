from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import desktop
from build_scripts import build_desktop, gen_icon


class DesktopReleaseTests(unittest.TestCase):
    def test_window_dimensions_reject_corrupt_and_out_of_range_values(self):
        self.assertEqual(
            desktop.window_dimensions({"width": "broken", "height": None}),
            (1440, 900),
        )
        self.assertEqual(
            desktop.window_dimensions({"width": 99999, "height": -1}),
            (2560, 680),
        )

    def test_window_state_is_written_as_valid_json(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp)
            desktop.save_window_state(data_dir, {"width": 1280, "height": 800})
            saved = json.loads((data_dir / "window_state.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, {"width": 1280, "height": 800})
            self.assertFalse((data_dir / "window_state.tmp").exists())

    def test_windows_single_instance_uses_exclusive_socket_binding(self):
        fake_socket = mock.Mock()
        with (
            mock.patch.object(desktop.sys, "platform", "win32"),
            mock.patch.object(desktop.socket, "SO_EXCLUSIVEADDRUSE", 99, create=True),
            mock.patch.object(desktop.socket, "socket", return_value=fake_socket),
        ):
            self.assertIs(desktop.acquire_single_instance_lock(), fake_socket)
        fake_socket.setsockopt.assert_called_once_with(socket.SOL_SOCKET, 99, 1)
        fake_socket.bind.assert_called_once_with(("127.0.0.1", desktop.SINGLE_INSTANCE_PORT))
        fake_socket.listen.assert_called_once_with(1)

    def test_desktop_environment_keeps_database_and_key_together(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp) / "app-data"
            log_dir = Path(temp) / "logs"
            with (
                mock.patch.object(desktop, "app_data_dir", return_value=data_dir),
                mock.patch.object(desktop, "app_log_dir", return_value=log_dir),
                mock.patch.object(desktop, "_migrate_existing_data"),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                configured = desktop.configure_environment()
                self.assertEqual(configured, data_dir)
                self.assertEqual(os.environ["STUDIO_DB"], str(data_dir / "studio.db"))
                self.assertEqual(
                    os.environ["STUDIO_MASTER_KEY_FILE"],
                    str(data_dir / ".master.key"),
                )
                self.assertEqual(os.environ["STUDIO_LOG_FILE"], str(log_dir / "studio.log"))

    def test_macos_build_preserves_symlinks_and_strictly_verifies_signature(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "build_scripts"
            / "build_desktop.py"
        ).read_text(encoding="utf-8")
        self.assertIn("copytree(app_bundle, root_app, symlinks=True)", source)
        self.assertIn('"--deep", "--strict"', source)
        self.assertIn("urllib.request.ProxyHandler({})", source)
        self.assertIn('"STUDIO_STARTUP_TRACE_FILE"', source)
        self.assertIn("def diagnostics()", source)
        self.assertIn('reconfigure(errors="replace")', source)
        self.assertNotIn("签名验证有警告（不影响运行", source)
        spec = (
            Path(__file__).resolve().parents[1]
            / "build_scripts"
            / "desktop.spec"
        ).read_text(encoding="utf-8")
        self.assertIn("PROJECT_ROOT = Path(SPEC).resolve().parent.parent", spec)
        self.assertIn('pathex=[str(PROJECT_ROOT), str(PROJECT_ROOT / "backend")]', spec)
        self.assertIn('PROJECT_ROOT / "build_scripts" / "hooks"', spec)
        self.assertIn("icon=icon_path", spec)
        self.assertIn('"server"', spec)
        self.assertIn('"source_fetcher"', spec)
        self.assertIn('"urllib.robotparser"', spec)
        self.assertNotIn("icon_path if Path(icon_path).exists() else None", spec)
        hook = (
            Path(__file__).resolve().parents[1]
            / "build_scripts"
            / "hooks"
            / "hook-workflow.py"
        ).read_text(encoding="utf-8")
        self.assertIn("datas = []", hook)
        self.assertIn("hiddenimports = []", hook)

    def test_custom_icon_source_has_safe_transparent_edges_and_multisize_ico(self):
        source_path = (
            Path(__file__).resolve().parents[1]
            / "build_assets"
            / "AppIconSource.png"
        )
        master = gen_icon.render_icon(1024, source_path)
        self.assertEqual(master.mode, "RGBA")
        self.assertEqual(master.size, (1024, 1024))
        alpha = master.getchannel("A")
        self.assertEqual(alpha.getpixel((0, 0)), 0)
        self.assertEqual(alpha.getpixel((1023, 1023)), 0)
        bbox = alpha.getbbox()
        self.assertIsNotNone(bbox)
        self.assertGreaterEqual(min(bbox[0], bbox[1]), 55)
        self.assertLessEqual(max(bbox[2], bbox[3]), 969)

        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            icns_path = gen_icon.build_macos_icon(temp_path, master)
            with Image.open(icns_path) as icon:
                self.assertEqual(icon.format, "ICNS")
                self.assertIn((512, 512, 2), icon.info["sizes"])

            ico_path = gen_icon.build_windows_icon(temp_path, master)
            with Image.open(ico_path) as icon:
                self.assertEqual(
                    icon.ico.sizes(),
                    {(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)},
                )

    def test_windows_workflow_checks_actual_health_contract(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "windows-desktop-build.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('PYTHONUTF8: "1"', workflow)
        self.assertIn("actions/checkout@v6", workflow)
        self.assertIn("actions/setup-python@v6", workflow)
        self.assertIn("actions/upload-artifact@v7", workflow)
        self.assertIn("$response.ok -eq $true", workflow)
        self.assertIn("if ($process.HasExited)", workflow)
        self.assertIn('ArgumentList "--server-only"', workflow)
        self.assertIn("http://127.0.0.1:$port/api/v2/health", workflow)
        self.assertIn("TcpListener", workflow)
        self.assertIn("-NoProxy", workflow)
        self.assertNotIn("foreach ($port in 5001..5020)", workflow)

    def test_server_only_mode_requires_and_reserves_exact_port(self):
        with mock.patch.object(desktop, "find_available_port", return_value=51423) as find:
            with mock.patch.dict(os.environ, {"STUDIO_PORT": "51423"}, clear=True):
                self.assertEqual(desktop.select_server_port(server_only=True), 51423)
        find.assert_called_once_with(start=51423, max_tries=1)

        with mock.patch.dict(os.environ, {"STUDIO_PORT": "invalid"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "有效的 STUDIO_PORT"):
                desktop.select_server_port(server_only=True)

    def test_backend_starts_before_gui_runtime_is_imported(self):
        source = (Path(__file__).resolve().parents[1] / "desktop.py").read_text(
            encoding="utf-8"
        )
        self.assertLess(source.index("server_thread.start()"), source.index("import webview"))
        self.assertIn(
            "lock_sock = None if server_only else acquire_single_instance_lock()",
            source,
        )
        self.assertIn('_trace_startup("server-serving")', source)

    def test_build_console_replaces_unencodable_status_characters(self):
        stdout = mock.Mock()
        stderr = mock.Mock()
        with (
            mock.patch.object(build_desktop.sys, "stdout", stdout),
            mock.patch.object(build_desktop.sys, "stderr", stderr),
        ):
            build_desktop.configure_console()
        stdout.reconfigure.assert_called_once_with(errors="replace")
        stderr.reconfigure.assert_called_once_with(errors="replace")

    def test_desktop_console_uses_utf8_before_backend_import(self):
        stdout = mock.Mock()
        stderr = mock.Mock()
        with (
            mock.patch.object(desktop.sys, "stdout", stdout),
            mock.patch.object(desktop.sys, "stderr", stderr),
        ):
            desktop.configure_console()
        stdout.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")
        stderr.reconfigure.assert_called_once_with(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    unittest.main()
