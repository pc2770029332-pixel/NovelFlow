"""NovelFlow 的可恢复、逐轮小说创作引擎。"""
from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..llm_client import LLMClient, LLMConfig
from . import prompts


def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return default


def _extract_chapter_outline(text: str, chapter_no: int) -> str:
    match = re.search(rf"(?:^|\n)#+\s*第\s*{chapter_no}\s*章[^\n]*", text)
    if not match:
        return text.strip()
    next_match = re.search(r"\n#+\s*第\s*\d+\s*章[^\n]*", text[match.end():])
    end = match.end() + next_match.start() if next_match else len(text)
    return text[match.start():end].strip()


@dataclass
class StepState:
    key: str
    label: str
    status: str = "pending"
    output: str = ""
    error: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NovelInput:
    title: str
    genre: str = "玄幻"
    theme: str = ""
    premise: str = ""
    audience: str = ""
    extra: str = ""
    total_words: int = 100000
    volume_words: int = 100000
    chapters: int = 50
    start_chapter: int = 1
    words_per_chapter: int = 2000
    batch_size: int = 3
    skip_volume_review: bool = False
    skip_outline_review: bool = False
    skip_body_review: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NovelInput":
        words = _bounded_int(data.get("words_per_chapter"), 2000, 500, 20000)
        total = _bounded_int(data.get("total_words"), 100000, 500, 10_000_000)
        volume_words = _bounded_int(data.get("volume_words"), 100000, 10000, 2_000_000)
        chapters = max(1, min(math.ceil(total / words), 500))
        return cls(
            title=str(data.get("title", "未命名小说")).strip() or "未命名小说",
            genre=str(data.get("genre", "玄幻")).strip() or "玄幻",
            theme=str(data.get("theme", "")).strip(), premise=str(data.get("premise", "")).strip(),
            audience=str(data.get("audience", "")).strip(), extra=str(data.get("extra", "")).strip(),
            total_words=total, volume_words=volume_words, chapters=chapters, words_per_chapter=words,
            start_chapter=_bounded_int(data.get("start_chapter"), 1, 1, chapters),
            batch_size=_bounded_int(data.get("batch_size"), 3, 1, 20),
            skip_volume_review=bool(data.get("skip_volume_review", False)),
            skip_outline_review=bool(data.get("skip_outline_review", False)),
            skip_body_review=bool(data.get("skip_body_review", False)),
        )


class NovelWorkflow:
    def __init__(self, workflow_id: str, novel_input: NovelInput, settings: dict[str, dict[str, Any]],
                 output_dir: Path, event_queue: asyncio.Queue | None = None,
                 on_event: Callable | None = None, initialize: bool = True) -> None:
        self.id, self.input, self.settings, self.output_dir = workflow_id, novel_input, settings, output_dir
        self.event_queue, self.on_event = event_queue, on_event
        self.llm = LLMClient()
        self.status, self.current_step, self.current_chapter = "pending", "", novel_input.start_chapter
        chapters_per_volume = max(1, math.ceil(novel_input.volume_words / novel_input.words_per_chapter))
        self.current_volume = max(1, math.ceil(novel_input.start_chapter / chapters_per_volume))
        self.batch_end = min(novel_input.batch_size, novel_input.chapters)
        self.error_message, self.created_at, self.finished_at = "", datetime.now().isoformat(timespec="seconds"), ""
        self.background, self.volume_plan, self.chapter_outline, self.continuity = "", "", "", ""
        self.chapters: list[dict[str, Any]] = []
        self.archive_path, self.archived_text = "", ""
        self.review_event = asyncio.Event()
        self.steps = {k: StepState(k, label) for k, label in {
            "background": "背景设定", "background_review": "背景确认", "outline": "本卷规划", "review": "卷规划审核",
            "chapter_outline": "本轮细纲", "chapter_review": "细纲审核", "writer": "初稿创作",
            "polisher": "润色修改", "continuity": "连续性记忆", "body_review": "正文审核", "handoff_review": "卷末交接审核",
            "archiver": "卷末整理",
        }.items()}
        self._prepare_dirs()
        if initialize:
            self._save_project(); self._save_state()

    @classmethod
    def restore(cls, output_dir: Path, settings: dict[str, dict[str, Any]], event_queue: asyncio.Queue | None = None) -> "NovelWorkflow":
        project = json.loads((output_dir / "project.json").read_text(encoding="utf-8"))
        saved = json.loads((output_dir / "state.json").read_text(encoding="utf-8"))
        wf = cls(str(saved.get("id") or output_dir.name), NovelInput.from_dict(project), settings, output_dir, event_queue, initialize=False)
        for attr in ("created_at", "finished_at", "current_step", "current_chapter", "current_volume", "batch_end", "error_message", "status", "archive_path"):
            if attr in saved: setattr(wf, attr, saved[attr])
        wf.background = wf._read("bible.md"); wf.volume_plan = wf._read(f"volumes/{wf.current_volume:03d}/volume-plan.md")
        wf.chapter_outline = wf._read(f"outline/batches/{wf.current_chapter:03d}.md")
        wf.continuity = wf._read("memory/continuity.md")
        for key, raw in (saved.get("steps") or {}).items():
            if key in wf.steps and isinstance(raw, dict):
                for field in ("status", "output", "error", "started_at", "finished_at"):
                    if field in raw: setattr(wf.steps[key], field, raw[field])
        wf.steps["background"].output = wf.background
        wf.steps["outline"].output = wf.volume_plan
        wf.steps["chapter_outline"].output = wf.chapter_outline
        wf.steps["continuity"].output = wf.continuity
        for no in range(1, wf.input.chapters + 1):
            draft, polished = wf._read(f"chapters/{no:03d}-draft.md"), wf._read(f"chapters/{no:03d}-polished.md")
            if not draft and not polished: continue
            wf.chapters.append({"no": no, "draft": draft, "polished": polished, "memory_updated": bool(saved.get("memory", {}).get(str(no), False))})
        if wf.status == "awaiting_background_review" and not wf.background:
            wf.status, wf.current_step, wf.error_message = "error", "background", "背景设定没有生成内容，请检查 AI 配置后重试"
            wf.steps["background"].status = "error"
            wf.steps["background"].error = wf.error_message
        if wf.status not in {"done", "error", "awaiting_background_review", "awaiting_review", "awaiting_chapter_review", "awaiting_body_review", "awaiting_handoff_review"}:
            wf.status, wf.error_message = "error", "上次运行被服务重启中断，可点击重试继续"
        wf._save_state(); return wf

    def _prepare_dirs(self) -> None:
        for name in ("volumes", "outline/batches", "chapters", "memory", "final"):
            (self.output_dir / name).mkdir(parents=True, exist_ok=True)

    def _cfg(self, role: str) -> LLMConfig:
        return LLMConfig.from_dict(self.settings.get(role) or self.settings.get("default"))

    def _write(self, relative: str, content: str) -> Path:
        path = self.output_dir / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content, encoding="utf-8"); return path

    def _read(self, relative: str) -> str:
        path = self.output_dir / relative; return path.read_text(encoding="utf-8") if path.exists() else ""

    def _save_project(self) -> None: self._write("project.json", json.dumps(asdict(self.input), ensure_ascii=False, indent=2))
    def _save_state(self) -> None: self._write("state.json", json.dumps(self.to_dict(False), ensure_ascii=False, indent=2))

    async def _emit(self, event: str, data: dict[str, Any]) -> None:
        payload = {"event": event, "data": data, "workflow_id": self.id, "ts": datetime.now().isoformat(timespec="seconds")}
        if self.event_queue is not None: await self.event_queue.put(payload)
        if self.on_event:
            try: await self.on_event(payload)
            except Exception: pass

    async def _step(self, key: str, status: str, output: str | None = None, error: str = "") -> None:
        step = self.steps[key]; step.status, step.error = status, error
        if output is not None: step.output = output
        if status == "running" and not step.started_at: step.started_at = datetime.now().isoformat(timespec="seconds")
        if status in {"done", "error"}: step.finished_at = datetime.now().isoformat(timespec="seconds")
        self._save_state(); await self._emit("step_update", step.to_dict())

    async def _stream(self, key: str, messages: list[dict[str, str]]) -> str:
        chunks: list[str] = []; self.steps[key].output = ""
        async for delta in self.llm.chat_stream(self._cfg(key), messages):
            chunks.append(delta); self.steps[key].output = "".join(chunks); await self._emit("chunk", {"step": key, "delta": delta})
        text = "".join(chunks).strip()
        if not text:
            raise RuntimeError(f"{self.steps[key].label}没有返回内容，请检查 AI 配置、模型或端点后重试")
        return text

    async def approve_review(self, content: str) -> None:
        if self.status not in {"awaiting_background_review", "awaiting_review", "awaiting_chapter_review", "awaiting_body_review", "awaiting_handoff_review"}: raise ValueError("当前不在审核阶段")
        if self.status == "awaiting_background_review" and content.strip():
            self.background = content.strip(); self._write("bible.md", self.background)
        if self.status == "awaiting_review" and content.strip():
            self.volume_plan = content.strip(); self._write(f"volumes/{self.current_volume:03d}/volume-plan.md", self.volume_plan)
        if self.status == "awaiting_chapter_review" and content.strip():
            self.chapter_outline = content.strip(); self._write(f"outline/batches/{self.current_chapter:03d}.md", self.chapter_outline)
        await self._step(self.current_step, "done", "已人工确认"); self.review_event.set()

    def prepare_retry(self) -> None:
        if self.status != "error" or self.current_step not in self.steps: raise ValueError("只有出错的流程可以重试")
        if self.current_step == "background_review" and not self.background:
            self.current_step = "background"
            self.steps["background"].status = "pending"
        self.steps[self.current_step].status, self.steps[self.current_step].error = "pending", ""; self.status, self.error_message = "pending", ""; self._save_state()

    def set_next_chapter(self, chapter_no: int) -> None:
        if self.status == "running" or self.status.startswith("awaiting_"):
            raise ValueError("当前流程正在生成或等待审核，请先完成当前节点")
        if chapter_no < 1 or chapter_no > self.input.chapters:
            raise ValueError(f"章节必须在 1-{self.input.chapters} 之间")
        chapters_per_volume = max(1, math.ceil(self.input.volume_words / self.input.words_per_chapter))
        self.current_chapter = chapter_no
        self.current_volume = max(1, math.ceil(chapter_no / chapters_per_volume))
        self.batch_end = min(chapter_no + self.input.batch_size - 1, self.input.chapters)
        self.volume_plan = self._read(f"volumes/{self.current_volume:03d}/volume-plan.md")
        self.chapter_outline = ""
        self.status, self.current_step, self.error_message, self.finished_at = "pending", "chapter_outline", "", ""
        for key in ("chapter_outline", "chapter_review", "writer", "polisher", "continuity", "body_review", "handoff_review", "archiver"):
            if key in self.steps: self.steps[key] = StepState(key, self.steps[key].label)
        self._save_state()

    async def step_background(self) -> None:
        await self._step("background", "running")
        self.background = await self._stream("background", [{"role": "system", "content": prompts.BACKGROUND_SYSTEM}, {"role": "user", "content": prompts.BACKGROUND_USER.format(title=self.input.title, genre=self.input.genre, theme=self.input.theme, premise=self.input.premise, audience=self.input.audience, extra=self.input.extra or "无")}])
        self._write("bible.md", self.background); await self._step("background", "done")

    async def step_volume_plan(self) -> None:
        await self._step("outline", "running")
        chapters_per_volume = max(1, math.ceil(self.input.volume_words / self.input.words_per_chapter)); start = (self.current_volume - 1) * chapters_per_volume + 1; end = min(self.current_volume * chapters_per_volume, self.input.chapters)
        self.current_chapter = max(self.current_chapter, start)
        self.volume_plan = await self._stream("outline", [{"role": "system", "content": prompts.OUTLINE_SYSTEM}, {"role": "user", "content": prompts.OUTLINE_USER.format(title=self.input.title, genre=self.input.genre, theme=self.input.theme, premise=self.input.premise, chapters=end-start+1, volume_no=self.current_volume, volumes=math.ceil(self.input.chapters / chapters_per_volume), start_chapter=start, end_chapter=end, total_words=self.input.total_words, words_per_chapter=self.input.words_per_chapter, background=self.background, previous_outline=self.continuity or "第一卷，无上一卷交接信息")}])
        self._write(f"volumes/{self.current_volume:03d}/volume-plan.md", self.volume_plan); await self._step("outline", "done", self.volume_plan)

    async def step_chapter_outline(self) -> None:
        await self._step("chapter_outline", "running")
        end = min(self.current_chapter + self.input.batch_size - 1, self.input.chapters)
        self.batch_end = end
        prompt = getattr(prompts, "CHAPTER_OUTLINE_USER", "请根据卷规划生成第 {start_chapter}-{end_chapter} 章连续详细细纲。")
        self.chapter_outline = await self._stream("chapter_outline", [{"role": "system", "content": getattr(prompts, "CHAPTER_OUTLINE_SYSTEM", prompts.OUTLINE_SYSTEM)}, {"role": "user", "content": prompt.format(title=self.input.title, volume_no=self.current_volume, start_chapter=self.current_chapter, end_chapter=end, background=self.background, volume_plan=self.volume_plan, continuity=self.continuity or "暂无")}])
        self._write(f"outline/batches/{self.current_chapter:03d}.md", self.chapter_outline)
        for no in range(self.current_chapter, end + 1): self._write(f"outline/chapters/{no:03d}.md", _extract_chapter_outline(self.chapter_outline, no))
        await self._step("chapter_outline", "done", self.chapter_outline)

    def _chapter(self, no: int) -> dict[str, Any]:
        while len(self.chapters) < no: self.chapters.append({"no": len(self.chapters)+1, "draft": "", "polished": "", "memory_updated": False})
        return self.chapters[no-1]

    async def step_chapter(self, no: int) -> None:
        c = self._chapter(no); previous = self._chapter(no-1).get("polished", "")[-3000:] if no > 1 else "第一章，无前情"
        await self._step("writer", "running")
        c["draft"] = await self._stream("writer", [{"role": "system", "content": prompts.WRITER_SYSTEM}, {"role": "user", "content": prompts.WRITER_USER.format(title=self.input.title, genre=self.input.genre, theme=self.input.theme, chapter_no=no, words_per_chapter=self.input.words_per_chapter, background=self.background, chapter_outline=_extract_chapter_outline(self.chapter_outline, no), previous_summary=previous, continuity=self.continuity or "暂无")}])
        self._write(f"chapters/{no:03d}-draft.md", c["draft"]); await self._step("writer", "done"); await self._emit("chapter_done", {"chapter": no, "stage": "draft"})
        await self._step("polisher", "running")
        c["polished"] = await self._stream("polisher", [{"role": "system", "content": prompts.POLISHER_SYSTEM}, {"role": "user", "content": prompts.POLISHER_USER.format(title=self.input.title, chapter_no=no, background=self.background, chapter_outline=_extract_chapter_outline(self.chapter_outline, no), draft=c["draft"])}])
        self._write(f"chapters/{no:03d}-polished.md", c["polished"]); await self._step("polisher", "done"); await self._emit("chapter_done", {"chapter": no, "stage": "polished"})
        await self._step("continuity", "running")
        self.continuity = await self._stream("continuity", [{"role": "system", "content": prompts.CONTINUITY_SYSTEM}, {"role": "user", "content": prompts.CONTINUITY_USER.format(chapter_no=no, previous_memory=self.continuity or "暂无", chapter=c["polished"])}])
        c["memory_updated"] = True; self._write("memory/continuity.md", self.continuity); self._save_state(); await self._step("continuity", "done"); await self._emit("chapter_done", {"chapter": no, "stage": "memory"})

    async def step_archive(self) -> None:
        await self._step("archiver", "running")
        text = "# " + self.input.title + "\n\n" + "\n\n".join((c.get("polished") or c.get("draft", "")) for c in self.chapters)
        safe = re.sub(r'[\\/:*?"<>|]', "_", self.input.title); path = self._write(f"final/《{safe}》全书.md", text.strip() + "\n"); self.archive_path = str(path); await self._step("archiver", "done", "已整理当前成果")

    async def run(self) -> None:
        self.status = "running"; await self._emit("start", self.to_dict())
        try:
            if not self.background: self.current_step = "background"; await self.step_background()
            if self.steps["background_review"].status != "done":
                self.current_step = "background_review"; self.status = "awaiting_background_review"
                await self._step("background_review", "running", self.background); await self._emit("background_review_required", self.to_dict())
                self.review_event = asyncio.Event(); await self.review_event.wait(); self.status = "running"
            if not self.volume_plan:
                self.current_step = "outline"; await self.step_volume_plan()
                if not self.input.skip_volume_review:
                    self.current_step = "review"; self.status = "awaiting_review"; await self._step("review", "running"); await self._emit("review_required", self.to_dict()); self.review_event = asyncio.Event(); await self.review_event.wait(); self.status = "running"
                else: await self._step("review", "done", "项目设置已跳过卷规划审核")
            while self.current_chapter <= self.input.chapters:
                self.current_step = "chapter_outline"; await self.step_chapter_outline()
                if not self.input.skip_outline_review:
                    self.current_step = "chapter_review"; self.status = "awaiting_chapter_review"; await self._step("chapter_review", "running"); await self._emit("chapter_review_required", self.to_dict()); self.review_event = asyncio.Event(); await self.review_event.wait(); self.status = "running"
                else: await self._step("chapter_review", "done", "项目设置已跳过细纲审核")
                for no in range(self.current_chapter, self.batch_end + 1):
                    self.current_chapter = no; await self._emit("progress", {"message": f"正在处理第 {no}/{self.input.chapters} 章", "chapter": no, "total": self.input.chapters}); await self.step_chapter(no)
                self.current_chapter = self.batch_end + 1; self._save_state()
                if not self.input.skip_body_review:
                    self.current_step = "body_review"; self.status = "awaiting_body_review"; await self._step("body_review", "running"); await self._emit("body_review_required", self.to_dict()); self.review_event = asyncio.Event(); await self.review_event.wait(); self.status = "running"
                else: await self._step("body_review", "done", "项目设置已跳过正文审核")
                self.chapter_outline = ""
                chapters_per_volume = max(1, math.ceil(self.input.volume_words / self.input.words_per_chapter))
                volume_end = min(self.current_volume * chapters_per_volume, self.input.chapters)
                if self.current_chapter > volume_end and self.current_chapter <= self.input.chapters:
                    handoff = "# 第 %d 卷交接信息\n\n%s" % (self.current_volume, self.continuity)
                    self._write(f"volumes/{self.current_volume:03d}/handoff.md", handoff)
                    self.current_step = "handoff_review"; self.status = "awaiting_handoff_review"
                    await self._step("handoff_review", "running", handoff); await self._emit("handoff_review_required", self.to_dict())
                    self.review_event = asyncio.Event(); await self.review_event.wait(); self.status = "running"
                    self.current_volume += 1; self.volume_plan = ""
                    self.steps["outline"] = StepState("outline", "本卷规划")
                    self.steps["review"] = StepState("review", "卷规划审核")
                    self.current_step = "outline"; await self.step_volume_plan()
                    if not self.input.skip_volume_review:
                        self.current_step = "review"; self.status = "awaiting_review"; await self._step("review", "running"); await self._emit("review_required", self.to_dict()); self.review_event = asyncio.Event(); await self.review_event.wait(); self.status = "running"
                    else: await self._step("review", "done", "项目设置已跳过卷规划审核")
                if self.current_chapter > self.input.chapters: break
            self.current_step = "archiver"; await self.step_archive(); self.status = "done"; self.finished_at = datetime.now().isoformat(timespec="seconds"); self._save_state(); await self._emit("done", self.to_dict())
        except Exception as exc:
            self.status, self.error_message = "error", str(exc); self.finished_at = datetime.now().isoformat(timespec="seconds")
            if self.current_step in self.steps: await self._step(self.current_step, "error", error=str(exc))
            self._save_state(); await self._emit("error", {"message": str(exc), **self.to_dict()})
        finally: await self.llm.close()

    def to_dict(self, include_outputs: bool = True) -> dict[str, Any]:
        steps = {k: v.to_dict() for k, v in self.steps.items()}
        if not include_outputs:
            for step in steps.values(): step["output"] = ""
        return {"id": self.id, "status": self.status, "current_step": self.current_step, "current_chapter": self.current_chapter, "current_volume": self.current_volume, "batch_end": self.batch_end, "error_message": self.error_message, "created_at": self.created_at, "finished_at": self.finished_at, "input": asdict(self.input), "steps": steps, "archive_path": self.archive_path, "output_dir": str(self.output_dir), "chapters_count": len([c for c in self.chapters if c.get("polished")]), "chapter_states": [{"no": c["no"], "has_draft": bool(c.get("draft")), "has_polished": bool(c.get("polished")), "memory_updated": bool(c.get("memory_updated"))} for c in self.chapters], "memory": {str(c["no"]): bool(c.get("memory_updated")) for c in self.chapters}}
