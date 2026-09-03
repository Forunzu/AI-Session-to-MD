# -*- coding: utf-8 -*-
"""本机 AI CLI 目录注册表：内置精细规则 + 自动探测 + 体积测量。

一条规则描述一个 CLI 的家目录该怎么搬：
  root      主目录（~/.claude）
  extras    HOME 级配套文件（~/.claude.json）
  sessions  「仅会话数据」范围要带的子路径
  secrets   凭证文件，单独标记（默认带，UI 可关）
  junk      「智能排除」范围要跳过的模式（fnmatch，相对 root）
"""
import fnmatch
import os
import re
import time

HOME = os.path.expanduser("~")

# 通用垃圾模式：自动探测出来的 CLI 都套这一份
GENERIC_JUNK = ["node_modules", "__pycache__", ".git", "cache", "Cache", "tmp", "temp",
                "logs", "log", "*.log", "*.sqlite-wal", "*.sqlite-shm",
                "*.bak", "*.bak.*", "*.backup.*", "Crashpad", "GPUCache"]

# 内置两条精细规则：目录内容逐项核对过（2026-09-03 本机实测）
BUILTIN = [
    {"key": "claude", "label": "Claude Code", "root": "~/.claude",
     "extras": ["~/.claude.json"],
     "sessions": ["projects", "sessions", "history.jsonl", "plans", "skills",
                  "CLAUDE.md", "settings.json"],
     "secrets": [".credentials.json"],
     "junk": ["downloads", "cache", "paste-cache", "shell-snapshots", "telemetry",
              "file-history", "session-env", "daemon", "jobs", "backups",
              "daemon.log", "daemon.lock", "daemon.status.json",
              "sessions/*.json", "*.bak", "*.bak.*"]},
    {"key": "codex", "label": "Codex", "root": "~/.codex",
     "extras": [],
     "sessions": ["sessions", "archived_sessions", "history.jsonl",
                  "session_index.jsonl", "session_family.json", "plans", "memories",
                  "skills", "AGENTS.md", "config.toml"],
     "secrets": ["auth.json"],
     "junk": ["cache", "tmp", "log", "browser", "node_repl", "thread-writer-locks",
              "computer-use", "computer-use-turn-ended", "ambient-suggestions",
              "logs_*.sqlite*", "goals_*.sqlite*", "state_*.sqlite*",
              "memories_*.sqlite*", "*.sqlite-wal", "*.sqlite-shm",
              "sandbox*.log", "backup-*", "backups", "backups_state",
              "*.bak", "*.bak.*", "*.backup.*"]},
]

# 已知 AI CLI / AI 工具目录名 → 显示名（本机实测存在的 + 常见的）
KNOWN_AI = {
    "gemini": "Gemini CLI", "grok": "Grok CLI", "kimi-code": "Kimi Code",
    "copilot": "GitHub Copilot CLI", "cursor": "Cursor", "factory": "Factory Droid",
    "qoder": "Qoder", "trae-cn": "Trae CN", "trae-aicc": "Trae AICC",
    "iflow": "iFlow", "qwen": "Qwen Code", "opencli": "OpenCLI",
    "openclaude": "OpenClaude", "openclaw": "OpenClaw", "jclaude": "jClaude",
    "codeg": "CodeG", "commandcode": "CommandCode", "zcode": "ZCode",
    "hermes": "Hermes", "monet": "Monet", "orca": "Orca", "pi": "Pi",
    "agents": "Agents 共享目录", "skillhub": "SkillHub", "redskill": "RedSkill",
    "cc-switch": "CC Switch", "code-switch": "Code Switch",
    "cli-manager": "CLI Manager", "ccgui": "CC GUI", "ccursor": "CCursor",
    "ai-shifu": "AI 师傅", "ai_completion": "AI Completion",
    "antigravity_cockpit": "Antigravity Cockpit", "lark-cli": "飞书 CLI",
    "lobehub-market": "LobeHub Market", "mcporter": "MCPorter",
    "tokentracker": "TokenTracker", "workbuddy": "WorkBuddy",
    "ppt-master": "PPT Master", "codex-ppt-skill": "Codex PPT Skill",
    "codex-session-delete": "Codex 会话清理", "agent-reach": "Agent Reach",
    "cua-driver": "CUA Driver", "dsh": "DSH", "wox": "Wox",
}

# 明确不是 AI CLI 的点目录：语言/包管理/系统工具，别混进备份列表
DENY = {
    "cache", "config", "local", "npm", "cargo", "rustup", "gradle", "android",
    "ssh", "git", "gitconfig", "docker", "kube", "aws", "azure", "nuget",
    "dotnet", "m2", "ipython", "jupyter", "matplotlib", "nvm", "yarn", "pnpm",
    "vscode", "vs", "idea", "eclipse", "templateengine", "vim", "emacs",
    "bash_history", "python_history", "conda", "anaconda", "pyenv", "poetry",
    "virtualenvs", "designer", "logseq", "omp", "sbx-denybin", "icube-remote-ssh",
}

# 兜底信号：目录名带这些词，或目录里有这些文件，就认为是个 AI CLI
_NAME_HINT = re.compile(r"claude|codex|gpt|llm|agent|ai[-_]|[-_]ai$|^ai$|copilot|"
                        r"gemini|grok|kimi|qwen|glm|deepseek|skill|mcp|cli|code",
                        re.I)
_TELLTALE = ("sessions", "projects", "history.jsonl", "AGENTS.md", "CLAUDE.md",
             "skills", "agents", "config.toml", "settings.json", "auth.json")


# ---------------- 路径 ----------------
def expand(p):
    """~ 展开 + 规范化；空值返回空串。"""
    return os.path.normpath(os.path.expanduser(p)) if p else ""


def match_junk(name, rel, patterns):
    """单个条目「自身」是否命中排除模式；带 / 的模式按相对 root 的路径匹配。"""
    for pat in patterns:
        if "/" in pat:
            if fnmatch.fnmatch(rel, pat):
                return True
        elif fnmatch.fnmatch(name, pat):
            return True
    return False


def is_junk(rel, patterns):
    """相对 root 的路径上任一级命中即算被排除（vault 逐文件复核用）。"""
    rel = rel.replace("\\", "/").strip("/")
    if not rel:
        return False
    segs = rel.split("/")
    for i, seg in enumerate(segs):
        if match_junk(seg, "/".join(segs[:i + 1]), patterns):
            return True
    return False


# ---------------- 体积测量 ----------------
_SIZE_CACHE = {}          # root(小写) -> (到期时间, 结果)
SIZE_TTL = 120            # 秒；同一次操作里反复问不用重扫


def dir_size(path, budget=8.0, junk=None, cache=False):
    """递归累加体积；junk 给了就顺带算「智能排除后」的量。

    排除判定按目录逐级下传（命中的目录整棵跳过），不对每个文件重跑全套模式——
    早期版本那么写，`.codex` 37 万个文件 0.8 秒只数得完零头。
    带时间预算，超时返回已数到的量并标 truncated。
    """
    key = os.path.normcase(path)
    if cache:
        hit = _SIZE_CACHE.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    total = files = keep_b = keep_f = 0
    deadline = time.time() + budget
    truncated = False
    n = len(path.rstrip("\\/")) + 1
    stack = [(path, False)]
    while stack:
        if time.time() > deadline:
            truncated = True
            break
        cur, excluded = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    rel = e.path[n:].replace("\\", "/")
                    try:
                        if e.is_dir(follow_symlinks=False):
                            sub = excluded or (junk is not None
                                               and match_junk(e.name, rel, junk))
                            stack.append((e.path, sub))
                            continue
                        sz = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    total += sz
                    files += 1
                    if junk is not None and not excluded \
                            and not match_junk(e.name, rel, junk):
                        keep_b += sz
                        keep_f += 1
        except OSError:
            continue
    if junk is None:
        keep_b, keep_f = total, files
    res = {"bytes": total, "files": files, "keep_bytes": keep_b,
           "keep_files": keep_f, "truncated": truncated}
    if cache and not truncated:
        _SIZE_CACHE[key] = (time.time() + SIZE_TTL, res)
    return res


def fmt_size(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return "%d %s" % (n, u) if u == "B" else "%.1f %s" % (n, u)
        n /= 1024.0


# ---------------- 探测 ----------------
def _looks_like_cli(name, path):
    """未列名的点目录要不要收：靠名字关键词或目录内的招牌文件判断。"""
    if _NAME_HINT.search(name):
        return True
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.name in _TELLTALE:
                    return True
    except OSError:
        pass
    return False


def _generic_rule(name, path, label=None, guessed=False):
    return {"key": name, "label": label or name, "root": path, "extras": [],
            "sessions": [s for s in ("sessions", "projects", "history.jsonl",
                                     "conversations", "chats", "threads", "skills")
                         if os.path.exists(os.path.join(path, s))],
            "secrets": [s for s in ("auth.json", ".credentials.json", "credentials.json",
                                    "token.json", "oauth_creds.json", "keys.json")
                        if os.path.exists(os.path.join(path, s))],
            "junk": list(GENERIC_JUNK), "builtin": False, "guessed": guessed}


def _under(home, p):
    """规则里的 `~/xxx` 换算到指定 HOME（测试时可指向临时 HOME）。"""
    if p.startswith("~/") or p.startswith("~\\"):
        return os.path.normpath(os.path.join(home, p[2:]))
    return expand(p)


def detect(home=None):
    """扫 HOME 下的点目录，产出规则列表。内置规则优先，其余自动探测。"""
    home = home or HOME
    rules, seen = [], set()
    for r in BUILTIN:
        root = _under(home, r["root"])
        if not os.path.isdir(root):
            continue
        d = dict(r)
        d["root"] = root
        d["extras"] = [p for p in (_under(home, x) for x in r["extras"])
                       if os.path.exists(p)]
        d["builtin"] = True
        d["guessed"] = False
        rules.append(d)
        seen.add(os.path.normcase(root))
    try:
        entries = sorted(os.scandir(home), key=lambda e: e.name.lower())
    except OSError:
        entries = []
    for e in entries:
        if not e.name.startswith(".") or os.path.normcase(e.path) in seen:
            continue
        try:
            if not e.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        name = e.name[1:]
        if not name or name.lower() in DENY or name.endswith("-venv") or "venv" in name:
            continue
        if name in KNOWN_AI:
            rules.append(_generic_rule(name, e.path, KNOWN_AI[name]))
        elif _looks_like_cli(name, e.path):
            rules.append(_generic_rule(name, e.path, name, guessed=True))
    return rules


# ---------------- 对外入口 ----------------
def registry(home=None, with_size=False, budget=8.0, custom=None):
    """UI 用的列表：规则 + 会话子路径存在性（+ 可选体积）。

    默认不算体积：本机 45 个目录、`.codex` 7.7 GB / 37 万文件，一次性全算要十几秒，
    界面会卡住。改成前端拿到列表后按行去问 /api/vault/size，逐格填。
    custom: [{label, root}] 用户自己添加的目录，一律走通用规则。
    """
    rules = detect(home)
    for c in (custom or []):
        root = expand(c.get("root") or c.get("path") or "")
        if root and os.path.isdir(root):
            r = _generic_rule(os.path.basename(root).lstrip(".") or "custom", root,
                              c.get("label") or root)
            r["custom"] = True
            rules.append(r)
    out = []
    for r in rules:
        item = {k: r[k] for k in ("key", "label", "root", "extras", "sessions",
                                  "secrets", "junk")}
        item["builtin"] = r.get("builtin", False)
        item["guessed"] = r.get("guessed", False)
        item["custom"] = r.get("custom", False)
        item["sessions"] = [s for s in r["sessions"]
                            if os.path.exists(os.path.join(r["root"], s))]
        item["secrets"] = [s for s in r["secrets"]
                           if os.path.exists(os.path.join(r["root"], s))]
        if with_size:
            item.update(measure(r["root"], r["junk"], budget=budget))
        out.append(item)
    # 内置的排前面，其余按名字排（体积是后到的，不能拿来排序）
    out.sort(key=lambda x: (not x["builtin"], x["label"]))
    return out


def measure(root, junk=None, budget=8.0):
    """单个目录的体积，带缓存，UI 逐行调用。"""
    m = dir_size(root, budget=budget, junk=junk, cache=True)
    pre = "> " if m["truncated"] else ""
    m = dict(m)
    m["size"] = pre + fmt_size(m["bytes"])
    m["keep_size"] = pre + fmt_size(m["keep_bytes"])
    return m


def find(key, home=None, custom=None):
    """按 key 取一条规则（备份/还原执行时用）。"""
    for r in detect(home):
        if r["key"] == key:
            return r
    for c in (custom or []):
        root = expand(c.get("root") or c.get("path") or "")
        k = os.path.basename(root).lstrip(".") or "custom"
        if k == key and os.path.isdir(root):
            r = _generic_rule(k, root, c.get("label") or root)
            r["custom"] = True
            return r
    return None
