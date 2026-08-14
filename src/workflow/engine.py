"""小说创作工作流引擎。

完整流程：
    1. 背景设计   → 世界观、人物、规则等设定
    2. 章节细纲   → 每章详细细纲
    3. 主笔创作   → 逐章写正文
    4. 润色修改   → 逐章润色
    5. 自动归档   → 汇总生成最终文档并保存到 output/

引擎通过队列向前端实时推送进度与流式文本。
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..llm_client import LLMClient, LLMConfig, LLMError
from . import prompts


def _extract_chapter_outline(outline_text: str, chapter_no: int) -> str:
    """从完整细纲中提取指定章节的片段。"""
    pattern = rf"##\s*第\s*{chapter_no}\s*章"
    match = re.search(pattern, outline_text)
    if not match:
        return outline_text
    start = match.start()
    next_pattern = rf"##\s*第\s*{chapter_no + 1}\s*章"
    next_match = re.search(next_pattern, outline_text[start + 1:])
    if next_match:
        end = start + 1 + next_match.start()
        return outline_text[start:end]
    return outline_text[start:]


def _summarize(text: str, limit: int = 400) -> str:
    """截取文本开头做摘要/前情提要。"""
    text = text.strip()
    return text[:limit] if len(text) > limit else text


def _chapter_count(chapters: Any) -> int:
    try:
        n = int(chapters)
        return max(1, min(n, 200))
    except (TypeError, ValueError):
        return 3


@dataclass
class StepState:
    """单个流程步骤的状态"""
    key: str
    label: str
    status: str = "pending"  # pending | running | done | error
    output: str = ""
    error: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass
class NovelInput:
    """小说创作输入参数"""
    title: str
    genre: str = "玄幻"
    theme: str = ""
    premise: str = ""
    audience: str = ""
    extra: str = ""
    chapters: int = 3
    words_per_chapter: int = 2000

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NovelInput":
        return cls(
            title=str(data.get("title", "未命名小说")).strip() or "未命名小说",
            genre=str(data.get("genre", "玄幻")).strip() or "玄幻",
            theme=str(data.get("theme", "")).strip(),
            premise=str(data.get("premise", "")).strip(),
            audience=str(data.get("audience", "")).strip(),
            extra=str(data.get("extra", "")).strip(),
            chapters=_chapter_count(data.get("chapters", 3)),
            words_per_chapter=int(data.get("words_per_chapter", 2000) or 2000),
        )


class NovelWorkflow:
    """小说全流程工作流"""

    def __init__(
        self,
        workflow_id: str,
        novel_input: NovelInput,
        settings: dict[str, dict[str, Any]],
        output_dir: Path,
        event_queue: asyncio.Queue | None = None,
        on_event: Callable | None = None,
    ) -> None:
        self.id = workflow_id
        self.input = novel_input
        self.settings = settings
        self.output_dir = output_dir
        self.event_queue = event_queue
        self.on_event = on_event
        self.llm = LLMClient()
        self.status = "pending"  # pending | running | done | error
        self.current_step = ""
        self.error_message = ""
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self.finished_at = ""

        self.background = ""
        self.outline = ""
        self.chapters: list[dict[str, str]] = []
        self.archived_text = ""
        self.archive_path = ""

        self.steps = {
            "background": StepState("background", "背景设计"),
            "outline": StepState("outline", "章节细纲"),
            "writer": StepState("writer", "主笔创作"),
            "polisher": StepState("polisher", "润色修改"),
            "archiver": StepState("archiver", "自动归档"),
        }

    # ---------- 配置 ----------
    def _cfg(self, role: str) -> LLMConfig:
        return LLMConfig.from_dict(self.settings.get(role) or self.settings.get("default"))

    # ---------- 事件 ----------
    async def _emit(self, event: str, data: dict[str, Any]) -> None:
        payload = {
            "event": event,
            "data": data,
            "workflow_id": self.id,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        if self.event_queue is not None:
            await self.event_queue.put(payload)
        if self.on_event is not None:
            try:
                await self.on_event(payload)
            except Exception:
                pass

    async def _update_step(self, key: str, **kw: Any) -> None:
        step = self.steps[key]
        for k, v in kw.items():
            setattr(step, k, v)
        if step.status == "running" and not step.started_at:
            step.started_at = datetime.now().isoformat(timespec="seconds")
        if step.status in ("done", "error") and not step.finished_at:
            step.finished_at = datetime.now().isoformat(timespec="seconds")
        await self._emit("step_update", step.to_dict())

    async def _stream_to_step(self, key: str, config: LLMConfig, messages: list[dict[str, str]]) -> str:
        """流式调用 LLM，并把增量文本写入步骤并推送前端。"""
        step = self.steps[key]
        step.output = ""
        chunks: list[str] = []
        async for delta in self.llm.chat_stream(config, messages):
            chunks.append(delta)
            step.output = "".join(chunks)
            await self._emit("chunk", {"step": key, "delta": delta})
        text = "".join(chunks).strip()
        step.output = text
        return text

    # ---------- 持久化 ----------
    def _save_working_file(self, name: str, content: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path = self.output_dir / name
        path.write_text(content, encoding="utf-8")
        return path

    # ---------- 步骤实现 ----------
    async def step_background(self) -> None:
        key = "background"
        await self._update_step(key, status="running")
        cfg = self._cfg(key)
        messages = [
            {"role": "system", "content": prompts.BACKGROUND_SYSTEM},
            {"role": "user", "content": prompts.BACKGROUND_USER.format(
                title=self.input.title,
                genre=self.input.genre,
                theme=self.input.theme,
                premise=self.input.premise,
                audience=self.input.audience,
                extra=self.input.extra or "无",
            )},
        ]
        try:
            text = await self._stream_to_step(key, cfg, messages)
            self.background = text
            self._save_working_file("01_背景设定.md", text)
            await self._update_step(key, status="done")
        except Exception as exc:
            await self._update_step(key, status="error", error=str(exc))
            raise

    async def step_outline(self) -> None:
        key = "outline"
        await self._update_step(key, status="running")
        cfg = self._cfg(key)
        messages = [
            {"role": "system", "content": prompts.OUTLINE_SYSTEM},
            {"role": "user", "content": prompts.OUTLINE_USER.format(
                title=self.input.title,
                genre=self.input.genre,
                theme=self.input.theme,
                premise=self.input.premise,
                chapters=self.input.chapters,
                words_per_chapter=self.input.words_per_chapter,
                background=self.background,
            )},
        ]
        try:
            text = await self._stream_to_step(key, cfg, messages)
            self.outline = text
            self._save_working_file("02_章节细纲.md", text)
            await self._update_step(key, status="done")
        except Exception as exc:
            await self._update_step(key, status="error", error=str(exc))
            raise

    async def step_write_chapter(self, chapter_no: int) -> None:
        key = "writer"
        step = self.steps[key]
        cfg = self._cfg(key)
        chapter_outline = _extract_chapter_outline(self.outline, chapter_no)
        previous_summary = ""
        if self.chapters:
            prev = self.chapters[-1].get("polished") or self.chapters[-1].get("draft", "")
            previous_summary = _summarize(prev, 600)

        messages = [
            {"role": "system", "content": prompts.WRITER_SYSTEM},
            {"role": "user", "content": prompts.WRITER_USER.format(
                title=self.input.title,
                genre=self.input.genre,
                theme=self.input.theme,
                chapter_no=chapter_no,
                words_per_chapter=self.input.words_per_chapter,
                background=self.background,
                chapter_outline=chapter_outline,
                previous_summary=previous_summary or "（第一章，无前情）",
            )},
        ]
        try:
            text = await self._stream_to_step(key, cfg, messages)
            self.chapters.append({
                "no": chapter_no,
                "draft": text,
                "polished": "",
            })
            self._save_working_file(f"第{chapter_no:02d}章_初稿.md", text)
            await self._emit("chapter_done", {"chapter": chapter_no, "stage": "draft"})
        except Exception as exc:
            await self._update_step(key, status="error", error=str(exc))
            raise

    async def step_polish_chapter(self, chapter_no: int) -> None:
        key = "polisher"
        step = self.steps[key]
        cfg = self._cfg(key)
        chapter = self.chapters[chapter_no - 1]
        chapter_outline = _extract_chapter_outline(self.outline, chapter_no)

        messages = [
            {"role": "system", "content": prompts.POLISHER_SYSTEM},
            {"role": "user", "content": prompts.POLISHER_USER.format(
                title=self.input.title,
                chapter_no=chapter_no,
                background=self.background,
                chapter_outline=chapter_outline,
                draft=chapter["draft"],
            )},
        ]
        try:
            text = await self._stream_to_step(key, cfg, messages)
            chapter["polished"] = text
            self._save_working_file(f"第{chapter_no:02d}章_润色稿.md", text)
            await self._emit("chapter_done", {"chapter": chapter_no, "stage": "polished"})
        except Exception as exc:
            await self._update_step(key, status="error", error=str(exc))
            raise

    async def step_archive(self) -> None:
        key = "archiver"
        await self._update_step(key, status="running")
        cfg = self._cfg(key)
        chapter_previews = "\n\n".join(
            f"===== 第 {c['no']} 章 =====\n{_summarize(c.get('polished') or c.get('draft', ''), 500)}"
            for c in self.chapters
        )
        messages = [
            {"role": "system", "content": prompts.ARCHIVER_SYSTEM},
            {"role": "user", "content": prompts.ARCHIVER_USER.format(
                title=self.input.title,
                genre=self.input.genre,
                theme=self.input.theme,
                chapters=self.input.chapters,
                background=self.background,
                outline=self.outline,
                chapter_previews=chapter_previews,
            )},
        ]
        try:
            self.archived_text = await self._stream_to_step(key, cfg, messages)

            # 拼接最终全书文档
            full_parts = [
                f"# {self.input.title}",
                "",
                "> 由 NovelFlow AI 全流程创作工作台生成",
                f"> 生成时间：{datetime.now().isoformat(timespec='seconds')}",
                "",
                "---",
                "",
                self.archived_text,
                "",
                "---",
                "",
                "# 背景设定",
                "",
                self.background,
                "",
                "---",
                "",
                "# 章节细纲",
                "",
                self.outline,
                "",
                "---",
                "",
                "# 正文（润色版）",
                "",
            ]
            for c in self.chapters:
                full_parts.append(f"\n## 第 {c['no']} 章\n")
                full_parts.append(c.get("polished") or c.get("draft", ""))
                full_parts.append("\n")

            full_text = "\n".join(full_parts)
            safe_title = re.sub(r"[\\/:*?\"<>|]", "_", self.input.title)
            path = self.output_dir / f"《{safe_title}》全书.md"
            path.write_text(full_text, encoding="utf-8")
            self.archive_path = str(path)

            await self._update_step(key, status="done")
        except Exception as exc:
            await self._update_step(key, status="error", error=str(exc))
            raise

    # ---------- 总调度 ----------
    async def run(self) -> None:
        self.status = "running"
        await self._emit("start", self.to_dict())
        try:
            await self.step_background()
            await self.step_outline()

            # 主笔 + 润色：逐章串行，保证上下文连贯
            for chapter_no in range(1, self.input.chapters + 1):
                await self._emit("progress", {
                    "message": f"正在创作第 {chapter_no}/{self.input.chapters} 章...",
                    "chapter": chapter_no,
                    "total": self.input.chapters,
                })
                await self.step_write_chapter(chapter_no)
                await self.step_polish_chapter(chapter_no)

            await self.step_archive()
            self.status = "done"
            self.finished_at = datetime.now().isoformat(timespec="seconds")
            await self._emit("done", self.to_dict())
        except Exception as exc:
            self.status = "error"
            self.error_message = str(exc)
            self.finished_at = datetime.now().isoformat(timespec="seconds")
            await self._emit("error", {"message": str(exc), **self.to_dict()})
        finally:
            await self.llm.close()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "current_step": self.current_step,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "input": {
                "title": self.input.title,
                "genre": self.input.genre,
                "theme": self.input.theme,
                "premise": self.input.premise,
                "chapters": self.input.chapters,
                "words_per_chapter": self.input.words_per_chapter,
            },
            "steps": {k: v.to_dict() for k, v in self.steps.items()},
            "archive_path": self.archive_path,
            "chapters_count": len(self.chapters),
        }
