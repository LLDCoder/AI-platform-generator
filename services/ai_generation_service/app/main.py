from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    AI_GENERATION_API_KEY: str | None = None
    AI_GENERATION_DATA_DIR: Path = Path("/data")
    AI_GENERATION_TIMEOUT_SECONDS: int = 1800

    HERMES_RUNS_BASE_URL: str = "http://hermes-agent:8642"
    HERMES_AGENT_API_KEY: str | None = "hermes-local-dev-key"
    HERMES_AGENT_MODEL: str = "gpt-5.5"

    PLATFORM_BASE_URL: str | None = "http://backend:8001"
    PLATFORM_API_KEY: str | None = None

    OPENAI_BASE_URL: str | None = None
    OPENAI_API_KEY: str | None = None

    RAGFLOW_BASE_URL: str | None = "http://host.docker.internal:9380"
    RAGFLOW_API_KEY: str | None = None
    RAGFLOW_TIMEOUT_SECONDS: int = 8


settings = Settings()
app = FastAPI(
    title="FF AI Generation Service",
    version="0.1.0",
    description="Standalone AI generation, Hermes orchestration, skill, and task-state bridge service.",
)


RunStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "pending_approval",
    "cancelled",
]


class WorkspaceSpec(BaseModel):
    container_runtime_root: str | None = None
    task_work_order_dir: str
    task_staging_dir: str | None = None
    note: str | None = None


class AiRunCreate(BaseModel):
    task_id: str = Field(min_length=1, max_length=128)
    tenant_id: str | None = Field(default=None, max_length=128)
    user_id: str | None = Field(default=None, max_length=128)
    title: str | None = Field(default=None, max_length=500)
    markdown: str = Field(default="", max_length=20000)
    mode: Literal["create", "refine", "repair"] = "create"
    base_task_id: str | None = Field(default=None, max_length=128)
    runtime_secrets: dict[str, str] = Field(default_factory=dict)
    workspace: WorkspaceSpec
    skill_context: str | None = Field(default=None, max_length=20000)
    repair_instruction: dict[str, Any] | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    callback: dict[str, Any] = Field(default_factory=dict)


class AiRunPublic(BaseModel):
    run_id: str
    hermes_run_id: str | None = None
    task_id: str
    status: RunStatus
    current_node: str | None = None
    summary: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    next_action: str = "wait"
    error: str = ""
    created_at: float
    updated_at: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskStatusUpdate(BaseModel):
    from_status: str | None = None
    to_status: str
    event_type: str
    message: str = ""
    error_detail: str | None = None
    payload_patch: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class TaskArtifactSave(BaseModel):
    artifact_type: str = Field(min_length=1, max_length=128)
    artifact_key: str = ""
    content: dict[str, Any] = Field(default_factory=dict)


class SkillIn(BaseModel):
    skill_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    category: str = Field(default="workflow-skill", max_length=128)
    description: str = ""
    prompt: str = ""
    status: str = "active"
    visibility: str = "public"
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillPatch(BaseModel):
    name: str | None = None
    category: str | None = None
    description: str | None = None
    prompt: str | None = None
    status: str | None = None
    visibility: str | None = None
    tags: list[str] | None = None
    metadata: dict[str, Any] | None = None


class SkillMatchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20000)
    top_k: int = Field(default=5, ge=1, le=20)


class QualityCheckRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=128)
    project_root: str | None = None
    files: list[dict[str, Any]] = Field(default_factory=list)
    requirements: dict[str, Any] = Field(default_factory=dict)


def _data_dir() -> Path:
    settings.AI_GENERATION_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return settings.AI_GENERATION_DATA_DIR


def _runs_file() -> Path:
    return _data_dir() / "runs.json"


def _skills_file() -> Path:
    return _data_dir() / "skills.json"


def _events_dir() -> Path:
    path = _data_dir() / "events"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback
    return data


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _require_auth(authorization: str | None = Header(default=None)) -> None:
    if not settings.AI_GENERATION_API_KEY:
        return
    expected = f"Bearer {settings.AI_GENERATION_API_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid AI generation service token")


def _hermes_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.HERMES_AGENT_API_KEY:
        headers["Authorization"] = f"Bearer {settings.HERMES_AGENT_API_KEY}"
    return headers


def _platform_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings.PLATFORM_API_KEY:
        headers["Authorization"] = f"Bearer {settings.PLATFORM_API_KEY}"
    return headers


def _now() -> float:
    return time.time()


def _run_store() -> dict[str, Any]:
    data = _load_json(_runs_file(), {})
    return data if isinstance(data, dict) else {}


def _save_run(run: dict[str, Any]) -> None:
    data = _run_store()
    data[str(run["run_id"])] = run
    _save_json(_runs_file(), data)


def _get_run(run_id: str) -> dict[str, Any]:
    run = _run_store().get(run_id)
    if not isinstance(run, dict):
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
    return run


def _append_event(run_id: str, event: dict[str, Any]) -> None:
    path = _events_dir() / f"{run_id}.jsonl"
    event = {"ts": _now(), **event}
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(event, ensure_ascii=False) + "\n")


def _events_for(run_id: str) -> list[dict[str, Any]]:
    path = _events_dir() / f"{run_id}.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _artifact_from_project_root(project_root: str) -> dict[str, Any] | None:
    root = Path(project_root)
    artifact_file = root / "artifact.json"
    if artifact_file.exists():
        try:
            artifact = json.loads(artifact_file.read_text(encoding="utf-8", errors="ignore"))
        except ValueError:
            artifact = {}
    else:
        artifact = {}

    html_candidates = [
        root / "index.html",
        root / "public" / "index.html",
        root / "dist" / "index.html",
        root / "build" / "index.html",
        root / "out" / "index.html",
        root / "src" / "index.html",
    ]
    frontend_entry = next((path for path in html_candidates if path.exists() and path.is_file()), None)
    if frontend_entry is None:
        return None

    backend_entry = None
    for relative in ("server/index.js", "server/index.ts", "server.js", "server.ts"):
        path = root / relative
        if path.exists() and path.is_file():
            backend_entry = path
            break

    build_dir = ""
    for relative in ("dist", "build", "out"):
        path = root / relative
        if (path / "index.html").exists():
            build_dir = str(path)
            break

    return {
        "status": "completed",
        "project_root": str(root),
        "frontend_entry": str(frontend_entry),
        "backend_entry": str(backend_entry or ""),
        "build_dir": build_dir,
        "summary": artifact.get("summary") or "Recovered project artifact from work-order directory.",
        "test_result": artifact.get("test_result") or {},
    }


def _prepare_workspace(body: AiRunCreate) -> list[str]:
    created: list[str] = []
    candidates = [
        Path(body.workspace.task_work_order_dir),
        Path(body.workspace.task_work_order_dir) / "source",
        Path(body.workspace.task_work_order_dir) / "source" / "public",
        Path(body.workspace.task_work_order_dir) / "source" / "server",
    ]
    if body.workspace.task_staging_dir:
        candidates.append(Path(body.workspace.task_staging_dir))

    for path in candidates:
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o777)
        except OSError:
            pass
        created.append(str(path))
    return created


def _build_hermes_input(body: AiRunCreate) -> str:
    direct_runtime_env = ""
    if body.runtime_secrets:
        env_lines = []
        for key, value in body.runtime_secrets.items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key or ""):
                env_lines.append(f"{key}={value}")
        if env_lines:
            direct_runtime_env = (
                "DIRECT TEST RUNTIME ENV (user-authorized for local AI generation):\n"
                + "\n".join(env_lines)
                + "\nDo not echo these values to UI, generated files, logs, URLs, or README.\n\n"
            )

    delivery_contract = (
        "PLATFORM DELIVERY CONTRACT (mandatory):\n"
        f"- Create and modify deliverables only under: {body.workspace.task_work_order_dir}\n"
        f"- Final source root must be: {body.workspace.task_work_order_dir}/source\n"
        f"- Write artifact JSON at: {body.workspace.task_work_order_dir}/source/artifact.json\n"
        "- artifact.json must include status, project_root, frontend_entry, backend_entry, build_dir, test_result, and summary.\n"
        "- Use dependency-free Node.js server based on native http/url/fs/path modules. Do not depend on Express/Fastify/Koa/Axios/npm install.\n"
        "- Use real external APIs when required; do not replace them with mock data.\n"
        "- Keep generated frontend preview-subpath safe and use relative asset paths.\n\n"
    )

    payload = {
        "task_id": body.task_id,
        "tenant_id": body.tenant_id,
        "user_id": body.user_id,
        "title": body.title,
        "markdown": body.markdown,
        "mode": body.mode,
        "base_task_id": body.base_task_id,
        "workspace": body.workspace.model_dump(),
        "skill_context": body.skill_context,
        "repair_instruction": body.repair_instruction,
        "constraints": body.constraints,
        "payload": body.payload,
    }
    return (
        "Run the complete AI generation work loop for this confirmed platform task. "
        "Analyze, plan, select skills, implement, test, and return machine-readable artifact details. "
        "If tools or approvals are blocked, return pending_approval with a concrete reason.\n\n"
        + direct_runtime_env
        + delivery_contract
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _post_platform(path: str, payload: dict[str, Any]) -> None:
    if not settings.PLATFORM_BASE_URL:
        return
    url = settings.PLATFORM_BASE_URL.rstrip("/") + path
    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(url, headers=_platform_headers(), json=payload)
            response.raise_for_status()
    except Exception:
        # Platform callback must not make the generation service fail.
        return


def _sync_run_from_hermes(run: dict[str, Any]) -> dict[str, Any]:
    hermes_run_id = str(run.get("hermes_run_id") or "")
    if not hermes_run_id or run.get("status") in {"completed", "failed", "cancelled"}:
        return run

    url = f"{settings.HERMES_RUNS_BASE_URL.rstrip('/')}/v1/runs/{hermes_run_id}"
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, headers=_hermes_headers())
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        run["updated_at"] = _now()
        run["summary"] = f"Hermes polling failed: {exc}"
        _save_run(run)
        return run

    status_value = str(data.get("status") or data.get("state") or "").strip().lower()
    run["updated_at"] = _now()
    run["metadata"]["hermes_status_payload"] = data
    if status_value in {"queued", "created", "pending", "started", "running", "in_progress"}:
        recovered = None
        workspace = run.get("metadata", {}).get("workspace") or {}
        for raw_root in (
            f"{workspace.get('task_work_order_dir', '')}/source",
            workspace.get("task_work_order_dir"),
            workspace.get("task_staging_dir"),
        ):
            if raw_root:
                recovered = _artifact_from_project_root(str(raw_root))
                if recovered:
                    break
        if recovered:
            run["status"] = "completed"
            run["current_node"] = "TESTING"
            run["summary"] = "Hermes is still running, but a complete staged artifact was recovered."
            run["artifacts"] = [
                {"type": "project_root", "path": recovered["project_root"]},
                {"type": "html_page", "path": recovered["frontend_entry"]},
            ]
            if recovered.get("backend_entry"):
                run["artifacts"].append({"type": "backend_entry", "path": recovered["backend_entry"]})
            run["metadata"]["recovered_artifact"] = recovered
            _append_event(run["run_id"], {"event": "artifact.recovered", "artifact": recovered})
            _post_platform(
                f"/api/v1/tasks/{run['task_id']}/artifacts",
                {"artifact_type": "ai_generation_result", "content": recovered},
            )
        else:
            run["status"] = "running"
            run["current_node"] = "CODING"
            run["summary"] = data.get("summary") or "Hermes work loop is running."
    elif status_value in {"failed", "failure", "error", "cancelled", "canceled"}:
        run["status"] = "failed" if "cancel" not in status_value else "cancelled"
        run["current_node"] = "FAILED"
        run["error"] = str(data.get("error") or data.get("message") or data.get("summary") or "Hermes run failed.")
        run["summary"] = run["error"]
    elif status_value in {"approval_required", "requires_approval", "waiting_for_approval", "pending_approval"}:
        run["status"] = "pending_approval"
        run["current_node"] = "PENDING_APPROVAL"
        run["summary"] = data.get("summary") or "Hermes run is waiting for approval."
        run["next_action"] = "approve_hermes_run"
    else:
        output = str(data.get("output") or "")
        run["status"] = "completed"
        run["current_node"] = "TESTING"
        run["summary"] = data.get("summary") or output[:1000] or "Hermes work loop completed."
        parsed = _extract_json_object(output)
        if parsed:
            run["metadata"]["hermes_output"] = parsed
            run["artifacts"] = parsed.get("artifacts") if isinstance(parsed.get("artifacts"), list) else run["artifacts"]

    _save_run(run)
    return run


def _extract_json_object(text: str) -> dict[str, Any] | None:
    clean = str(text or "").strip()
    if not clean:
        return None
    start = clean.find("{")
    end = clean.rfind("}")
    if start == -1 or end < start:
        return None
    try:
        parsed = json.loads(clean[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


@app.get("/api/ai/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "ai-generation-service",
        "hermes_runs_base_url": settings.HERMES_RUNS_BASE_URL,
        "platform_base_url": settings.PLATFORM_BASE_URL,
    }


@app.get("/api/ai/runtime/status", dependencies=[Depends(_require_auth)])
def runtime_status() -> dict[str, Any]:
    hermes = {"healthy": False}
    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.get(f"{settings.HERMES_RUNS_BASE_URL.rstrip('/')}/v1/models", headers=_hermes_headers())
            hermes = {"healthy": response.status_code < 500, "status_code": response.status_code}
    except Exception as exc:
        hermes = {"healthy": False, "error": str(exc)}
    return {
        "runtime": "hermes",
        "model": settings.HERMES_AGENT_MODEL,
        "hermes": hermes,
        "openai_proxy_configured": bool(settings.OPENAI_BASE_URL and settings.OPENAI_API_KEY),
        "ragflow_configured": bool(settings.RAGFLOW_BASE_URL),
    }


@app.post("/api/ai/runs", response_model=AiRunPublic, dependencies=[Depends(_require_auth)])
def create_run(body: AiRunCreate) -> AiRunPublic:
    run_id = f"airun_{uuid.uuid4().hex}"
    workspace_dirs: list[str] = []
    try:
        workspace_dirs = _prepare_workspace(body)
    except Exception as exc:
        now = _now()
        run = {
            "run_id": run_id,
            "hermes_run_id": "",
            "task_id": body.task_id,
            "status": "failed",
            "current_node": "FAILED",
            "summary": f"Failed to prepare workspace: {exc}",
            "artifacts": [],
            "next_action": "manual_input",
            "error": str(exc),
            "created_at": now,
            "updated_at": now,
            "metadata": {
                "tenant_id": body.tenant_id,
                "user_id": body.user_id,
                "mode": body.mode,
                "base_task_id": body.base_task_id,
                "workspace": body.workspace.model_dump(),
                "callback": body.callback,
                "request": body.model_dump(exclude={"runtime_secrets"}),
            },
        }
        _save_run(run)
        _append_event(run_id, "workspace.prepare.failed", {"error": str(exc)})
        return AiRunPublic(**run)

    request_body = {
        "model": settings.HERMES_AGENT_MODEL,
        "input": _build_hermes_input(body),
        "metadata": {
            "source": "ff_ai_generation_service",
            "task_id": body.task_id,
            "tenant_id": body.tenant_id,
            "workflow_owner": "hermes",
        },
    }

    hermes_run_id = ""
    status_value: RunStatus = "queued"
    summary = "AI generation run queued."
    error = ""
    try:
        with httpx.Client(timeout=float(settings.AI_GENERATION_TIMEOUT_SECONDS)) as client:
            response = client.post(
                f"{settings.HERMES_RUNS_BASE_URL.rstrip('/')}/v1/runs",
                headers=_hermes_headers(),
                json=request_body,
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                hermes_run_id = str(data.get("run_id") or data.get("id") or "")
                status_value = "running"
                summary = "Hermes run submitted."
    except Exception as exc:
        status_value = "failed"
        error = str(exc)
        summary = f"Failed to submit Hermes run: {exc}"

    now = _now()
    run = {
        "run_id": run_id,
        "hermes_run_id": hermes_run_id,
        "task_id": body.task_id,
        "status": status_value,
        "current_node": "CODING" if status_value == "running" else "FAILED",
        "summary": summary,
        "artifacts": [],
        "next_action": "wait" if status_value == "running" else "manual_input",
        "error": error,
        "created_at": now,
        "updated_at": now,
        "metadata": {
            "tenant_id": body.tenant_id,
            "user_id": body.user_id,
            "mode": body.mode,
                "base_task_id": body.base_task_id,
                "workspace": body.workspace.model_dump(),
                "workspace_dirs": workspace_dirs,
                "callback": body.callback,
                "request": body.model_dump(exclude={"runtime_secrets"}),
            },
    }
    _save_run(run)
    _append_event(run_id, {"event": "run.created", "status": status_value, "hermes_run_id": hermes_run_id})
    _post_platform(
        f"/api/v1/tasks/{body.task_id}/status",
        {
            "to_status": run["current_node"],
            "event_type": "AI_GENERATION_RUN_CREATED",
            "message": summary,
            "payload_patch": {
                "ai_generation_run_id": run_id,
                "hermes_work_loop_run_id": hermes_run_id,
                "execution_runtime": "ai_generation_service",
                "workflow_owner": "hermes",
            },
        },
    )
    return AiRunPublic.model_validate(run)


@app.get("/api/ai/runs/{run_id}", response_model=AiRunPublic, dependencies=[Depends(_require_auth)])
def get_run(run_id: str) -> AiRunPublic:
    run = _sync_run_from_hermes(_get_run(run_id))
    return AiRunPublic.model_validate(run)


@app.get("/api/ai/runs/{run_id}/events", dependencies=[Depends(_require_auth)])
def run_events(run_id: str, stream: bool = False):
    run = _get_run(run_id)
    hermes_run_id = str(run.get("hermes_run_id") or "")
    if stream and hermes_run_id:
        url = f"{settings.HERMES_RUNS_BASE_URL.rstrip('/')}/v1/runs/{hermes_run_id}/events"

        def iter_events():
            try:
                with httpx.stream("GET", url, headers=_hermes_headers(), timeout=60.0) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line:
                            yield line + "\n"
            except Exception as exc:
                yield "data: " + json.dumps({"event": "events.error", "error": str(exc)}, ensure_ascii=False) + "\n\n"

        return StreamingResponse(iter_events(), media_type="text/event-stream")
    return {"data": _events_for(run_id), "count": len(_events_for(run_id))}


@app.post("/api/ai/runs/{run_id}/retry", response_model=AiRunPublic, dependencies=[Depends(_require_auth)])
def retry_run(run_id: str) -> AiRunPublic:
    run = _get_run(run_id)
    request_data = run.get("metadata", {}).get("request") or {}
    if not isinstance(request_data, dict):
        raise HTTPException(status_code=400, detail="Original request is missing.")
    body = AiRunCreate.model_validate(request_data)
    return create_run(body)


@app.post("/api/ai/runs/{run_id}/cancel", response_model=AiRunPublic, dependencies=[Depends(_require_auth)])
def cancel_run(run_id: str) -> AiRunPublic:
    run = _get_run(run_id)
    run["status"] = "cancelled"
    run["current_node"] = "FAILED"
    run["summary"] = "AI generation run cancelled by request."
    run["updated_at"] = _now()
    _save_run(run)
    _append_event(run_id, {"event": "run.cancelled"})
    return AiRunPublic.model_validate(run)


@app.post("/api/ai/tasks/{task_id}/resume", response_model=AiRunPublic, dependencies=[Depends(_require_auth)])
def resume_task(task_id: str, body: AiRunCreate) -> AiRunPublic:
    body.task_id = task_id
    body.mode = "refine"
    return create_run(body)


@app.get("/api/ai/tasks/{task_id}/artifacts", dependencies=[Depends(_require_auth)])
def task_artifacts(task_id: str) -> dict[str, Any]:
    runs = [run for run in _run_store().values() if isinstance(run, dict) and run.get("task_id") == task_id]
    runs.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    artifacts = []
    for run in runs:
        artifacts.extend(run.get("artifacts") or [])
        recovered = run.get("metadata", {}).get("recovered_artifact")
        if isinstance(recovered, dict):
            artifacts.append({"type": "recovered_artifact", **recovered})
    return {"task_id": task_id, "artifacts": artifacts, "runs": runs}


@app.post("/api/ai/tasks/{task_id}/quality", dependencies=[Depends(_require_auth)])
def task_quality(task_id: str, body: QualityCheckRequest) -> dict[str, Any]:
    body.task_id = task_id
    return quality_check(body)


@app.post("/api/ai/quality/check", dependencies=[Depends(_require_auth)])
def quality_check(body: QualityCheckRequest) -> dict[str, Any]:
    issues: list[str] = []
    files = body.files
    if body.project_root:
        root = Path(body.project_root)
        if root.exists():
            files = [
                {"path": str(path.relative_to(root)).replace("\\", "/"), "content": path.read_text(encoding="utf-8", errors="ignore")}
                for path in root.rglob("*")
                if path.is_file() and path.stat().st_size < 2_000_000
            ]
        else:
            issues.append("project_root does not exist")

    paths = {str(item.get("path") or "") for item in files if isinstance(item, dict)}
    if not any(path.endswith(".html") for path in paths):
        issues.append("missing html entry")
    if body.requirements.get("backend_proxy_required") and not any(path in {"server/index.js", "server.js"} for path in paths):
        issues.append("missing Node backend proxy")
    for item in files:
        path = str(item.get("path") or "")
        content = str(item.get("content") or "")
        if path.endswith((".js", ".ts")) and re.search(r"require\(['\"](?:express|fastify|koa|axios)['\"]\)", content):
            issues.append(f"external npm dependency detected: {path}")
        if body.requirements.get("no_mock_data") and re.search(r"\b(mock|fake|sample)\b", content, re.I):
            issues.append(f"mock/sample data signal detected: {path}")

    return {
        "task_id": body.task_id,
        "passed": not issues,
        "issues": issues,
        "checked_file_count": len(files),
        "checks": [
            {"name": "html_entry", "status": "passed" if any(path.endswith(".html") for path in paths) else "failed"},
            {"name": "dependency_free_node", "status": "passed" if not any("external npm dependency" in item for item in issues) else "failed"},
        ],
    }


def _skill_store() -> dict[str, Any]:
    data = _load_json(_skills_file(), {})
    return data if isinstance(data, dict) else {}


def _save_skill_store(data: dict[str, Any]) -> None:
    _save_json(_skills_file(), data)


@app.get("/api/ai/skills", dependencies=[Depends(_require_auth)])
def list_skills(category: str | None = None, keyword: str | None = None) -> dict[str, Any]:
    skills = list(_skill_store().values())
    if category:
        skills = [item for item in skills if item.get("category") == category]
    if keyword:
        needle = keyword.lower()
        skills = [
            item
            for item in skills
            if needle in " ".join(
                [
                    str(item.get("skill_id") or ""),
                    str(item.get("name") or ""),
                    str(item.get("description") or ""),
                    " ".join(item.get("tags") or []),
                ]
            ).lower()
        ]
    return {"data": skills, "count": len(skills)}


@app.post("/api/ai/skills", dependencies=[Depends(_require_auth)])
def create_skill(body: SkillIn) -> dict[str, Any]:
    data = _skill_store()
    if body.skill_id in data:
        raise HTTPException(status_code=409, detail="Skill already exists")
    item = body.model_dump()
    item["created_at"] = _now()
    item["updated_at"] = item["created_at"]
    data[body.skill_id] = item
    _save_skill_store(data)
    return item


@app.get("/api/ai/skills/{skill_id}", dependencies=[Depends(_require_auth)])
def get_skill(skill_id: str) -> dict[str, Any]:
    item = _skill_store().get(skill_id)
    if not isinstance(item, dict):
        raise HTTPException(status_code=404, detail="Skill not found")
    return item


@app.patch("/api/ai/skills/{skill_id}", dependencies=[Depends(_require_auth)])
def update_skill(skill_id: str, body: SkillPatch) -> dict[str, Any]:
    data = _skill_store()
    item = data.get(skill_id)
    if not isinstance(item, dict):
        raise HTTPException(status_code=404, detail="Skill not found")
    patch = body.model_dump(exclude_unset=True)
    item.update(patch)
    item["updated_at"] = _now()
    data[skill_id] = item
    _save_skill_store(data)
    return item


@app.delete("/api/ai/skills/{skill_id}", dependencies=[Depends(_require_auth)])
def delete_skill(skill_id: str) -> dict[str, Any]:
    data = _skill_store()
    item = data.pop(skill_id, None)
    if not isinstance(item, dict):
        raise HTTPException(status_code=404, detail="Skill not found")
    _save_skill_store(data)
    return {"skill_id": skill_id, "deleted": True}


@app.post("/api/ai/skills/match", dependencies=[Depends(_require_auth)])
def match_skills(body: SkillMatchRequest) -> dict[str, Any]:
    query_tokens = set(re.findall(r"[\w\u4e00-\u9fff]+", body.query.lower()))
    ranked: list[tuple[int, dict[str, Any]]] = []
    for item in _skill_store().values():
        if not isinstance(item, dict) or item.get("status") == "archived":
            continue
        corpus = " ".join(
            [
                str(item.get("skill_id") or ""),
                str(item.get("name") or ""),
                str(item.get("category") or ""),
                str(item.get("description") or ""),
                str(item.get("prompt") or ""),
                " ".join(item.get("tags") or []),
            ]
        ).lower()
        skill_tokens = set(re.findall(r"[\w\u4e00-\u9fff]+", corpus))
        score = len(query_tokens & skill_tokens)
        if score > 0:
            ranked.append((score, item))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return {
        "matches": [
            {
                "score": score,
                "skill_id": item.get("skill_id"),
                "name": item.get("name"),
                "category": item.get("category"),
                "description": item.get("description"),
                "prompt": item.get("prompt"),
            }
            for score, item in ranked[: body.top_k]
        ]
    }


@app.post("/api/ai/skills/export/hermes", dependencies=[Depends(_require_auth)])
def export_hermes_skills() -> dict[str, Any]:
    export_dir = _data_dir() / "hermes-skills"
    export_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for item in _skill_store().values():
        if not isinstance(item, dict) or item.get("status") == "archived":
            continue
        skill_dir = export_dir / str(item.get("skill_id"))
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            "\n".join(
                [
                    "---",
                    f"name: {item.get('skill_id')}",
                    f"description: {item.get('description') or item.get('name')}",
                    "---",
                    "",
                    f"# {item.get('name')}",
                    "",
                    str(item.get("prompt") or ""),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        written.append(str(skill_file))
    return {"export_dir": str(export_dir), "written": written, "count": len(written)}


@app.post("/api/ai/skills/reload", dependencies=[Depends(_require_auth)])
def reload_skills() -> dict[str, Any]:
    # Hermes image may not expose a reload endpoint; this records intent for ops.
    return {"ok": True, "message": "Skill reload requested. Restart or signal Hermes Agent if runtime reload is unavailable."}


async def _proxy_openai(path: str, request: Request) -> JSONResponse:
    if not settings.OPENAI_BASE_URL or not settings.OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OPENAI_BASE_URL or OPENAI_API_KEY is not configured")
    body = await request.json()
    url = f"{settings.OPENAI_BASE_URL.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=float(settings.AI_GENERATION_TIMEOUT_SECONDS)) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}", "Content-Type": "application/json"},
            json=body,
        )
    return JSONResponse(status_code=response.status_code, content=response.json())


@app.post("/api/ai/model/v1/chat/completions", dependencies=[Depends(_require_auth)])
async def proxy_chat_completions(request: Request):
    return await _proxy_openai("/v1/chat/completions", request)


@app.post("/api/ai/model/v1/responses", dependencies=[Depends(_require_auth)])
async def proxy_responses(request: Request):
    return await _proxy_openai("/v1/responses", request)


@app.post("/api/ai/knowledge/search", dependencies=[Depends(_require_auth)])
def ragflow_search(body: dict[str, Any]) -> dict[str, Any]:
    if not settings.RAGFLOW_BASE_URL:
        return {"ok": False, "degraded": True, "reason": "RAGFLOW_BASE_URL is not configured", "results": []}
    headers = {"Content-Type": "application/json"}
    if settings.RAGFLOW_API_KEY:
        headers["Authorization"] = f"Bearer {settings.RAGFLOW_API_KEY}"
    try:
        with httpx.Client(timeout=float(settings.RAGFLOW_TIMEOUT_SECONDS)) as client:
            response = client.post(f"{settings.RAGFLOW_BASE_URL.rstrip('/')}/api/v1/retrieval", headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        return {"ok": False, "degraded": True, "reason": str(exc), "results": []}
    return {"ok": True, "provider": "ragflow", "data": data}


@app.get("/health")
def root_health() -> dict[str, str]:
    return {"status": "ok"}
