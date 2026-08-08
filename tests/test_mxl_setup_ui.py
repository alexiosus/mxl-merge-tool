from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock

from mxl_setup import Discovery, OnecCandidate
from mxl_setup_ui import (
    MANUAL_PATH,
    DropdownOption,
    _require_existing,
    _verify_git_attributes,
    build_setup_form,
    client_note,
    default_dropdown_value,
    dropdown_options,
    git_missing_note,
    install_enabled,
    resolve_selected_path,
    run_repo_setup,
    run_setup,
)

CLIENT = OnecCandidate(
    (8, 3, 27, 2074),
    "8.3.27.2074",
    PureWindowsPath(r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8c.exe"),
    True,
)
EDITOR = OnecCandidate(
    (0,), "", PureWindowsPath(r"C:\Program Files (x86)\1cv8fv\bin\1cv8fv.exe"), False
)
NO_BATCH_CLIENT = OnecCandidate(
    (8, 3, 20, 1),
    "8.3.20.1",
    PureWindowsPath(r"C:\Program Files\1cv8\8.3.20.1\bin\1cv8c.exe"),
    False,
)


class DropdownOptionsTests(unittest.TestCase):
    def test_starts_with_the_not_used_option_and_ends_with_manual(self) -> None:
        options = dropdown_options((CLIENT,), "Не использовать 1С")
        self.assertEqual(options[0], DropdownOption("Не использовать 1С", ""))
        self.assertEqual(options[-1], DropdownOption("Указать путь вручную…", MANUAL_PATH))

    def test_a_versioned_candidate_shows_version_and_path(self) -> None:
        options = dropdown_options((CLIENT,), "Не использовать 1С")
        self.assertEqual(options[1].label, "8.3.27.2074 — " + str(CLIENT.path))
        self.assertEqual(options[1].value, str(CLIENT.path))

    def test_a_candidate_without_a_version_shows_only_the_path(self) -> None:
        options = dropdown_options((EDITOR,), "Не использовать")
        self.assertEqual(options[1].label, str(EDITOR.path))

    def test_multiple_candidates_keep_the_given_order(self) -> None:
        options = dropdown_options((CLIENT, NO_BATCH_CLIENT), "Не использовать 1С")
        self.assertEqual([item.value for item in options[1:-1]], [str(CLIENT.path), str(NO_BATCH_CLIENT.path)])

    def test_no_candidates_still_has_the_not_used_and_manual_options(self) -> None:
        options = dropdown_options((), "Не использовать 1С")
        self.assertEqual(len(options), 2)


class DefaultDropdownValueTests(unittest.TestCase):
    def test_the_newest_candidate_is_preselected(self) -> None:
        # discover()/sort_candidates() hand candidates in newest-first order;
        # the default follows whatever order it is given, not a re-sort.
        self.assertEqual(default_dropdown_value((CLIENT, NO_BATCH_CLIENT)), str(CLIENT.path))

    def test_no_candidates_defaults_to_the_not_used_option(self) -> None:
        self.assertEqual(default_dropdown_value(()), "")


class ClientNoteTests(unittest.TestCase):
    def test_a_batch_incapable_candidate_gets_the_warning(self) -> None:
        message = client_note((NO_BATCH_CLIENT,), str(NO_BATCH_CLIENT.path))
        self.assertEqual(
            message,
            "Рядом с выбранным клиентом нет 1cv8.exe — пакетный "
            "предпросмотр будет недоступен",
        )

    def test_a_batch_capable_candidate_has_no_note(self) -> None:
        self.assertEqual(client_note((CLIENT,), str(CLIENT.path)), "")

    def test_the_not_used_option_has_no_note(self) -> None:
        self.assertEqual(client_note((CLIENT, NO_BATCH_CLIENT), ""), "")

    def test_an_unmatched_value_has_no_note(self) -> None:
        self.assertEqual(client_note((NO_BATCH_CLIENT,), r"C:\other.exe"), "")


class ResolveSelectedPathTests(unittest.TestCase):
    def test_manual_selection_uses_the_trimmed_typed_path(self) -> None:
        self.assertEqual(
            resolve_selected_path(MANUAL_PATH, "  C:\\typed\\1cv8c.exe  "),
            "C:\\typed\\1cv8c.exe",
        )

    def test_manual_selection_with_blank_text_resolves_to_none(self) -> None:
        self.assertIsNone(resolve_selected_path(MANUAL_PATH, "   "))

    def test_the_not_used_option_resolves_to_none(self) -> None:
        self.assertIsNone(resolve_selected_path("", "ignored"))

    def test_a_concrete_selection_passes_through_unchanged(self) -> None:
        self.assertEqual(resolve_selected_path(r"C:\1cv8c.exe", ""), r"C:\1cv8c.exe")


class InstallEnabledTests(unittest.TestCase):
    def test_disabled_and_noted_when_git_is_missing(self) -> None:
        discovery = Discovery(None, (), ())
        self.assertFalse(install_enabled(discovery))
        self.assertEqual(
            git_missing_note(discovery),
            "Git не найден. Установите Git и запустите установку заново",
        )

    def test_enabled_and_unnoted_when_git_is_present(self) -> None:
        discovery = Discovery(r"C:\Program Files\Git\bin\git.exe", (), ())
        self.assertTrue(install_enabled(discovery))
        self.assertEqual(git_missing_note(discovery), "")


class BuildSetupFormTests(unittest.TestCase):
    def test_composes_both_dropdowns_and_the_git_state(self) -> None:
        view = build_setup_form(Discovery("git", (CLIENT,), (EDITOR,)))
        self.assertEqual(view.client_default, str(CLIENT.path))
        self.assertEqual(view.editor_default, str(EDITOR.path))
        self.assertEqual(len(view.client_options), 3)  # not-used, CLIENT, manual
        self.assertEqual(len(view.editor_options), 3)
        self.assertTrue(view.install_enabled)
        self.assertEqual(view.git_note, "")

    def test_missing_git_disables_install_in_the_composed_view(self) -> None:
        view = build_setup_form(Discovery(None, (), ()))
        self.assertFalse(view.install_enabled)
        self.assertNotEqual(view.git_note, "")


class ManualPathValidationTests(unittest.TestCase):
    def test_existing_path_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            client = Path(raw) / "1cv8c.exe"
            client.write_bytes(b"")
            _require_existing("Путь", str(client))

    def test_missing_path_is_rejected_with_the_path_in_the_message(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            _require_existing("Указанный путь к 1С", r"C:\typo\1cv8c.exe")
        self.assertIn(r"C:\typo\1cv8c.exe", str(caught.exception))

    def test_an_empty_field_stays_optional(self) -> None:
        _require_existing("Путь", None)
        _require_existing("Путь", "")


class RunSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        # run_setup and run_repo_setup still print for a console run; keep the
        # test output readable.
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_installed_flag_is_authoritative_over_path_comparison(self) -> None:
        # The install directory here is a PureWindowsPath, so comparing it
        # with the source path can only ever disagree; the flag must win.
        with mock.patch("mxl_setup.WindowsEnvironment"), mock.patch(
            "mxl_setup.install_dir", return_value=PureWindowsPath(r"C:\install")
        ), mock.patch("mxl_setup.discover"), mock.patch(
            "mxl_setup.copy_payload"
        ) as copy_payload, mock.patch(
            "mxl_setup_gui.run_setup_window"
        ) as run_window:
            code = run_setup(Path("/somewhere/else"), installed=True)

        self.assertEqual(code, 0)
        self.assertFalse(copy_payload.called)
        self.assertTrue(run_window.called)

    def test_without_the_flag_a_foreign_directory_is_copied_and_relaunched(self) -> None:
        with mock.patch("mxl_setup.WindowsEnvironment"), mock.patch(
            "mxl_setup.install_dir", return_value=PureWindowsPath(r"C:\install")
        ), mock.patch(
            "mxl_setup.copy_payload", return_value=Path("/install/launcher.exe")
        ), mock.patch("subprocess.Popen") as popen:
            code = run_setup(Path("/somewhere/else"))

        self.assertEqual(code, 0)
        # The relaunch says so explicitly instead of re-deriving it from paths.
        self.assertIn("--installed", popen.call_args.args[0])

    def test_missing_tkinter_is_reported_with_the_console_fallback(self) -> None:
        with mock.patch("mxl_setup.WindowsEnvironment"), mock.patch(
            "mxl_setup.install_dir", return_value=PureWindowsPath(r"C:\install")
        ), mock.patch("mxl_setup.discover"), mock.patch.dict(
            sys.modules, {"tkinter": None}
        ), mock.patch("mxl_setup.report") as reported:
            code = run_setup(Path("/x"), installed=True)

        self.assertEqual(code, 1)
        message = reported.call_args.args[0]
        self.assertIn("tkinter", message)
        self.assertIn("setup-gui", message)
        self.assertTrue(reported.call_args.kwargs["error"])

    def test_a_failure_before_the_window_is_reported_not_swallowed(self) -> None:
        with mock.patch(
            "mxl_setup.WindowsEnvironment", side_effect=RuntimeError("LOCALAPPDATA")
        ), mock.patch("mxl_setup.report") as reported:
            code = run_setup(Path("/x"))

        self.assertEqual(code, 1)
        self.assertIn("LOCALAPPDATA", reported.call_args.args[0])
        self.assertTrue(reported.call_args.kwargs["error"])


class RunRepoSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        for redirect in (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            redirect.__enter__()
            self.addCleanup(redirect.__exit__, None, None, None)

    def test_success_is_announced(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            subprocess.run(["git", "init", "-q", raw], check=True)
            with mock.patch("mxl_setup.report") as reported:
                self.assertEqual(run_repo_setup(raw), 0)
        self.assertIn(".gitattributes", reported.call_args.args[0])
        self.assertFalse(reported.call_args.kwargs.get("error", False))

    def test_a_folder_outside_git_is_announced_as_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            plain = Path(raw) / "plain"
            plain.mkdir()
            with mock.patch.dict(
                os.environ, {"GIT_CEILING_DIRECTORIES": str(plain.parent)}
            ):
                with mock.patch("mxl_setup.report") as reported:
                    code = run_repo_setup(str(plain))
        self.assertEqual(code, 2)
        self.assertTrue(reported.call_args.kwargs["error"])

    def test_gits_own_words_reach_the_message(self) -> None:
        # A user whose folder plainly contained .git was told it "is not a Git
        # work tree", because every non-zero exit produced that one guess and
        # stderr was captured but dropped. Git refuses repositories for
        # reasons of its own — dubious ownership on a network share being the
        # common one — so its explanation has to reach the dialog.
        failure = subprocess.CompletedProcess(
            args=["git"],
            returncode=128,
            stdout="",
            stderr="fatal: detected dubious ownership in repository at '/x'\n",
        )
        with mock.patch("mxl_subprocess.run", return_value=failure):
            with mock.patch("mxl_setup.report") as reported:
                code = run_repo_setup(r"\\Mac\Development\project")
        self.assertEqual(code, 2)
        shown = reported.call_args.args[0]
        self.assertIn("dubious ownership", shown)
        self.assertIn(r"\\Mac\Development\project", shown)

    def test_a_silent_failure_still_produces_a_message(self) -> None:
        failure = subprocess.CompletedProcess(
            args=["git"], returncode=1, stdout="", stderr=""
        )
        with mock.patch("mxl_subprocess.run", return_value=failure):
            with mock.patch("mxl_setup.report") as reported:
                code = run_repo_setup("C:\\somewhere")
        self.assertEqual(code, 2)
        self.assertTrue(reported.call_args.args[0].strip())


class VerifyGitAttributesTests(unittest.TestCase):
    def test_reaches_a_real_verdict_instead_of_a_not_a_repository_error(self) -> None:
        # Whatever the machine's own global Git config says about mxl, the
        # probe must run inside a real work tree it creates itself: it must
        # not fail with Git's "not a git repository" complaint, regardless
        # of whether the drivers happen to be installed here.
        ok, message = _verify_git_attributes()
        self.assertIsInstance(ok, bool)
        lowered = message.lower()
        self.assertNotIn("not a git repository", lowered)
        self.assertNotIn("fatal", lowered)

    def test_reports_success_when_the_global_attributes_file_configures_mxl(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            attributes = Path(home) / "attributes"
            attributes.write_text("*.mxl diff=mxl merge=mxl\n", encoding="utf-8")
            config = Path(home) / "gitconfig"
            config.write_text(
                f"[core]\n\tattributesFile = {attributes}\n", encoding="utf-8"
            )
            with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}):
                ok, message = _verify_git_attributes()
        self.assertTrue(ok)
        self.assertEqual(message, "Git применяет драйверы mxl к файлам .mxl")


if __name__ == "__main__":
    unittest.main()
