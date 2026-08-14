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

from .workflow.engine import NovelInput, NovelWorkflow

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
CONFIG_DIR = ROOT / "config"
OUTPUT_DIR = ROOT / "output"
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
    "writer": {"temperature": 0.9, "max_tokens": 8192},
    "polisher": {"temperature": 0.6, "max_tokens": 8192},
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
    for role in ["background", "outline", "writer", "polisher", "archiver"]:
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


class SettingsPayload(BaseModel):
    settings: dict[str, dict[str, Any]]


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
    new_settings = payload.settings
    old_settings = _load_settings()

    for role, cfg in new_settings.items():
        if not isinstance(cfg, dict):
            continue
        old = old_settings.get(role, {})
        api_key = str(cfg.get("api_key", ""))
        # 脱敏值保持不变
        if "****" in api_key:
            cfg["api_key"] = old.get("api_key", "")
        old_settings.setdefault(role, {}).update(cfg)

    _save_settings(old_settings)
    return JSONResponse({"ok": True})


# ============ 工作流 API ============
class RunPayload(BaseModel):
    input: dict[str, Any]
    settings: dict[str, dict[str, Any]] | None = None


@app.post("/api/run")
async def start_workflow(payload: RunPayload) -> JSONResponse:
    """启动小说创作工作流。"""
    workflow_id = uuid.uuid4().hex[:12]
    novel_input = NovelInput.from_dict(payload.input)

    # 如果传了 settings，先保存
    if payload.settings:
        _save_settings(payload.settings)

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
