# -*- coding: utf-8 -*-
"""跨 CLI 迁移：统一事件流 → 目标 CLI 的原生会话文件 / 通用交接包。

两条通道：
  A 原生续聊：写出 Claude 会话 JSONL 或 Codex rollout，直接 claude --resume / codex resume 接着聊
  B 通用交接包：写出「会话记录.md + 交接提示词.txt」，任何 CLI 粘贴即用

硬性约束：
- 绝不生成真的 tool_use / tool_result 块（缺配对会让下一轮请求直接失败），
  工具活动一律折叠成〔…〕文本摘要
- 只新建文件，不改动任何已有会话
"""
import datetime
import json
import os
import re
import time
import uuid

from . import converter, parser, scanner

SCOPE_ALL = "all"      # 全部历史
SCOPE_LAST = "last"    # 最近 N 轮
SCOPE_CAP = "cap"      # 字符上限（从尾部保留）

DEFAULT_LAST_N = 20
DEFAULT_CHAR_CAP = 60000
TOOL_TEXT_CAP = 200    # 单条工具入参/结果压缩后的最大字符数

CLAUDE_FALLBACK_VERSION = "2.1.238"
CLAUDE_FALLBACK_MODEL = "claude-opus-5"
CODEX_FALLBACK = {"cli_version": "0.150.0", "originator": "codex_cli_rs",
                  "source": "cli", "model_provider": "openai"}

_ACK = "已读取迁移过来的历史对话，理解当前项目进度，随时可以继续。"
_NO_REPLY = "（本轮没有助手回复：可能被用户打断，或只有已省略的工具操作。）"

# Claude 会话里的纯标记型「用户消息」，不是真实指令，迁移时整轮丢掉
_MARKER_USER = ("[Request interrupted", "[Image: source:")


# ---------------- 时间 / ID ----------------
def _iso_utc(dt=None):
    """Claude 与 Codex 的 timestamp 都是带毫秒的 UTC ISO8601 + Z。"""
    dt = dt or datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _uuid7():
    """Codex 的 session_id 是 UUIDv7（毫秒时间戳前缀），照它的形状生成。"""
    ms = int(time.time() * 1000)
    b = bytearray(ms.to_bytes(6, "big") + os.urandom(10))
    b[6] = (b[6] & 0x0F) | 0x70   # version 7
    b[8] = (b[8] & 0x3F) | 0x80   # variant RFC4122
    return str(uuid.UUID(bytes=bytes(b)))


def _write_jsonl(path, records):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _newest_files(root, limit=3):
    """按修改时间取最新的若干 .jsonl，用来嗅探本机 CLI 当前写出的字段值。"""
    out = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            p = os.path.join(dirpath, fn)
            try:
                out.append((os.stat(p).st_mtime, p))
            except OSError:
                pass
    out.sort(reverse=True)
    return [p for _t, p in out[:limit]]


def _git_branch(cwd):
    """直接读 .git/HEAD，避免起子进程。"""
    try:
        with open(os.path.join(cwd, ".git", "HEAD"), "r", encoding="utf-8") as f:
            ref = f.read().strip()
        if ref.startswith("ref: refs/heads/"):
            return ref[len("ref: refs/heads/"):]
    except Exception:
        pass
    return ""


# ---------------- 事件流 → 轮次 ----------------
def _tool_digest(e):
    """工具调用/结果压成一行文本摘要，绝不还原成真的 tool_use / tool_result 块。"""
    if e.get("kind") == "tool_call":
        inp = e.get("input")
        if not isinstance(inp, str):
            try:
                inp = json.dumps(inp, ensure_ascii=False)
            except Exception:
                inp = str(inp)
        inp = " ".join((inp or "").split())
        if len(inp) > TOOL_TEXT_CAP:
            inp = inp[:TOOL_TEXT_CAP] + "…"
        return "〔工具 %s｜%s〕" % (e.get("name", ""), inp)
    out = " ".join((e.get("output") or "").split())
    if len(out) > TOOL_TEXT_CAP:
        out = out[:TOOL_TEXT_CAP] + "…"
    return "〔结果 %s〕" % out


def _is_wrapper_user(e):
    """user 事件是否只是上下文包裹噪音。

    parser 的 noise 标记看的是「原始文本」，而真实指令常常被 CONTEXT ENTRY 包裹之后才被
    clean_user_text 提取出来，所以这里用清洗后的文本再复核一次，避免把真指令当噪音丢掉。
    """
    return bool(e.get("noise")) and parser.is_noise(e.get("text") or "")


def _is_marker_user(txt):
    """`[Request interrupted…]` / `[Image: source: …]` 这类纯标记不是用户说的话。

    留着会让接手的 CLI 把它当成一条真实指令；`[Image #1] 帮我看红框` 这种前缀后面还有
    真实文字的要保留，所以只在整条消息就是标记时才丢。
    """
    return txt.startswith(_MARKER_USER) and "\n" not in txt


def merge_turns(events, with_tools=True):
    """统一事件流 → [{user, parts}]。parts 按原顺序混放助手文本与工具摘要。"""
    turns, cur = [], None
    for e in events:
        k = e.get("kind")
        if k == "user":
            if _is_wrapper_user(e):
                continue
            txt = (e.get("text") or "").strip()
            if not txt or _is_marker_user(txt):
                continue
            cur = {"user": txt, "parts": []}
            turns.append(cur)
        elif cur is None:
            continue          # 首条用户指令之前的内容（系统提示等）不迁移
        elif k == "assistant":
            txt = (e.get("text") or "").strip()
            if txt:
                cur["parts"].append(txt)
        elif k in ("tool_call", "tool_result") and with_tools:
            cur["parts"].append(_tool_digest(e))
    return turns


def normalize_turns(turns):
    """保证每轮都有助手内容。

    没有助手回复的轮次（被打断、或整轮只有被省略的工具操作）写进 Codex rollout 后，
    两条相邻 user_message 之间不会出现 agent_message，parse_codex 的「相邻重复用户消息
    折叠」就会把其中一条吞掉——真实会话里确实出现过连续两次一模一样的指令。
    补一句占位回复，两个 writer 就都能保证每轮一问一答。
    """
    for t in turns:
        if not any(p.strip() for p in t["parts"]):
            t["parts"] = [_NO_REPLY]
    return turns


def _turn_chars(t):
    return len(t["user"]) + sum(len(p) for p in t["parts"])


def trim_turns(turns, scope=SCOPE_ALL, last_n=DEFAULT_LAST_N, char_cap=DEFAULT_CHAR_CAP):
    """按范围裁剪，一律从尾部保留（最近的最有用）。返回 (保留轮次, 原始轮数)。"""
    total = len(turns)
    if scope == SCOPE_LAST and last_n and last_n > 0:
        return turns[-int(last_n):], total
    if scope == SCOPE_CAP and char_cap and char_cap > 0:
        kept, used = [], 0
        for t in reversed(turns):
            c = _turn_chars(t)
            if kept and used + c > int(char_cap):
                break
            kept.append(t)
            used += c
        kept.reverse()
        return kept, total
    return list(turns), total


def _preamble(meta, kept_n, total):
    src = "Claude CLI" if meta.get("source") == "claude" else "Codex"
    lines = ["【会话迁移】以下是从 %s 迁移过来的历史对话，由「会话转 MD」工具写入。" % src,
             "- 原会话标题：%s" % (meta.get("title") or "未命名会话")]
    if meta.get("project"):
        lines.append("- 原项目：%s" % meta["project"])
    if meta.get("cwd"):
        lines.append("- 原工作目录：%s" % meta["cwd"])
    if meta.get("created") or meta.get("modified"):
        lines.append("- 原会话时间：%s → %s" % (meta.get("created") or "—",
                                            meta.get("modified") or "—"))
    if kept_n < total:
        lines.append("- 历史轮数：原共 %d 轮，本次携带最近 %d 轮。" % (total, kept_n))
    else:
        lines.append("- 历史轮数：共 %d 轮，已全部携带。" % total)
    lines.append("")
    lines.append("请先读完下面的历史对话，理解项目现状和已完成的工作，再继续。"
                 "其中〔工具 …〕〔结果 …〕是被压缩过的工具调用摘要，只作背景参考，"
                 "不要当成本轮真实执行过的操作。")
    return "\n".join(lines)


# ---------------- 通道 A-1：写 Claude 会话 ----------------
def claude_slug(cwd):
    """Claude 的项目目录名 = 工作目录里所有非字母数字字符替换成 '-'。
    实测：E:\\在办项目\\AI会话转MD → E-------AI---MD"""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd or "")


def _dir_cwd(d):
    """读目录里第一个 jsonl 的 cwd，用来反查这个目录对应哪个工作目录。"""
    try:
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".jsonl"):
                continue
            for o in parser.read_json_lines(os.path.join(d, fn), max_lines=40):
                if o.get("cwd"):
                    return str(o["cwd"])
            return ""
    except OSError:
        pass
    return ""


def resolve_claude_project_dir(cwd, projects_root):
    """优先复用已有目录（本机同时存在两种 slug 编码），命中不了才按规则新建。"""
    target = os.path.normcase(os.path.abspath(cwd or ""))
    try:
        for name in sorted(os.listdir(projects_root)):
            d = os.path.join(projects_root, name)
            if not os.path.isdir(d):
                continue
            c = _dir_cwd(d)
            if c and os.path.normcase(os.path.abspath(c)) == target:
                return d
    except OSError:
        pass
    return os.path.join(projects_root, claude_slug(cwd))


def _detect_claude_meta(projects_root):
    """从本机最新的会话文件里嗅探 version / model，跟着已装版本走。"""
    ver, model = None, None
    for p in _newest_files(projects_root, limit=3):
        for o in parser.read_json_lines(p, max_lines=60):
            ver = ver or o.get("version")
            m = (o.get("message") or {}).get("model")
            model = model or m
            if ver and model:
                return ver, model
    return ver or CLAUDE_FALLBACK_VERSION, model or CLAUDE_FALLBACK_MODEL


def write_claude_session(turns, target_cwd, title=""):
    """写出一个可被 `claude --resume` 列出并载入的会话文件。"""
    root = scanner._claude_root()                       # 支持 CLAUDE_CONFIG_DIR
    proj = resolve_claude_project_dir(target_cwd, root)
    os.makedirs(proj, exist_ok=True)
    sid = str(uuid.uuid4())
    ver, model = _detect_claude_meta(root)
    branch = _git_branch(target_cwd)
    base = datetime.datetime.now(datetime.timezone.utc)
    seq = [0]

    def stamp():
        seq[0] += 1
        return _iso_utc(base + datetime.timedelta(milliseconds=seq[0] * 20))

    def common(uid):
        return {"parentUuid": None, "isSidechain": False, "uuid": uid,
                "timestamp": stamp(), "cwd": target_cwd, "sessionId": sid,
                "version": ver, "gitBranch": branch, "userType": "external"}

    recs = [{"type": "ai-title", "aiTitle": title or "↪ 迁移会话", "sessionId": sid}]
    parent = None
    for t in turns:
        uid = str(uuid.uuid4())
        r = common(uid)
        r.update({"parentUuid": parent, "type": "user",
                  "message": {"role": "user", "content": t["user"]}})
        recs.append(r)
        parent = uid
        body = "\n\n".join(p for p in t["parts"] if p).strip()
        if not body:
            continue
        uid = str(uuid.uuid4())
        r = common(uid)
        r.update({"parentUuid": parent, "type": "assistant",
                  "message": {"id": "msg_" + uuid.uuid4().hex, "role": "assistant",
                              "model": model, "type": "message",
                              "content": [{"type": "text", "text": body}]}})
        recs.append(r)
        parent = uid
    recs.append({"type": "last-prompt", "lastPrompt": turns[-1]["user"][:2000],
                 "leafUuid": parent, "sessionId": sid})
    path = os.path.join(proj, sid + ".jsonl")
    _write_jsonl(path, recs)
    # Claude 的 --resume 认 sessionId，也能不带参数走列表；列表按当前目录过滤，所以先 cd。
    return {"target": "claude", "path": path, "session_id": sid, "project_dir": proj,
            "cwd": target_cwd,
            "resume": 'cd "%s" && claude --resume %s' % (target_cwd, sid),
            "resume_alt": 'cd "%s" && claude --resume   （不带 id 走列表挑）' % target_cwd}


# ---------------- 通道 A-2：写 Codex rollout ----------------
def _detect_codex_meta(sessions_root):
    """从最新 rollout 的 session_meta 嗅探 cli_version / originator / source / model_provider。"""
    info = dict(CODEX_FALLBACK)
    for p in _newest_files(sessions_root, limit=3):
        for o in parser.read_json_lines(p, max_lines=3):
            if o.get("type") != "session_meta":
                continue
            pl = o.get("payload") or {}
            for k in info:
                if pl.get(k):
                    info[k] = pl[k]
            return info
    return info


def write_codex_rollout(turns, target_cwd):
    """写出一个可被 `codex resume` 载入的 rollout。

    骨架完全照抄 Codex 自带的「导入外部 agent 会话」产物（见 plan 第一节），
    每轮 task_started → user_message → response_item(user) → agent_message →
    response_item(assistant) → task_complete。
    Codex 的线程名取自首条用户消息，不需要单独写标题记录。
    """
    root = scanner._codex_root()                        # 支持 CODEX_HOME
    now = datetime.datetime.now()                       # 文件名用本地时间
    day = os.path.join(root, now.strftime("%Y"), now.strftime("%m"), now.strftime("%d"))
    os.makedirs(day, exist_ok=True)
    sid = _uuid7()
    path = os.path.join(day, "rollout-%s-%s.jsonl" % (now.strftime("%Y-%m-%dT%H-%M-%S"), sid))
    info = _detect_codex_meta(root)
    started = int(time.time())

    def ev(kind, extra):
        pl = {"type": kind}
        pl.update(extra)
        return {"timestamp": _iso_utc(), "type": "event_msg", "payload": pl}

    def ri(payload):
        return {"timestamp": _iso_utc(), "type": "response_item", "payload": payload}

    lines = [{"timestamp": _iso_utc(), "type": "session_meta", "payload": {
        "session_id": sid, "id": sid, "timestamp": _iso_utc(), "cwd": target_cwd,
        "originator": info["originator"], "cli_version": info["cli_version"],
        "source": info["source"], "model_provider": info["model_provider"],
        "history_mode": "legacy", "multi_agent_version": "v1"}}]
    for n, t in enumerate(turns, 1):
        tid = "external-import-turn-%d" % n
        lines.append(ev("task_started", {"turn_id": tid, "started_at": started,
                                         "model_context_window": None,
                                         "collaboration_mode_kind": "default"}))
        lines.append(ev("user_message", {"message": t["user"], "local_images": [],
                                         "local_audio": [], "text_elements": []}))
        lines.append(ri({"type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": t["user"]}]}))
        body = "\n\n".join(p for p in t["parts"] if p).strip()
        if body:
            lines.append(ev("agent_message", {"message": body, "phase": None,
                                              "memory_citation": None}))
            lines.append(ri({"type": "message", "role": "assistant",
                             "content": [{"type": "output_text", "text": body}]}))
        lines.append(ev("task_complete", {"turn_id": tid, "last_agent_message": None,
                                          "started_at": started}))
    _write_jsonl(path, lines)
    # codex resume 收的是**会话 id（UUID）或线程名**，不是文件路径：
    # 传路径会直接报 `No saved session found with ID <路径>`（实测 codex-cli 0.145.0）。
    # 另外不带 id 的 picker 默认按 cwd 过滤，所以先 cd 到目标目录再 resume。
    return {"target": "codex", "path": path, "session_id": sid, "project_dir": day,
            "cwd": target_cwd,
            "resume": 'cd "%s" && codex resume %s' % (target_cwd, sid),
            "resume_alt": "codex resume --all   （不记 id 时用列表挑，--all 关掉 cwd 过滤）"}


# ---------------- 通道 B：通用交接包 ----------------
_HANDOFF_PROMPT = """我要把一段此前在 {src} 里进行的工作交接给你，请接手继续。

- 项目工作目录：{cwd}
- 历史对话完整记录（Markdown）：{md}

请先读取上面那个 Markdown 文件，它包含此前全部的对话、关键决策与产出；
读完后用三句话向我汇报：当前阶段 / 上次做到哪 / 建议下一步，然后等我的指令。
不要重复已经做完的工作，也不要凭猜测改动文件。
"""


def write_handoff(events, meta, outdir, mode=converter.MODE_TOOLS):
    """生成交接包目录：会话记录.md + 交接提示词.txt。任何 CLI 都能用。"""
    os.makedirs(outdir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    base = os.path.join(outdir, "handoff_%s_%s" % (scanner.slugify(meta.get("title"), 40), stamp))
    d, n = base, 1
    while os.path.exists(d):
        d = "%s_%d" % (base, n)
        n += 1
    os.makedirs(d, exist_ok=True)
    md_path = os.path.join(d, "会话记录.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(converter.convert(events, meta, mode))
    prompt = _HANDOFF_PROMPT.format(
        src="Claude CLI" if meta.get("source") == "claude" else "Codex",
        cwd=meta.get("cwd") or "（未记录，请以当前目录为准）", md=md_path)
    txt_path = os.path.join(d, "交接提示词.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(prompt)
    return {"target": "handoff", "path": d, "md": md_path, "prompt_file": txt_path,
            "prompt": prompt, "cwd": meta.get("cwd") or "",
            "resume": "把「交接提示词.txt」的内容整段粘进目标 CLI 即可"}


# ---------------- 对外入口 ----------------
def migrate(item, target, opts=None):
    """item: 会话字典（scan 的输出，需含 path/source/title/cwd 等）
    target: 'claude' | 'codex' | 'handoff'"""
    opts = opts or {}
    events = parser.parse(item["path"], item.get("source", "claude"))
    if target == "handoff":
        outdir = opts.get("output_dir")
        if not outdir:
            raise ValueError("交接包需要指定输出目录")
        return write_handoff(events, item, outdir, opts.get("mode") or converter.MODE_TOOLS)

    turns = merge_turns(events, with_tools=bool(opts.get("with_tools", True)))
    if not turns:
        raise ValueError("这个会话里没有可迁移的对话轮次")
    kept, total = trim_turns(turns, opts.get("scope") or SCOPE_ALL,
                             opts.get("last_n") or DEFAULT_LAST_N,
                             opts.get("char_cap") or DEFAULT_CHAR_CAP)
    if not kept:
        kept = turns[-1:]
    all_turns = normalize_turns(
        [{"user": _preamble(item, len(kept), total), "parts": [_ACK]}] + kept)
    cwd = opts.get("target_cwd") or item.get("cwd") or os.path.expanduser("~")
    if target == "codex":
        res = write_codex_rollout(all_turns, cwd)
    else:
        res = write_claude_session(all_turns, cwd,
                                  "↪ 迁移：" + (item.get("title") or "未命名会话")[:60])
    res.update({"turns": len(kept), "total_turns": total})
    return res
