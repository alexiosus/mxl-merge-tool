"""1C:Enterprise-backed MXL to HTML renderer."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


DEFAULT_RENDER_TIMEOUT_SECONDS = 120
INFOBASE_MARKER = "1Cv8.1CD"
MANAGED_INFOBASE_USER = "KOTStartupService"
MANAGED_INFOBASE_PASSWORD = ""
MANAGED_STATE_FILE = ".mxl-merge-renderer.json"
BATCH_CAPABILITY_MARKER = "mxl-merge-batch-v1"
LEGACY_SINGLE_RENDER_EPF_SHA256 = (
    "aa894caf035962974c1834fa8ae9e123a0f3f89182ce53dfc9ad8d1eae0a1e56"
)
KNOWN_BATCH_RENDER_EPF_SHA256 = frozenset(
    {"70ae14205f391cde144aabd291c5d993ecc2adc44de7112cf91900df8e4e15ad"}
)


class OneCRenderError(RuntimeError):
    """Raised when the 1C renderer cannot produce a valid HTML file."""


class MxlEditorError(RuntimeError):
    """Raised when an MXL file cannot be opened in the external editor."""


@dataclass(frozen=True)
class OneCRenderSettings:
    client_exe: Path
    infobase: Path
    epf: Path
    username: str | None = None
    password: str | None = None
    timeout_seconds: int = DEFAULT_RENDER_TIMEOUT_SECONDS
    managed_infobase: bool = False
    batch_capable: bool = False


def _git_config(key: str) -> str | None:
    try:
        value = subprocess.check_output(
            ["git", "config", "--get", key],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return value or None


def _configured_value(explicit: str | None, environment: str, git_key: str) -> str | None:
    if explicit and explicit.strip():
        return explicit.strip()
    environment_value = os.environ.get(environment, "").strip()
    if environment_value:
        return environment_value
    return _git_config(git_key)


def configured_mxl_editor(explicit: str | None = None) -> Path | None:
    """Return the configured 1C file editor executable, if it is usable."""
    value = _configured_value(
        explicit, "MXL_ONEC_FILE_EDITOR", "mxl.onecFileEditor"
    )
    if not value:
        return None
    editor = Path(value).expanduser().resolve()
    return editor if editor.is_file() else None


def mxl_editor_available(explicit: str | None = None) -> bool:
    """Whether the UI can ask the operating system to edit an MXL file."""
    return configured_mxl_editor(explicit) is not None or (
        os.name == "nt" and hasattr(os, "startfile")
    )


def launch_mxl_editor(
    path: str | Path, explicit: str | None = None
) -> subprocess.Popen[bytes] | None:
    """Open an MXL file without invoking a shell.

    A configured ``1cv8fv.exe`` is preferred. On Windows, the registered MXL
    application is a fallback; that API does not expose a process handle.
    """
    document = Path(path).resolve()
    if not document.is_file():
        raise MxlEditorError(f"MXL file was not found: {document}")
    editor = configured_mxl_editor(explicit)
    if editor is not None:
        try:
            return subprocess.Popen(
                [str(editor), str(document)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            raise MxlEditorError(
                f"Unable to start the MXL editor {editor}: {error}"
            ) from error
    if os.name == "nt" and hasattr(os, "startfile"):
        try:
            os.startfile(str(document))  # type: ignore[attr-defined]
        except OSError as error:
            raise MxlEditorError(f"Unable to open {document}: {error}") from error
        return None
    raise MxlEditorError(
        "1C file editor is not configured; run install with "
        "--onec-file-editor or set MXL_ONEC_FILE_EDITOR"
    )


def _platform_version(client_exe: Path) -> str:
    parent = client_exe.parent
    candidate = parent.parent.name if parent.name.lower() == "bin" else parent.name
    version = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate).strip("-.")
    return version or "default"


def default_renderer_infobase(client_exe: Path) -> Path:
    """Return the per-user, per-platform service infobase location."""
    configured_root = _configured_value(
        None, "MXL_ONEC_RUNTIME", "mxl.onecRuntime"
    )
    if configured_root:
        root = Path(configured_root).expanduser()
    elif os.environ.get("LOCALAPPDATA", "").strip():
        root = Path(os.environ["LOCALAPPDATA"]) / "MxlMerge"
    else:
        root = Path.home() / ".mxl-merge"
    return (root / "renderer" / _platform_version(client_exe) / "ib").resolve()


def resolve_designer_exe(client_exe: Path) -> Path:
    """Resolve 1cv8.exe next to a configured thin-client executable."""
    if client_exe.name.lower() in {"1cv8.exe", "1cv8"}:
        designer = client_exe
    else:
        suffix = ".exe" if client_exe.suffix.lower() == ".exe" else ""
        designer = client_exe.with_name(f"1cv8{suffix}")
    if not designer.is_file():
        raise OneCRenderError(
            f"1C Designer executable was not found next to the client: {designer}"
        )
    return designer


def _has_infobase_marker(infobase: Path) -> bool:
    marker = infobase / INFOBASE_MARKER
    if marker.is_file():
        return True
    try:
        return any(item.name.lower() == INFOBASE_MARKER.lower() for item in infobase.iterdir())
    except OSError:
        return False


def _managed_template_path() -> Path:
    return Path(__file__).resolve().parent / "onec" / "MxlRendererTemplate.dt"


def _managed_state_path(infobase: Path) -> Path:
    return infobase.parent / f"{infobase.name}{MANAGED_STATE_FILE}"


def _template_descriptor(template: Path) -> str:
    digest = hashlib.sha256()
    with template.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_managed_state(infobase: Path) -> dict[str, object] | None:
    try:
        state = json.loads(_managed_state_path(infobase).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return state if isinstance(state, dict) else None


def _valid_managed_state(infobase: Path, descriptor: str | None = None) -> bool:
    state = _read_managed_state(infobase)
    if not state or state.get("username") != MANAGED_INFOBASE_USER:
        return False
    return descriptor is None or state.get("templateSha256") == descriptor


def _managed_authentication(settings: OneCRenderSettings) -> tuple[str | None, str | None]:
    if settings.username:
        return settings.username, settings.password
    if _valid_managed_state(settings.infobase):
        return MANAGED_INFOBASE_USER, MANAGED_INFOBASE_PASSWORD
    return None, None


@contextmanager
def _renderer_lock(infobase: Path):
    """Serialize creation and use of the shared per-version service infobase."""
    lock_path = infobase.parent / ".mxl-merge-renderer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def ensure_renderer_infobase(settings: OneCRenderSettings) -> Path:
    with _renderer_lock(settings.infobase):
        return _ensure_renderer_infobase_locked(settings)


def _initialize_renderer_infobase(
    settings: OneCRenderSettings, infobase: Path, template: Path
) -> None:
    designer = resolve_designer_exe(settings.client_exe)
    infobase.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mxl-onec-create-") as directory:
        log_path = Path(directory) / "create-infobase.log"
        command = [
            str(designer),
            "CREATEINFOBASE",
            _file_infobase_connection(infobase),
            "/Out",
            str(log_path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=settings.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise OneCRenderError(
                f"Timed out while creating the renderer infobase at {infobase}"
            ) from error
        if completed.returncode != 0:
            details = (
                _read_text_if_present(log_path)
                or completed.stderr.strip()
                or completed.stdout.strip()
                or "no details"
            )
            raise OneCRenderError(
                f"1C could not create the renderer infobase "
                f"(exit code {completed.returncode}): {details}"
            )

    if not _has_infobase_marker(infobase):
        raise OneCRenderError(
            f"1C reported success but the renderer infobase was not created: {infobase}"
        )

    with tempfile.TemporaryDirectory(prefix="mxl-onec-restore-") as directory:
        log_path = Path(directory) / "restore-infobase.log"
        command = [
            str(designer),
            "DESIGNER",
            "/DisableStartupDialogs",
            "/DisableStartupMessages",
            "/IBConnectionString",
            _file_infobase_connection(infobase),
            "/RestoreIB",
            str(template),
            "/Out",
            str(log_path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=settings.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise OneCRenderError(
                f"Timed out while preparing the renderer infobase at {infobase}"
            ) from error
        if completed.returncode != 0:
            details = (
                _read_text_if_present(log_path)
                or completed.stderr.strip()
                or completed.stdout.strip()
                or "no details"
            )
            raise OneCRenderError(
                "1C could not restore the bundled renderer infobase template "
                f"(exit code {completed.returncode}): {details}"
            )


def _ensure_renderer_infobase_locked(settings: OneCRenderSettings) -> Path:
    """Create and prepare the service file infobase, then reuse it."""
    infobase = settings.infobase
    template = _managed_template_path()
    if not template.is_file():
        raise OneCRenderError(f"Bundled renderer infobase template was not found: {template}")
    descriptor = _template_descriptor(template)

    has_existing_marker = _has_infobase_marker(infobase)
    if has_existing_marker:
        if not settings.managed_infobase or _valid_managed_state(infobase, descriptor):
            return infobase

    if infobase.exists() and not infobase.is_dir():
        raise OneCRenderError(f"1C renderer infobase path is not a directory: {infobase}")
    if infobase.is_dir() and not has_existing_marker:
        try:
            has_unrelated_files = any(infobase.iterdir())
        except OSError as error:
            raise OneCRenderError(f"Cannot inspect renderer infobase: {error}") from error
        if has_unrelated_files:
            raise OneCRenderError(
                f"Renderer infobase directory is not empty and has no {INFOBASE_MARKER}: "
                f"{infobase}"
            )

    backup: Path | None = None
    staging: Path | None = None
    if settings.managed_infobase:
        infobase.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{infobase.name}.building-", dir=infobase.parent)
        )
        try:
            _initialize_renderer_infobase(settings, staging, template)
            if infobase.exists():
                backup = Path(
                    tempfile.mkdtemp(prefix=f".{infobase.name}.backup-", dir=infobase.parent)
                )
                backup.rmdir()
                os.replace(infobase, backup)
            try:
                os.replace(staging, infobase)
            except BaseException:
                if backup is not None and not infobase.exists():
                    os.replace(backup, infobase)
                raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    else:
        _initialize_renderer_infobase(settings, infobase, template)

    _atomic_write_text(
        _managed_state_path(infobase),
        json.dumps(
            {
                "schemaVersion": 1,
                "templateSha256": descriptor,
                "username": MANAGED_INFOBASE_USER,
                "password": MANAGED_INFOBASE_PASSWORD,
            },
            indent=2,
        ),
    )
    if backup is not None and backup.exists():
        shutil.rmtree(backup)
    return infobase


def resolve_onec_settings(
    *,
    client_exe: str | None = None,
    infobase: str | None = None,
    epf: str | None = None,
    username: str | None = None,
    password: str | None = None,
    timeout_seconds: int = DEFAULT_RENDER_TIMEOUT_SECONDS,
) -> OneCRenderSettings:
    if timeout_seconds <= 0:
        raise OneCRenderError("1C render timeout must be greater than zero")
    bundled_epf = Path(__file__).resolve().parent / "onec" / "MxlToHtml.epf"
    client_value = _configured_value(client_exe, "MXL_ONEC_CLIENT", "mxl.onecClient")
    infobase_value = _configured_value(infobase, "MXL_ONEC_INFOBASE", "mxl.onecInfobase")
    epf_value = _configured_value(epf, "MXL_ONEC_EPF", "mxl.onecEpf")
    username_value = _configured_value(username, "MXL_ONEC_USERNAME", "mxl.onecUsername")
    password_value = password if password is not None else os.environ.get("MXL_ONEC_PASSWORD")
    batch_value = _configured_value(
        None, "MXL_ONEC_BATCH_CAPABLE", "mxl.onecBatchCapable"
    )

    if not client_value:
        raise OneCRenderError(
            "1C client is not configured; set mxl.onecClient or MXL_ONEC_CLIENT"
        )
    resolved_client = Path(client_value).expanduser().resolve()
    settings = OneCRenderSettings(
        resolved_client,
        (
            Path(infobase_value).expanduser().resolve()
            if infobase_value
            else default_renderer_infobase(resolved_client)
        ),
        Path(epf_value).expanduser().resolve() if epf_value else bundled_epf,
        username_value,
        password_value,
        timeout_seconds,
        not bool(infobase_value),
        str(batch_value or "").lower() in {"1", "true", "yes", "on"},
    )
    if not settings.client_exe.is_file():
        raise OneCRenderError(f"1C client was not found: {settings.client_exe}")
    if not settings.epf.is_file():
        raise OneCRenderError(f"MxlToHtml.epf was not found: {settings.epf}")
    return settings


def _file_infobase_connection(infobase: Path) -> str:
    value = str(infobase)
    escaped = value.replace('"', '""')
    if re.search(r'[\s;"]', value):
        escaped = f'"{escaped}"'
    return f"File={escaped};"


def build_onec_command(
    settings: OneCRenderSettings, job_path: Path, log_path: Path
) -> list[str]:
    command = [
        str(settings.client_exe),
        "ENTERPRISE",
        "/IBConnectionString",
        _file_infobase_connection(settings.infobase),
    ]
    username, password = _managed_authentication(settings)
    if username:
        command.extend(["/N", username])
        if password is not None:
            command.extend(["/P", password])
    command.extend(
        [
            "/DisableStartupDialogs",
            "/DisableStartupMessages",
            "/Execute",
            str(settings.epf),
            "/C",
            str(job_path),
            "/Out",
            str(log_path),
        ]
    )
    return command


def _read_text_if_present(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return ""


def epf_supports_batch(epf: Path, explicit: bool | None = None) -> bool:
    """Recognize verified bundled builds or an explicit capability declaration."""
    if explicit is not None:
        return explicit
    try:
        descriptor = _template_descriptor(epf)
    except OSError:
        return False
    if descriptor in KNOWN_BATCH_RENDER_EPF_SHA256:
        return True
    if descriptor == LEGACY_SINGLE_RENDER_EPF_SHA256:
        return False
    marker = Path(f"{epf}.batch-capable")
    try:
        return marker.read_text(encoding="utf-8").strip() == BATCH_CAPABILITY_MARKER
    except (OSError, UnicodeError):
        return False


def _run_onec_render_job(
    payload: Mapping[str, object],
    expected_targets: list[Path],
    settings: OneCRenderSettings,
) -> None:
    with _renderer_lock(settings.infobase):
        _ensure_renderer_infobase_locked(settings)
        for target in expected_targets:
            target.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="mxl-onec-render-") as directory:
            work_directory = Path(directory)
            job_path = work_directory / "job.json"
            status_path = work_directory / "status.json"
            log_path = work_directory / "1c.log"
            job_payload = dict(payload)
            job_payload["statusPath"] = str(status_path)
            job_path.write_text(
                json.dumps(job_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            command = build_onec_command(settings, job_path, log_path)
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=settings.timeout_seconds,
                )
            except subprocess.TimeoutExpired as error:
                status_text = _read_text_if_present(status_path)
                log_text = _read_text_if_present(log_path)
                details = status_text or log_text or "no status or 1C log was produced"
                raise OneCRenderError(
                    f"1C renderer timed out after {settings.timeout_seconds}s: {details}"
                ) from error

            status: dict[str, object] | None = None
            try:
                status = json.loads(status_path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass

            if completed.returncode != 0:
                details = (
                    _read_text_if_present(log_path)
                    or completed.stderr.strip()
                    or completed.stdout.strip()
                    or "no details"
                )
                raise OneCRenderError(
                    f"1C renderer exited with code {completed.returncode}: {details}"
                )
            if status is None:
                details = _read_text_if_present(log_path) or "status file was not created"
                raise OneCRenderError(f"1C renderer did not report completion: {details}")
            if status.get("success") is not True:
                raise OneCRenderError(str(status.get("error") or "1C renderer failed"))
            missing = [str(target) for target in expected_targets if not target.is_file()]
            if missing:
                raise OneCRenderError(
                    "1C renderer reported success but HTML was not created: "
                    + ", ".join(missing)
                )


def render_mxl_with_onec(
    input_path: str | Path,
    output_path: str | Path,
    settings: OneCRenderSettings,
) -> Path:
    source = Path(input_path).expanduser().resolve()
    target = Path(output_path).expanduser().resolve()
    if not source.is_file():
        raise OneCRenderError(f"Input MXL was not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".rendering", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        _run_onec_render_job(
            {"inputPath": str(source), "outputPath": str(temporary)},
            [temporary],
            settings,
        )
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    return target


def render_mxl_batch_with_onec(
    items: Mapping[str, tuple[str | Path, str | Path]],
    settings: OneCRenderSettings,
) -> dict[str, Path]:
    """Render multiple MXL documents in one 1C:Enterprise process."""
    if not items:
        raise OneCRenderError("Batch render manifest contains no items")
    if not (settings.batch_capable or epf_supports_batch(settings.epf)):
        raise OneCRenderError(
            "Configured MxlToHtml.epf supports only one document; rebuild it "
            "from the updated batch-capable MxlToHtml.bsl"
        )
    resolved: dict[str, tuple[Path, Path]] = {}
    seen_targets: set[Path] = set()
    for name, (input_path, output_path) in items.items():
        source = Path(input_path).expanduser().resolve()
        target = Path(output_path).expanduser().resolve()
        if not source.is_file():
            raise OneCRenderError(f"Input MXL was not found: {source}")
        if target in seen_targets:
            raise OneCRenderError(f"Duplicate batch output path: {target}")
        seen_targets.add(target)
        resolved[name] = (source, target)
    temporary_targets: dict[str, Path] = {}
    try:
        for name, (_, target) in resolved.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".rendering", dir=target.parent
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            temporary.unlink()
            temporary_targets[name] = temporary
        _run_onec_render_job(
            {
                "items": [
                    {
                        "name": name,
                        "inputPath": str(source),
                        "outputPath": str(temporary_targets[name]),
                    }
                    for name, (source, _) in resolved.items()
                ]
            },
            list(temporary_targets.values()),
            settings,
        )
        for name, (_, target) in resolved.items():
            os.replace(temporary_targets[name], target)
    finally:
        for temporary in temporary_targets.values():
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return {name: target for name, (_, target) in resolved.items()}
