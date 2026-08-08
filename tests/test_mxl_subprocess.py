"""Tests for the console-hiding subprocess wrappers."""

from __future__ import annotations

import unittest
from unittest import mock

import mxl_subprocess


class HideConsoleTests(unittest.TestCase):
    def test_run_adds_create_no_window_on_windows(self) -> None:
        with mock.patch.object(mxl_subprocess.sys, "platform", "win32"), mock.patch(
            "subprocess.run"
        ) as run:
            mxl_subprocess.run(
                ["git", "status"],
                check=True,
                capture_output=True,
                text=True,
                cwd="/tmp",
            )

        run.assert_called_once_with(
            ["git", "status"],
            check=True,
            capture_output=True,
            text=True,
            cwd="/tmp",
            creationflags=mxl_subprocess.CREATE_NO_WINDOW,
        )

    def test_run_omits_the_flag_off_windows(self) -> None:
        with mock.patch.object(mxl_subprocess.sys, "platform", "darwin"), mock.patch(
            "subprocess.run"
        ) as run:
            mxl_subprocess.run(["git", "status"], check=True)

        run.assert_called_once_with(["git", "status"], check=True)
        self.assertNotIn("creationflags", run.call_args.kwargs)

    def test_run_respects_a_caller_supplied_creationflags(self) -> None:
        with mock.patch.object(mxl_subprocess.sys, "platform", "win32"), mock.patch(
            "subprocess.run"
        ) as run:
            mxl_subprocess.run(["git", "status"], creationflags=0x1234)

        run.assert_called_once_with(["git", "status"], creationflags=0x1234)

    def test_popen_adds_create_no_window_on_windows(self) -> None:
        with mock.patch.object(mxl_subprocess.sys, "platform", "win32"), mock.patch(
            "subprocess.Popen"
        ) as popen:
            mxl_subprocess.popen(["git", "status"], cwd="/tmp", stdout=-1, stderr=-1)

        popen.assert_called_once_with(
            ["git", "status"],
            cwd="/tmp",
            stdout=-1,
            stderr=-1,
            creationflags=mxl_subprocess.CREATE_NO_WINDOW,
        )

    def test_popen_omits_the_flag_off_windows(self) -> None:
        with mock.patch.object(mxl_subprocess.sys, "platform", "darwin"), mock.patch(
            "subprocess.Popen"
        ) as popen:
            mxl_subprocess.popen(["git", "status"])

        popen.assert_called_once_with(["git", "status"])
        self.assertNotIn("creationflags", popen.call_args.kwargs)

    def test_popen_respects_a_caller_supplied_creationflags(self) -> None:
        with mock.patch.object(mxl_subprocess.sys, "platform", "win32"), mock.patch(
            "subprocess.Popen"
        ) as popen:
            mxl_subprocess.popen(["git", "status"], creationflags=0x1234)

        popen.assert_called_once_with(["git", "status"], creationflags=0x1234)

    def test_check_output_adds_create_no_window_on_windows(self) -> None:
        with mock.patch.object(mxl_subprocess.sys, "platform", "win32"), mock.patch(
            "subprocess.check_output"
        ) as check_output:
            mxl_subprocess.check_output(["git", "rev-parse"], text=True, input="x")

        check_output.assert_called_once_with(
            ["git", "rev-parse"],
            text=True,
            input="x",
            creationflags=mxl_subprocess.CREATE_NO_WINDOW,
        )

    def test_check_output_omits_the_flag_off_windows(self) -> None:
        with mock.patch.object(mxl_subprocess.sys, "platform", "darwin"), mock.patch(
            "subprocess.check_output"
        ) as check_output:
            mxl_subprocess.check_output(["git", "rev-parse"])

        check_output.assert_called_once_with(["git", "rev-parse"])
        self.assertNotIn("creationflags", check_output.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
