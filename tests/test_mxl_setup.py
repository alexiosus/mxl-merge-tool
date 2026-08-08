from __future__ import annotations

import base64
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path, PurePath, PureWindowsPath
from unittest import mock

import mxl_setup
from mxl_setup import OnecCandidate, parse_version, sort_candidates


class ParseVersionTests(unittest.TestCase):
    def test_parses_four_part_version(self) -> None:
        self.assertEqual(parse_version("8.3.27.2074"), (8, 3, 27, 2074))

    def test_parses_short_version(self) -> None:
        self.assertEqual(parse_version("8.3"), (8, 3))

    def test_rejects_non_version_directory(self) -> None:
        self.assertIsNone(parse_version("common"))

    def test_rejects_empty(self) -> None:
        self.assertIsNone(parse_version(""))


class SortCandidatesTests(unittest.TestCase):
    def test_orders_newest_first_numerically(self) -> None:
        candidates = [
            OnecCandidate((8, 3, 9, 100), "8.3.9.100", PureWindowsPath("a"), False),
            OnecCandidate((8, 3, 27, 2074), "8.3.27.2074", PureWindowsPath("b"), True),
            OnecCandidate((8, 3, 10, 5), "8.3.10.5", PureWindowsPath("c"), False),
        ]
        ordered = [item.version_text for item in sort_candidates(candidates)]
        self.assertEqual(ordered, ["8.3.27.2074", "8.3.10.5", "8.3.9.100"])

    def test_deduplicates_by_path(self) -> None:
        first = OnecCandidate((8, 3, 27, 1), "8.3.27.1", PureWindowsPath("x"), True)
        second = OnecCandidate((8, 3, 27, 1), "8.3.27.1", PureWindowsPath("x"), True)
        self.assertEqual(len(sort_candidates([first, second])), 1)


from mxl_setup import Discovery, discover, discover_file_editors, discover_onec_clients


class FakeEnvironment:
    """In-memory stand-in for the Windows environment."""

    def __init__(
        self,
        files: set[str],
        directories: dict[str, list[str]] | None = None,
        registry: dict[tuple[str, str], list[str]] | None = None,
        git: str | None = r"C:\Program Files\Git\cmd\git.exe",
    ) -> None:
        self._files = {item.casefold() for item in files}
        self._directories = directories or {}
        self._registry = registry or {}
        self._git = git

    def program_files(self) -> tuple[PureWindowsPath, ...]:
        return (
            PureWindowsPath(r"C:\Program Files"),
            PureWindowsPath(r"C:\Program Files (x86)"),
        )

    def local_app_data(self) -> PureWindowsPath:
        return PureWindowsPath(r"C:\Users\dev\AppData\Local")

    def roaming_app_data(self) -> PureWindowsPath:
        return PureWindowsPath(r"C:\Users\dev\AppData\Roaming")

    def exists(self, path: PurePath) -> bool:
        return str(path).casefold() in self._files

    def list_directory(self, path: PurePath) -> tuple[str, ...]:
        return tuple(self._directories.get(str(path), ()))

    def registry_subkeys(self, root: str, key: str) -> tuple[str, ...]:
        return tuple(self._registry.get((root, key), ()))

    def find_git(self) -> str | None:
        return self._git


CLIENT_27 = r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8c.exe"
THICK_27 = r"C:\Program Files\1cv8\8.3.27.2074\bin\1cv8.exe"
CLIENT_10 = r"C:\Program Files\1cv8\8.3.10.5\bin\1cv8c.exe"
EDITOR = r"C:\Program Files (x86)\1cv8fv\bin\1cv8fv.exe"


class DiscoverOnecClientsTests(unittest.TestCase):
    def test_finds_clients_under_program_files_newest_first(self) -> None:
        env = FakeEnvironment(
            files={CLIENT_27, THICK_27, CLIENT_10},
            directories={r"C:\Program Files\1cv8": ["8.3.27.2074", "8.3.10.5", "common"]},
        )
        found = discover_onec_clients(env)
        self.assertEqual([item.version_text for item in found], ["8.3.27.2074", "8.3.10.5"])

    def test_marks_batch_capable_only_when_thick_client_is_present(self) -> None:
        env = FakeEnvironment(
            files={CLIENT_27, THICK_27, CLIENT_10},
            directories={r"C:\Program Files\1cv8": ["8.3.27.2074", "8.3.10.5"]},
        )
        found = {item.version_text: item.batch_capable for item in discover_onec_clients(env)}
        self.assertTrue(found["8.3.27.2074"])
        self.assertFalse(found["8.3.10.5"])

    def test_uses_registry_versions_when_directory_listing_is_empty(self) -> None:
        env = FakeEnvironment(
            files={CLIENT_27, THICK_27},
            registry={("HKLM", r"SOFTWARE\1C\1Cv8"): ["8.3.27.2074"]},
        )
        found = discover_onec_clients(env)
        self.assertEqual([item.version_text for item in found], ["8.3.27.2074"])

    def test_returns_empty_when_1c_is_absent(self) -> None:
        self.assertEqual(discover_onec_clients(FakeEnvironment(files=set())), ())


class DiscoverFileEditorsTests(unittest.TestCase):
    def test_finds_file_work_application(self) -> None:
        env = FakeEnvironment(
            files={EDITOR},
            directories={r"C:\Program Files (x86)\1cv8fv": ["bin"]},
        )
        found = discover_file_editors(env)
        self.assertEqual([str(item.path) for item in found], [EDITOR])


class DiscoverTests(unittest.TestCase):
    def test_collects_everything_into_one_result(self) -> None:
        env = FakeEnvironment(
            files={CLIENT_27, THICK_27, EDITOR},
            directories={
                r"C:\Program Files\1cv8": ["8.3.27.2074"],
                r"C:\Program Files (x86)\1cv8fv": ["bin"],
            },
        )
        result = discover(env)
        self.assertIsInstance(result, Discovery)
        self.assertEqual(result.git, r"C:\Program Files\Git\cmd\git.exe")
        self.assertEqual(len(result.clients), 1)
        self.assertEqual(len(result.file_editors), 1)

    def test_missing_git_is_reported_as_none(self) -> None:
        result = discover(FakeEnvironment(files=set(), git=None))
        self.assertIsNone(result.git)


from mxl_setup import (
    install_dir,
    install_root,
    is_installed_copy,
    launcher_path,
    start_menu_dir,
)


class InstallPathTests(unittest.TestCase):
    def test_install_root_is_under_local_app_data(self) -> None:
        env = FakeEnvironment(files=set())
        self.assertEqual(
            str(install_root(env)),
            r"C:\Users\dev\AppData\Local\mxl-merge-tool",
        )

    def test_install_dir_is_version_scoped(self) -> None:
        env = FakeEnvironment(files=set())
        self.assertEqual(
            str(install_dir(env)),
            r"C:\Users\dev\AppData\Local\mxl-merge-tool\0.3.0",
        )

    def test_launcher_path_sits_next_to_runtime(self) -> None:
        env = FakeEnvironment(files=set())
        self.assertEqual(
            str(launcher_path(env)),
            r"C:\Users\dev\AppData\Local\mxl-merge-tool\0.3.0\MXL merge tool.exe",
        )

    def test_start_menu_folder_is_per_user(self) -> None:
        env = FakeEnvironment(files=set())
        self.assertEqual(
            str(start_menu_dir(env)),
            r"C:\Users\dev\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\MXL Merge Tool",
        )

    def test_recognises_a_copy_already_in_place(self) -> None:
        env = FakeEnvironment(files=set())
        target = PureWindowsPath(r"C:\Users\dev\AppData\Local\mxl-merge-tool\0.3.0")
        self.assertTrue(is_installed_copy(env, target))

    def test_rejects_a_copy_running_from_downloads(self) -> None:
        env = FakeEnvironment(files=set())
        source = PureWindowsPath(r"C:\Users\dev\Downloads\mxl-merge-tool")
        self.assertFalse(is_installed_copy(env, source))

    def test_comparison_ignores_case(self) -> None:
        env = FakeEnvironment(files=set())
        target = PureWindowsPath(r"C:\USERS\DEV\APPDATA\LOCAL\MXL-MERGE-TOOL\0.3.0")
        self.assertTrue(is_installed_copy(env, target))


from mxl_setup import (
    MAINTENANCE_LAUNCHER_NAME,
    SETTINGS_LAUNCHER_NAME,
    copy_payload,
)


class CopyPayloadTests(unittest.TestCase):
    def _make_source(self, root: Path) -> Path:
        source = root / "mxl-merge-tool"
        (source / "runtime").mkdir(parents=True)
        (source / "runtime" / "python.exe").write_bytes(b"binary")
        (source / "app").mkdir()
        (source / "app" / "mxl_tool.py").write_text("print()", encoding="utf-8")
        (source / "MXL merge tool.exe").write_bytes(b"launcher")
        (source / "README.txt").write_text("readme", encoding="utf-8")
        (source / "stray.log").write_text("ignore me", encoding="utf-8")
        return source

    def test_copies_declared_entries_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self._make_source(root)
            target = root / "installed"
            copy_payload(source, target)
            self.assertTrue((target / "runtime" / "python.exe").exists())
            self.assertTrue((target / "app" / "mxl_tool.py").exists())
            self.assertTrue((target / "MXL merge tool.exe").exists())
            self.assertEqual(
                (target / SETTINGS_LAUNCHER_NAME).read_bytes(), b"launcher"
            )
            self.assertEqual(
                (target / MAINTENANCE_LAUNCHER_NAME).read_bytes(), b"launcher"
            )
            self.assertFalse((target / "stray.log").exists())

    def test_returns_the_installed_launcher_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self._make_source(root)
            target = root / "installed"
            self.assertEqual(copy_payload(source, target), target / "MXL merge tool.exe")

    def test_replaces_an_existing_installation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self._make_source(root)
            target = root / "installed"
            (target / "app").mkdir(parents=True)
            (target / "app" / "obsolete.py").write_text("old", encoding="utf-8")
            copy_payload(source, target)
            self.assertFalse((target / "app" / "obsolete.py").exists())
            self.assertTrue((target / "app" / "mxl_tool.py").exists())

    def test_missing_optional_entry_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = self._make_source(root)
            (source / "README.txt").unlink()
            target = root / "installed"
            copy_payload(source, target)
            self.assertFalse((target / "README.txt").exists())


from mxl_setup import (
    MENU_BACKGROUND_KEY,
    MENU_KEY,
    MAINTENANCE_APP_USER_MODEL_ID,
    SETUP_SHORTCUT_NAME,
    SETTINGS_APP_USER_MODEL_ID,
    UNINSTALL_KEY,
    UNINSTALL_SHORTCUT_NAME,
    Shortcut,
    WindowsShortcutWriter,
    menu_values,
    notify_shell_change,
    register_start_menu,
    register_windows_integration,
    start_menu_shortcuts,
    uninstall_flags,
    uninstall_values,
    unregister_start_menu,
    unregister_windows_integration,
)


class FakeRegistry:
    def __init__(self, undeletable: set[str] | None = None) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.dwords: dict[tuple[str, str], int] = {}
        self.deleted: list[str] = []
        self._undeletable = undeletable or set()

    def set_value(self, key: str, name: str, value: str) -> None:
        self.values[(key, name)] = value

    def set_dword(self, key: str, name: str, value: int) -> None:
        self.dwords[(key, name)] = value

    def delete_tree(self, key: str) -> bool:
        if key in self._undeletable:
            return False
        self.deleted.append(key)
        for existing in list(self.values):
            if existing[0] == key:
                del self.values[existing]
        for existing in list(self.dwords):
            if existing[0] == key:
                del self.dwords[existing]
        return True


LAUNCHER = PureWindowsPath(
    r"C:\Users\dev\AppData\Local\mxl-merge-tool\0.3.0\MXL merge tool.exe"
)
ICON = PureWindowsPath(r"C:\Users\dev\AppData\Local\mxl-merge-tool\0.3.0\app\mxl.ico")
START_MENU = Path("start-menu") / "MXL Merge Tool"


class FakeShortcutWriter:
    def __init__(
        self, delete_ok: bool = True, fail_on_create: int | None = None
    ) -> None:
        self.created: list[Shortcut] = []
        self.deleted: list[Path] = []
        self.delete_ok = delete_ok
        self.fail_on_create = fail_on_create

    def create(self, shortcut: Shortcut) -> None:
        if self.fail_on_create == len(self.created):
            raise OSError("shortcut failed")
        self.created.append(shortcut)

    def delete_directory(self, path: Path) -> bool:
        self.deleted.append(path)
        return self.delete_ok


class MenuValuesTests(unittest.TestCase):
    def test_default_value_is_the_visible_caption(self) -> None:
        values = menu_values(LAUNCHER, ICON)
        self.assertEqual(values[""], "MXL merge: настроить этот репозиторий")

    def test_icon_points_at_the_bundled_file(self) -> None:
        values = menu_values(LAUNCHER, ICON)
        self.assertEqual(values["Icon"], f'"{ICON}"')

    def test_folder_command_passes_the_clicked_directory(self) -> None:
        values = menu_values(LAUNCHER, ICON)
        self.assertEqual(values["command"], f'"{LAUNCHER}" setup-repo "%V"')

    def test_both_menu_keys_receive_the_same_command(self) -> None:
        registry = FakeRegistry()
        register_windows_integration(registry, LAUNCHER, ICON)
        self.assertEqual(
            registry.values[(MENU_KEY + r"\command", "")],
            registry.values[(MENU_BACKGROUND_KEY + r"\command", "")],
        )


class UninstallValuesTests(unittest.TestCase):
    def test_declares_display_name_and_version(self) -> None:
        values = uninstall_values(LAUNCHER)
        self.assertEqual(values["DisplayName"], "MXL Merge Tool")
        self.assertEqual(values["DisplayVersion"], "0.3.0")

    def test_uninstall_string_calls_the_launcher(self) -> None:
        values = uninstall_values(LAUNCHER)
        self.assertEqual(values["UninstallString"], f'"{LAUNCHER}" uninstall')

    def test_flags_are_numbers_not_strings(self) -> None:
        # Programs and Features reads these as DWORD; a REG_SZ "1" is ignored
        # and the dead Modify and Repair buttons appear.
        self.assertEqual(uninstall_flags(), {"NoModify": 1, "NoRepair": 1})
        self.assertNotIn("NoModify", uninstall_values(LAUNCHER))


class RegisterIntegrationTests(unittest.TestCase):
    def test_writes_both_menu_keys_and_the_uninstall_entry(self) -> None:
        registry = FakeRegistry()
        register_windows_integration(registry, LAUNCHER, ICON)
        written_keys = {key for key, _ in registry.values}
        self.assertIn(MENU_KEY, written_keys)
        self.assertIn(MENU_BACKGROUND_KEY, written_keys)
        self.assertIn(UNINSTALL_KEY, written_keys)

    def test_no_modify_and_no_repair_are_written_as_dwords(self) -> None:
        registry = FakeRegistry()
        register_windows_integration(registry, LAUNCHER, ICON)
        self.assertEqual(registry.dwords[(UNINSTALL_KEY, "NoModify")], 1)
        self.assertEqual(registry.dwords[(UNINSTALL_KEY, "NoRepair")], 1)

    def test_command_lands_in_the_command_subkey(self) -> None:
        registry = FakeRegistry()
        register_windows_integration(registry, LAUNCHER, ICON)
        self.assertEqual(
            registry.values[(MENU_KEY + r"\command", "")],
            f'"{LAUNCHER}" setup-repo "%V"',
        )

    def test_unregister_removes_every_key(self) -> None:
        registry = FakeRegistry()
        register_windows_integration(registry, LAUNCHER, ICON)
        unregister_windows_integration(registry)
        self.assertEqual(registry.values, {})
        self.assertIn(MENU_KEY, registry.deleted)
        self.assertIn(MENU_BACKGROUND_KEY, registry.deleted)
        self.assertIn(UNINSTALL_KEY, registry.deleted)


class StartMenuTests(unittest.TestCase):
    def test_creates_setup_and_uninstall_shortcuts(self) -> None:
        shortcuts = start_menu_shortcuts(START_MENU, Path(LAUNCHER), Path(ICON))

        self.assertEqual(
            [item.path.name for item in shortcuts],
            [SETUP_SHORTCUT_NAME, UNINSTALL_SHORTCUT_NAME],
        )
        self.assertEqual(shortcuts[0].arguments, "setup-gui --installed")
        self.assertEqual(shortcuts[1].arguments, "uninstall")
        self.assertEqual(shortcuts[0].target.name, SETTINGS_LAUNCHER_NAME)
        self.assertEqual(shortcuts[1].target.name, MAINTENANCE_LAUNCHER_NAME)
        self.assertNotEqual(shortcuts[0].target, shortcuts[1].target)
        self.assertEqual(
            shortcuts[0].app_user_model_id, SETTINGS_APP_USER_MODEL_ID
        )
        self.assertEqual(
            shortcuts[1].app_user_model_id, MAINTENANCE_APP_USER_MODEL_ID
        )
        self.assertNotEqual(
            shortcuts[0].app_user_model_id, shortcuts[1].app_user_model_id
        )
        self.assertTrue(all(item.icon == Path(ICON) for item in shortcuts))

    def test_register_writes_both_shortcuts(self) -> None:
        writer = FakeShortcutWriter()
        register_start_menu(writer, START_MENU, Path(LAUNCHER), Path(ICON))
        self.assertEqual(len(writer.created), 2)

    def test_partial_registration_is_cleaned_up(self) -> None:
        writer = FakeShortcutWriter(fail_on_create=1)
        with self.assertRaisesRegex(OSError, "shortcut failed"):
            register_start_menu(writer, START_MENU, Path(LAUNCHER), Path(ICON))
        self.assertEqual(writer.deleted, [START_MENU])

    def test_unregister_reports_a_folder_that_survived(self) -> None:
        writer = FakeShortcutWriter(delete_ok=False)
        self.assertEqual(
            unregister_start_menu(writer, START_MENU), (str(START_MENU),)
        )

    def test_real_writer_passes_values_without_powershell_quoting(self) -> None:
        with tempfile.TemporaryDirectory() as raw, mock.patch(
            "mxl_setup.mxl_subprocess.run"
        ) as run, mock.patch(
            "mxl_setup.set_shortcut_app_user_model_id"
        ) as set_app_id, mock.patch("mxl_setup.notify_shell_change") as notify:
            shortcut = Shortcut(
                path=Path(raw) / SETUP_SHORTCUT_NAME,
                target=Path(LAUNCHER),
                arguments="setup-gui --installed",
                icon=Path(ICON),
                working_directory=Path(LAUNCHER).parent,
                description="Настройка MXL Merge Tool",
                app_user_model_id=SETTINGS_APP_USER_MODEL_ID,
            )

            def save_temporary_link(*_args: object, **kwargs: object) -> object:
                environment = kwargs["env"]
                assert isinstance(environment, dict)
                Path(environment["MXL_MERGE_SHORTCUT_PATH"]).write_bytes(b"link")
                return mock.Mock(returncode=0, stdout="", stderr="")

            run.side_effect = save_temporary_link
            WindowsShortcutWriter().create(shortcut)
            saved_link = shortcut.path.read_bytes()

        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(command[0], "powershell.exe")
        self.assertIn("-EncodedCommand", command)
        encoded = command[command.index("-EncodedCommand") + 1]
        self.assertEqual(
            base64.b64decode(encoded).decode("utf-16-le"),
            mxl_setup._POWERSHELL_SHORTCUT_SCRIPT,
        )
        temporary_path = Path(environment["MXL_MERGE_SHORTCUT_PATH"])
        self.assertTrue(temporary_path.name.startswith("mxl-shortcut-"))
        self.assertNotIn("Настройка", temporary_path.name)
        self.assertEqual(saved_link, b"link")
        self.assertEqual(environment["MXL_MERGE_SHORTCUT_TARGET"], str(shortcut.target))
        self.assertEqual(
            environment["MXL_MERGE_SHORTCUT_ARGUMENTS"], "setup-gui --installed"
        )
        self.assertEqual(notify.call_count, 2)
        self.assertEqual(notify.call_args_list[0].args[2], shortcut.path)
        set_app_id.assert_called_once_with(
            shortcut.path, SETTINGS_APP_USER_MODEL_ID
        )

    def test_real_writer_notifies_shell_when_folder_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as raw, mock.patch(
            "mxl_setup.notify_shell_change"
        ) as notify:
            directory = Path(raw) / "MXL Merge Tool"
            directory.mkdir()
            (directory / "shortcut.lnk").write_bytes(b"link")

            self.assertTrue(WindowsShortcutWriter().delete_directory(directory))

        self.assertEqual(notify.call_count, 2)
        self.assertEqual(notify.call_args_list[0].args[1], directory)

    def test_real_writer_surfaces_powershell_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as raw, mock.patch(
            "mxl_setup.mxl_subprocess.run"
        ) as run:
            run.return_value.returncode = 1
            run.return_value.stderr = "COM-компонент недоступен"
            shortcut = Shortcut(
                path=Path(raw) / SETUP_SHORTCUT_NAME,
                target=Path(LAUNCHER),
                arguments="setup-gui --installed",
                icon=Path(ICON),
                working_directory=Path(LAUNCHER).parent,
                description="Настройка MXL Merge Tool",
                app_user_model_id=SETTINGS_APP_USER_MODEL_ID,
            )
            with self.assertRaisesRegex(
                RuntimeError, "COM-компонент недоступен"
            ):
                WindowsShortcutWriter().create(shortcut)


from mxl_setup import GIT_CONFIG_KEYS, unset_git_config


class FakeGitRunner:
    def __init__(
        self,
        failing: set[str] | None = None,
        values: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.reads: list[str] = []
        # Every interaction in order, so tests can assert that uninstall reads
        # the provenance keys before it starts unsetting them.
        self.log: list[tuple[str, str]] = []
        self._failing = failing or set()
        self._values = values or {}

    def run(self, arguments: list[str]) -> int:
        self.calls.append(list(arguments))
        self.log.append(("run", arguments[-1]))
        return 5 if arguments[-1] in self._failing else 0

    def get(self, key: str) -> str | None:
        self.reads.append(key)
        self.log.append(("get", key))
        return self._values.get(key)


class UnsetGitConfigTests(unittest.TestCase):
    def test_removes_every_key_the_installer_writes(self) -> None:
        runner = FakeGitRunner()
        unset_git_config(runner)
        requested = [call[-1] for call in runner.calls]
        self.assertEqual(requested, list(GIT_CONFIG_KEYS))

    def test_uses_global_scope_and_unset_all(self) -> None:
        runner = FakeGitRunner()
        unset_git_config(runner)
        self.assertEqual(runner.calls[0][:3], ["config", "--global", "--unset-all"])

    def test_absent_key_is_not_an_error(self) -> None:
        runner = FakeGitRunner(failing={"mxl.previewBatchCommand"})
        self.assertEqual(unset_git_config(runner), ())

    def test_reports_failures_when_missing_is_not_tolerated(self) -> None:
        runner = FakeGitRunner(failing={"merge.mxl.driver"})
        failed = unset_git_config(runner, treat_missing_as_success=False)
        self.assertEqual(failed, ("merge.mxl.driver",))

    def test_covers_the_keys_install_writes(self) -> None:
        for key in (
            "diff.mxl.textconv",
            "merge.mxl.driver",
            "mergetool.mxl.cmd",
            "mxl.onecClient",
            "mxl.onecFileEditor",
            "mxl.previewCommand",
            "mxl.previewBatchCommand",
        ):
            self.assertIn(key, GIT_CONFIG_KEYS)


from mxl_setup import (
    RUNONCE_KEY,
    RUNONCE_VALUE_NAME,
    UninstallResult,
    remove_global_attributes,
    strip_attributes_line,
    uninstall,
)
from mxl_tool import MXL_ATTRIBUTES_LINE


class RemoveGlobalAttributesTests(unittest.TestCase):
    def _file(self, root: Path, extra: str = "*.txt text\n") -> Path:
        path = root / "gitattributes"
        path.write_text(f"{extra}{MXL_ATTRIBUTES_LINE}\n", encoding="utf-8")
        return path

    def test_keeps_a_user_file_minus_our_line(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = self._file(Path(raw))
            runner = FakeGitRunner(
                values={
                    "mxl.attributesFile": str(path),
                    "mxl.ownsAttributesFile": "false",
                }
            )

            self.assertEqual(remove_global_attributes(runner), ())

            self.assertTrue(path.exists())
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertNotIn(MXL_ATTRIBUTES_LINE, lines)
        self.assertIn("*.txt text", lines)
        # A file the user configured stays wired up in their Git config.
        self.assertNotIn("core.attributesFile", [call[-1] for call in runner.calls])

    def test_deletes_the_file_it_created_and_unsets_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = self._file(Path(raw), extra="")
            runner = FakeGitRunner(
                values={
                    "mxl.attributesFile": str(path),
                    "mxl.ownsAttributesFile": "true",
                    "core.attributesFile": str(path),
                }
            )

            self.assertEqual(remove_global_attributes(runner), ())

            self.assertFalse(path.exists())
        self.assertEqual(
            runner.calls,
            [["config", "--global", "--unset-all", "core.attributesFile"]],
        )

    def test_leaves_a_file_with_foreign_content_in_place(self) -> None:
        # MINOR E: "owns" only means this tool is allowed to unset
        # core.attributesFile and manage its line — it does not mean the
        # whole file is ours to delete. If something besides our line is
        # still there after stripping it, that content did not come from
        # _ensure_attributes_file, so the file must survive.
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "gitattributes"
            path.write_text(f"*.txt text\n{MXL_ATTRIBUTES_LINE}\n", encoding="utf-8")
            runner = FakeGitRunner(
                values={
                    "mxl.attributesFile": str(path),
                    "mxl.ownsAttributesFile": "true",
                    "core.attributesFile": str(path),
                }
            )

            self.assertEqual(remove_global_attributes(runner), ())

            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding="utf-8"), "*.txt text\n")

    def test_leaves_core_attributesfile_alone_when_the_user_repointed_it(self) -> None:
        # MINOR D: if the user changed core.attributesFile after install, it
        # no longer names our file, and unsetting it would silently drop
        # their setting.
        with tempfile.TemporaryDirectory() as raw:
            path = self._file(Path(raw), extra="")
            theirs = str(Path(raw) / "elsewhere")
            runner = FakeGitRunner(
                values={
                    "mxl.attributesFile": str(path),
                    "mxl.ownsAttributesFile": "true",
                    "core.attributesFile": theirs,
                }
            )

            self.assertEqual(remove_global_attributes(runner), ())

        self.assertEqual(runner.calls, [])

    def test_a_non_utf8_file_round_trips_without_crashing(self) -> None:
        # IMPORTANT C: a cp1251 gitattributes is entirely plausible on a
        # Russian Windows box. Reading it as strict utf-8 raises
        # UnicodeDecodeError, which used to abort uninstall before anything
        # was removed.
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "gitattributes"
            cp1251_comment = "# комментарий".encode("cp1251")
            path.write_bytes(cp1251_comment + b"\n" + MXL_ATTRIBUTES_LINE.encode("ascii") + b"\n")
            runner = FakeGitRunner(
                values={
                    "mxl.attributesFile": str(path),
                    "mxl.ownsAttributesFile": "false",
                }
            )

            self.assertEqual(remove_global_attributes(runner), ())

            remainder = path.read_bytes()
        self.assertNotIn(MXL_ATTRIBUTES_LINE.encode("ascii"), remainder)
        self.assertIn(cp1251_comment, remainder)

    def test_file_already_gone_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "absent" / "gitattributes"
            runner = FakeGitRunner(
                values={
                    "mxl.attributesFile": str(path),
                    "mxl.ownsAttributesFile": "true",
                }
            )
            self.assertEqual(remove_global_attributes(runner), ())

    def test_nothing_recorded_means_nothing_touched(self) -> None:
        runner = FakeGitRunner()
        self.assertEqual(remove_global_attributes(runner), ())
        self.assertEqual(runner.calls, [])

    def test_strip_leaves_a_file_without_our_line_intact(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "gitattributes"
            path.write_text("*.txt text\n", encoding="utf-8")
            strip_attributes_line(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "*.txt text\n")

    def test_provenance_keys_are_cleaned_up_too(self) -> None:
        self.assertIn("mxl.attributesFile", GIT_CONFIG_KEYS)
        self.assertIn("mxl.ownsAttributesFile", GIT_CONFIG_KEYS)


class UninstallTests(unittest.TestCase):
    def test_removes_config_registry_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mxl-merge-tool"
            (root / "0.3.0" / "app").mkdir(parents=True)
            registry = FakeRegistry()
            registry.set_value(MENU_KEY, "", "caption")
            runner = FakeGitRunner()

            result = uninstall(registry, runner, root)

            self.assertFalse(root.exists())
            self.assertEqual(registry.values, {})
            self.assertEqual(len(runner.calls), len(GIT_CONFIG_KEYS))
            self.assertEqual(result, UninstallResult())

    def test_removes_start_menu_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mxl-merge-tool"
            root.mkdir()
            start_menu = Path(raw) / "Start Menu" / "MXL Merge Tool"
            shortcuts = FakeShortcutWriter()

            result = uninstall(
                FakeRegistry(),
                FakeGitRunner(),
                root,
                shortcuts=shortcuts,
                start_menu=start_menu,
            )

            self.assertEqual(shortcuts.deleted, [start_menu])
            self.assertEqual(result, UninstallResult())

    def test_reports_start_menu_folder_that_could_not_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            start_menu = Path(raw) / "Start Menu" / "MXL Merge Tool"
            result = uninstall(
                FakeRegistry(),
                FakeGitRunner(),
                Path(raw) / "absent",
                shortcuts=FakeShortcutWriter(delete_ok=False),
                start_menu=start_menu,
            )

        self.assertIn(str(start_menu), result.failed_keys)

    def test_missing_directory_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "absent"
            self.assertEqual(
                uninstall(FakeRegistry(), FakeGitRunner(), root), UninstallResult()
            )

    def test_reads_the_provenance_keys_before_unsetting_anything(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "absent"
            runner = FakeGitRunner(
                values={
                    "mxl.attributesFile": str(Path(raw) / "gitattributes"),
                    "mxl.ownsAttributesFile": "true",
                }
            )
            uninstall(FakeRegistry(), runner, root)

        reads = [index for index, (kind, _) in enumerate(runner.log) if kind == "get"]
        first_unset = next(
            index
            for index, (kind, key) in enumerate(runner.log)
            if kind == "run" and key in GIT_CONFIG_KEYS
        )
        # The attributes file does not exist, so remove_global_attributes
        # never reaches the strip/unlink branch, but owns=true still makes
        # it read core.attributesFile (MINOR D) before deciding whether to
        # unset it — that read belongs before any unsetting too.
        self.assertEqual(
            runner.reads,
            ["mxl.attributesFile", "mxl.ownsAttributesFile", "core.attributesFile"],
        )
        self.assertTrue(all(index < first_unset for index in reads))

    def test_strips_the_attributes_line_of_a_user_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            attributes = Path(raw) / "gitattributes"
            attributes.write_text(f"*.txt text\n{MXL_ATTRIBUTES_LINE}\n", encoding="utf-8")
            runner = FakeGitRunner(
                values={
                    "mxl.attributesFile": str(attributes),
                    "mxl.ownsAttributesFile": "false",
                }
            )

            self.assertEqual(
                uninstall(FakeRegistry(), runner, Path(raw) / "absent"),
                UninstallResult(),
            )

            self.assertTrue(attributes.exists())
            self.assertNotIn(
                MXL_ATTRIBUTES_LINE, attributes.read_text(encoding="utf-8")
            )

    def test_surviving_files_are_scheduled_and_reported_informationally(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mxl-merge-tool"
            (root / "0.3.0" / "runtime").mkdir(parents=True)
            (root / "0.3.0" / "runtime" / "python312.dll").write_bytes(b"held open")
            registry = FakeRegistry()
            register_windows_integration(registry, LAUNCHER, ICON)

            with mock.patch("mxl_setup.shutil.rmtree"), mock.patch(
                "mxl_setup.report"
            ) as reported:
                result = uninstall(registry, FakeGitRunner(), root)

            # Leftover files are expected, not a failure: they are kept out
            # of failed_keys and surfaced only through leftover_paths.
            self.assertEqual(result.failed_keys, ())
            self.assertTrue(result.ok)
            self.assertIn(
                str(root / "0.3.0" / "runtime" / "python312.dll"),
                result.leftover_paths,
            )
            self.assertEqual(reported.call_count, 1)
            self.assertFalse(reported.call_args.kwargs.get("error", False))
            # N1: since MoveFileExW/MOVEFILE_DELAY_UNTIL_REBOOT needs
            # elevation this non-elevated installer never has, cleanup goes
            # through a RunOnce value under HKCU instead — no elevation
            # required, and Windows deletes the value itself once it runs.
            command = registry.values[(RUNONCE_KEY, RUNONCE_VALUE_NAME)]
            self.assertIn(str(root), command)
            self.assertIn("rmdir", command)

    def test_nothing_scheduled_when_removal_is_clean(self) -> None:
        # The other half of N1: a directory that rmtree fully removes must
        # not leave a RunOnce cleanup entry behind — there is nothing left
        # to clean up at the next sign-in.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mxl-merge-tool"
            (root / "0.3.0" / "app").mkdir(parents=True)
            registry = FakeRegistry()

            uninstall(registry, FakeGitRunner(), root)

            self.assertNotIn((RUNONCE_KEY, RUNONCE_VALUE_NAME), registry.values)

    def test_registry_keys_are_unregistered_even_when_files_survive(self) -> None:
        # This is CRITICAL A: the previous fix wave only unregistered the
        # registry once root.exists() was False, which can never happen on
        # Windows because the uninstaller's own pythonw.exe and
        # python312.dll live under root and cannot delete themselves while
        # running. That left the context menu and the "Programs and
        # Features" entry permanently registered. Registry removal must now
        # happen unconditionally, before rmtree is even attempted.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mxl-merge-tool"
            (root / "0.3.0" / "runtime").mkdir(parents=True)
            (root / "0.3.0" / "runtime" / "python312.dll").write_bytes(b"held open")
            registry = FakeRegistry()
            register_windows_integration(registry, LAUNCHER, ICON)
            self.assertIn((UNINSTALL_KEY, "DisplayName"), registry.values)

            with mock.patch("mxl_setup.shutil.rmtree"), mock.patch("mxl_setup.report"):
                result = uninstall(registry, FakeGitRunner(), root)

            self.assertTrue(result.leftover_paths, "files must still survive in this test")
            # The integration keys — context menu and "Programs and
            # Features" entry — must be gone, regardless of what else the
            # registry now holds (namely the RunOnce cleanup value below).
            self.assertIn(MENU_KEY, registry.deleted)
            self.assertIn(MENU_BACKGROUND_KEY, registry.deleted)
            self.assertIn(UNINSTALL_KEY, registry.deleted)
            self.assertNotIn((UNINSTALL_KEY, "DisplayName"), registry.values)
            self.assertNotIn((MENU_KEY, ""), registry.values)
            self.assertNotIn((MENU_BACKGROUND_KEY, ""), registry.values)
            # The only thing left behind is the deliberate RunOnce cleanup
            # entry scheduled because files survived.
            self.assertEqual(list(registry.values.keys()), [(RUNONCE_KEY, RUNONCE_VALUE_NAME)])
            self.assertIn(str(root), registry.values[(RUNONCE_KEY, RUNONCE_VALUE_NAME)])

    def test_a_registry_key_that_survives_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mxl-merge-tool"
            root.mkdir()
            registry = FakeRegistry(undeletable={MENU_KEY})
            register_windows_integration(registry, LAUNCHER, ICON)

            result = uninstall(registry, FakeGitRunner(), root)

        self.assertIn(MENU_KEY, result.failed_keys)
        self.assertFalse(result.ok)

    def test_degrades_instead_of_aborting_when_git_is_not_on_path(self) -> None:
        # N2: a user who uninstalled Git before this tool used to be stuck
        # forever — SubprocessGitRunner.run/get raise FileNotFoundError, and
        # that call sat outside any try, so uninstall raised before the
        # registry or the files were ever touched. Git config keys may now
        # survive (they are inert once the drivers are gone), but the
        # visible integration and the files must still go.
        class MissingGitRunner:
            def run(self, arguments: list[str]) -> int:
                raise FileNotFoundError("git")

            def get(self, key: str) -> str | None:
                raise FileNotFoundError("git")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mxl-merge-tool"
            (root / "0.3.0" / "app").mkdir(parents=True)
            registry = FakeRegistry()
            register_windows_integration(registry, LAUNCHER, ICON)

            result = uninstall(registry, MissingGitRunner(), root)

            self.assertFalse(root.exists())
            self.assertEqual(registry.values, {})
            self.assertIn(MENU_KEY, registry.deleted)
            self.assertIn(UNINSTALL_KEY, registry.deleted)
            self.assertTrue(result.failed_keys)


from mxl_setup import confirm_uninstall, report


class ReportTests(unittest.TestCase):
    def test_falls_back_to_stdout_without_windll(self) -> None:
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            report("готово")
        self.assertIn("готово", stream.getvalue())

    def test_errors_go_to_stderr(self) -> None:
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream):
            report("сломалось", error=True)
        self.assertIn("сломалось", stream.getvalue())

    def test_a_none_stream_is_not_an_error(self) -> None:
        # This is the pythonw.exe case: no console, so no streams either.
        with mock.patch.object(mxl_setup.sys, "stdout", None), mock.patch.object(
            mxl_setup.sys, "stderr", None
        ):
            report("никуда")
            report("никуда", error=True)


class ConfirmUninstallTests(unittest.TestCase):
    def _answer(self, result: int) -> tuple[bool, mock.Mock]:
        ctypes = mock.Mock()
        ctypes.windll.user32.MessageBoxW.return_value = result
        with mock.patch.object(mxl_setup.sys, "platform", "win32"), mock.patch.dict(
            sys.modules, {"ctypes": ctypes}
        ):
            answer = confirm_uninstall()
        return answer, ctypes.windll.user32.MessageBoxW

    def test_yes_allows_uninstall(self) -> None:
        answer, message_box = self._answer(6)
        self.assertTrue(answer)
        self.assertIn("Удалить MXL Merge Tool?", message_box.call_args.args[1])

    def test_no_cancels_uninstall(self) -> None:
        answer, _ = self._answer(7)
        self.assertFalse(answer)

    def test_no_is_the_default_button(self) -> None:
        _, message_box = self._answer(7)
        flags = message_box.call_args.args[3]
        self.assertTrue(flags & 0x100)


class ShellNotificationTests(unittest.TestCase):
    def test_rename_event_passes_both_unicode_paths_and_flushes(self) -> None:
        ctypes = mock.Mock()
        with mock.patch.object(mxl_setup.sys, "platform", "win32"), mock.patch.dict(
            sys.modules, {"ctypes": ctypes}
        ):
            notify_shell_change(1, Path("Старый.lnk"), Path("Новый.lnk"))

        call = ctypes.windll.shell32.SHChangeNotify.call_args
        self.assertEqual(call.args[0], 1)
        self.assertEqual(call.args[2], "Старый.lnk")
        self.assertEqual(call.args[3], "Новый.lnk")
        self.assertTrue(call.args[1] & 0x1000)


class PruneOldVersionsTests(unittest.TestCase):
    def _root(self, raw: str, *names: str) -> Path:
        root = Path(raw) / "mxl-merge-tool"
        for name in names:
            (root / name).mkdir(parents=True)
        return root

    def test_removes_earlier_versions_and_keeps_the_current_one(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = self._root(raw, "0.2.1", "0.3.0", "0.1.0")
            self.assertEqual(mxl_setup.prune_old_versions(root, keep="0.3.0"), ())
            self.assertTrue((root / "0.3.0").exists())
            self.assertFalse((root / "0.2.1").exists())
            self.assertFalse((root / "0.1.0").exists())

    def test_leaves_anything_that_is_not_a_version_alone(self) -> None:
        # The root is under %LOCALAPPDATA%; a user or another tool may keep
        # something there, and an installer has no business deleting it.
        with tempfile.TemporaryDirectory() as raw:
            root = self._root(raw, "0.3.0", "logs", "backup-0.2.1")
            mxl_setup.prune_old_versions(root, keep="0.3.0")
            self.assertTrue((root / "logs").exists())
            self.assertTrue((root / "backup-0.2.1").exists())

    def test_a_missing_root_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(
                mxl_setup.prune_old_versions(Path(raw) / "absent", keep="0.3.0"), ()
            )

    def test_a_directory_that_survives_removal_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = self._root(raw, "0.3.0", "0.2.1")
            with mock.patch("mxl_setup.shutil.rmtree"):  # simulate locked files
                survivors = mxl_setup.prune_old_versions(root, keep="0.3.0")
            self.assertEqual(survivors, ("0.2.1",))

    def test_files_in_the_root_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = self._root(raw, "0.3.0")
            (root / "0.2.1").write_text("not a directory", encoding="utf-8")
            mxl_setup.prune_old_versions(root, keep="0.3.0")
            self.assertTrue((root / "0.2.1").exists())


class VersionSingleSourceTests(unittest.TestCase):
    """The version used to sit in seven places across five files.

    Bumping it meant editing all of them, and missing one produced an archive
    that installed into the previous version's directory or an executable
    reporting the wrong version. mxl_setup.APP_VERSION is now the only place
    it is written; these tests fail if a literal creeps back.
    """

    REPO = Path(__file__).resolve().parent.parent

    def test_launcher_resources_carry_placeholders_not_literals(self) -> None:
        for name in ("launcher.rc", "launcher.manifest"):
            text = (self.REPO / "tools" / "launcher" / name).read_text(encoding="utf-8")
            self.assertIn("@VERSION_", text, f"{name} lost its placeholders")
            self.assertNotIn(mxl_setup.APP_VERSION, text, f"{name} hardcodes the version")

    def test_the_build_script_takes_the_version_from_mxl_setup(self) -> None:
        text = (self.REPO / "tools" / "build_windows.py").read_text(encoding="utf-8")
        self.assertIn("from mxl_setup import APP_VERSION", text)
        self.assertNotIn(f'APP_VERSION = "{mxl_setup.APP_VERSION}"', text)

    def test_the_changelog_documents_the_current_version(self) -> None:
        text = (self.REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## {mxl_setup.APP_VERSION}", text)


if __name__ == "__main__":
    unittest.main()
