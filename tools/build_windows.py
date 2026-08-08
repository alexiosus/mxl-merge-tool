#!/usr/bin/env python3
"""Build the Windows distribution archive.

Runs on macOS or Linux. The launcher is compiled with mingw-w64; without a
compiler the script still produces a working archive that uses a .cmd file
instead, marked as a development build.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mxl_setup import APP_VERSION  # noqa: E402  (needs REPO_ROOT on sys.path)

# Windows resources want four components; APP_VERSION carries three.
_VERSION_PARTS = (APP_VERSION.split(".") + ["0", "0", "0", "0"])[:4]
VERSION_DOTTED = ".".join(_VERSION_PARTS)
VERSION_COMMA = ",".join(_VERSION_PARTS)
PYTHON_VERSION = "3.12.8"
PYTHON_ARCHIVE = f"python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/{PYTHON_ARCHIVE}"
# Taken from the official release page. Never edit without re-checking there.
PYTHON_SHA256 = "8d3f33be9eb810f23c102f08475af2854e50484b8e4e06275e937be61ce3d2fb"

# The embeddable package above has no tkinter. The per-component MSI does;
# python.org publishes it but its release page does not list a checksum for
# it (only for the embeddable/installer archives), so unlike PYTHON_SHA256
# this sum cannot be cross-checked against a published value. Provenance is
# weaker: downloaded from python.org over TLS, hash pinned thereafter.
TCLTK_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/amd64/tcltk.msi"
TCLTK_SHA256 = "bc24775a633b4056ca8f9f47b69feddf5c5a910a348e11e2b1ad98c3dae12900"
MSIEXTRACT = "msiextract"

APP_FILES = (
    "mxl_tool.py",
    "mxl_ui.py",
    "mxl_onec.py",
    "mxl_preview.py",
    "mxl_setup.py",
    "mxl_setup_ui.py",
    "mxl_setup_gui.py",
    "mxl_html.py",
    "mxl_subprocess.py",
    "ui.html",
    "mxl.ico",
)
ONEC_FILES = ("MxlToHtml.epf", "MxlRendererTemplate.dt")
LAUNCHER_NAME = "MXL merge tool.exe"
COMPILER = "x86_64-w64-mingw32-gcc"
RESOURCE_COMPILER = "x86_64-w64-mingw32-windres"

README_TEXT = """MXL Merge Tool {version}

Установка: двойной клик по «{launcher}».

Если запуск заблокирован политиками, откройте командную строку в этой папке
и выполните:

    runtime\\python.exe app\\mxl_tool.py setup-gui
"""

DEV_CMD_TEXT = (
    "@echo off\r\n"
    'start "" "%~dp0runtime\\pythonw.exe" "%~dp0app\\mxl_tool.py" setup-gui\r\n'
)


def download_python(cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / PYTHON_ARCHIVE
    if not archive.exists():
        print(f"Скачиваю {PYTHON_URL}")
        with urllib.request.urlopen(PYTHON_URL) as response:
            archive.write_bytes(response.read())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if PYTHON_SHA256 == "PASTE_THE_OFFICIAL_SHA256_HERE":
        raise SystemExit(
            f"Заполните PYTHON_SHA256. Сумма скачанного файла: {digest}\n"
            "Сверьте её со страницей релиза python.org, прежде чем вписывать."
        )
    if digest != PYTHON_SHA256:
        raise SystemExit(f"Контрольная сумма не совпала: {digest}")
    return archive


def build_runtime(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        source.extractall(target)
    pth = next(target.glob("python*._pth"))
    lines = pth.read_text(encoding="utf-8").splitlines()
    if "..\\app" not in lines:
        lines.append("..\\app")
    if "Lib" not in lines:
        lines.append("Lib")
    pth.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


def download_tcltk(cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / "tcltk.msi"
    if not archive.exists():
        print(f"Скачиваю {TCLTK_URL}")
        with urllib.request.urlopen(TCLTK_URL) as response:
            archive.write_bytes(response.read())
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if digest != TCLTK_SHA256:
        raise SystemExit(f"Контрольная сумма tcltk.msi не совпала: {digest}")
    return archive


def build_tkinter(archive: Path, work: Path, runtime: Path) -> None:
    """Copy tkinter/Tcl/Tk out of the tcltk.msi into the embeddable runtime.

    Takes only what mxl_setup_gui.py needs: the extension module, the three
    DLLs it links against, the tkinter package, and the Tcl/Tk script
    libraries. Skips IDLE, turtledemo, import libraries and Tix -- about
    4.2 MB this tool never uses.
    """

    if shutil.which(MSIEXTRACT) is None:
        raise SystemExit(
            "Не найден msiextract. Установите: brew install msitools"
        )
    extracted = work / "tcltk"
    shutil.rmtree(extracted, ignore_errors=True)
    extracted.mkdir(parents=True)
    subprocess.run([MSIEXTRACT, "-C", str(extracted), str(archive)], check=True)

    shutil.copy2(extracted / "DLLs" / "_tkinter.pyd", runtime / "_tkinter.pyd")
    for name in ("tcl86t.dll", "tk86t.dll", "zlib1.dll"):
        shutil.copy2(extracted / "DLLs" / name, runtime / name)

    lib_tkinter = runtime / "Lib" / "tkinter"
    shutil.rmtree(lib_tkinter, ignore_errors=True)
    shutil.copytree(extracted / "Lib" / "tkinter", lib_tkinter)

    tcl_dir = runtime / "tcl"
    tcl_dir.mkdir(exist_ok=True)
    for name in ("tcl8.6", "tk8.6", "tcl8"):
        destination = tcl_dir / name
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(extracted / "tcl" / name, destination)


def build_app(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in APP_FILES:
        shutil.copy2(REPO_ROOT / name, target / name)
    onec = target / "onec"
    onec.mkdir(exist_ok=True)
    for name in ONEC_FILES:
        shutil.copy2(REPO_ROOT / "onec" / name, onec / name)


def build_launcher(work: Path, target: Path) -> bool:
    """Compile the launcher. Returns False when no compiler is available."""

    if shutil.which(COMPILER) is None or shutil.which(RESOURCE_COMPILER) is None:
        return False
    staging = work / "launcher"
    staging.mkdir(parents=True, exist_ok=True)
    for name in ("launcher.rc", "launcher.manifest"):
        source = (REPO_ROOT / "tools" / "launcher" / name).read_text(encoding="utf-8")
        substituted = source.replace("@VERSION_DOTTED@", VERSION_DOTTED).replace(
            "@VERSION_COMMA@", VERSION_COMMA
        )
        if "@VERSION_" in substituted:
            raise SystemExit(f"{name}: незаполненный placeholder версии")
        (staging / name).write_text(substituted, encoding="utf-8")
    shutil.copy2(REPO_ROOT / "mxl.ico", staging / "mxl.ico")
    resource = staging / "launcher.res"
    subprocess.run(
        [
            RESOURCE_COMPILER,
            str(staging / "launcher.rc"),
            "-O",
            "coff",
            "-o",
            str(resource),
        ],
        check=True,
    )
    subprocess.run(
        [
            COMPILER,
            "-municode",
            "-mwindows",
            "-O2",
            "-static",
            "-o",
            str(target / LAUNCHER_NAME),
            str(REPO_ROOT / "tools" / "launcher" / "launcher.c"),
            str(resource),
        ],
        check=True,
    )
    return True


def build(output: Path, cache: Path) -> Path:
    work = output / "work"
    shutil.rmtree(work, ignore_errors=True)
    payload = work / "mxl-merge-tool"
    payload.mkdir(parents=True)

    build_runtime(download_python(cache), payload / "runtime")
    build_tkinter(download_tcltk(cache), work, payload / "runtime")
    build_app(payload / "app")
    compiled = build_launcher(work, payload)
    if not compiled:
        print("Компилятор mingw не найден: собираю dev-архив с Настроить.cmd")
        (payload / "Настроить.cmd").write_text(DEV_CMD_TEXT, encoding="cp866")
    (payload / "README.txt").write_text(
        README_TEXT.format(version=APP_VERSION, launcher=LAUNCHER_NAME),
        encoding="utf-8",
    )

    suffix = "" if compiled else "-dev"
    archive = output / f"mxl-merge-tool-{APP_VERSION}-win64{suffix}.zip"
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for item in sorted(payload.rglob("*")):
            bundle.write(item, item.relative_to(work))

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = archive.with_name(archive.name + ".sha256")
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"Готово: {archive}")
    print(f"SHA-256: {digest}")
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(REPO_ROOT / "dist"))
    parser.add_argument("--cache", default=str(REPO_ROOT / "build" / "cache"))
    arguments = parser.parse_args()
    output = Path(arguments.output)
    output.mkdir(parents=True, exist_ok=True)
    build(output, Path(arguments.cache))
    return 0


if __name__ == "__main__":
    sys.exit(main())
