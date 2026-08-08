from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from mxl_setup_gui import (
    _tcl_tk_candidate_dirs,
    configure_tcl_tk_environment,
    resolve_tcl_tk_library_dirs,
)


def _existing(*paths: Path) -> object:
    present = set(paths)
    return lambda path: path in present


class ResolveTclTkLibraryDirsTests(unittest.TestCase):
    def test_returns_none_pair_when_no_candidate_matches(self) -> None:
        dirs = (Path("/one"), Path("/two"))
        result = resolve_tcl_tk_library_dirs(dirs, lambda path: False)
        self.assertEqual(result, (None, None))

    def test_returns_paths_under_matching_candidate(self) -> None:
        base = Path("/runtime")
        exists = _existing(base / "tcl" / "tcl8.6", base / "tcl" / "tk8.6")
        result = resolve_tcl_tk_library_dirs((base,), exists)
        self.assertEqual(result, (base / "tcl" / "tcl8.6", base / "tcl" / "tk8.6"))

    def test_requires_both_directories_from_the_same_candidate(self) -> None:
        # Only tcl8.6 exists under this base -- must not report a partial hit.
        base = Path("/runtime")
        exists = _existing(base / "tcl" / "tcl8.6")
        result = resolve_tcl_tk_library_dirs((base,), exists)
        self.assertEqual(result, (None, None))

    def test_falls_through_to_the_next_candidate(self) -> None:
        first = Path("/no-bundle")
        second = Path("/runtime")
        exists = _existing(second / "tcl" / "tcl8.6", second / "tcl" / "tk8.6")
        result = resolve_tcl_tk_library_dirs((first, second), exists)
        self.assertEqual(result, (second / "tcl" / "tcl8.6", second / "tcl" / "tk8.6"))

    def test_prefers_the_first_matching_candidate(self) -> None:
        first = Path("/runtime")
        second = Path("/also-has-a-bundle")
        exists = _existing(
            first / "tcl" / "tcl8.6",
            first / "tcl" / "tk8.6",
            second / "tcl" / "tcl8.6",
            second / "tcl" / "tk8.6",
        )
        result = resolve_tcl_tk_library_dirs((first, second), exists)
        self.assertEqual(result, (first / "tcl" / "tcl8.6", first / "tcl" / "tk8.6"))


class TclTkCandidateDirsTests(unittest.TestCase):
    # Uses POSIX-style paths so the test behaves the same on the developer's
    # (non-Windows) machine as it would on the real Windows target -- on
    # POSIX, backslashes in a string are not path separators, which would
    # otherwise make a Windows-style fixture path collapse to one segment.

    def test_includes_prefix_and_executable_directory(self) -> None:
        with mock.patch("mxl_setup_gui.sys.prefix", "/install/runtime"), mock.patch(
            "mxl_setup_gui.sys.executable", "/install/runtime/python.exe"
        ):
            dirs = _tcl_tk_candidate_dirs()
        self.assertIn(Path("/install/runtime"), dirs)

    def test_deduplicates_when_prefix_and_executable_dir_match(self) -> None:
        with mock.patch("mxl_setup_gui.sys.prefix", "/install/runtime"), mock.patch(
            "mxl_setup_gui.sys.executable", "/install/runtime/python.exe"
        ):
            dirs = _tcl_tk_candidate_dirs()
        self.assertEqual(len(dirs), 1)


class ConfigureTclTkEnvironmentTests(unittest.TestCase):
    def test_noop_off_windows_even_when_bundle_present(self) -> None:
        base = Path("/runtime")
        exists = _existing(base / "tcl" / "tcl8.6", base / "tcl" / "tk8.6")
        environ: dict[str, str] = {}
        configure_tcl_tk_environment(
            platform="darwin", candidate_dirs=(base,), exists=exists, environ=environ
        )
        self.assertEqual(environ, {})

    def test_noop_on_windows_when_bundle_absent(self) -> None:
        environ: dict[str, str] = {}
        configure_tcl_tk_environment(
            platform="win32",
            candidate_dirs=(Path("/runtime"),),
            exists=lambda path: False,
            environ=environ,
        )
        self.assertEqual(environ, {})

    def test_sets_both_variables_on_windows_when_bundle_present(self) -> None:
        base = Path(r"C:\install\runtime")
        exists = _existing(base / "tcl" / "tcl8.6", base / "tcl" / "tk8.6")
        environ: dict[str, str] = {}
        configure_tcl_tk_environment(
            platform="win32", candidate_dirs=(base,), exists=exists, environ=environ
        )
        self.assertEqual(environ["TCL_LIBRARY"], str(base / "tcl" / "tcl8.6"))
        self.assertEqual(environ["TK_LIBRARY"], str(base / "tcl" / "tk8.6"))

    def test_never_overwrites_a_value_already_set(self) -> None:
        base = Path(r"C:\install\runtime")
        exists = _existing(base / "tcl" / "tcl8.6", base / "tcl" / "tk8.6")
        environ = {"TCL_LIBRARY": r"C:\custom\tcl8.6"}
        configure_tcl_tk_environment(
            platform="win32", candidate_dirs=(base,), exists=exists, environ=environ
        )
        self.assertEqual(environ["TCL_LIBRARY"], r"C:\custom\tcl8.6")
        self.assertEqual(environ["TK_LIBRARY"], str(base / "tcl" / "tk8.6"))


if __name__ == "__main__":
    unittest.main()
