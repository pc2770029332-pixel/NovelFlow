"""NovelFlow FastAPI 主服务。

提供：
    - 静态页面服务
    - 设置管理（API Key / 端点 / 模型）
    - 启动工作流
    - SSE 实时进度推送
    - 历史记录与下载
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .llm_client import LLMClient, LLMConfig, LLMError
from .workflow.engine import NovelInput, NovelWorkflow

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONFIG_DIR = ROOT / "config"
OUTPUT_DIR = Path.home() / "Desktop" / "NovelFlow作品"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

app = FastAPI(title="NovelFlow", version="0.1.0")

# ============ 全局状态 ============
_workflows: dict[str, NovelWorkflow] = {}
_queues: dict[str, asyncio.Queue] = {}
_tasks: dict[str, asyncio.Task] = {}


# ============ 设置管理 ============
DEFAULT_SETTINGS: dict[str, dict[str, Any]] = {
    "default": {
        "api_key": os.getenv("DEFAULT_API_KEY", ""),
        "base_url": os.getenv("DEFAULT_BASE_URL", "https://api.openai.com/v1"),
        "model": os.getenv("DEFAULT_MODEL", "gpt-4o-mini"),
        "temperature": 0.8,
        "max_tokens": 4096,
    },
    "background": {"temperature": 0.8, "max_tokens": 4096},
    "outline": {"temperature": 0.7, "max_tokens": 8192},
    "chapter_outline": {"temperature": 0.7, "max_tokens": 8192},
    "writer": {"temperature": 0.9, "max_tokens": 8192},
    "polisher": {"temperature": 0.6, "max_tokens": 8192},
    "continuity": {"temperature": 0.3, "max_tokens": 4096},
    "archiver": {"temperature": 0.5, "max_tokens": 4096},
}

_settings_cache: dict[str, dict[str, Any]] | None = None


def _load_settings() -> dict[str, dict[str, Any]]:
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache

    settings: dict[str, dict[str, Any]] = {}
    # 先写默认值
    for role, cfg in DEFAULT_SETTINGS.items():
        settings[role] = dict(cfg)

    # 再从文件覆盖
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                for role, cfg in saved.items():
                    if isinstance(cfg, dict):
                        settings.setdefault(role, {})
                        settings[role].update(cfg)
        except Exception:
            pass

    # 补齐所有角色的模型配置（继承 default）
    base = settings.get("default", {})
    for role in ["background", "outline", "chapter_outline", "writer", "polisher", "continuity", "archiver"]:
        merged = dict(base)
        merged.update(settings.get(role, {}))
        settings[role] = merged

    _settings_cache = settings
    return settings


def _save_settings(settings: dict[str, dict[str, Any]]) -> None:
    global _settings_cache
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    _settings_cache = None


@app.on_event("startup")
async def restore_saved_workflows() -> None:
    """从桌面项目目录恢复历史记录和断点状态。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    settings = _load_settings()
    for project_dir in OUTPUT_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        if not (project_dir / "project.json").exists() or not (project_dir / "state.json").exists():
            continue
        try:
            queue: asyncio.Queue = asyncio.Queue()
            workflow = NovelWorkflow.restore(project_dir, settings, queue)
            _workflows[workflow.id] = workflow
            _queues[workflow.id] = queue
        except Exception as exc:
            print(f"跳过无法恢复的项目 {project_dir.name}: {exc}")


def _merge_settings(
    new_settings: dict[str, dict[str, Any]],
    inherit_roles: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """合并配置；前端传回脱敏 Key 时保留磁盘中的完整值。"""
    old_settings = _load_settings()
    merged = {role: dict(cfg) for role, cfg in old_settings.items()}
    for role, cfg in new_settings.items():
        if not isinstance(cfg, dict):
            continue
        incoming = dict(cfg)
        api_key = str(incoming.get("api_key", ""))
        if "****" in api_key:
            incoming["api_key"] = old_settings.get(role, {}).get("api_key", "")
        merged.setdefault(role, {}).update(incoming)
    default_config = dict(merged.get("default", {}))
    for role in inherit_roles or []:
        if role in {"background", "outline", "chapter_outline", "writer", "polisher", "continuity", "archiver"}:
            merged[role] = dict(default_config)
    return merged


class SettingsPayload(BaseModel):
    settings: dict[str, dict[str, Any]]
    inherit_roles: list[str] = []


@app.get("/api/settings")
def get_settings() -> JSONResponse:
    """获取当前设置（API Key 做脱敏处理）"""
    settings = _load_settings()
    masked: dict[str, dict[str, Any]] = {}
    for role, cfg in settings.items():
        masked[role] = dict(cfg)
        key = masked[role].get("api_key", "")
        if key:
            masked[role]["api_key"] = key[:6] + "****" + key[-4:] if len(key) > 12 else "****"
    return JSONResponse({"settings": masked, "keys_are_masked": True})


@app.post("/api/settings")
def save_settings(payload: SettingsPayload) -> JSONResponse:
    """保存设置。前端传回脱敏的 API Key 时保留原值。"""
    _save_settings(_merge_settings(payload.settings, payload.inherit_roles))
    return JSONResponse({"ok": True})


# ============ 工作流 API ============
class RunPayload(BaseModel):
    input: dict[str, Any]
    settings: dict[str, dict[str, Any]] | None = None


class ReviewPayload(BaseModel):
    outline: str


class BatchApprovalPayload(BaseModel):
    approved: bool = True


class ChapterSelectPayload(BaseModel):
    chapter: int


class ConnectionTestPayload(BaseModel):
    config: dict[str, Any]
    role: str = "default"


@app.post("/api/run")
async def start_workflow(payload: RunPayload) -> JSONResponse:
    """启动小说创作工作流。"""
    workflow_id = uuid.uuid4().hex[:12]
    novel_input = NovelInput.from_dict(payload.input)

    # 如果传了 settings，先保存
    if payload.settings:
        _save_settings(_merge_settings(payload.settings))

    settings = _load_settings()
    project_dir = OUTPUT_DIR / workflow_id
    queue: asyncio.Queue = asyncio.Queue()
    _queues[workflow_id] = queue

    wf = NovelWorkflow(
        workflow_id=workflow_id,
        novel_input=novel_input,
        settings=settings,
        output_dir=project_dir,
        event_queue=queue,
    )
    _workflows[workflow_id] = wf

    task = asyncio.create_task(wf.run())
    _tasks[workflow_id] = task

    return JSONResponse({"workflow_id": workflow_id})


@app.post("/api/workflows/{workflow_id}/approve")
async def approve_workflow_review(workflow_id: str, payload: ReviewPayload) -> JSONResponse:
    """保存人工审核后的细纲，并继续正文创作。"""
    wf = _workflows.get(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="工作流不存在")
    try:
        await wf.approve_review(payload.outline)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    task = _tasks.get(workflow_id)
    if task is None or task.done():
        _tasks[workflow_id] = asyncio.create_task(wf.run())
    return JSONResponse(wf.to_dict())


@app.post("/api/workflows/{workflow_id}/continue-batch")
async def continue_batch(workflow_id: str, payload: BatchApprovalPayload | None = None) -> JSONResponse:
    wf = _workflows.get(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="工作流不存在")
    try:
        await wf.approve_review("")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    task = _tasks.get(workflow_id)
    if task is None or task.done():
        _tasks[workflow_id] = asyncio.create_task(wf.run())
    return JSONResponse(wf.to_dict())


@app.post("/api/workflows/{workflow_id}/retry")
async def retry_workflow(workflow_id: str) -> JSONResponse:
    """仅重试当前失败阶段，保留已生成文件和章节。"""
    wf = _workflows.get(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="工作流不存在")
    try:
        wf.prepare_retry()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _tasks[workflow_id] = asyncio.create_task(wf.run())
    return JSONResponse(wf.to_dict())


@app.post("/api/workflows/{workflow_id}/select-chapter")
async def select_workflow_chapter(workflow_id: str, payload: ChapterSelectPayload) -> JSONResponse:
    """保留已有文件，从用户指定章节开始下一轮生成。"""
    wf = _workflows.get(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="工作流不存在")
    try:
        wf.set_next_chapter(payload.chapter)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    task = _tasks.get(workflow_id)
    if task is None or task.done():
        _tasks[workflow_id] = asyncio.create_task(wf.run())
    return JSONResponse(wf.to_dict())


@app.post("/api/test-connection")
async def test_connection(payload: ConnectionTestPayload) -> JSONResponse:
    """测试一套 OpenAI 兼容 API 配置是否可用。"""
    raw_config = dict(payload.config)
    if "****" in str(raw_config.get("api_key", "")):
        raw_config["api_key"] = _load_settings().get(payload.role, {}).get("api_key", "")
    config = LLMConfig.from_dict(raw_config)
    if not config.api_key:
        raise HTTPException(status_code=400, detail="请先填写 API Key")
    client = LLMClient()
    try:
        reply = await client.chat(
            config,
            [{"role": "user", "content": "Reply with OK."}],
        )
        return JSONResponse({"ok": True, "model": config.model, "reply": reply[:200]})
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"连接测试失败: {exc}") from exc
    finally:
        await client.close()


@app.get("/api/workflows")
def list_workflows() -> JSONResponse:
    """列出所有工作流。"""
    items = [w.to_dict() for w in _workflows.values()]
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return JSONResponse({"workflows": items})


@app.get("/api/workflows/{workflow_id}")
def get_workflow(workflow_id: str) -> JSONResponse:
    wf = _workflows.get(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="工作流不存在")
    return JSONResponse(wf.to_dict())


@app.get("/api/workflows/{workflow_id}/chapters")
def get_chapters(workflow_id: str) -> JSONResponse:
    """获取各章节正文（初稿 + 润色稿），供前端历史查看。"""
    wf = _workflows.get(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="工作流不存在")
    return JSONResponse({"chapters": wf.chapters})


@app.get("/api/workflows/{workflow_id}/stream")
async def stream_workflow(workflow_id: str) -> StreamingResponse:
    """SSE 实时进度流。"""
    queue = _queues.get(workflow_id)
    if queue is None:
        raise HTTPException(status_code=404, detail="工作流不存在")

    async def event_generator():
        # 先发送当前完整状态
        wf = _workflows.get(workflow_id)
        if wf:
            yield f"data: {json.dumps({'event': 'snapshot', 'data': wf.to_dict()}, ensure_ascii=False)}\n\n"

        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=25.0)
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                # 心跳，保持连接
                yield ": keepalive\n\n"
            except asyncio.CancelledError:
                break

            wf = _workflows.get(workflow_id)
            if wf and wf.status in ("done", "error"):
                # 等队列清空后结束
                if queue.empty():
                    yield f"data: {json.dumps({'event': 'end', 'data': wf.to_dict()}, ensure_ascii=False)}\n\n"
                    break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/workflows/{workflow_id}/download")
def download_archive(workflow_id: str):
    wf = _workflows.get(workflow_id)
    if not wf or not wf.archive_path:
        raise HTTPException(status_code=404, detail="归档文件尚未生成")
    path = Path(wf.archive_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="归档文件不存在")
    return FileResponse(
        path,
        filename=path.name,
        media_type="text/markdown; charset=utf-8",
    )


# ============ 静态页面 ============
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
