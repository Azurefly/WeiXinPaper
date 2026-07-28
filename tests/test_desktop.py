from __future__ import annotations

import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import desktop
from build_scripts import build_desktop


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
        self.assertIn('reconfigure(errors="replace")', source)
        self.assertNotIn("签名验证有警告（不影响运行", source)

    def test_windows_workflow_checks_actual_health_contract(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "windows-desktop-build.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('PYTHONUTF8: "1"', workflow)
        self.assertIn("$response.ok -eq $true", workflow)
        self.assertIn("if ($process.HasExited)", workflow)
        self.assertIn("http://127.0.0.1:5001/api/v2/health", workflow)
        self.assertNotIn("foreach ($port in 5001..5020)", workflow)

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


if __name__ == "__main__":
    unittest.main()
