"""NovelFlow FastAPI 主服务。

提供：
    - 静态页面服务
    - 设置管理（API Key / 端点 / 模型）
    - 启动工作流
    - SSE 实时进度推送
    - 历史记录与下载（重启后仍可从 output/ 恢复）
    - LLM 连接测试
"""
from __future__ import annotations

import asyncio
import json
import os
import time
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
OUTPUT_DIR = ROOT / "output"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
META_FILENAME = "meta.json"

app = FastAPI(title="NovelFlow", version="0.2.0")

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
    for role, cfg in DEFAULT_SETTINGS.items():
        settings[role] = dict(cfg)

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
        if "****" in api_key:
            cfg["api_key"] = old.get("api_key", "")
        old_settings.setdefault(role, {}).update(cfg)

    # 前端只提交 default + 已自定义的角色；其余角色应回到「跟随默认」，
    # 因此清除它们的历史单独覆盖，避免切换回“跟随默认”后仍用旧 Key / 旧模型。
    for role in ["background", "outline", "writer", "polisher", "archiver"]:
        if role not in new_settings:
            old_settings[role] = {}

    _save_settings(old_settings)
    return JSONResponse({"ok": True})


class TestConnectionPayload(BaseModel):
    role: str = "default"
    config: dict[str, Any] | None = None


@app.post("/api/test-connection")
async def test_connection(payload: TestConnectionPayload) -> JSONResponse:
    """用一条极短消息测试 LLM 配置是否可用。"""
    if payload.config:
        cfg = LLMConfig.from_dict(payload.config)
    else:
        settings = _load_settings()
        cfg = LLMConfig.from_dict(settings.get(payload.role) or settings.get("default"))

    # 若前端传来的是脱敏 Key，则用已保存的真实 Key 补回
    if "****" in (cfg.api_key or ""):
        settings = _load_settings()
        saved = (settings.get(payload.role) or settings.get("default") or {}).get("api_key", "")
        cfg.api_key = saved

    base = (cfg.base_url or "").lower()
    is_local = any(h in base for h in ("localhost", "127.0.0.1", "0.0.0.0"))
    if not cfg.api_key and not is_local:
        return JSONResponse({"ok": False, "error": "API Key 为空，请先在「默认配置」里填写 API Key。"})

    client = LLMClient()
    try:
        started = time.time()
        reply = await client.chat(cfg, [{"role": "user", "content": "请只回复：连接成功"}])
        elapsed = round(time.time() - started, 2)
        return JSONResponse({"ok": True, "model": cfg.model, "latency_seconds": elapsed, "reply": reply[:120]})
    except LLMError as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        await client.close()


# ============ 工作流 API ============
class RunPayload(BaseModel):
    input: dict[str, Any]
    settings: dict[str, dict[str, Any]] | None = None


@app.post("/api/run")
async def start_workflow(payload: RunPayload) -> JSONResponse:
    """启动小说创作工作流。"""
    workflow_id = uuid.uuid4().hex[:12]
    novel_input = NovelInput.from_dict(payload.input)

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
    wf.persist_meta()

    task = asyncio.create_task(wf.run())
    _tasks[workflow_id] = task

    return JSONResponse({"workflow_id": workflow_id})


def _read_meta(workflow_id: str) -> dict[str, Any] | None:
    p = OUTPUT_DIR / workflow_id / META_FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _scan_history() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not OUTPUT_DIR.exists():
        return items
    for meta in OUTPUT_DIR.glob(f"*/{META_FILENAME}"):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("id"):
                items.append(data)
        except Exception:
            continue
    return items


@app.get("/api/workflows")
def list_workflows() -> JSONResponse:
    """列出所有工作流（内存 + 磁盘历史）。"""
    items = [w.to_dict() for w in _workflows.values()]
    seen = {w["id"] for w in items}
    for meta in _scan_history():
        wid = meta.get("id")
        if wid and wid not in seen:
            items.append(meta)
            seen.add(wid)
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return JSONResponse({"workflows": items})


@app.get("/api/workflows/{workflow_id}")
def get_workflow(workflow_id: str) -> JSONResponse:
    wf = _workflows.get(workflow_id)
    if wf:
        return JSONResponse(wf.to_dict())
    meta = _read_meta(workflow_id)
    if meta:
        return JSONResponse(meta)
    raise HTTPException(status_code=404, detail="工作流不存在")


@app.get("/api/workflows/{workflow_id}/chapters")
def get_chapters(workflow_id: str) -> JSONResponse:
    """获取各章节正文（初稿 + 润色稿），供前端历史查看。"""
    wf = _workflows.get(workflow_id)
    if wf:
        return JSONResponse({"chapters": wf.chapters})
    meta = _read_meta(workflow_id)
    if meta:
        return JSONResponse({"chapters": meta.get("chapters", [])})
    raise HTTPException(status_code=404, detail="工作流不存在")


@app.get("/api/workflows/{workflow_id}/stream")
async def stream_workflow(workflow_id: str) -> StreamingResponse:
    """SSE 实时进度流。"""
    queue = _queues.get(workflow_id)
    if queue is None:
        raise HTTPException(status_code=404, detail="工作流不存在")

    async def event_generator():
        wf = _workflows.get(workflow_id)
        if wf:
            yield f"data: {json.dumps({'event': 'snapshot', 'data': wf.to_dict()}, ensure_ascii=False)}\n\n"

        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=25.0)
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
            except asyncio.CancelledError:
                break

            wf = _workflows.get(workflow_id)
            if wf and wf.status in ("done", "error"):
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
    path: Path | None = None
    wf = _workflows.get(workflow_id)
    if wf and wf.archive_path:
        path = Path(wf.archive_path)

    if path is None:
        proj = OUTPUT_DIR / workflow_id
        if proj.exists():
            candidates = sorted(proj.glob("*.md"))
            for c in candidates:
                if "全书" in c.name:
                    path = c
                    break
            if path is None and candidates:
                path = candidates[-1]

    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="归档文件尚未生成")
    return FileResponse(path, filename=path.name, media_type="text/markdown; charset=utf-8")


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "version": app.version, "workflows_in_memory": len(_workflows)})


# ============ 静态页面 ============
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
