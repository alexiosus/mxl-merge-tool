"""Native tkinter setup window for the Windows installer.

Ported from the browser page this replaces (formerly ``setup.html``): same
two dropdowns, same manual-path entry, same install/verify/close flow, same
Russian copy — just drawn with tkinter instead of served over HTTP to a
browser tab. Kept thin: everything that decides *what* to show lives in
``mxl_setup_ui.py`` as plain functions over a ``Discovery``, so only the
"how it's drawn" part is here, and only that part is untestable headlessly.

The embeddable Windows distribution bundles tkinter itself (see
``tools/build_windows.py``): ``_tkinter.pyd`` plus ``tcl86t.dll``/
``tk86t.dll`` beside ``python.exe``, the ``tkinter`` package under
``runtime/Lib``, and the Tcl/Tk script libraries under ``runtime/tcl``.
That layout is flatter than a normal Python install, so before ``tkinter``
is imported below, ``configure_tcl_tk_environment()`` points ``TCL_LIBRARY``
and ``TK_LIBRARY`` at the bundled script directories -- Tcl's own
relative-to-DLL lookup is not guaranteed to find them there. It is a no-op
off Windows and when the bundled directories are absent, so running from
source is unaffected.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, MutableMapping, Sequence


def _tcl_tk_candidate_dirs() -> tuple[Path, ...]:
    """Directories that might hold the bundled ``tcl/`` script libraries.

    The embeddable layout sets ``sys.prefix`` to the runtime directory
    (where ``python.exe`` lives), which is where the build script places
    ``tcl/``. The interpreter's own directory is included as a fallback in
    case ``sys.prefix`` ever diverges from it.
    """

    seen: list[Path] = []
    for raw in (sys.prefix, str(Path(sys.executable).resolve().parent)):
        candidate = Path(raw)
        if candidate not in seen:
            seen.append(candidate)
    return tuple(seen)


def resolve_tcl_tk_library_dirs(
    candidate_dirs: Sequence[Path], exists: Callable[[Path], bool]
) -> tuple[Path | None, Path | None]:
    """Find the bundled ``tcl8.6``/``tk8.6`` directories, if present.

    Returns ``(None, None)`` when no candidate base has both -- e.g. when
    running from a source checkout, where Tcl/Tk (if any) comes from a
    system Python install instead of the bundle.
    """

    for base in candidate_dirs:
        tcl_library = base / "tcl" / "tcl8.6"
        tk_library = base / "tcl" / "tk8.6"
        if exists(tcl_library) and exists(tk_library):
            return tcl_library, tk_library
    return None, None


def configure_tcl_tk_environment(
    *,
    platform: str = sys.platform,
    candidate_dirs: Sequence[Path] | None = None,
    exists: Callable[[Path], bool] = lambda path: path.is_dir(),
    environ: MutableMapping[str, str] = os.environ,
) -> None:
    """Point Tcl/Tk at the bundled script libraries before tkinter loads.

    Windows-only: off Windows, Tcl/Tk comes from wherever the running
    interpreter normally gets it, and this must not interfere. Never
    overwrites a value the environment already set, so a developer running
    with their own Tcl/Tk configured -- or from a system Python -- is
    unaffected either way.
    """

    if platform != "win32":
        return
    dirs = _tcl_tk_candidate_dirs() if candidate_dirs is None else candidate_dirs
    tcl_library, tk_library = resolve_tcl_tk_library_dirs(dirs, exists)
    if tcl_library is not None:
        environ.setdefault("TCL_LIBRARY", str(tcl_library))
    if tk_library is not None:
        environ.setdefault("TK_LIBRARY", str(tk_library))


configure_tcl_tk_environment()

# tkinter must be imported after configure_tcl_tk_environment() has had a
# chance to set TCL_LIBRARY/TK_LIBRARY -- Tcl reads them at init time.
import tkinter as tk  # noqa: E402
from tkinter import ttk  # noqa: E402

from mxl_setup import Discovery  # noqa: E402
from mxl_setup_ui import (  # noqa: E402
    MANUAL_PATH,
    DropdownOption,
    build_setup_form,
    client_note,
    resolve_selected_path,
)

WINDOW_TITLE = "Установка MXL Merge Tool"

_OK_COLOR = "#2e7d32"
_ERROR_COLOR = "#c62828"
_WARNING_COLOR = "#b26a00"

_MANUAL_PLACEHOLDER = "Полный путь к исполняемому файлу"


class _Dropdown:
    """A combobox plus the manual-path entry that appears for its last option."""

    def __init__(
        self,
        parent: tk.Widget,
        options: tuple[DropdownOption, ...],
        default_value: str,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.options = options
        self._on_change = on_change

        self.combobox = ttk.Combobox(
            parent, state="readonly", values=[item.label for item in options], width=52
        )
        default_index = next(
            (i for i, item in enumerate(options) if item.value == default_value), 0
        )
        self.combobox.current(default_index)
        self.combobox.bind("<<ComboboxSelected>>", self._handle_change)

        self.manual_entry = ttk.Entry(parent, width=54, foreground="#888888")
        self._manual_placeholder_active = True
        self.manual_entry.insert(0, _MANUAL_PLACEHOLDER)
        self.manual_entry.bind("<FocusIn>", self._clear_placeholder)
        self.manual_entry.bind("<FocusOut>", self._restore_placeholder)

        # Visibility is synced once both widgets are placed by grid_into()
        # below: grid_remove() on a widget that was never grid()-ed yet is a
        # silent no-op, so doing it here (before placement) would not hide
        # anything.

    def grid_into(self, row: int, column: int = 0) -> int:
        """Place the combobox at `row` and the manual entry right below it.

        Returns the next free row, so callers can lay out a form top to
        bottom without hand-tracking two rows per dropdown.
        """

        self.combobox.grid(row=row, column=column, sticky="ew")
        self.manual_entry.grid(row=row + 1, column=column, sticky="ew", pady=(4, 0))
        self._sync_manual_visibility()
        return row + 2

    @property
    def selected_value(self) -> str:
        return self.options[self.combobox.current()].value

    def manual_text(self) -> str:
        return "" if self._manual_placeholder_active else self.manual_entry.get()

    def resolved_path(self) -> str | None:
        return resolve_selected_path(self.selected_value, self.manual_text())

    def set_enabled(self, enabled: bool) -> None:
        self.combobox.configure(state="readonly" if enabled else "disabled")
        self.manual_entry.configure(state="normal" if enabled else "disabled")

    def _clear_placeholder(self, _event: object = None) -> None:
        if self._manual_placeholder_active:
            self.manual_entry.delete(0, tk.END)
            self.manual_entry.configure(foreground="")
            self._manual_placeholder_active = False

    def _restore_placeholder(self, _event: object = None) -> None:
        if not self.manual_entry.get().strip():
            self.manual_entry.delete(0, tk.END)
            self.manual_entry.insert(0, _MANUAL_PLACEHOLDER)
            self.manual_entry.configure(foreground="#888888")
            self._manual_placeholder_active = True

    def _sync_manual_visibility(self) -> None:
        if self.selected_value == MANUAL_PATH:
            self.manual_entry.grid()
            self.manual_entry.focus_set()
        else:
            self.manual_entry.grid_remove()

    def _handle_change(self, _event: object = None) -> None:
        self._sync_manual_visibility()
        if self._on_change:
            self._on_change()


class SetupWindow:
    """Builds and drives the setup window for one Discovery."""

    def __init__(
        self,
        root: tk.Tk,
        discovery: Discovery,
        installer: Any,
        verifier: Any,
    ) -> None:
        self.root = root
        self.discovery = discovery
        self.installer = installer
        self.verifier = verifier
        self.view = build_setup_form(discovery)

        root.title(WINDOW_TITLE)
        root.resizable(False, False)

        self.container = ttk.Frame(root, padding=24)
        self.container.grid(row=0, column=0, sticky="nsew")

        self.form_frame = ttk.Frame(self.container)
        self.done_frame = ttk.Frame(self.container)

        self._build_form(self.form_frame)
        self._build_done(self.done_frame)

        self.form_frame.grid(row=0, column=0, sticky="nsew")
        self.done_frame.grid(row=0, column=0, sticky="nsew")
        self.done_frame.grid_remove()

    # -- form -----------------------------------------------------------

    def _build_form(self, parent: ttk.Frame) -> None:
        row = 0
        ttk.Label(parent, text=WINDOW_TITLE, font=("TkDefaultFont", 13, "bold")).grid(
            row=row, column=0, sticky="w", pady=(0, 16)
        )
        row += 1

        ttk.Label(parent, text="Тонкий клиент 1С").grid(row=row, column=0, sticky="w")
        row += 1
        self.client_dropdown = _Dropdown(
            parent, self.view.client_options, self.view.client_default, self._update_client_note
        )
        row = self.client_dropdown.grid_into(row)
        self.client_note_var = tk.StringVar()
        self.client_note_label = ttk.Label(
            parent, textvariable=self.client_note_var, foreground=_WARNING_COLOR, wraplength=460
        )
        self.client_note_label.grid(row=row, column=0, sticky="w", pady=(4, 0))
        row += 1

        ttk.Label(parent, text="1С:Предприятие — Работа с файлами").grid(
            row=row, column=0, sticky="w", pady=(16, 0)
        )
        row += 1
        self.editor_dropdown = _Dropdown(
            parent, self.view.editor_options, self.view.editor_default
        )
        row = self.editor_dropdown.grid_into(row)

        self.git_note_var = tk.StringVar(value=self.view.git_note)
        ttk.Label(
            parent, textvariable=self.git_note_var, foreground=_ERROR_COLOR, wraplength=460
        ).grid(row=row, column=0, sticky="w", pady=(16, 0))
        row += 1

        self.failure_var = tk.StringVar()
        ttk.Label(
            parent, textvariable=self.failure_var, foreground=_ERROR_COLOR, wraplength=460
        ).grid(row=row, column=0, sticky="w", pady=(8, 0))
        row += 1

        self.busy_var = tk.StringVar()
        ttk.Label(parent, textvariable=self.busy_var).grid(row=row, column=0, sticky="w")
        row += 1

        self.install_button = ttk.Button(
            parent, text="Установить", command=self._handle_install
        )
        self.install_button.grid(row=row, column=0, sticky="w", pady=(16, 0))
        if not self.view.install_enabled:
            self.install_button.configure(state="disabled")

        parent.grid_columnconfigure(0, weight=1)

        self._update_client_note()

    def _update_client_note(self) -> None:
        self.client_note_var.set(
            client_note(self.discovery.clients, self.client_dropdown.selected_value)
        )

    def _set_form_enabled(self, enabled: bool) -> None:
        self.client_dropdown.set_enabled(enabled)
        self.editor_dropdown.set_enabled(enabled)
        can_install = enabled and self.view.install_enabled
        self.install_button.configure(state="normal" if can_install else "disabled")

    # -- done -------------------------------------------------------------

    def _build_done(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text="Готово. Инструмент настроен для всех репозиториев.",
            foreground=_OK_COLOR,
        ).grid(row=0, column=0, sticky="w")

        self.summary_frame = ttk.Frame(parent)
        self.summary_frame.grid(row=1, column=0, sticky="w", pady=(8, 0))

        self.verify_button = ttk.Button(
            parent, text="Проверить", command=self._handle_verify
        )
        self.verify_button.grid(row=2, column=0, sticky="w", pady=(16, 0))

        self.verify_result_var = tk.StringVar()
        self.verify_result_label = ttk.Label(parent, textvariable=self.verify_result_var)
        self.verify_result_label.grid(row=3, column=0, sticky="w", pady=(4, 0))

        self.close_button = ttk.Button(parent, text="Закрыть", command=self._handle_close)
        self.close_button.grid(row=4, column=0, sticky="w", pady=(16, 0))

        parent.grid_columnconfigure(0, weight=1)

    def _show_done(self, summary: list[str]) -> None:
        for child in self.summary_frame.winfo_children():
            child.destroy()
        for index, line in enumerate(summary):
            ttk.Label(self.summary_frame, text=f"• {line}").grid(
                row=index, column=0, sticky="w"
            )
        self.verify_result_var.set("")
        self.form_frame.grid_remove()
        self.done_frame.grid()

    # -- install ------------------------------------------------------------

    def _handle_install(self) -> None:
        self.failure_var.set("")
        self._set_form_enabled(False)
        self.busy_var.set("Настраиваю…")
        client_path = self.client_dropdown.resolved_path()
        editor_path = self.editor_dropdown.resolved_path()

        def worker() -> None:
            try:
                summary = list(self.installer(client_path, editor_path))
                result: tuple[bool, Any] = (True, summary)
            except Exception as error:
                # Shown verbatim: the user is the only one who can act on a
                # failed install, so nothing here is swallowed.
                result = (False, str(error))
            self.root.after(0, lambda: self._apply_install_result(result))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_install_result(self, result: tuple[bool, Any]) -> None:
        self.busy_var.set("")
        ok, payload = result
        if ok:
            self._show_done(payload)
            return
        self._set_form_enabled(True)
        self.failure_var.set(str(payload))

    # -- verify / close -------------------------------------------------

    def _handle_verify(self) -> None:
        self.verify_button.configure(state="disabled")

        def worker() -> None:
            try:
                ok, message = self.verifier()
            except Exception as error:
                ok, message = False, str(error)
            self.root.after(0, lambda: self._apply_verify_result(ok, message))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_verify_result(self, ok: bool, message: str) -> None:
        self.verify_button.configure(state="normal")
        self.verify_result_var.set(message)
        self.verify_result_label.configure(foreground=_OK_COLOR if ok else _ERROR_COLOR)

    def _handle_close(self) -> None:
        self.root.destroy()


def run_setup_window(discovery: Discovery, installer: Any, verifier: Any) -> None:
    root = tk.Tk()
    SetupWindow(root, discovery, installer, verifier)
    root.mainloop()
