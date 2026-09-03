# -*- coding: utf-8 -*-
"""扫描 Claude / Codex 会话目录，产出会话列表（只读文件头，避免整读大文件）。"""
import os
import re
import json
import datetime

from . import parser

HOME = os.path.expanduser("~")
HEAD_LINES = 250   # 提取标题/项目时只读文件头这么多行

_CODEX_DATE_RE = re.compile(r"rollout-(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})")


def _claude_root():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    return os.path.join(base, "projects")


def _codex_root():
    base = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
    return os.path.join(base, "sessions")


def default_sources():
    """启动时自动读取的默认来源目录。"""
    return [
        {"type": "claude", "label": "Claude CLI", "path": _claude_root()},
        {"type": "codex", "label": "Codex", "path": _codex_root()},
    ]


def sniff_format(path):
    """读文件头几行判断格式：有 payload -> codex；有 message/type -> claude。"""
    try:
        for o in parser.read_json_lines(path, max_lines=15):
            if "payload" in o:
                return "codex"
            if "message" in o or o.get("type") in ("user", "assistant", "summary"):
                return "claude"
    except Exception:
        pass
    return "claude"


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _peek(path, source):
    """读文件头，返回 (title, project, cwd)。cwd 供迁移功能用作默认目标工作目录。"""
    title, project, cwd = "", "", ""
    try:
        events = (parser.parse_codex(path, max_lines=HEAD_LINES) if source == "codex"
                  else parser.parse_claude(path, max_lines=HEAD_LINES))
        for e in events:
            if e.get("kind") == "user" and e.get("text"):
                title = " ".join(e["text"].split())[:80]
                break
        if not title:
            for e in events:
                if e.get("kind") == "assistant" and e.get("text"):
                    title = " ".join(e["text"].split())[:80]
                    break
    except Exception:
        pass
    try:
        # Claude 每行都带 cwd；Codex 的工作目录在首行 session_meta.payload.cwd
        for o in parser.read_json_lines(path, max_lines=HEAD_LINES):
            if source == "codex":
                if o.get("type") == "session_meta":
                    cwd = str((o.get("payload") or {}).get("cwd") or "")
                    break
            elif o.get("cwd"):
                cwd = str(o["cwd"])
                break
    except Exception:
        pass
    if cwd:
        project = os.path.basename(cwd.rstrip("/\\"))
    return title or "（无文本内容）", project, cwd


def _iso_to_local(s):
    """ISO8601（Claude 每行的 timestamp）→ ('YYYY-MM-DD HH:MM', epoch) 或 None。"""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M"), dt.timestamp()
    except Exception:
        return None


def _codex_created(path):
    """Codex 文件名 rollout-日期 即会话创建时间。"""
    m = _CODEX_DATE_RE.search(os.path.basename(path))
    if m:
        try:
            dt = datetime.datetime(*[int(g) for g in m.groups()])
            return dt.strftime("%Y-%m-%d %H:%M"), dt.timestamp()
        except Exception:
            pass
    return None


def _claude_created(path):
    """Claude 会话取文件头若干行里首个带 timestamp 的行作为创建时间。"""
    try:
        for o in parser.read_json_lines(path, max_lines=8):
            r = _iso_to_local(o.get("timestamp"))
            if r:
                return r
    except Exception:
        pass
    return None


def scan(sources):
    """sources: [{type,label,path}]。返回会话字典列表，按日期倒序。"""
    items = []
    for src in sources:
        root = src.get("path", "")
        if not root or not os.path.isdir(root):
            continue
        forced = src.get("type") if src.get("type") in ("claude", "codex") else None
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".jsonl"):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                fmt = forced or sniff_format(full)
                mtime_ts = st.st_mtime
                modified = datetime.datetime.fromtimestamp(mtime_ts).strftime("%Y-%m-%d %H:%M")
                title, project, cwd = _peek(full, fmt)
                created = _codex_created(full) if fmt == "codex" else _claude_created(full)
                if created:
                    created_str, created_ts = created
                else:
                    # 回退：文件创建时间(Windows 上 st_ctime 即创建)，再退最后修改时间
                    created_ts = getattr(st, "st_ctime", mtime_ts) or mtime_ts
                    created_str = datetime.datetime.fromtimestamp(created_ts).strftime("%Y-%m-%d %H:%M")
                items.append({
                    "id": full, "path": full, "source": fmt,
                    "group_label": src.get("label", fmt),
                    "title": title, "project": project, "cwd": cwd,
                    "created": created_str, "created_ts": created_ts,
                    "modified": modified, "modified_ts": mtime_ts,
                    "size": _fmt_size(st.st_size), "bytes": st.st_size,
                })
    items.sort(key=lambda x: x["modified_ts"], reverse=True)
    return items


def slugify(text, maxlen=50):
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", text or "").strip(" ._")
    return (text[:maxlen] or "会话").rstrip(" ._")
