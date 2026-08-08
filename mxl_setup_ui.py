"""Setup screen logic for the Windows installation.

The view is a native tkinter window (``mxl_setup_gui.py``); this module holds
everything that can be exercised without a display: the pure data-shaping
functions the window renders (dropdown contents, the batch-capability note,
manual-path resolution, the install-button state) and the non-UI logic that
must survive any presentation layer (copy-then-relaunch, the Git-attributes
verifier, the installer itself).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import mxl_setup
import mxl_subprocess
from mxl_setup import Discovery, OnecCandidate

# Sentinel value carried by the "type a path" option in each dropdown; never
# a real path, so it can't collide with a discovered candidate.
MANUAL_PATH = "__manual__"

CLIENT_NONE_LABEL = "Не использовать 1С"
EDITOR_NONE_LABEL = "Не использовать"
MANUAL_LABEL = "Указать путь вручную…"


@dataclass(frozen=True)
class DropdownOption:
    """One entry of a candidate dropdown: what is shown, and what it means."""

    label: str
    value: str  # "" for "not used", MANUAL_PATH, or a candidate path


@dataclass(frozen=True)
class SetupFormView:
    """Everything the window needs to render the form, from one Discovery."""

    client_options: tuple[DropdownOption, ...]
    client_default: str
    editor_options: tuple[DropdownOption, ...]
    editor_default: str
    git_note: str
    install_enabled: bool


def candidate_label(candidate: OnecCandidate) -> str:
    if candidate.version_text:
        return f"{candidate.version_text} — {candidate.path}"
    return str(candidate.path)


def dropdown_options(
    candidates: Sequence[OnecCandidate], none_label: str
) -> tuple[DropdownOption, ...]:
    """Build one dropdown's contents: the "not used" option first, the
    manual-path option last, discovered candidates in between, in the order
    they were given (callers pass them newest-first)."""

    options = [DropdownOption(none_label, "")]
    options.extend(
        DropdownOption(candidate_label(item), str(item.path)) for item in candidates
    )
    options.append(DropdownOption(MANUAL_LABEL, MANUAL_PATH))
    return tuple(options)


def default_dropdown_value(candidates: Sequence[OnecCandidate]) -> str:
    """The newest candidate's path, or the "not used" option when there is none."""

    return str(candidates[0].path) if candidates else ""


def client_note(candidates: Sequence[OnecCandidate], selected_value: str) -> str:
    """The warning shown under the client dropdown, or "" when none applies."""

    for item in candidates:
        if str(item.path) == selected_value:
            if not item.batch_capable:
                return (
                    "Рядом с выбранным клиентом нет 1cv8.exe — пакетный "
                    "предпросмотр будет недоступен"
                )
            return ""
    return ""


def resolve_selected_path(value: str, manual_text: str) -> str | None:
    """Turn a dropdown's raw selection into the path the installer should use."""

    if value == MANUAL_PATH:
        stripped = manual_text.strip()
        return stripped or None
    return value or None


def install_enabled(discovery: Discovery) -> bool:
    return discovery.git is not None


def git_missing_note(discovery: Discovery) -> str:
    if discovery.git is None:
        return "Git не найден. Установите Git и запустите установку заново"
    return ""


def build_setup_form(discovery: Discovery) -> SetupFormView:
    return SetupFormView(
        client_options=dropdown_options(discovery.clients, CLIENT_NONE_LABEL),
        client_default=default_dropdown_value(discovery.clients),
        editor_options=dropdown_options(discovery.file_editors, EDITOR_NONE_LABEL),
        editor_default=default_dropdown_value(discovery.file_editors),
        git_note=git_missing_note(discovery),
        install_enabled=install_enabled(discovery),
    )


def _verify_git_attributes() -> tuple[bool, str]:
    """Ask Git what it would do with a .mxl file.

    check-attr only reports anything meaningful inside a Git work tree, and
    the caller's cwd (the install directory, or wherever run_setup happens to
    be launched from) is not one. So a throwaway repository is created here:
    a freshly initialised repo still honours the global
    core.attributesFile that ``install_git_config --global`` writes, which is
    exactly what this check needs to prove.
    """

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        repo = Path(raw)
        init = mxl_subprocess.run(
            ["git", "init", "-q"],
            cwd=str(repo),
            check=False,
            capture_output=True,
            text=True,
        )
        if init.returncode != 0:
            return False, "Не удалось запустить Git для проверки"
        probe = repo / "probe.mxl"
        probe.write_bytes(b"")
        result = mxl_subprocess.run(
            ["git", "check-attr", "diff", "merge", "--", "probe.mxl"],
            cwd=str(repo),
            check=False,
            capture_output=True,
            text=True,
        )
    output = result.stdout
    ok = "diff: mxl" in output and "merge: mxl" in output
    message = (
        "Git применяет драйверы mxl к файлам .mxl"
        if ok
        else "Git не применяет драйверы mxl. Вывод: " + (output.strip() or "пусто")
    )
    return ok, message


def _require_existing(label: str, value: str | None) -> None:
    """Reject a path typed by hand that is not there.

    The window shows the message and returns the user to the form. Without
    this a typo is stored happily and only surfaces much later, as a failed
    1C launch during a merge.
    """

    if value and not Path(value).exists():
        raise RuntimeError(f"{label} не найден: {value}")


def _make_installer(target: Path, environment: mxl_setup.Environment) -> Any:
    import mxl_tool

    def install(onec_client: str | None, onec_file_editor: str | None) -> list[str]:
        _require_existing("Указанный путь к тонкому клиенту 1С", onec_client)
        _require_existing("Указанный путь к внешнему редактору", onec_file_editor)
        code = mxl_tool.install_git_config(
            onec_client=onec_client,
            onec_infobase=None,
            onec_epf=None,
            onec_username=None,
            onec_batch_capable=None,
            global_install=True,
            onec_file_editor=onec_file_editor,
        )
        if code != 0:
            raise RuntimeError(f"install_git_config вернул код {code}")
        mxl_setup.register_windows_integration(
            mxl_setup.WindowsRegistryWriter(),
            target / mxl_setup.LAUNCHER_NAME,
            target / "app" / "mxl.ico",
        )
        mxl_setup.register_start_menu(
            mxl_setup.WindowsShortcutWriter(),
            Path(mxl_setup.start_menu_dir(environment)),
            target / mxl_setup.LAUNCHER_NAME,
            target / "app" / "mxl.ico",
        )
        summary = [
            "Драйверы diff и merge настроены для всех репозиториев",
            "Пункт контекстного меню зарегистрирован",
            "Ярлыки добавлены в меню «Пуск»",
            "Добавлена запись в «Программы и компоненты»",
        ]
        if onec_client:
            summary.append(f"Предпросмотр через 1С: {onec_client}")
        if onec_file_editor:
            summary.append(f"Внешний редактор: {onec_file_editor}")
        return summary

    return install


def run_setup(source_root: Path, installed: bool = False) -> int:
    """Copy the payload when needed, then show the setup window.

    Everything is wrapped: under pythonw.exe there is no stream to print a
    traceback to, so an unreported failure means the user double-clicks the
    icon and nothing whatsoever happens.
    """

    try:
        return _run_setup(source_root, installed)
    except Exception as error:
        mxl_setup.report(
            "Не удалось выполнить настройку MXL Merge Tool.\n\n"
            f"{type(error).__name__}: {error}\n\n"
            "Если программа уже запущена, закройте её и попробуйте снова.",
            error=True,
        )
        return 1


def _run_setup(source_root: Path, installed: bool) -> int:
    """Copy the payload into %LOCALAPPDATA%, or open the setup window.

    When the launcher is started from the unpacked archive, the payload is
    copied and the installed copy takes over, carrying --installed so the
    decision is never re-derived from path text: behind a junction, a subst
    drive or a short name, comparing paths would copy the install over itself
    and relaunch forever, with no window to show it.
    """

    env = mxl_setup.WindowsEnvironment()
    target = Path(mxl_setup.install_dir(env))
    if not installed and not mxl_setup.is_installed_copy(env, source_root):
        launcher = mxl_setup.copy_payload(source_root, target)
        # Prune before relaunching, while this process still runs from the
        # unpacked archive: nothing under the install root is our own image
        # yet, so an older version cannot be holding its own files open.
        mxl_setup.prune_old_versions(Path(mxl_setup.install_root(env)))
        mxl_subprocess.popen([str(launcher), "setup-gui", "--installed"], cwd=str(target))
        return 0

    try:
        import tkinter  # noqa: F401  (presence check only; see except below)
    except ImportError:
        # The official embeddable Python does not bundle tkinter, and a
        # separate task adds it to the distributed archive — so a build that
        # missed that step, or a hand-rolled Python next to the app files,
        # can genuinely land here. There is no window to show that fact in,
        # so it goes through report() like every other pythonw.exe failure,
        # and points at the one path that still works: a console run.
        mxl_setup.report(
            "В этой копии Python не установлен модуль tkinter, поэтому "
            "окно установки недоступно.\n\n"
            "Запустите настройку из командной строки:\n"
            "runtime\\python.exe app\\mxl_tool.py setup-gui",
            error=True,
        )
        return 1

    try:
        from tools.mxl_merge.mxl_setup_gui import run_setup_window
    except ModuleNotFoundError:
        from mxl_setup_gui import run_setup_window  # type: ignore[no-redef]

    discovery = mxl_setup.discover(env)
    run_setup_window(discovery, _make_installer(target, env), _verify_git_attributes)
    return 0


def run_repo_setup(directory: str) -> int:
    """Add .gitattributes to one repository. Reached from the context menu.

    Both outcomes go through a message box: the context menu verb runs under
    pythonw.exe, where print reaches nobody, and the user cannot otherwise
    tell success from "this folder is not a Git work tree".
    """

    import sys

    import mxl_tool

    result = mxl_subprocess.run(
        ["git", "-C", directory, "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Git fails here for several unrelated reasons: the folder really is
        # not a work tree, or it is one Git refuses to touch — dubious
        # ownership on a network share is the common case. Reporting only our
        # own guess hid the real cause from a user whose folder plainly had a
        # .git directory, so Git's own words go into the message.
        details = (result.stderr or "").strip() or (result.stdout or "").strip()
        message = f"Не удалось прочитать репозиторий в {directory}"
        if details:
            message += f"\n\nGit сообщает:\n{details}"
        print(f"mxl-tool: {message}", file=sys.stderr)
        mxl_setup.report(message, error=True)
        return 2
    root = Path(result.stdout.strip())
    mxl_tool._ensure_attributes_file(root / ".gitattributes")
    message = f"Файл .gitattributes в {root} обновлён"
    print(message)
    mxl_setup.report(message)
    return 0
