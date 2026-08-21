# -*- coding: utf-8 -*-
"""统一事件流 → Markdown。支持三种导出模式。"""
import json
import re

# 从指令正文里取一行做短标题：首个非空行、去掉行首的 markdown 标记、限长
_HEAD_STRIP_RE = re.compile(r"^[#>\-\*\s`]+")


def _first_line(text, cap=48):
    for ln in (text or "").splitlines():
        ln = _HEAD_STRIP_RE.sub("", ln.strip())
        if ln:
            return ln[:cap] + ("…" if len(ln) > cap else "")
    return "（空指令）"

MODE_PLAIN = "plain"    # 纯对话
MODE_TOOLS = "tools"    # 对话+工具
MODE_FULL = "full"      # 完整原始

MODE_LABELS = {MODE_PLAIN: "纯对话", MODE_TOOLS: "对话+工具", MODE_FULL: "完整原始"}
RESULT_CAP = 2000       # tools 模式下工具结果的最大字符数


def _truncate(s, cap):
    s = s or ""
    if cap and len(s) > cap:
        return s[:cap] + f"\n…（已截断，原文共 {len(s)} 字符）"
    return s


def _fmt_input(inp):
    """工具入参格式化：Codex 的 arguments 是 JSON 字符串，尽量美化。"""
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except Exception:
            return inp
    if isinstance(inp, (dict, list)):
        try:
            return json.dumps(inp, ensure_ascii=False, indent=2)
        except Exception:
            return str(inp)
    return str(inp)


def convert(events, meta, mode=MODE_TOOLS):
    out = []
    title = (meta.get("title") or "未命名会话").strip()
    out.append(f"# {title}\n")
    src = "Claude CLI" if meta.get("source") == "claude" else "Codex"
    info = [f"来源：{src}"]
    if meta.get("project"):
        info.append(f"项目：{meta['project']}")
    if meta.get("created"):
        info.append(f"创建：{meta['created']}")
    if meta.get("modified"):
        info.append(f"改动：{meta['modified']}")
    info.append(f"导出模式：{MODE_LABELS.get(mode, mode)}")
    out.append("> " + " · ".join(info))
    out.append("\n---\n")

    user_no = 0
    for e in events:
        k = e.get("kind")
        if k == "user":
            # 每条用户指令生成「## N. 首行摘要」短标题，正文原样放在下面。
            # 这样任何 Markdown 编辑器（Typora / VS Code / Obsidian / 语雀）都能
            # 自动生成可点击的大纲/目录，且无需把长正文改成标题格式。
            user_no += 1
            out.append(f"## {user_no}. {_first_line(e['text'])}\n")
            out.append(f"**🧑 我：**\n\n{e['text']}\n")
        elif k == "assistant":
            out.append(f"**🤖 AI：**\n\n{e['text']}\n")
        elif k == "thinking":
            if mode == MODE_FULL:
                out.append(f"<details>\n<summary>💭 思考</summary>\n\n{e['text']}\n\n</details>\n")
        elif k == "system":
            if mode == MODE_FULL:
                out.append(f"<details>\n<summary>⚙ 系统消息</summary>\n\n```\n{e['text']}\n```\n\n</details>\n")
        elif k == "tool_call":
            if mode in (MODE_TOOLS, MODE_FULL):
                inp = _fmt_input(e.get("input"))
                if mode != MODE_FULL:
                    inp = _truncate(inp, RESULT_CAP)
                out.append(f"**🔧 工具调用：{e.get('name', '')}**\n\n```json\n{inp}\n```\n")
        elif k == "tool_result":
            if mode in (MODE_TOOLS, MODE_FULL):
                cap = None if mode == MODE_FULL else RESULT_CAP
                body = _truncate(e.get("output", ""), cap)
                out.append(f"**↳ 结果**\n\n```\n{body}\n```\n")
    return "\n".join(out).rstrip() + "\n"
