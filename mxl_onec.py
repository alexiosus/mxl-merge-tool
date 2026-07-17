"""1C:Enterprise-backed MXL to HTML renderer."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


DEFAULT_RENDER_TIMEOUT_SECONDS = 120
INFOBASE_MARKER = "1Cv8.1CD"
MANAGED_INFOBASE_USER = "KOTStartupService"
MANAGED_INFOBASE_PASSWORD = ""
MANAGED_STATE_FILE = ".mxl-merge-renderer.json"
LEGACY_SINGLE_RENDER_EPF_SHA256 = (
    "aa894caf035962974c1834fa8ae9e123a0f3f89182ce53dfc9ad8d1eae0a1e56"
)


class OneCRenderError(RuntimeError):
    """Raised when the 1C renderer cannot produce a valid HTML file."""


@dataclass(frozen=True)
class OneCRenderSettings:
    client_exe: Path
    infobase: Path
    epf: Path
    username: str | None = None
    password: str | None = None
    timeout_seconds: int = DEFAULT_RENDER_TIMEOUT_SECONDS
    managed_infobase: bool = False


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


def ensure_renderer_infobase(settings: OneCRenderSettings) -> Path:
    """Create and prepare the service file infobase, then reuse it."""
    infobase = settings.infobase
    template = _managed_template_path()
    if not template.is_file():
        raise OneCRenderError(f"Bundled renderer infobase template was not found: {template}")
    descriptor = _template_descriptor(template)

    if _has_infobase_marker(infobase):
        if not settings.managed_infobase or _valid_managed_state(infobase, descriptor):
            return infobase
        # Only the automatically selected per-user runtime is recreated here.
        # An explicitly configured infobase is never deleted by the tool.
        shutil.rmtree(infobase)
        try:
            _managed_state_path(infobase).unlink()
        except FileNotFoundError:
            pass

    if infobase.exists() and not infobase.is_dir():
        raise OneCRenderError(f"1C renderer infobase path is not a directory: {infobase}")
    if infobase.is_dir():
        try:
            has_unrelated_files = any(infobase.iterdir())
        except OSError as error:
            raise OneCRenderError(f"Cannot inspect renderer infobase: {error}") from error
        if has_unrelated_files:
            raise OneCRenderError(
                f"Renderer infobase directory is not empty and has no {INFOBASE_MARKER}: "
                f"{infobase}"
            )

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
                f"1C could not restore the bundled renderer infobase template "
                f"(exit code {completed.returncode}): {details}"
            )

    _managed_state_path(infobase).write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "templateSha256": descriptor,
                "username": MANAGED_INFOBASE_USER,
                "password": MANAGED_INFOBASE_PASSWORD,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
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
    bundled_epf = Path(__file__).resolve().parent / "onec" / "MxlToHtml.epf"
    client_value = _configured_value(client_exe, "MXL_ONEC_CLIENT", "mxl.onecClient")
    infobase_value = _configured_value(infobase, "MXL_ONEC_INFOBASE", "mxl.onecInfobase")
    epf_value = _configured_value(epf, "MXL_ONEC_EPF", "mxl.onecEpf")
    username_value = _configured_value(username, "MXL_ONEC_USERNAME", "mxl.onecUsername")
    password_value = password if password is not None else os.environ.get("MXL_ONEC_PASSWORD")

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


def epf_supports_batch(epf: Path) -> bool:
    """Return false for the known single-document EPF bundled initially."""
    try:
        return _template_descriptor(epf) != LEGACY_SINGLE_RENDER_EPF_SHA256
    except OSError:
        return False


def _run_onec_render_job(
    payload: Mapping[str, object],
    expected_targets: list[Path],
    settings: OneCRenderSettings,
) -> None:
    ensure_renderer_infobase(settings)
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
    _run_onec_render_job(
        {"inputPath": str(source), "outputPath": str(target)},
        [target],
        settings,
    )

    return target


def render_mxl_batch_with_onec(
    items: Mapping[str, tuple[str | Path, str | Path]],
    settings: OneCRenderSettings,
) -> dict[str, Path]:
    """Render multiple MXL documents in one 1C:Enterprise process."""
    if not items:
        raise OneCRenderError("Batch render manifest contains no items")
    if not epf_supports_batch(settings.epf):
        raise OneCRenderError(
            "Configured MxlToHtml.epf supports only one document; rebuild it "
            "from the updated batch-capable MxlToHtml.bsl"
        )
    resolved: dict[str, tuple[Path, Path]] = {}
    for name, (input_path, output_path) in items.items():
        source = Path(input_path).expanduser().resolve()
        target = Path(output_path).expanduser().resolve()
        if not source.is_file():
            raise OneCRenderError(f"Input MXL was not found: {source}")
        resolved[name] = (source, target)
    _run_onec_render_job(
        {
            "items": [
                {
                    "name": name,
                    "inputPath": str(source),
                    "outputPath": str(target),
                }
                for name, (source, target) in resolved.items()
            ]
        },
        [target for _, target in resolved.values()],
        settings,
    )
    return {name: target for name, (_, target) in resolved.items()}
