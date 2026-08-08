"""Windows installation support for the MXL merge tool.

The module keeps discovery and registry composition free of side effects so it
can be exercised on any platform. Every operation that touches the registry,
the file system or Git goes through a narrow protocol that tests replace.
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Iterable, NamedTuple, Protocol, Sequence

import mxl_subprocess

APP_NAME = "mxl-merge-tool"
APP_VERSION = "0.3.0"

_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+)*$")

_MB_OK = 0x0
_MB_ICONERROR = 0x10
_MB_ICONINFORMATION = 0x40


def report(message: str, title: str = "MXL merge tool", error: bool = False) -> None:
    """Tell the user something, even with no console attached.

    The launcher starts everything through pythonw.exe, where sys.stdout and
    sys.stderr are None and print goes nowhere. A message box is the only
    channel that reaches a user who double-clicked an icon; it is also what
    the C launcher uses for its own failures. Off Windows, or when the ctypes
    call is unavailable, fall back to the streams.
    """

    if sys.platform == "win32":
        try:
            import ctypes

            flags = _MB_OK | (_MB_ICONERROR if error else _MB_ICONINFORMATION)
            ctypes.windll.user32.MessageBoxW(None, message, title, flags)  # type: ignore[attr-defined]
            return
        except Exception:
            pass
    stream = sys.stderr if error else sys.stdout
    if stream is not None:
        print(f"{title}: {message}", file=stream)


@dataclass(frozen=True)
class OnecCandidate:
    """A single 1C executable discovered on the machine."""

    version: tuple[int, ...]
    version_text: str
    path: PurePath
    batch_capable: bool


@dataclass(frozen=True)
class Discovery:
    git: str | None
    clients: tuple[OnecCandidate, ...]
    file_editors: tuple[OnecCandidate, ...]


def parse_version(text: str) -> tuple[int, ...] | None:
    """Return a comparable version tuple, or None when text is not a version."""

    if not _VERSION_PATTERN.match(text):
        return None
    return tuple(int(part) for part in text.split("."))


def sort_candidates(candidates: Iterable[OnecCandidate]) -> tuple[OnecCandidate, ...]:
    """Newest first, one entry per executable path."""

    unique: dict[str, OnecCandidate] = {}
    for candidate in candidates:
        key = str(candidate.path).casefold()
        unique.setdefault(key, candidate)
    return tuple(sorted(unique.values(), key=lambda item: item.version, reverse=True))


class Environment(Protocol):
    """Everything the discovery code needs from the host machine."""

    def program_files(self) -> Sequence[PurePath]: ...

    def local_app_data(self) -> PurePath: ...

    def exists(self, path: PurePath) -> bool: ...

    def list_directory(self, path: PurePath) -> Sequence[str]: ...

    def registry_subkeys(self, root: str, key: str) -> Sequence[str]: ...

    def find_git(self) -> str | None: ...


_ONEC_ROOT = "1cv8"
_ONEC_REGISTRY_KEY = r"SOFTWARE\1C\1Cv8"
_FILE_EDITOR_ROOT = "1cv8fv"


def _version_names(env: Environment, root: PurePath) -> tuple[str, ...]:
    listed = tuple(env.list_directory(root))
    if listed:
        return listed
    names: list[str] = []
    for registry_root in ("HKLM", "HKCU"):
        names.extend(env.registry_subkeys(registry_root, _ONEC_REGISTRY_KEY))
    return tuple(names)


def discover_onec_clients(env: Environment) -> tuple[OnecCandidate, ...]:
    """Locate 1cv8c.exe thin clients, newest first."""

    candidates: list[OnecCandidate] = []
    for base in env.program_files():
        root = base / _ONEC_ROOT
        for name in _version_names(env, root):
            version = parse_version(name)
            if version is None:
                continue
            client = root / name / "bin" / "1cv8c.exe"
            if not env.exists(client):
                continue
            thick = root / name / "bin" / "1cv8.exe"
            candidates.append(
                OnecCandidate(version, name, client, env.exists(thick))
            )
    return sort_candidates(candidates)


def discover_file_editors(env: Environment) -> tuple[OnecCandidate, ...]:
    """Locate the 1C:Enterprise File Work application."""

    candidates: list[OnecCandidate] = []
    for base in env.program_files():
        root = base / _FILE_EDITOR_ROOT
        direct = root / "bin" / "1cv8fv.exe"
        if env.exists(direct):
            candidates.append(OnecCandidate((0,), "", direct, False))
        for name in env.list_directory(root):
            version = parse_version(name)
            if version is None:
                continue
            executable = root / name / "bin" / "1cv8fv.exe"
            if env.exists(executable):
                candidates.append(OnecCandidate(version, name, executable, False))
    return sort_candidates(candidates)


def discover_git(env: Environment) -> str | None:
    return env.find_git()


def discover(env: Environment) -> Discovery:
    return Discovery(
        git=discover_git(env),
        clients=discover_onec_clients(env),
        file_editors=discover_file_editors(env),
    )


LAUNCHER_NAME = "MXL merge tool.exe"
PAYLOAD_ENTRIES = ("runtime", "app", LAUNCHER_NAME, "README.txt")


def install_root(env: Environment) -> PurePath:
    return env.local_app_data() / APP_NAME


def install_dir(env: Environment) -> PurePath:
    return install_root(env) / APP_VERSION


def launcher_path(env: Environment) -> PurePath:
    return install_dir(env) / LAUNCHER_NAME


def prune_old_versions(root: Path, keep: str = APP_VERSION) -> tuple[str, ...]:
    """Remove install directories left behind by earlier versions.

    The install directory is version-scoped, so upgrading leaves the previous
    version sitting under the same root — about 14 MB every time, forever.

    Only directories whose name parses as a version are touched, so anything
    else a user put under the root is left alone, and `keep` never is. A
    previous version whose files are still locked (a merge window open from
    it, say) is skipped rather than failing the installation: the names that
    survive come back to the caller.
    """

    survivors: list[str] = []
    if not root.exists():
        return ()
    for item in sorted(root.iterdir()):
        if not item.is_dir() or item.name == keep:
            continue
        if parse_version(item.name) is None:
            continue
        shutil.rmtree(item, ignore_errors=True)
        if item.exists():
            survivors.append(item.name)
    return tuple(survivors)


def is_installed_copy(env: Environment, candidate: PurePath) -> bool:
    """True when candidate is the version directory we install into."""

    return str(candidate).casefold().rstrip("\\/") == str(install_dir(env)).casefold()


def copy_payload(source: Path, target: Path) -> Path:
    """Copy the distribution payload into target, replacing what is there.

    Only the entries listed in PAYLOAD_ENTRIES travel. Anything the user
    dropped next to the launcher stays behind.
    """

    target.mkdir(parents=True, exist_ok=True)
    for name in PAYLOAD_ENTRIES:
        origin = source / name
        destination = target / name
        if not origin.exists():
            continue
        if origin.is_dir():
            shutil.rmtree(destination, ignore_errors=True)
            shutil.copytree(origin, destination)
        else:
            shutil.copy2(origin, destination)
    return target / LAUNCHER_NAME


class WindowsEnvironment:
    """Real Windows environment. Exercised by the manual Windows checklist."""

    def program_files(self) -> Sequence[PurePath]:
        import os

        seen: list[PurePath] = []
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            value = os.environ.get(variable)
            if value and Path(value) not in seen:
                seen.append(Path(value))
        return tuple(seen)

    def local_app_data(self) -> PurePath:
        import os

        value = os.environ.get("LOCALAPPDATA")
        if not value:
            raise RuntimeError("LOCALAPPDATA is not set; cannot locate the install directory")
        return Path(value)

    def exists(self, path: PurePath) -> bool:
        return Path(path).exists()

    def list_directory(self, path: PurePath) -> Sequence[str]:
        try:
            return tuple(sorted(item.name for item in Path(path).iterdir() if item.is_dir()))
        except OSError:
            return ()

    def registry_subkeys(self, root: str, key: str) -> Sequence[str]:
        try:
            import winreg
        except ImportError:
            return ()
        hive = winreg.HKEY_LOCAL_MACHINE if root == "HKLM" else winreg.HKEY_CURRENT_USER
        names: list[str] = []
        try:
            with winreg.OpenKey(hive, key) as handle:
                index = 0
                while True:
                    try:
                        names.append(winreg.EnumKey(handle, index))
                    except OSError:
                        break
                    index += 1
        except OSError:
            return ()
        return tuple(names)

    def find_git(self) -> str | None:
        import shutil as shutil_module

        return shutil_module.which("git")


MENU_KEY = r"Software\Classes\Directory\shell\MxlMergeTool"
MENU_BACKGROUND_KEY = r"Software\Classes\Directory\Background\shell\MxlMergeTool"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\MxlMergeTool"
MENU_CAPTION = "MXL merge: настроить этот репозиторий"


class RegistryWriter(Protocol):
    """Writes under HKEY_CURRENT_USER. Never touches HKEY_LOCAL_MACHINE."""

    def set_value(self, key: str, name: str, value: str) -> None: ...

    def set_dword(self, key: str, name: str, value: int) -> None: ...

    def delete_tree(self, key: str) -> bool: ...


def menu_values(launcher: PurePath, icon: PurePath) -> dict[str, str]:
    """Values for one context menu key.

    Both the folder and the folder-background keys use %V: for a folder click
    it expands to the clicked folder, for a background click to the open one,
    so both keys carry exactly the same values.
    """

    return {
        "": MENU_CAPTION,
        "Icon": f'"{icon}"',
        "command": f'"{launcher}" setup-repo "%V"',
    }


def uninstall_values(launcher: PurePath) -> dict[str, str]:
    return {
        "DisplayName": "MXL Merge Tool",
        "DisplayVersion": APP_VERSION,
        "Publisher": "MXL Merge Tool",
        "DisplayIcon": f'"{launcher}"',
        "UninstallString": f'"{launcher}" uninstall',
        "InstallLocation": str(launcher.parent),
    }


def uninstall_flags() -> dict[str, int]:
    """Numeric values of the uninstall entry.

    "Programs and Features" reads NoModify and NoRepair as DWORD. A REG_SZ
    "1" is ignored, so the Modify and Repair buttons would appear and do
    nothing.
    """

    return {"NoModify": 1, "NoRepair": 1}


def _write_menu(writer: RegistryWriter, key: str, values: dict[str, str]) -> None:
    for name, value in values.items():
        if name == "command":
            writer.set_value(key + r"\command", "", value)
        else:
            writer.set_value(key, name, value)


def register_windows_integration(
    writer: RegistryWriter, launcher: PurePath, icon: PurePath
) -> None:
    for key in (MENU_KEY, MENU_BACKGROUND_KEY):
        _write_menu(writer, key, menu_values(launcher, icon))
    for name, value in uninstall_values(launcher).items():
        writer.set_value(UNINSTALL_KEY, name, value)
    for name, flag in uninstall_flags().items():
        writer.set_dword(UNINSTALL_KEY, name, flag)


def unregister_windows_integration(writer: RegistryWriter) -> tuple[str, ...]:
    """Remove every key the installer wrote. Returns the keys that survived."""

    surviving: list[str] = []
    for key in (
        MENU_KEY + r"\command",
        MENU_KEY,
        MENU_BACKGROUND_KEY + r"\command",
        MENU_BACKGROUND_KEY,
        UNINSTALL_KEY,
    ):
        if not writer.delete_tree(key):
            surviving.append(key)
    return tuple(surviving)


GIT_CONFIG_KEYS = (
    "diff.mxl.textconv",
    "diff.mxl.cachetextconv",
    "merge.mxl.name",
    "merge.mxl.driver",
    "merge.mxl.recursive",
    "mergetool.mxl.cmd",
    "mergetool.mxl.trustExitCode",
    "mxl.onecClient",
    "mxl.onecInfobase",
    "mxl.onecEpf",
    "mxl.onecUsername",
    "mxl.onecFileEditor",
    "mxl.onecBatchCapable",
    "mxl.previewCommand",
    "mxl.previewBatchCommand",
    # Written by _install_global_attributes so uninstall knows which file it
    # touched and whether it, rather than the user, configured Git to read it.
    "mxl.attributesFile",
    "mxl.ownsAttributesFile",
)

# git config --unset-all returns 5 when the key is simply absent.
_GIT_MISSING_KEY = 5

# Recorded in UninstallResult.failed_keys when git itself could not be run
# (FileNotFoundError, typically because git was uninstalled first), so the
# Git configuration steps are skipped rather than aborting the whole
# uninstall.
_GIT_UNAVAILABLE = "git: not found on PATH (Git configuration left in place)"


class GitRunner(Protocol):
    def run(self, arguments: Sequence[str]) -> int: ...

    def get(self, key: str) -> str | None: ...


def unset_git_config(
    runner: GitRunner, treat_missing_as_success: bool = True
) -> tuple[str, ...]:
    """Remove every key the installer writes. Returns the keys that failed."""

    failed: list[str] = []
    for key in GIT_CONFIG_KEYS:
        code = runner.run(["config", "--global", "--unset-all", key])
        if code == 0:
            continue
        if code == _GIT_MISSING_KEY and treat_missing_as_success:
            continue
        failed.append(key)
    return tuple(failed)


class SubprocessGitRunner:
    """Runs the real git executable."""

    def run(self, arguments: Sequence[str]) -> int:
        return mxl_subprocess.run(["git", *arguments], check=False).returncode

    def get(self, key: str) -> str | None:
        result = mxl_subprocess.run(
            ["git", "config", "--global", "--get", key],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        value = (result.stdout or "").strip()
        return value or None


def _attributes_line() -> str:
    try:
        from tools.mxl_merge.mxl_tool import MXL_ATTRIBUTES_LINE
    except ModuleNotFoundError:
        from mxl_tool import MXL_ATTRIBUTES_LINE  # type: ignore[no-redef]

    return MXL_ATTRIBUTES_LINE


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    try:
        from tools.mxl_merge.mxl_tool import atomic_write_bytes
    except ModuleNotFoundError:
        from mxl_tool import atomic_write_bytes  # type: ignore[no-redef]

    atomic_write_bytes(path, data)


def strip_attributes_line(path: Path) -> str:
    """Remove the tool's line from an attributes file, keeping the rest.

    Reads and writes with surrogateescape so a file in any encoding (a
    cp1251 gitattributes on a Russian Windows box, say) round-trips without
    raising UnicodeDecodeError or corrupting bytes we don't understand. The
    rewrite goes through atomic_write_bytes so a crash mid-write cannot
    truncate a file that belongs to the user, and the file's own line ending
    is preserved rather than normalised to \\n. Returns the text that
    remains, so a caller can tell whether the file still holds anything of
    the user's.
    """

    line = _attributes_line()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ""
    text = data.decode("utf-8", errors="surrogateescape")
    newline = "\r\n" if "\r\n" in text else "\n"
    kept = [item for item in text.splitlines() if item.strip() != line]
    remainder = "".join(f"{item}{newline}" for item in kept)
    _atomic_write_bytes(path, remainder.encode("utf-8", errors="surrogateescape"))
    return remainder


def remove_global_attributes(runner: GitRunner) -> tuple[str, ...]:
    """Undo what _install_global_attributes did to core.attributesFile.

    The install records both the path it used and whether it created the
    core.attributesFile setting itself. Deleting the whole file is only
    correct when this tool is the one that created it: if, once our line is
    stripped, anything else remains, that content did not come from us and
    the file is left in place, minus our line. core.attributesFile is only
    unset while it still points at our file — a user who repointed it after
    installing keeps their own setting.
    """

    import os

    configured = runner.get("mxl.attributesFile")
    owns = (runner.get("mxl.ownsAttributesFile") or "").strip().casefold() == "true"
    if not configured:
        return ()
    path = Path(os.path.expandvars(configured)).expanduser()
    problems: list[str] = []
    try:
        if path.exists():
            remainder = strip_attributes_line(path)
            if owns and not remainder.strip():
                path.unlink()
    except OSError:
        problems.append(str(path))
    if owns:
        current = runner.get("core.attributesFile")
        if current is not None and current == configured:
            code = runner.run(
                ["config", "--global", "--unset-all", "core.attributesFile"]
            )
            if code not in (0, _GIT_MISSING_KEY):
                problems.append("core.attributesFile")
    return tuple(problems)


class UninstallResult(NamedTuple):
    """What uninstall could not finish.

    failed_keys covers Git configuration keys and registry keys that could
    not be removed, plus a marker when git itself could not be run at all —
    genuine failures the user must act on. leftover_paths
    lists program files that survived rmtree because a running instance
    still held them open; on Windows that is expected, not a failure, so it
    is kept in a separate typed field rather than folded into failed_keys
    behind a string prefix that both this module and mxl_tool had to agree
    on by convention.
    """

    failed_keys: tuple[str, ...] = ()
    leftover_paths: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed_keys


RUNONCE_KEY = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"
RUNONCE_VALUE_NAME = "MxlMergeToolCleanup"


def _schedule_delayed_removal(writer: RegistryWriter, root: Path) -> bool:
    """Ask Windows to finish removing root at the next sign-in.

    A running pythonw.exe cannot delete the python312.dll it has mapped into
    memory, so a nonempty root after rmtree is expected here, not
    exceptional. This installer is deliberately non-elevated — only HKCU and
    %LOCALAPPDATA%, never anything requiring administrator rights — which
    rules out MOVEFILE_DELAY_UNTIL_REBOOT: it writes to
    HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session
    Manager\\PendingFileRenameOperations, and Microsoft documents that key as
    writable only by a caller in the administrators group or LocalSystem, so
    every call from here would fail with ERROR_ACCESS_DENIED. A RunOnce
    value under HKCU needs no elevation and does the same job without it:
    Windows runs the command and deletes the value itself as it does, so it
    fires exactly once, at the next sign-in, and does not linger behind.
    Routed through RegistryWriter so it is one mechanism, testable with the
    same fake as everything else in this module. Returns whether the value
    was written, so the caller can tell the user what will actually happen
    rather than promising something that may not have been scheduled.
    """

    command = f'cmd /c rmdir /s /q "{root}"'
    try:
        writer.set_value(RUNONCE_KEY, RUNONCE_VALUE_NAME, command)
        return True
    except OSError:
        return False


def uninstall(
    registry: RegistryWriter, runner: GitRunner, root: Path
) -> UninstallResult:
    """Undo the installation. Returns everything that could not be removed.

    Order matters, and the previous order was the bug that motivated this
    one: the uninstaller runs from <root>\\<version>\\MXL merge tool.exe, and
    the pythonw.exe and python312.dll behind it live under
    <root>\\<version>\\runtime, so a running instance can never delete its
    own directory — root.exists() is always true afterwards. Waiting for it
    to become false before touching the registry meant the registry keys,
    and with them the context menu and the "Programs and Features" entry,
    were never removed at all.

    So now: Git configuration and the attributes file are undone first,
    exactly as before, but a user who removed Git before this tool must
    still be able to finish uninstalling — SubprocessGitRunner.run/get raise
    FileNotFoundError when git is not on PATH, and that used to abort here,
    before the registry or the files were ever touched. That failure is
    caught, recorded, and the rest proceeds: the Git steps are the only ones
    that need git, and leftover config keys are inert once the drivers below
    them are gone. Then the registry keys are unregistered
    unconditionally — that is the part the user can see, and the part that
    must not survive regardless of what happens to Git or the files. Only
    after that is root removed with rmtree. Anything left over is expected
    on Windows, not a failure the user must act on: it is scheduled for
    removal at the next sign-in via a HKCU RunOnce entry (no elevation
    needed) and reported informationally rather than as an error.

    The repository's .gitattributes is deliberately left alone: it is under
    version control and belongs to the project, not to this tool.
    """

    failed: list[str] = []
    try:
        failed.extend(remove_global_attributes(runner))
        failed.extend(unset_git_config(runner))
    except OSError:
        failed.append(_GIT_UNAVAILABLE)
    failed.extend(unregister_windows_integration(registry))

    shutil.rmtree(root, ignore_errors=True)

    leftovers: tuple[str, ...] = ()
    if root.exists():
        leftovers = tuple(sorted(str(item) for item in root.rglob("*")))
        if _schedule_delayed_removal(registry, root):
            report(
                "MXL Merge Tool удалён. Часть файлов из\n"
                f"{root}\n"
                "будет удалена автоматически при следующем входе в "
                "систему. Чтобы удалить их прямо сейчас, можно удалить эту "
                "папку вручную."
            )
        else:
            report(
                "MXL Merge Tool удалён. Часть файлов из\n"
                f"{root}\n"
                "не удалось удалить автоматически — удалите эту папку "
                "вручную."
            )

    return UninstallResult(failed_keys=tuple(failed), leftover_paths=leftovers)


class WindowsRegistryWriter:
    """Real HKCU writer. Exercised by the manual Windows checklist."""

    def set_value(self, key: str, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as handle:
            winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)

    def set_dword(self, key: str, name: str, value: int) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as handle:
            winreg.SetValueEx(handle, name, 0, winreg.REG_DWORD, value)

    def delete_tree(self, key: str) -> bool:
        """Delete a key and its children. False when something survived.

        A key with children cannot be removed directly, so children go first.
        Failures are reported rather than swallowed: an uninstall that leaves
        the context menu registered must not claim success.
        """

        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                children: list[str] = []
                index = 0
                while True:
                    try:
                        children.append(winreg.EnumKey(handle, index))
                    except OSError:
                        break
                    index += 1
        except FileNotFoundError:
            return True
        except OSError:
            return False
        for child in children:
            if not self.delete_tree(key + "\\" + child):
                return False
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True
