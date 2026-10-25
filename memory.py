# memory.py — 可逆的分层上下文记忆（借鉴 ACP 的摘要 + 归档思路）
"""
设计目标：
1. 代码负责预算、轮次边界和“最近 N 轮不可压缩”的硬保护；
2. 模型只负责写“哪些信息值得留下”的摘要；
3. 被替换的原始消息完整归档，之后可检索、可解压；
4. 旧的 manage_context 自动压缩仍保留，作为模型不主动压缩时的兜底。

存储位置：<workspace>/.hqin_memory/
  index.json          归档索引（只含摘要和元数据，不含原文）
  blocks/b0001.json   单块原始消息 + 摘要
  restored/           解压到文件的副本
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

import config
import workspace as _ws

# 这些 user 消息是“记忆摘要”，不是新的用户轮次；轮次划分时必须跳过。
MEMORY_MARKERS = ("[历史记忆", "[历史操作摘要]")
SUMMARY_HEADER_RE = re.compile(
    r"^\[历史记忆 (b\d{4}) \| 已压缩 turn (\d+)[–-](\d+)(?: \| ([^\]]*))?\]"
)
BLOCK_ID_RE = re.compile(r"^b\d{4}$")
NOTICE_MARKER = "\n\n[HQIN_CONTEXT_NOTICE]\n"
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

_ACTIVE_MESSAGES: list[dict] | None = None
_STORE: "MemoryStore | None" = None
# 最近一次主请求由 API 实际返回的 usage（LM Studio 的 prompt_tokens）。
# 仅作为显示层的校准参考，不参与任何预算决策。
_REAL_USAGE: dict | None = None


# ──────────────────────────────────────────────
# 估算与轮次
# ──────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """粗略估算 token：中文按 1 字/token，其他按 3.5 字符/token。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return max(1, int(round(cjk + other / 3.5)))


def message_text(msg: dict) -> str:
    """把一个消息的可计费内容拼成文本，包含 tool_calls。"""
    parts: list[str] = []
    content = msg.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif content is not None:
        parts.append(json.dumps(content, ensure_ascii=False))
    for tc in msg.get("tool_calls") or []:
        parts.append(json.dumps(tc, ensure_ascii=False))
    return "\n".join(parts)


def estimate_messages_tokens(messages: list[dict]) -> int:
    # 每条消息的角色、分隔符和协议开销按 4 token 计。
    return sum(estimate_tokens(message_text(m)) + 4 for m in messages)


def is_memory_message(msg: dict) -> bool:
    content = msg.get("content")
    return isinstance(content, str) and content.startswith(MEMORY_MARKERS)


def _archived_turn_count(messages: list[dict]) -> int:
    """读取摘要头，得到已经归档的绝对轮次数量。"""
    archived = 0
    for msg in messages:
        if not is_memory_message(msg):
            continue
        match = SUMMARY_HEADER_RE.match(msg.get("content") or "")
        if match:
            archived = max(archived, int(match.group(3)))
    return archived


def turn_ranges(messages: list[dict]) -> list[dict]:
    """
    按完整用户轮次切分。一个轮次从 user 开始，到下一条 user 前结束，
    因此 assistant/tool 消息不会被拆开。记忆摘要消息不算新轮次。
    返回 [{turn, local_turn, start, end}]；turn 是跨压缩稳定的绝对轮次，
    local_turn 是当前可见轮次中的序号，end 为开区间。
    """
    ranges: list[dict] = []
    start: int | None = None
    archived = _archived_turn_count(messages)
    local_turn = 0
    for index, msg in enumerate(messages):
        if index == 0 and msg.get("role") == "system":
            continue
        if msg.get("role") != "user" or is_memory_message(msg):
            continue
        if start is not None:
            ranges.append({
                "turn": archived + local_turn,
                "local_turn": local_turn,
                "start": start,
                "end": index,
            })
        local_turn += 1
        start = index
    if start is not None:
        ranges.append({
            "turn": archived + local_turn,
            "local_turn": local_turn,
            "start": start,
            "end": len(messages),
        })
    return ranges


def _range_tokens(messages: list[dict], item: dict) -> int:
    return estimate_messages_tokens(messages[item["start"]:item["end"]])


def _preview(text: str, limit: int = 70) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _limit_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原长度 {len(text)} 字符）"


# ──────────────────────────────────────────────
# 归档存储
# ──────────────────────────────────────────────

def _default_root() -> Path:
    try:
        base = Path(_ws.get_workspace_str())
    except RuntimeError:
        base = Path(os.getcwd())
    return base / config.MEMORY_DIR


class MemoryStore:
    """按工作区持久化的摘要块仓库。"""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else _default_root()
        self.blocks_dir = self.root / "blocks"
        self.restored_dir = self.root / "restored"
        self.index_path = self.root / "index.json"
        self._index = self._load_index()

    def _load_index(self) -> list[dict]:
        if not self.index_path.exists():
            return []
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data = data.get("blocks", [])
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save_index(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.index_path)

    def _next_id(self) -> str:
        numbers = []
        for item in self._index:
            block_id = item.get("id", "")
            if BLOCK_ID_RE.match(block_id):
                numbers.append(int(block_id[1:]))
        if self.blocks_dir.exists():
            for path in self.blocks_dir.glob("b*.json"):
                if BLOCK_ID_RE.match(path.stem):
                    numbers.append(int(path.stem[1:]))
        return f"b{max(numbers, default=0) + 1:04d}"

    def archive(
        self,
        messages: list[dict],
        summary: str,
        topic: str,
        start_turn: int,
        end_turn: int,
    ) -> dict:
        block_id = self._next_id()
        created_at = datetime.now().isoformat(timespec="seconds")
        block = {
            "id": block_id,
            "created_at": created_at,
            "topic": topic,
            "start_turn": start_turn,
            "end_turn": end_turn,
            "summary": summary,
            "message_count": len(messages),
            "tokens": estimate_messages_tokens(messages),
            "messages": messages,
        }
        self.blocks_dir.mkdir(parents=True, exist_ok=True)
        block_path = self.blocks_dir / f"{block_id}.json"
        block_path.write_text(
            json.dumps(block, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._index.append({
            "id": block_id,
            "created_at": created_at,
            "topic": topic,
            "start_turn": start_turn,
            "end_turn": end_turn,
            "summary": summary,
            "message_count": block["message_count"],
            "tokens": block["tokens"],
        })
        self._save_index()
        return block

    def get(self, block_id: str) -> dict:
        if not BLOCK_ID_RE.match(block_id or ""):
            raise ValueError(f"非法归档块编号: {block_id!r}")
        path = self.blocks_dir / f"{block_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"归档块不存在: {block_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict]:
        return list(self._index)


def get_store() -> MemoryStore:
    global _STORE
    if _STORE is None:
        _STORE = MemoryStore()
    return _STORE


def set_store(store: MemoryStore | None) -> None:
    global _STORE
    _STORE = store


def set_active_messages(messages: list[dict] | None) -> None:
    global _ACTIVE_MESSAGES
    _ACTIVE_MESSAGES = messages


def reset_state() -> None:
    """测试或切换工作区时清空进程内状态（不删除磁盘归档）。"""
    global _ACTIVE_MESSAGES, _STORE, _REAL_USAGE
    _ACTIVE_MESSAGES = None
    _STORE = None
    _REAL_USAGE = None


# ──────────────────────────────────────────────
# 实测用量记录与按角色分解（仅用于显示校准）
# ──────────────────────────────────────────────

def record_real_usage(prompt_tokens, completion_tokens: int = 0,
                      messages: list[dict] | None = None) -> None:
    """
    记录最近一次主模型请求返回的 usage，并同步快照当时的原始估算。
    零值、非法值（个别 OpenAI 兼容服务不上报 usage）不覆盖上一次的有效测量。
    """
    global _REAL_USAGE
    try:
        prompt = int(prompt_tokens or 0)
    except (TypeError, ValueError):
        return
    if prompt <= 0:
        return
    try:
        completion = int(completion_tokens or 0)
    except (TypeError, ValueError):
        completion = 0
    msgs = _ACTIVE_MESSAGES if messages is None else messages
    raw = estimate_messages_tokens(msgs) if isinstance(msgs, list) else 0
    _REAL_USAGE = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "raw_estimate": raw,
    }


def get_real_usage() -> dict | None:
    return _REAL_USAGE


def reset_real_usage() -> None:
    global _REAL_USAGE
    _REAL_USAGE = None


def is_calibrated() -> bool:
    """是否已有 API 实测锚点（可用于校准估算）。"""
    return _REAL_USAGE is not None


def _calibration_offset() -> int:
    """
    实测与原始估算的系统性差值。工具 Schema、chat template 等每次请求
    的固定开销都包含在这个差值里；校准估算 = 原始估算 + 差值。
    """
    if not _REAL_USAGE:
        return 0
    return _REAL_USAGE["prompt_tokens"] - _REAL_USAGE.get("raw_estimate", 0)


def calibrated_messages_tokens(messages: list[dict]) -> int:
    """
    校准估算：锚定最近一次 API 实测，只对之后的增量做本地估算。
    没有锚点时退化为原始估算。预算判断和显示层统一使用本口径。
    """
    raw = estimate_messages_tokens(messages)
    offset = _calibration_offset()
    if not offset:
        return raw
    return max(1, raw + offset)


def context_usage_breakdown(messages: list[dict]) -> dict:
    """
    按角色分解的 token 估算，与 estimate_messages_tokens 严格同口径
    （含每条消息 +4 的协议开销），保证各部分之和等于总量。
    """
    roles: dict[str, dict] = {}
    total = 0
    for msg in messages:
        role = str(msg.get("role") or "?")
        entry = roles.setdefault(role, {"messages": 0, "tokens": 0})
        tokens = estimate_tokens(message_text(msg)) + 4
        entry["messages"] += 1
        entry["tokens"] += tokens
        total += tokens
    ordered = {
        role: roles[role]
        for role in sorted(roles, key=lambda name: (-roles[name]["tokens"], name))
    }
    return {"roles": ordered, "total": total, "messages": len(messages)}


def _require_messages():
    if _ACTIVE_MESSAGES is None:
        return "Error: 记忆上下文未初始化；这是 Harness 内部错误，请重试。"
    return _ACTIVE_MESSAGES


# ──────────────────────────────────────────────
# 预算
# ──────────────────────────────────────────────

def over_soft_budget(messages: list[dict]) -> bool:
    return calibrated_messages_tokens(messages) > (
        config.MODEL_CONTEXT_TOKENS * config.CONTEXT_SOFT_RATIO
    )


def over_hard_budget(messages: list[dict]) -> bool:
    return calibrated_messages_tokens(messages) > (
        config.MODEL_CONTEXT_TOKENS * config.CONTEXT_HARD_RATIO
    )


# ──────────────────────────────────────────────
# 工具实现
# ──────────────────────────────────────────────

def context_status(messages: list[dict] | None = None) -> str:
    """查看上下文用量、可压缩轮次和已有归档块。"""
    msgs = _ACTIVE_MESSAGES if messages is None else messages
    if not isinstance(msgs, list):
        return msgs

    ranges = turn_ranges(msgs)
    keep = min(max(0, config.MEMORY_KEEP_RECENT_TURNS), len(ranges))
    compressible = ranges[:-keep] if keep else list(ranges)

    tokens = calibrated_messages_tokens(msgs)
    budget = max(1, config.MODEL_CONTEXT_TOKENS)
    ratio = tokens / budget * 100

    lines = [
        "📚 上下文状态（校准估算）" if is_calibrated() else "📚 上下文状态（估算）",
        f"- 消息: {len(msgs)} 条 | 轮次: {len(ranges)} | token: "
        f"{tokens:,} / {budget:,} ({ratio:.0f}%)",
    ]
    real = get_real_usage()
    if real:
        raw = estimate_messages_tokens(msgs)
        drift = (raw - real["prompt_tokens"]) / real["prompt_tokens"] * 100
        lines.append(
            f"- 最近实测: {real['prompt_tokens']:,} prompt tokens"
            f"（当前用量已按其校准为 {tokens:,}；未校准估算 {raw:,}，"
            f"系统偏差 {drift:+.1f}%）"
        )
    if compressible:
        lines.append(
            f"- 可压缩: turn {compressible[0]['turn']}–{compressible[-1]['turn']} "
            f"（共 {len(compressible)} 轮）"
        )
    else:
        lines.append("- 可压缩: 无（当前只剩受保护轮次）")
    if keep:
        lines.append(
            f"- 受保护: 最近 {keep} 轮（turn {ranges[-keep]['turn']}–{ranges[-1]['turn']}）"
        )
    else:
        lines.append("- 受保护: 无用户轮次")

    lines.append("按轮次:")
    for item in ranges:
        protected = item["local_turn"] > (len(ranges) - keep)
        mark = "受保护" if protected else "可压缩"
        first_user = msgs[item["start"]].get("content") or ""
        lines.append(
            f"  turn {item['turn']} | 消息 {item['start'] + 1}–{item['end']} | "
            f"{_range_tokens(msgs, item):,} tokens | {mark} | {_preview(first_user)}"
        )

    summaries = [m for m in msgs if is_memory_message(m)]
    if summaries:
        lines.append(f"上下文中的记忆摘要: {len(summaries)} 条")
        for msg in summaries[-3:]:
            text = msg.get("content") or ""
            match = SUMMARY_HEADER_RE.match(text)
            label = match.group(1) if match else "?"
            lines.append(f"  {label} | {_preview(text.splitlines()[0], 90)}")

    blocks = get_store().list()
    lines.append(f"磁盘归档块: {len(blocks)}")
    for entry in blocks[-10:]:
        lines.append(
            f"  {entry['id']} | turn {entry['start_turn']}–{entry['end_turn']} | "
            f"{entry.get('tokens', 0):,} tokens | {_preview(entry.get('topic', ''), 40)}"
        )
    if len(blocks) > 10:
        lines.append(f"  … 还有 {len(blocks) - 10} 个，可用 search_context 查找")
    return _limit_text("\n".join(lines), 6000)


def compress_context(
    start_turn: int,
    end_turn: int,
    summary: str,
    topic: str = "",
) -> str:
    """把连续的旧轮次压缩成一个摘要块；原始消息归档到磁盘。"""
    msgs = _require_messages()
    if isinstance(msgs, str):
        return msgs

    try:
        start = int(start_turn)
        end = int(end_turn)
    except (TypeError, ValueError):
        return "Error: start_turn/end_turn 必须是整数。"
    summary = (summary or "").strip()
    topic = " ".join((topic or "").split()).replace("]", "")[:60]
    if not summary:
        return "Error: summary 不能为空；请写成自包含摘要。"
    if len(summary) > config.MEMORY_SUMMARY_MAX_CHARS:
        return (
            f"Error: summary 过长（{len(summary)} 字符），"
            f"上限 {config.MEMORY_SUMMARY_MAX_CHARS} 字符。"
        )

    ranges = turn_ranges(msgs)
    keep = min(max(0, config.MEMORY_KEEP_RECENT_TURNS), len(ranges))
    max_compressible = len(ranges) - keep
    if max_compressible < 1:
        return "Error: 当前没有可压缩的旧轮次；当前任务和最近轮次受保护。"
    first_turn = ranges[0]["turn"]
    max_turn = ranges[max_compressible - 1]["turn"]
    if start < first_turn or end < start or end > max_turn:
        return (
            f"Error: 轮次范围无效（请求 turn {start}–{end}，"
            f"当前可压缩 turn {first_turn}–{max_turn}）。\n"
            + context_status(msgs)
        )

    archived = _archived_turn_count(msgs)
    start_local = start - archived
    end_local = end - archived
    if start_local < 1 or end_local > max_compressible:
        return (
            f"Error: 轮次范围无效（请求 turn {start}–{end}，"
            f"当前可压缩 turn {first_turn}–{max_turn}）。\n"
            + context_status(msgs)
        )

    start_idx = ranges[start_local - 1]["start"]
    end_idx = ranges[end_local - 1]["end"]
    original = msgs[start_idx:end_idx]
    before = calibrated_messages_tokens(msgs)

    try:
        block = get_store().archive(
            messages=original,
            summary=summary,
            topic=topic,
            start_turn=start,
            end_turn=end,
        )
    except Exception as exc:
        return f"Error: 归档失败 - {exc}"

    header = f"[历史记忆 {block['id']} | 已压缩 turn {start}–{end}"
    if topic:
        header += f" | {topic}"
    header += "]\n"
    msgs[start_idx:end_idx] = [{"role": "user", "content": header + summary}]
    after = calibrated_messages_tokens(msgs)

    return (
        f"Success: 已压缩 turn {start}–{end} → {block['id']}，"
        f"归档 {len(original)} 条原始消息。\n"
        f"上下文约 {before:,} → {after:,} tokens。\n"
        f"需要旧细节时调用 search_context 或 decompress_context。"
    )


def _transcript(messages: list[dict]) -> str:
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = msg.get("content")
        if isinstance(content, str) and content:
            lines.append(f"[{role}] {content}")
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            lines.append(
                f"[{role}/tool_call] {fn.get('name', '?')} {fn.get('arguments', '')}"
            )
    return "\n".join(lines)


def decompress_context(block_id: str, mode: str = "preview") -> str:
    """读取归档块：preview 摘要、full 全文、file 写到 restored/。"""
    block_id = (block_id or "").strip()
    mode = (mode or "preview").strip().lower()
    try:
        block = get_store().get(block_id)
    except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        return f"Error: {exc}"

    if mode == "preview":
        body = _transcript(block.get("messages", []))
        return (
            f"归档块 {block['id']} | turn {block['start_turn']}–{block['end_turn']} | "
            f"{block.get('message_count', 0)} 条 | {block.get('tokens', 0):,} tokens\n"
            f"主题: {block.get('topic') or '(未命名)'}\n"
            f"摘要:\n{block.get('summary', '')}\n\n"
            f"原文预览:\n{_limit_text(body, 1500)}"
        )

    if mode == "full":
        body = _transcript(block.get("messages", []))
        return (
            f"归档块 {block['id']} 全文（{block.get('message_count', 0)} 条）:\n"
            + _limit_text(body, config.MEMORY_DECOMPRESS_LIMIT)
        )

    if mode == "file":
        store = get_store()
        store.restored_dir.mkdir(parents=True, exist_ok=True)
        path = store.restored_dir / f"{block_id}.json"
        path.write_text(
            json.dumps(block, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            rel = os.path.relpath(path, _ws.get_workspace_str())
        except (RuntimeError, ValueError):
            rel = str(path)
        return f"Success: 已写入 {rel}（完整 JSON，{path.stat().st_size:,} 字节）。"

    return "Error: mode 只能是 preview、full 或 file。"


def _snippet(text: str, query: str, width: int = 70) -> str:
    lower, needle = text.lower(), query.lower()
    index = lower.find(needle)
    if index < 0:
        return _preview(text, width * 2)
    start = max(0, index - width)
    end = min(len(text), index + len(query) + width)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return prefix + " ".join(text[start:end].split()) + suffix


def search_context(query: str, limit: int = 3) -> str:
    """在归档块的摘要和原始消息中做简单子串检索。"""
    query = (query or "").strip()
    if not query:
        return "Error: query 不能为空。"
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = config.MEMORY_SEARCH_LIMIT

    store = get_store()
    scored: list[tuple[int, dict, list[str]]] = []
    for entry in store.list():
        try:
            block = store.get(entry["id"])
        except (ValueError, FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        score = 0
        snippets: list[str] = []
        if query.lower() in (block.get("summary") or "").lower():
            score += 5
            snippets.append("摘要: " + _snippet(block.get("summary", ""), query))
        messages = block.get("messages", [])
        for msg in messages:
            text = message_text(msg)
            if query.lower() in text.lower():
                score += 2 if msg.get("role") == "user" else 1
                if len(snippets) < 3:
                    snippets.append(f"{msg.get('role')}: {_snippet(text, query)}")
        if score:
            scored.append((score, block, snippets))

    scored.sort(key=lambda item: (-item[0], item[1]["id"]))
    if not scored:
        return f"没有找到包含“{query}”的归档块。可用 context_status 查看已有块。"

    lines = [f"找到 {min(len(scored), max(1, limit))} 个相关归档块（按相关度）:"]
    for score, block, snippets in scored[:max(1, limit)]:
        lines.append(
            f"\n[{block['id']}] turn {block['start_turn']}–{block['end_turn']} | "
            f"topic: {block.get('topic') or '(未命名)'} | 相关度 {score}"
        )
        for snippet in snippets:
            lines.append("  " + snippet)
        lines.append(f"  查看: decompress_context(block_id=\"{block['id']}\")")
    return _limit_text("\n".join(lines), 5000)


def context_notice(messages: list[dict] | None = None) -> str:
    """超过软阈值时给模型的一段短提示；没有可压缩轮次时返回空串。"""
    msgs = _ACTIVE_MESSAGES if messages is None else messages
    if not isinstance(msgs, list):
        return ""
    if not over_soft_budget(msgs):
        return ""
    ranges = turn_ranges(msgs)
    keep = min(max(0, config.MEMORY_KEEP_RECENT_TURNS), len(ranges))
    compressible = len(ranges) - keep
    if compressible < 1:
        return ""

    tokens = calibrated_messages_tokens(msgs)
    ratio = tokens / max(1, config.MODEL_CONTEXT_TOKENS) * 100
    first_turn = ranges[0]["turn"]
    last_turn = ranges[compressible - 1]["turn"]
    return (
        f"📚 上下文约 {ratio:.0f}%（{tokens:,}/{config.MODEL_CONTEXT_TOKENS:,} tokens）。"
        f"可压缩旧轮次 turn {first_turn}–{last_turn}；当前任务和最近 {keep} 轮受保护。"
        "若当前任务不依赖旧细节，先 context_status，再调用 "
        f"compress_context(start_turn={first_turn}, end_turn={last_turn}, summary=...)。"
        "需要旧细节时用 search_context/decompress_context，不要凭记忆猜。"
    )


def apply_notice(messages: list[dict], base_system_prompt: str) -> None:
    """把动态上下文提示附加到唯一的 system 消息，避免插入额外 system 消息。"""
    if not messages or messages[0].get("role") != "system":
        return
    base = base_system_prompt.split(NOTICE_MARKER)[0]
    notice = context_notice(messages)
    messages[0]["content"] = base + (NOTICE_MARKER + notice if notice else "")
