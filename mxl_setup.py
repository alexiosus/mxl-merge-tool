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
_MB_YESNO = 0x4
_MB_ICONERROR = 0x10
_MB_ICONWARNING = 0x30
_MB_ICONINFORMATION = 0x40
_MB_DEFBUTTON2 = 0x100
_IDYES = 6

_SHCNE_RENAMEITEM = 0x00000001
_SHCNE_RMDIR = 0x00000010
_SHCNE_UPDATEDIR = 0x00001000
_SHCNF_PATHW = 0x0005
_SHCNF_FLUSH = 0x1000


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


def confirm_uninstall() -> bool:
    """Ask before destructive cleanup; No is the safe default button."""

    if sys.platform != "win32":
        return True
    try:
        import ctypes

        result = ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
            None,
            "Удалить MXL Merge Tool?\n\n"
            "Будут удалены настройки Git, ярлыки и файлы программы. "
            "Файлы .gitattributes в ваших репозиториях останутся без изменений.",
            "Удаление MXL Merge Tool",
            _MB_YESNO | _MB_ICONWARNING | _MB_DEFBUTTON2,
        )
        return result == _IDYES
    except Exception:
        # Under pythonw.exe there is no console fallback. If confirmation
        # cannot be shown, refusing to delete is safer than silently
        # proceeding with a destructive operation.
        return False


def notify_shell_change(
    event: int, path: Path, destination: Path | None = None
) -> None:
    """Synchronously tell Explorer about a Start-menu rename or removal."""

    if sys.platform != "win32":
        return
    try:
        import ctypes

        notify = ctypes.windll.shell32.SHChangeNotify  # type: ignore[attr-defined]
        notify.argtypes = [
            ctypes.c_long,
            ctypes.c_uint,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
        ]
        notify.restype = None
        notify(
            event,
            _SHCNF_PATHW | _SHCNF_FLUSH,
            str(path),
            str(destination) if destination is not None else None,
        )
    except Exception:
        # The shortcut itself is already correct on disk. Shell notification
        # is cache synchronisation, so its failure must not roll back an
        # otherwise usable installation.
        return


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

    def roaming_app_data(self) -> PurePath: ...

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
SETTINGS_LAUNCHER_NAME = "MXL Merge Tool Settings.exe"
MAINTENANCE_LAUNCHER_NAME = "MXL Merge Tool Maintenance.exe"
PAYLOAD_ENTRIES = ("runtime", "app", LAUNCHER_NAME, "README.txt")
START_MENU_FOLDER_NAME = "MXL Merge Tool"
SETUP_SHORTCUT_NAME = "Настройка MXL Merge Tool.lnk"
UNINSTALL_SHORTCUT_NAME = "Удаление MXL Merge Tool.lnk"
SETTINGS_APP_USER_MODEL_ID = "MxlMergeTool.Desktop.Settings"
MAINTENANCE_APP_USER_MODEL_ID = "MxlMergeTool.Desktop.Maintenance"


def install_root(env: Environment) -> PurePath:
    return env.local_app_data() / APP_NAME


def install_dir(env: Environment) -> PurePath:
    return install_root(env) / APP_VERSION


def launcher_path(env: Environment) -> PurePath:
    return install_dir(env) / LAUNCHER_NAME


def start_menu_dir(env: Environment) -> PurePath:
    """Per-user Start-menu folder; creating it never needs elevation."""

    return (
        env.roaming_app_data()
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / START_MENU_FOLDER_NAME
    )


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
    launcher = target / LAUNCHER_NAME
    if launcher.exists():
        # Windows 11 collapses Start-menu shortcuts that target the same
        # executable, even when their arguments differ. Two byte-identical
        # launcher aliases give Settings and Maintenance stable, distinct
        # Shell identities while keeping all dispatch logic in one binary.
        for alias in (SETTINGS_LAUNCHER_NAME, MAINTENANCE_LAUNCHER_NAME):
            shutil.copy2(launcher, target / alias)
    return launcher


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

    def roaming_app_data(self) -> PurePath:
        import os

        value = os.environ.get("APPDATA")
        if not value:
            raise RuntimeError("APPDATA is not set; cannot locate the Start menu")
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


@dataclass(frozen=True)
class Shortcut:
    path: Path
    target: Path
    arguments: str
    icon: Path
    working_directory: Path
    description: str
    app_user_model_id: str


class ShortcutWriter(Protocol):
    """Creates and removes per-user Windows Start-menu shortcuts."""

    def create(self, shortcut: Shortcut) -> None: ...

    def delete_directory(self, path: Path) -> bool: ...


def start_menu_shortcuts(
    directory: Path, launcher: Path, icon: Path
) -> tuple[Shortcut, Shortcut]:
    settings_launcher = launcher.with_name(SETTINGS_LAUNCHER_NAME)
    maintenance_launcher = launcher.with_name(MAINTENANCE_LAUNCHER_NAME)
    return (
        Shortcut(
            path=directory / SETUP_SHORTCUT_NAME,
            target=settings_launcher,
            arguments="setup-gui --installed",
            icon=icon,
            working_directory=settings_launcher.parent,
            description="Настройка MXL Merge Tool",
            app_user_model_id=SETTINGS_APP_USER_MODEL_ID,
        ),
        Shortcut(
            path=directory / UNINSTALL_SHORTCUT_NAME,
            target=maintenance_launcher,
            arguments="uninstall",
            icon=icon,
            working_directory=maintenance_launcher.parent,
            description="Удаление MXL Merge Tool",
            app_user_model_id=MAINTENANCE_APP_USER_MODEL_ID,
        ),
    )


def set_shortcut_app_user_model_id(path: Path, app_id: str) -> None:
    """Write System.AppUserModel.ID into an existing .lnk via Shell COM.

    WScript.Shell can create a shortcut but does not expose its IPropertyStore.
    Windows 11 otherwise assigns both of our launch modes the same heuristic
    identity and collapses them into one Start-menu entry. The distribution is
    amd64-only, so a 24-byte PROPVARIANT buffer matches its native ABI.
    """

    if sys.platform != "win32":
        return

    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [
            ("data1", ctypes.c_uint32),
            ("data2", ctypes.c_uint16),
            ("data3", ctypes.c_uint16),
            ("data4", ctypes.c_ubyte * 8),
        ]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]

    def guid(value: str) -> GUID:
        return GUID.from_buffer_copy(uuid.UUID(value).bytes_le)

    hresult = ctypes.c_int32
    shell_link: ctypes.c_void_p | None = ctypes.c_void_p()
    persist_file: ctypes.c_void_p | None = ctypes.c_void_p()
    property_store: ctypes.c_void_p | None = ctypes.c_void_p()
    must_uninitialize = False

    class PROPVARIANT_VALUE(ctypes.Union):
        _fields_ = [
            ("pwsz_val", ctypes.c_wchar_p),
            ("_storage", ctypes.c_byte * 16),
        ]

    class PROPVARIANT(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = [
            ("vt", ctypes.c_ushort),
            ("reserved1", ctypes.c_ushort),
            ("reserved2", ctypes.c_ushort),
            ("reserved3", ctypes.c_ushort),
            ("value", PROPVARIANT_VALUE),
        ]

    # VT_LPWSTR stores a pointer at offset 8. Keep the Python-owned buffer
    # alive until SetValue has copied it; this avoids relying on the optional
    # InitPropVariantFromString export, which is absent on some Windows builds.
    app_id_buffer = ctypes.create_unicode_buffer(app_id)
    variant = PROPVARIANT()
    variant.vt = 31  # VT_LPWSTR
    variant.pwsz_val = ctypes.cast(app_id_buffer, ctypes.c_wchar_p)

    def check(result: int, operation: str) -> None:
        if result < 0:
            code = result & 0xFFFFFFFF
            raise OSError(f"{operation}: HRESULT 0x{code:08X}")

    def method(
        interface: ctypes.c_void_p,
        index: int,
        result_type: object,
        *argument_types: object,
    ) -> object:
        table = ctypes.cast(
            interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        prototype = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
            result_type, ctypes.c_void_p, *argument_types
        )
        return prototype(table[index])

    ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
    clsid_shell_link = guid("00021401-0000-0000-C000-000000000046")
    iid_shell_link_w = guid("000214F9-0000-0000-C000-000000000046")
    iid_persist_file = guid("0000010B-0000-0000-C000-000000000046")
    iid_property_store = guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")
    app_id_key = PROPERTYKEY(
        guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5
    )

    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = hresult
    initialized = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    changed_mode = ctypes.c_int32(0x80010106).value
    if initialized == changed_mode:
        # COM is already initialised in another mode on this thread; that is
        # sufficient for the in-process ShellLink object, but must not be
        # balanced with CoUninitialize by us.
        initialized = 0
    else:
        check(initialized, "CoInitializeEx")
        must_uninitialize = True

    try:
        ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(GUID),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        ole32.CoCreateInstance.restype = hresult
        check(
            ole32.CoCreateInstance(
                ctypes.byref(clsid_shell_link),
                None,
                1,  # CLSCTX_INPROC_SERVER
                ctypes.byref(iid_shell_link_w),
                ctypes.byref(shell_link),
            ),
            "CoCreateInstance(CLSID_ShellLink)",
        )

        query_interface = method(
            shell_link,
            0,
            hresult,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )
        check(
            query_interface(
                shell_link, ctypes.byref(iid_persist_file), ctypes.byref(persist_file)
            ),
            "QueryInterface(IPersistFile)",
        )
        load = method(
            persist_file, 5, hresult, ctypes.c_wchar_p, ctypes.c_uint32
        )
        check(load(persist_file, str(path), 2), "IPersistFile.Load")

        check(
            query_interface(
                shell_link,
                ctypes.byref(iid_property_store),
                ctypes.byref(property_store),
            ),
            "QueryInterface(IPropertyStore)",
        )

        set_value = method(
            property_store,
            6,
            hresult,
            ctypes.POINTER(PROPERTYKEY),
            ctypes.c_void_p,
        )
        check(
            set_value(
                property_store, ctypes.byref(app_id_key), ctypes.byref(variant)
            ),
            "IPropertyStore.SetValue(System.AppUserModel.ID)",
        )
        commit = method(property_store, 7, hresult)
        check(commit(property_store), "IPropertyStore.Commit")

        save = method(persist_file, 6, hresult, ctypes.c_wchar_p, ctypes.c_int)
        check(save(persist_file, str(path), 1), "IPersistFile.Save")
    finally:
        for interface in (property_store, persist_file, shell_link):
            if interface and interface.value:
                release = method(interface, 2, ctypes.c_uint32)
                release(interface)
        if must_uninitialize:
            ole32.CoUninitialize()


def register_start_menu(
    writer: ShortcutWriter, directory: Path, launcher: Path, icon: Path
) -> None:
    """Create both product-owned shortcuts, leaving no half-created folder."""

    try:
        for shortcut in start_menu_shortcuts(directory, launcher, icon):
            writer.create(shortcut)
    except Exception:
        try:
            writer.delete_directory(directory)
        except OSError:
            pass
        raise


def unregister_start_menu(writer: ShortcutWriter, directory: Path) -> tuple[str, ...]:
    return () if writer.delete_directory(directory) else (str(directory),)


_POWERSHELL_SHORTCUT_SCRIPT = """
$ErrorActionPreference = 'Stop'
try {
    $shellType = [type]::GetTypeFromProgID('WScript.Shell')
    $shell = [Activator]::CreateInstance($shellType)
    $shortcut = $shell.CreateShortcut($env:MXL_MERGE_SHORTCUT_PATH)
    $shortcut.TargetPath = $env:MXL_MERGE_SHORTCUT_TARGET
    $shortcut.Arguments = $env:MXL_MERGE_SHORTCUT_ARGUMENTS
    $shortcut.IconLocation = $env:MXL_MERGE_SHORTCUT_ICON
    $shortcut.WorkingDirectory = $env:MXL_MERGE_SHORTCUT_WORKING_DIRECTORY
    $shortcut.Description = $env:MXL_MERGE_SHORTCUT_DESCRIPTION
    $shortcut.Save()
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
""".strip()


class WindowsShortcutWriter:
    """Create Shell Link files through Windows' built-in WScript COM object."""

    def create(self, shortcut: Shortcut) -> None:
        import base64
        import os
        import tempfile

        shortcut.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(
            prefix="mxl-shortcut-", suffix=".lnk", dir=shortcut.path.parent
        )
        os.close(handle)
        temporary_path = Path(temporary_name)
        temporary_path.unlink()
        environment = os.environ.copy()
        environment.update(
            {
                # WScript.Shell on Windows PowerShell 5.1 can fail in Save()
                # when the .lnk filename contains Cyrillic. Save under a
                # generated ASCII name first; os.replace below uses Windows'
                # Unicode filesystem API to give it the requested display
                # name afterwards.
                "MXL_MERGE_SHORTCUT_PATH": str(temporary_path),
                "MXL_MERGE_SHORTCUT_TARGET": str(shortcut.target),
                "MXL_MERGE_SHORTCUT_ARGUMENTS": shortcut.arguments,
                "MXL_MERGE_SHORTCUT_ICON": str(shortcut.icon),
                "MXL_MERGE_SHORTCUT_WORKING_DIRECTORY": str(
                    shortcut.working_directory
                ),
                "MXL_MERGE_SHORTCUT_DESCRIPTION": shortcut.description,
            }
        )
        # powershell.exe treats everything after -Command as source text, not
        # as one ordinary argv item. Python's Windows argv quoting can therefore
        # turn a valid multiline script into a parse error. EncodedCommand is
        # explicitly designed for native-process callers and preserves the
        # UTF-16 script byte for byte, including its Cyrillic descriptions.
        encoded_script = base64.b64encode(
            _POWERSHELL_SHORTCUT_SCRIPT.encode("utf-16-le")
        ).decode("ascii")
        try:
            result = mxl_subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-EncodedCommand",
                    encoded_script,
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            if result.returncode != 0:
                details = (result.stderr or result.stdout or "").strip()
                message = f"Не удалось создать ярлык «{shortcut.path.name}»"
                if details:
                    message += f": {details}"
                raise RuntimeError(message)
            os.replace(temporary_path, shortcut.path)
            set_shortcut_app_user_model_id(
                shortcut.path, shortcut.app_user_model_id
            )
            notify_shell_change(_SHCNE_RENAMEITEM, temporary_path, shortcut.path)
            notify_shell_change(_SHCNE_UPDATEDIR, shortcut.path.parent)
        finally:
            temporary_path.unlink(missing_ok=True)

    def delete_directory(self, path: Path) -> bool:
        existed = path.exists()
        shutil.rmtree(path, ignore_errors=True)
        removed = not path.exists()
        if existed and removed:
            notify_shell_change(_SHCNE_RMDIR, path)
            notify_shell_change(_SHCNE_UPDATEDIR, path.parent)
        return removed


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

    failed_keys covers Git configuration keys, registry keys and integration
    paths that could not be removed, plus a marker when git itself could not
    be run at all — genuine failures the user must act on. leftover_paths
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
    registry: RegistryWriter,
    runner: GitRunner,
    root: Path,
    *,
    shortcuts: ShortcutWriter | None = None,
    start_menu: Path | None = None,
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
    must not survive regardless of what happens to Git or the files. The
    Start-menu shortcuts are removed alongside those keys. Only after that is
    root removed with rmtree. Anything left over is expected
    on Windows, not a failure the user must act on: it is scheduled for
    removal at the next sign-in via a HKCU RunOnce entry (no elevation
    needed) and reported informationally rather than as an error.

    The repository's .gitattributes is deliberately left alone: it is under
    version control and belongs to the project, not to this tool.
    """

    failed: list[str] = []
    if (shortcuts is None) != (start_menu is None):
        raise ValueError("shortcuts and start_menu must be provided together")
    try:
        failed.extend(remove_global_attributes(runner))
        failed.extend(unset_git_config(runner))
    except OSError:
        failed.append(_GIT_UNAVAILABLE)
    failed.extend(unregister_windows_integration(registry))
    if shortcuts is not None and start_menu is not None:
        failed.extend(unregister_start_menu(shortcuts, start_menu))

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
