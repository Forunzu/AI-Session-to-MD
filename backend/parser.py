# -*- coding: utf-8 -*-
"""解析 Claude CLI 与 Codex 的 JSONL 会话为统一事件流。

统一事件 (event)：
  {"kind": "user"|"assistant", "text": str}
  {"kind": "thinking", "text": str}          # 仅“完整原始”模式用
  {"kind": "tool_call",   "name": str, "input": any}
  {"kind": "tool_result", "output": str}
  {"kind": "system", "text": str}            # 仅“完整原始”模式用
"""
import json
import re

# 用户消息里常见的“包裹上下文/系统噪音”，正常模式整段跳过
_NOISE_PREFIXES = (
    "<environment_context",
    "<permissions instructions",
    "# AGENTS.md instructions",
    "<turn_aborted",
    "# Files mentioned by the user",
    "<system-reminder",
    "--- CONTEXT ENTRY BEGIN ---",
    "<command-name>",
    "<local-command",
)

# Claude 会话里真正的用户输入被包在 USER MESSAGE BEGIN/END 之间
_USER_MSG_RE = re.compile(r"USER MESSAGE BEGIN ---\s*(.*?)\s*--- USER MESSAGE END", re.S)


def read_json_lines(path, max_lines=None):
    """逐行读取 JSONL，UTF-8，容错跳过坏行。"""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if max_lines is not None and i >= max_lines:
                break
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def clean_user_text(text):
    """剥离包裹上下文，提取真正的用户消息。"""
    if not text:
        return ""
    m = _USER_MSG_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def is_noise(text):
    if not text:
        return True
    return text.lstrip().startswith(_NOISE_PREFIXES)

def _stringify(content):
    """把 tool_result / output 等内容统一转成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                parts.append(str(b.get("text", b.get("content", ""))))
            else:
                parts.append(str(b))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        return str(content.get("text", content.get("content",
                   json.dumps(content, ensure_ascii=False))))
    return str(content)


def _extract_codex_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(b.get("text", "")) for b in content if isinstance(b, dict)]
        return "\n".join(p for p in parts if p)
    return ""


# ------------------------- Claude -------------------------
def parse_claude(path, max_lines=None):
    events = []
    for o in read_json_lines(path, max_lines):
        if o.get("type") not in ("user", "assistant"):
            continue
        msg = o.get("message") or {}
        role = msg.get("role", o.get("type"))
        content = msg.get("content")
        if isinstance(content, str):
            raw = content.strip()
            txt = clean_user_text(raw) if role == "user" else raw
            if txt:
                events.append({"kind": role, "text": txt,
                               "noise": role == "user" and is_noise(raw)})
            continue
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    raw = b.get("text", "").strip()
                    txt = clean_user_text(raw) if role == "user" else raw
                    if txt:
                        events.append({"kind": role, "text": txt,
                                       "noise": role == "user" and is_noise(raw)})
                elif bt == "thinking":
                    th = (b.get("thinking") or b.get("text") or "").strip()
                    if th:
                        events.append({"kind": "thinking", "text": th})
                elif bt == "tool_use":
                    events.append({"kind": "tool_call", "name": b.get("name", ""),
                                   "input": b.get("input", {})})
                elif bt == "tool_result":
                    events.append({"kind": "tool_result",
                                   "output": _stringify(b.get("content"))})
    return events

# ------------------------- Codex -------------------------
def parse_codex(path, max_lines=None):
    """Codex 在 resume/压缩时会重放历史，用户消息被反复注入。
    策略：用户回合只取 event_msg.user_message（已去掉上下文包裹），role=user 的
    response_item 一律丢弃；再对“中间没有助手/工具回合的相邻重复用户消息”折叠，
    去掉重放，同时保留真正被隔开的重复输入（如多次“继续”）。"""
    events = []
    last_user = None          # 最近一次已输出的用户消息文本
    turn_progressed = True     # 自上次用户消息后，是否出现过助手/工具回合
    for o in read_json_lines(path, max_lines):
        p = o.get("payload")
        if not isinstance(p, dict):
            continue
        pt = p.get("type")
        if pt == "user_message":
            raw = p.get("message")
            txt = clean_user_text(raw if isinstance(raw, str) else _extract_codex_text(raw))
            if not txt or is_noise(txt):
                continue
            if txt == last_user and not turn_progressed:
                continue  # 相邻重放，折叠
            events.append({"kind": "user", "text": txt})
            last_user = txt
            turn_progressed = False
        elif pt == "message":
            role = p.get("role")
            txt = _extract_codex_text(p.get("content")).strip()
            if not txt:
                continue
            if role == "assistant":
                events.append({"kind": "assistant", "text": txt})
                turn_progressed = True
            elif role in ("system", "developer"):
                events.append({"kind": "system", "text": txt})
        elif pt == "reasoning":
            th = _extract_codex_text(p.get("content") or p.get("summary")).strip()
            if th:
                events.append({"kind": "thinking", "text": th})
        elif pt == "function_call":
            events.append({"kind": "tool_call", "name": p.get("name", ""),
                           "input": p.get("arguments", "")})
            turn_progressed = True
        elif pt == "function_call_output":
            events.append({"kind": "tool_result", "output": _stringify(p.get("output"))})
            turn_progressed = True
    return events


def parse(path, source):
    return parse_codex(path) if source == "codex" else parse_claude(path)
