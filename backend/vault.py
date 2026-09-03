# -*- coding: utf-8 -*-
"""CLI 家目录备份 / 还原引擎：干跑、后台任务、进度、取消、换机路径改写。

设计约束（换电脑是刚需，出错代价高，所以全程保守）：
- 备份只读源目录；还原只增不改不删，覆盖前先留 .bak-<时间戳>
- 任何真跑之前都能先干跑（plan_*），把文件数/字节数/最大的几个先摆给用户看
- 多 GB 的任务放后台线程，前端轮询进度、可取消
- 路径改写是唯一会动会话内容的操作：独立步骤 + 干跑 + 逐文件 .bak
"""
import datetime
import json
import os
import platform
import re
import shutil
import threading
import time
import uuid
import zipfile

from . import cli_registry as reg
from . import migrator, parser

SCOPE_SESSIONS = "sessions"   # 仅会话数据
SCOPE_ROOT = "root"           # 整个根目录（智能排除）
SCOPE_FULL = "full"           # 完整不排除

CONFLICT_SKIP = "skip"        # 目标已存在就跳过（默认）
CONFLICT_OVERWRITE = "overwrite"   # 覆盖，但先把原文件存成 .bak-<时间戳>

MANIFEST = "manifest.json"
SCHEMA = 1
MTIME_TOL = 2                 # 秒；同名同大小且 mtime 相差在此以内视为同一份
TOP_N = 10                    # 干跑里列出的最大文件个数
LONG_PATH = 240               # 超过这个长度就加 \\?\ 前缀
DEFAULT_SUBDIR = "AI-CLI-Backup"   # 备份多个 CLI 时的快照目录前缀
README_NAME = "备份说明.txt"       # 快照目录里的人类可读说明
SNAP_RE = re.compile(r"_\d{8}-\d{4,6}$")   # 快照目录名的尾巴：_20260903-1930


def is_drive_root(p):
    """p 是不是盘根（E:\\ / E:/）或 UNC 共享根（\\\\srv\\share）。

    实测过的坑：选 `E:\\` 当备份目录会把 manifest.json 和各 CLI 子目录直接摊在盘根上，
    而且打 zip 时 `os.walk(dest)` 会遍历整个 E 盘（撞上 pagefile.sys 直接 Permission denied）。
    """
    if not p:
        return False
    q = os.path.abspath(p)
    if os.path.splitdrive(q)[1].strip("\\/") == "":     # E:\  /  E:/
        return True
    if q.startswith("\\\\"):                            # \\srv\share 只有两段
        return len([x for x in q.strip("\\").split("\\") if x]) <= 2
    return q == os.sep


def snapshot_name(keys=None):
    """快照目录名：只备一个 CLI 就用它的 key，多个用统一前缀，后面一律跟导出时间。

    用户反馈过「不应该是单独的一个 claude」—— 一个叫 `claude` 的裸目录既看不出是备份、
    也看不出哪天导的。带上时间戳后，manifest 与各 CLI 子目录都收在这个目录里面。
    """
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    base = keys[0] if keys and len(keys) == 1 and keys[0] else DEFAULT_SUBDIR
    return "%s_%s" % (re.sub(r"[^\w.-]", "_", str(base)), stamp)


def normalize_dest(dest, keys=None):
    """把备份目标规整成一个独立快照目录，返回 (最终目录, 提示或 '')。

    按顺序判三条：
    ① 选中的目录里已经有 manifest.json → 那就是一份旧备份，原地增量更新它；
    ② 目录名本身就是快照名（`xxx_20260903-1930`）→ 直接用（干跑与真跑两次调用要落到同一处）；
    ③ 其余情况（含盘根）→ 在它下面新建一个带日期的快照目录。
    以前只对盘根下钻，选普通目录时 manifest.json 会和 `claude\\` 一起摊在用户自己的目录里，
    打包解压后 json 就显得在文件夹外边，还原端跟着找不着。
    """
    if not dest:
        return dest, ""
    d = os.path.abspath(dest)
    if os.path.isfile(long_path(os.path.join(d, MANIFEST))):
        return d, "所选目录里已有 %s，将增量更新这份备份（同名同大小同修改时间的文件跳过）。" % MANIFEST
    if SNAP_RE.search(os.path.basename(d.rstrip("\\/")) or ""):
        return d, ""
    final = os.path.join(d, snapshot_name(keys))
    tail = "（盘根不能直接当备份目录）" if is_drive_root(d) else ""
    return final, ("备份落在 %s%s：%s、%s 和各 CLI 子目录都在这一个目录里，整目录或它的 zip 可直接搬走。"
                   "想增量更新上次那份，把目标直接选到那份备份（有 %s 的那一层）。"
                   % (final, tail, MANIFEST, README_NAME, MANIFEST))


def now_stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def long_path(p):
    """Windows 上超长路径要加 \\\\?\\ 前缀才能打开（Codex 自己的记录里就是这么存的）。"""
    if os.name != "nt" or len(p) < LONG_PATH or p.startswith("\\\\?\\"):
        return p
    p = os.path.abspath(p)
    return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p


def _stat(p):
    try:
        return os.stat(long_path(p))
    except OSError:
        return None


def same_file(src, dst):
    """同名 + 同大小 + mtime 相差 ≤2s 就认为已经备份过，可跳过。

    不比哈希：几 GB 的目录逐字节校验太慢，而备份场景里「大小 + 时间」误判率足够低。
    """
    a, b = _stat(src), _stat(dst)
    return bool(a and b and a.st_size == b.st_size
                and abs(a.st_mtime - b.st_mtime) <= MTIME_TOL)


def _is_subpath(child, parent):
    """child 是否在 parent 里面（含相等）。防止把备份目录设成源目录的子目录。

    走 realpath：Windows 上同一个目录可能写成 8.3 短名（C:\\Users\\ADMINI~1\\…）或经过
    junction/符号链接，纯字符串比会漏判，那道「目标不能在源里面」的护栏就白设了。
    """
    def norm(p):
        try:
            return os.path.normcase(os.path.realpath(os.path.abspath(p)))
        except Exception:
            return os.path.normcase(os.path.abspath(p))
    try:
        a, b = norm(child), norm(parent)
    except Exception:
        return False
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)


def check_target(dest, rules):
    """备份目标目录不能落在任何源目录里面，否则会自我递归复制把盘写满。"""
    for r in rules:
        if _is_subpath(dest, r["root"]):
            raise ValueError("备份目录不能放在源目录 %s 里面" % r["root"])
    return True


# ---------------- 文件枚举 ----------------
def iter_entry_files(rule, scope, include_secrets=True):
    """产出 (源绝对路径, 备份内相对路径)。相对路径形如 `root/projects/a.jsonl`、`home/.claude.json`。

    三种范围的差别只在「进不进这个文件」，遍历逻辑共用一套。
    """
    root = rule["root"]
    junk = rule["junk"] if scope == SCOPE_ROOT else []
    secrets = {s.replace("\\", "/").strip("/") for s in rule.get("secrets") or []}
    if scope == SCOPE_SESSIONS:
        tops = list(rule.get("sessions") or [])
    else:
        try:
            tops = sorted(os.listdir(long_path(root)))
        except OSError:
            tops = []
    if include_secrets:
        tops += [s for s in secrets if s not in tops]
    for top in tops:
        src = os.path.join(root, top)
        rel0 = top.replace("\\", "/").strip("/")
        if not os.path.exists(long_path(src)):
            continue
        if rel0 in secrets and not include_secrets:
            continue
        if junk and reg.match_junk(os.path.basename(rel0), rel0, junk):
            continue
        if os.path.isfile(long_path(src)):
            yield src, "root/" + rel0
            continue
        for sub, rel in _walk(src, rel0, junk, secrets, include_secrets):
            yield sub, "root/" + rel
    for extra in rule.get("extras") or []:
        if os.path.isfile(long_path(extra)):
            yield extra, "home/" + os.path.basename(extra)


def _walk(top_dir, rel0, junk, secrets, include_secrets):
    """深度遍历一个子目录，排除命中的整棵跳过（不对每个文件重跑全套模式）。"""
    stack = [(top_dir, rel0)]
    while stack:
        cur, rel = stack.pop()
        try:
            with os.scandir(long_path(cur)) as it:
                items = list(it)
        except OSError:
            continue
        for e in items:
            r = rel + "/" + e.name
            if junk and reg.match_junk(e.name, r, junk):
                continue
            if r in secrets and not include_secrets:
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, r))
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            yield e.path, r


# ---------------- 干跑 ----------------
def plan_backup(entries, dest, home=None, custom=None):
    """entries: [{key, scope}]。返回每个 CLI 的文件数/字节数 + 最大的几个文件 + 已存在可跳过数。"""
    dest, dest_note = normalize_dest(dest, [en.get("key") for en in entries])
    out = {"action": "backup", "dest": dest, "dest_note": dest_note, "entries": [],
           "files": 0, "bytes": 0,
           "skip_files": 0, "skip_bytes": 0, "top": [], "secrets": []}
    rules = []
    for en in entries:
        rule = reg.find(en["key"], home, custom)
        if not rule:
            continue
        rules.append(rule)
        inc = bool(en.get("include_secrets", True))
        scope = en.get("scope") or SCOPE_ROOT
        n = b = sk = skb = 0
        top = []
        for src, rel in iter_entry_files(rule, scope, inc):
            st = _stat(src)
            sz = st.st_size if st else 0
            n += 1
            b += sz
            if same_file(src, os.path.join(dest, rule["key"], rel)):
                sk += 1
                skb += sz
            if len(top) < TOP_N or sz > top[-1][0]:
                top.append((sz, rel))
                top.sort(reverse=True)
                del top[TOP_N:]
        got_secrets = [s for s in (rule.get("secrets") or [])
                       if os.path.exists(os.path.join(rule["root"], s))]
        out["entries"].append({
            "key": rule["key"], "label": rule["label"], "root": rule["root"],
            "scope": scope, "files": n, "bytes": b, "size": reg.fmt_size(b),
            "skip_files": sk, "skip_bytes": skb, "include_secrets": inc,
            "secrets": got_secrets if inc else [],
            "top": [{"size": reg.fmt_size(s), "path": p} for s, p in top]})
        if inc and got_secrets:
            out["secrets"] += ["%s/%s" % (rule["key"], s) for s in got_secrets]
        out["files"] += n
        out["bytes"] += b
        out["skip_files"] += sk
        out["skip_bytes"] += skb
    if dest:
        check_target(dest, rules)
    out["size"] = reg.fmt_size(out["bytes"])
    out["skip_size"] = reg.fmt_size(out["skip_bytes"])
    out["net_size"] = reg.fmt_size(max(0, out["bytes"] - out["skip_bytes"]))
    return out


# ---------------- 后台任务 ----------------
JOBS = {}
_LOCK = threading.Lock()


def new_job(kind):
    jid = uuid.uuid4().hex[:12]
    with _LOCK:
        JOBS[jid] = {"id": jid, "kind": kind, "state": "running", "current": "",
                     "done_files": 0, "total_files": 0, "done_bytes": 0,
                     "total_bytes": 0, "skipped": 0, "errors": [], "cancel": False,
                     "started": time.time(), "result": None}
    return JOBS[jid]


def get_job(jid):
    j = JOBS.get(jid)
    if not j:
        return None
    out = {k: v for k, v in j.items() if k != "cancel"}
    out["errors"] = j["errors"][-20:]
    out["error_count"] = len(j["errors"])
    tb, db = j["total_bytes"], j["done_bytes"]
    out["percent"] = round(db * 100.0 / tb, 1) if tb else (100.0 if j["state"] == "done" else 0.0)
    out["done_size"] = reg.fmt_size(db)
    out["total_size"] = reg.fmt_size(tb)
    out["elapsed"] = int(time.time() - j["started"])
    return out


def cancel_job(jid):
    j = JOBS.get(jid)
    if not j:
        return False
    j["cancel"] = True
    return True


def _copy(job, src, dst):
    """复制单个文件，父目录按需建。返回 'copied' / 'skip' / 'error'。"""
    if same_file(src, dst):
        job["skipped"] += 1
        return "skip"
    try:
        d = os.path.dirname(dst)
        if d:
            os.makedirs(long_path(d), exist_ok=True)
        shutil.copy2(long_path(src), long_path(dst))
        return "copied"
    except Exception as ex:
        job["errors"].append("%s → %s：%s" % (src, dst, ex))
        return "error"


# ---------------- 备份 ----------------
def start_backup(entries, dest, home=None, custom=None, make_zip=False):
    """起一个后台备份任务，立刻返回 job（前端拿 id 轮询）。"""
    if not dest:
        raise ValueError("请先选择备份目录")
    dest, _note = normalize_dest(dest, [en.get("key") for en in entries])
    rules = [r for r in (reg.find(e["key"], home, custom) for e in entries) if r]
    if not rules:
        raise ValueError("没有可备份的目录")
    check_target(dest, rules)
    job = new_job("backup")
    t = threading.Thread(target=_run_backup, daemon=True,
                         args=(job, entries, dest, home, custom, make_zip))
    t.start()
    return job


def _run_backup(job, entries, dest, home, custom, make_zip):
    try:
        plan = plan_backup(entries, dest, home, custom)
        job["total_files"] = plan["files"]
        job["total_bytes"] = plan["bytes"]
        os.makedirs(long_path(dest), exist_ok=True)
        man = {"tool": "会话转MD", "schema": SCHEMA, "created": now_stamp(),
               "host": platform.node(), "user": os.environ.get("USERNAME") or "",
               "home": home or reg.HOME, "os": platform.platform(),
               "includes_secrets": bool(plan["secrets"]), "entries": []}
        for en in entries:
            if job["cancel"]:
                break
            rule = reg.find(en["key"], home, custom)
            if not rule:
                continue
            scope = en.get("scope") or SCOPE_ROOT
            inc = bool(en.get("include_secrets", True))
            base = os.path.join(dest, rule["key"])
            n = b = 0
            for src, rel in iter_entry_files(rule, scope, inc):
                if job["cancel"]:
                    break
                job["current"] = rel
                st = _stat(src)
                sz = st.st_size if st else 0
                if _copy(job, src, os.path.join(base, rel)) != "error":
                    n += 1
                    b += sz
                job["done_files"] += 1
                job["done_bytes"] += sz
            man["entries"].append({
                "key": rule["key"], "label": rule["label"], "source": rule["root"],
                "scope": scope, "dest": rule["key"], "files": n, "bytes": b,
                "extras": [os.path.basename(x) for x in rule.get("extras") or []],
                "include_secrets": inc})
        with open(os.path.join(dest, MANIFEST), "w", encoding="utf-8") as f:
            json.dump(man, f, ensure_ascii=False, indent=2)
        _write_readme(dest, man)
        job["result"] = {"dest": dest, "manifest": os.path.join(dest, MANIFEST)}
        if make_zip and not job["cancel"]:
            job["current"] = "打包 zip…"
            try:
                job["result"]["zip"] = _make_zip(dest, man)
            except Exception as ex:                 # 打包失败不该抹掉已经复制好的备份
                job["errors"].append("打包 zip 失败：%s（文件已备份到 %s）" % (ex, dest))
                job["result"]["zip_error"] = str(ex)
        job["state"] = "canceled" if job["cancel"] else "done"
    except Exception as ex:
        job["errors"].append(str(ex))
        job["state"] = "error"
    finally:
        job["current"] = ""


def _write_readme(dest, man):
    """快照目录里放一份人类可读说明：解压后不用猜这是什么、manifest 该在哪、怎么还原。"""
    first = ((man.get("entries") or [{}])[0].get("dest")
             or (man.get("entries") or [{}])[0].get("key") or "claude")
    lines = ["AI CLI 备份快照（由「会话转 MD」生成，勿手动挪动里面的文件）", "",
             "备份时间：%s" % man.get("created", ""),
             "来源机器：%s（用户 %s）" % (man.get("host", ""), man.get("user", "")),
             "来源 HOME：%s" % man.get("home", ""),
             "系统：%s" % man.get("os", ""),
             "含凭证文件：%s" % ("是 —— 这份备份等于账号，别放公开网盘"
                            if man.get("includes_secrets") else "否"),
             "", "包含的 CLI："]
    for en in man.get("entries") or []:
        lines.append("  - %s（%s）：%s → 本目录下的 %s\\，%d 个文件 / %s"
                     % (en.get("label") or en.get("key"), en.get("key"), en.get("source"),
                        en.get("dest") or en.get("key"), en.get("files", 0),
                        reg.fmt_size(en.get("bytes", 0))))
    lines += ["", "目录结构：",
              "  %s        还原用的清单，别删、别单独挪走" % MANIFEST,
              "  %s\\root\\…      对应源目录 ~/.%s 下的文件" % (first, first),
              "  %s\\home\\…      对应 HOME 里的配套文件（如 .claude.json）" % first,
              "", "怎么还原：",
              "  1. 把这个目录（或它的 zip）整个拷到新电脑，zip 解压出来就是这个目录；",
              "  2. 打开「会话转 MD」→ 🔀 迁移 → 备份 / 还原 → 还原；",
              "  3. 「备份来源」选**本目录**（%s 所在的这一层，不是里面的 %s\\ 子目录；"
              "选错层会自动往上/往下找一层）；" % (MANIFEST, first),
              "  4. 先点「读取并预览」核对清单和「还原到」，再点「开始还原」；",
              "  5. 还原前请先关掉对应 CLI，还原只增不删，覆盖前会先留 .bak-时间戳。"]
    with open(long_path(os.path.join(dest, README_NAME)), "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")


def _make_zip(dest, man):
    """把备份目录打成 zip，放在备份目录的**同级**，包内统一套一层与 zip 同名的目录。

    只收 manifest + 说明 + manifest 里记下的各 CLI 子目录，不 `os.walk(dest)`：
    ① 目标目录里可能还有别人的文件，不该一起打包；
    ② zip 自己就在旁边，走整棵树会把不断变大的 zip 也收进去；
    ③ 早先 `dest.rstrip('\\\\/')` 遇到 `E:\\` 会退化成 `E:` —— 这是「当前盘当前目录」的
       相对写法，zip 被写到进程 cwd 里，同时 walk 整个 E 盘撞上 pagefile.sys 直接失败。
    包内套一层是用户实测反馈的：以前 manifest.json 和 `claude/` 平铺在包根，解压到现成目录里
    json 就散在文件夹外边，还原时选中 `claude` 目录直接报「没有 manifest.json」。
    """
    root = os.path.abspath(dest)
    parent, base = os.path.split(root.rstrip("\\/"))
    base = base or "backup"
    stem = base if SNAP_RE.search(base) else "%s_%s" % (base, now_stamp())
    zp = os.path.join(parent or root, stem + ".zip")
    items = [MANIFEST, README_NAME] + [en.get("dest") or en["key"]
                                       for en in man.get("entries") or []]
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for it in items:
            p = os.path.join(root, it)
            if os.path.isfile(long_path(p)):
                z.write(long_path(p), "%s/%s" % (stem, it))
                continue
            for dirpath, _dirs, files in os.walk(long_path(p)):
                for fn in files:
                    fp = os.path.join(dirpath, fn)
                    rel = os.path.relpath(fp, root).replace("\\", "/")
                    z.write(long_path(fp), "%s/%s" % (stem, rel))
    return zp


# ---------------- 还原 ----------------
def _man_at(d):
    """读 d/manifest.json；读不出来或不像本工具的清单就当没有（别把随便一个同名文件当备份）。"""
    p = os.path.join(d, MANIFEST)
    if not os.path.isfile(long_path(p)):
        return None
    try:
        with open(long_path(p), "r", encoding="utf-8") as f:
            man = json.load(f)
    except Exception:
        return None
    return man if isinstance(man, dict) and isinstance(man.get("entries"), list) else None


def _entry_dirs_here(root, man):
    """manifest 记的 CLI 子目录，有几个真的在 root 底下。"""
    return sum(1 for en in man.get("entries") or []
               if os.path.isdir(long_path(os.path.join(root, en.get("dest")
                                                       or en.get("key") or ""))))


def _sub_manifests(d, limit=6):
    """d 的直接子目录里带 manifest 的那些（最多找 limit 个，够判断唯一性 + 报错时列名字）。"""
    hits = []
    try:
        for name in sorted(os.listdir(long_path(d))):
            sub = os.path.join(d, name)
            if os.path.isdir(long_path(sub)) and _man_at(sub):
                hits.append(sub)
                if len(hits) >= limit:
                    break
    except OSError:
        pass
    return hits


def locate_backup(d):
    """把用户随手选的目录归位，返回 (内容根目录, manifest)。内容根 = 各 CLI 子目录所在那层。

    实测用户会选到四种位置，全都要认下来，不然只能干巴巴回一句「不是本工具生成的备份」：
    ① 正好选中备份目录 → 原样用；
    ② 选到上一层（盘根，或解压出来的外层目录）→ 下钻唯一那份备份；
    ③ 选到里面的 CLI 子目录（`E:\\claude`）→ 上浮一层，父目录带 manifest 就用父目录；
    ④ manifest 被手动挪进了 CLI 子目录 → manifest 用找到的这份，内容根取父目录。
    ③④ 是用户实测踩到的：早先只会往下钻，选中 `E:\\claude` 就报「没有 manifest.json」；
    手动把 json 挪进去之后条目目录又被算成 `E:\\claude\\claude`，预览出来 0 个文件 0 B。
    返回值把 manifest 一起带出来，就是因为 ④ 里「清单在哪」和「文件在哪」不在同一层，
    只返回目录的话后面再按目录去找清单，撞上同层有两份备份（`E:\\claude` 和
    `E:\\AI-CLI-Backup`）就会挑错人。
    """
    if not d:
        raise ValueError("请先选择备份目录或 zip 包")
    d = os.path.abspath(d)
    man = _man_at(d)
    if man and _entry_dirs_here(d, man):
        return d, man                                     # ①
    subs = _sub_manifests(d)
    only = subs[0] if len(subs) == 1 else None
    oman = _man_at(only) if only else None
    if oman and _entry_dirs_here(only, oman):
        return only, oman                                 # ②
    parent = os.path.dirname(d.rstrip("\\/"))
    up = parent if parent and parent != d else ""
    if man:
        if up and _entry_dirs_here(up, man):
            return up, man                                # ④
        return d, man          # 层级对不上，交给 plan_restore 把期望目录摆给用户看
    if up:
        pman = _man_at(up)
        if pman and _entry_dirs_here(up, pman):
            return up, pman                               # ③
    if only:
        return only, oman      # 唯一那份备份但条目目录不全，同样走提示而不是猜
    if len(subs) > 1:
        raise ValueError("%s 下面有 %d 份备份（%s），请直接选中要还原的那一份。"
                         % (d, len(subs), "、".join(os.path.basename(s) for s in subs)))
    raise ValueError("这个目录里没有 %s，或它不是本工具生成的备份：%s\n"
                     "请选到 %s 和各 CLI 子目录同在的那一层（选它们的上一层、或里面的某个 CLI "
                     "子目录也能自动认出来）；zip 请先解压，再选解出来的那个目录。"
                     % (MANIFEST, d, MANIFEST))


def resolve_backup_dir(d):
    """只要内容根目录；认不出来就原样返回，让调用方自己报错。"""
    try:
        return locate_backup(d)[0]
    except ValueError:
        return d


def read_manifest(backup_dir):
    return locate_backup(backup_dir)[1]


def unzip_backup(zip_path, workdir=None):
    """还原端接受 zip：先解到临时目录，再走同一条还原路径。

    包内现在统一套一层与包同名的目录，解出来是 `<workdir>/<快照名>/manifest.json`，
    交给 resolve_backup_dir 下钻一层即可；老版本平铺在包根的 zip 也照样认。
    """
    workdir = workdir or os.path.join(os.path.dirname(zip_path),
                                      "_unzip_" + now_stamp())
    os.makedirs(long_path(workdir), exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(long_path(workdir))
    return resolve_backup_dir(workdir)


def restore_targets(backup_dir, home=None, overrides=None):
    """按 manifest 推导「备份子目录 → 本机目标目录」，overrides 可逐条改。

    默认把 manifest 里记的旧 HOME 换成当前 HOME：源 C:\\Users\\A\\.claude 记在
    manifest 里，新机 HOME 变了也能落到新机的 ~/.claude。
    """
    backup_dir, man = locate_backup(backup_dir)
    home = home or reg.HOME
    old_home = man.get("home") or home
    out = []
    for en in man.get("entries") or []:
        src_root = en.get("source") or ""
        tgt = src_root
        if src_root and _is_subpath(src_root, old_home):
            tgt = os.path.join(home, os.path.relpath(src_root, old_home))
        tgt = (overrides or {}).get(en["key"]) or tgt
        out.append({"key": en["key"], "label": en.get("label") or en["key"],
                    "scope": en.get("scope"), "files": en.get("files", 0),
                    "bytes": en.get("bytes", 0), "size": reg.fmt_size(en.get("bytes", 0)),
                    "backup_source": src_root, "root_target": os.path.normpath(tgt),
                    "home_target": home,
                    "dir": os.path.join(backup_dir, en.get("dest") or en["key"])})
    return {"manifest": man, "backup_dir": backup_dir, "old_home": old_home,
            "home": home, "entries": out}


def _iter_backup_files(entry_dir):
    """遍历备份里某个 CLI 子目录，产出 (源文件, 'root'|'home', 目录内相对路径)。"""
    for bucket in ("root", "home"):
        base = os.path.join(entry_dir, bucket)
        if not os.path.isdir(long_path(base)):
            continue
        for dirpath, _dirs, files in os.walk(long_path(base)):
            for fn in files:
                p = os.path.join(dirpath, fn)
                yield p, bucket, os.path.relpath(p, base)


def _dest_of(item, bucket, rel):
    return os.path.join(item["root_target"] if bucket == "root" else item["home_target"], rel)


def plan_restore(backup_dir, home=None, overrides=None, keys=None,
                 conflict=CONFLICT_SKIP):
    """还原干跑：每条目录会写多少文件、多少字节，多少个目标已存在（会跳过或备份覆盖）。"""
    info = restore_targets(backup_dir, home, overrides)
    backup_dir = info["backup_dir"]        # 归位只做一次，别来回跳（会算成 <dir>\<key>\<key>）
    out = {"action": "restore", "backup_dir": backup_dir, "old_home": info["old_home"],
           "home": info["home"], "conflict": conflict, "entries": [],
           "manifest": info["manifest"], "files": 0, "bytes": 0, "exists": 0, "top": []}
    for item in info["entries"]:
        if keys and item["key"] not in keys:
            continue
        n = b = ex = 0
        top = []
        for src, bucket, rel in _iter_backup_files(item["dir"]):
            st = _stat(src)
            sz = st.st_size if st else 0
            n += 1
            b += sz
            dst = _dest_of(item, bucket, rel)
            if os.path.exists(long_path(dst)):
                ex += 1
            if len(top) < TOP_N or sz > top[-1][0]:
                top.append((sz, bucket + "/" + rel.replace("\\", "/")))
                top.sort(reverse=True)
                del top[TOP_N:]
        row = dict(item)
        row.update({"files": n, "bytes": b, "size": reg.fmt_size(b), "exists": ex,
                    "missing": not os.path.isdir(long_path(item["dir"])),
                    "top": [{"size": reg.fmt_size(s), "path": p} for s, p in top]})
        out["entries"].append(row)
        out["files"] += n
        out["bytes"] += b
        out["exists"] += ex
    out["size"] = reg.fmt_size(out["bytes"])
    miss = [e["dir"] for e in out["entries"] if e["missing"]]
    if miss:
        out["missing"] = miss
    if not out["files"]:
        # 一份 0 文件的「有效备份」和一份找错层的备份，在界面上长得一模一样（都是 0 个 / 0 B），
        # 用户实测就卡在这儿。直接把期望的目录摆出来，别让人猜是不是「全被跳过了」。
        out["warn"] = ("没找到可还原的文件：本工具期望在 %s 下面看到 root\\ 或 home\\ 子目录。"
                       "常见原因是 %s 和各 CLI 子目录没放在同一层（比如 json 被单独挪进了子目录），"
                       "把它放回 %s 再试，或直接选它们共同的上一层目录。"
                       % ("、".join(miss) if miss else backup_dir, MANIFEST, backup_dir))
    return out


def start_restore(backup_dir, home=None, overrides=None, keys=None,
                  conflict=CONFLICT_SKIP, rewrite=None):
    """起后台还原任务。rewrite: {'enabled':True,'mapping':{旧路径:新路径}} 才做路径改写。"""
    resolved, man = locate_backup(backup_dir)      # 先校验是本工具的备份
    if not _entry_dirs_here(resolved, man):
        # 层级不对时不能默默跑一个 0 文件的「成功」还原，那比报错更难查。
        raise ValueError("%s 里找不到 manifest 记的任何 CLI 子目录（%s），层级不对。"
                         "请选到 %s 和这些子目录同在的那一层。"
                         % (resolved,
                            "、".join((en.get("dest") or en.get("key") or "?")
                                      for en in man.get("entries") or []) or "无",
                            MANIFEST))
    job = new_job("restore")
    job["result"] = {"backup_dir": resolved}
    # 传给后台线程的是**用户原样选的路径**：归位只能对原始输入做一次。拿归位后的路径再跑一遍
    # locate_backup，遇到「manifest 在子目录、同层还有另一份备份」就会因为同层有两份而报歧义。
    t = threading.Thread(target=_run_restore, daemon=True,
                         args=(job, backup_dir, home, overrides, keys, conflict, rewrite))
    t.start()
    return job


def _run_restore(job, backup_dir, home, overrides, keys, conflict, rewrite):
    try:
        plan = plan_restore(backup_dir, home, overrides, keys, conflict)
        job["total_files"] = plan["files"]
        job["total_bytes"] = plan["bytes"]
        written = []
        for item in plan["entries"]:
            for src, bucket, rel in _iter_backup_files(item["dir"]):
                if job["cancel"]:
                    break
                job["current"] = rel
                st = _stat(src)
                sz = st.st_size if st else 0
                dst = _dest_of(item, bucket, rel)
                if os.path.exists(long_path(dst)):
                    if conflict == CONFLICT_SKIP:
                        job["skipped"] += 1
                        job["done_files"] += 1
                        job["done_bytes"] += sz
                        continue
                    _backup_aside(job, dst)
                if _copy(job, src, dst) == "copied":
                    written.append(dst)
                job["done_files"] += 1
                job["done_bytes"] += sz
            if job["cancel"]:
                break
        job["result"] = {"backup_dir": plan["backup_dir"], "written": len(written),
                         "targets": [e["root_target"] for e in plan["entries"]]}
        if rewrite and rewrite.get("enabled") and not job["cancel"]:
            job["current"] = "改写会话里的项目路径…"
            job["result"]["rewrite"] = do_rewrite(
                rewrite.get("mapping") or {}, home=home,
                roots=[e["root_target"] for e in plan["entries"]], job=job)
        job["state"] = "canceled" if job["cancel"] else "done"
    except Exception as ex:
        job["errors"].append(str(ex))
        job["state"] = "error"
    finally:
        job["current"] = ""


def _backup_aside(job, path):
    """覆盖前把原文件改名成 .bak-<时间戳>，绝不直接丢掉目标端的东西。"""
    bak = "%s.bak-%s" % (path, now_stamp())
    try:
        shutil.move(long_path(path), long_path(bak))
    except Exception as ex:
        job["errors"].append("留底失败 %s：%s" % (path, ex))


# ---------------- 换机路径改写 ----------------
# 四处（本机实测口径）：
#   1 ~/.claude/projects/<slug>/ 目录名（slug = 非字母数字全换成 -）
#   2 Claude JSONL 每行 cwd / Codex rollout 的 session_meta.payload.cwd
#   3 ~/.claude.json 的 projects 键 —— 正斜杠绝对路径
#   4 ~/.claude/history.jsonl 的 project 字段 —— 反斜杠绝对路径
def _hit(value, old):
    """value 是否以 old 这个目录为前缀（按目录边界判断，避免 E:\\在办 命中 E:\\在办项目）。"""
    if not isinstance(value, str) or not value or not old:
        return None
    o = old.rstrip("\\/")
    v, ov = value.replace("/", "\\").lower(), o.replace("/", "\\").lower()
    if v == ov:
        return ""
    if v.startswith(ov + "\\"):
        return value[len(o):]
    return None


def remap(value, mapping):
    """按映射换路径头；命中就用新头 + 原尾（保留原串的斜杠风格），否则原样返回。"""
    if not isinstance(value, str) or not value:
        return value
    for old, new in mapping.items():
        tail = _hit(value, old)
        if tail is None:
            continue
        n = new.rstrip("\\/")
        if "/" in value and "\\" not in value:     # 原串是正斜杠风格
            n, tail = n.replace("\\", "/"), tail.replace("\\", "/")
        else:
            n, tail = n.replace("/", "\\"), tail.replace("/", "\\")
        return n + tail
    return value


def _bak(path):
    """改写前留一份 .bak；已存在就不再覆盖那份原始底。"""
    b = path + ".bak-" + now_stamp()
    if not os.path.exists(long_path(b)):
        shutil.copy2(long_path(path), long_path(b))
    return b


def _jsonl_cwd_pass(path, mapping, apply=False):
    """扫一个 JSONL：Claude 每行的 cwd、Codex 的 session_meta.payload.cwd。

    返回命中行数。apply=True 时整文件重写（先 .bak），非 JSON 行原样保留。
    """
    hits, out = 0, []
    try:
        with open(long_path(path), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.rstrip("\n")
                changed = False
                if s.strip().startswith("{"):
                    try:
                        o = json.loads(s)
                    except Exception:
                        o = None
                    if isinstance(o, dict):
                        for holder, key in ((o, "cwd"),
                                            (o.get("payload") if isinstance(o.get("payload"), dict) else None, "cwd")):
                            if holder is None:
                                continue
                            v = holder.get(key)
                            nv = remap(v, mapping)
                            if isinstance(v, str) and nv != v:
                                holder[key] = nv
                                changed = True
                        if changed:
                            hits += 1
                            s = json.dumps(o, ensure_ascii=False)
                if apply:
                    out.append(s)
    except OSError:
        return 0
    if apply and hits:
        _bak(path)
        with open(long_path(path), "w", encoding="utf-8", newline="\n") as f:
            for s in out:
                f.write(s + "\n")
    return hits


def _history_pass(path, mapping, apply=False):
    """Claude history.jsonl 的 project 字段（反斜杠绝对路径）。"""
    hits, out = 0, []
    try:
        with open(long_path(path), "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.rstrip("\n")
                try:
                    o = json.loads(s)
                except Exception:
                    o = None
                if isinstance(o, dict):
                    v = o.get("project")
                    nv = remap(v, mapping)
                    if isinstance(v, str) and nv != v:
                        o["project"] = nv.replace("/", "\\")
                        hits += 1
                        s = json.dumps(o, ensure_ascii=False)
                if apply:
                    out.append(s)
    except OSError:
        return 0
    if apply and hits:
        _bak(path)
        with open(long_path(path), "w", encoding="utf-8", newline="\n") as f:
            for s in out:
                f.write(s + "\n")
    return hits


def _claude_json_pass(path, mapping, apply=False):
    """~/.claude.json 的 projects 键（正斜杠绝对路径）。"""
    try:
        with open(long_path(path), "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:
        return 0
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return 0
    hits, new = 0, {}
    for k, v in projects.items():
        nk = remap(k, mapping)
        if nk != k:
            hits += 1
            nk = nk.replace("\\", "/")
        new[nk] = v
    if apply and hits:
        _bak(path)
        data["projects"] = new
        with open(long_path(path), "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return hits


def _dir_cwd(d):
    """读目录里第一个 jsonl 的 cwd，反查这个 slug 目录对应哪个工作目录。"""
    try:
        for fn in sorted(os.listdir(long_path(d))):
            if not fn.endswith(".jsonl"):
                continue
            for o in parser.read_json_lines(os.path.join(d, fn), max_lines=40):
                if o.get("cwd"):
                    return str(o["cwd"])
            return ""
    except OSError:
        pass
    return ""


def _project_dirs_pass(projects_root, mapping, apply=False):
    """~/.claude/projects/<slug>/ 目录改名：cwd 换了，slug 也得跟着换。"""
    plans = []
    try:
        names = sorted(os.listdir(long_path(projects_root)))
    except OSError:
        return plans
    for name in names:
        d = os.path.join(projects_root, name)
        if not os.path.isdir(long_path(d)):
            continue
        cwd = _dir_cwd(d)
        new_cwd = remap(cwd, mapping)
        if not cwd or new_cwd == cwd:
            continue
        new_name = migrator.claude_slug(new_cwd)
        if new_name == name:
            continue
        plans.append({"old": d, "new": os.path.join(projects_root, new_name),
                      "cwd": cwd, "new_cwd": new_cwd})
    if apply:
        for p in plans:
            try:
                if os.path.exists(long_path(p["new"])):
                    # 目标 slug 目录已存在（两种编码并存时会遇到）：逐个文件搬过去
                    for fn in os.listdir(long_path(p["old"])):
                        s, t = os.path.join(p["old"], fn), os.path.join(p["new"], fn)
                        if not os.path.exists(long_path(t)):
                            shutil.move(long_path(s), long_path(t))
                    if not os.listdir(long_path(p["old"])):
                        os.rmdir(long_path(p["old"]))
                    p["merged"] = True
                else:
                    shutil.move(long_path(p["old"]), long_path(p["new"]))
            except Exception as ex:
                p["error"] = str(ex)
    return plans


def _iter_jsonl(root, subs):
    for sub in subs:
        base = os.path.join(root, sub) if sub else root
        if not os.path.isdir(long_path(base)):
            continue
        for dirpath, _dirs, files in os.walk(long_path(base)):
            for fn in files:
                if fn.endswith(".jsonl"):
                    yield os.path.join(dirpath, fn)


def _rewrite(mapping, home=None, roots=None, apply=False, job=None):
    """四处路径改写的统一入口。apply=False 就是干跑，只数不改。"""
    mapping = {k: v for k, v in (mapping or {}).items() if k and v}
    home = home or reg.HOME
    out = {"mapping": mapping, "apply": apply, "dirs": [], "cwd_files": 0,
           "cwd_lines": 0, "history_lines": 0, "claude_json_keys": 0, "errors": []}
    if not mapping:
        return out
    for root in (roots or []):
        if not root or not os.path.isdir(long_path(root)):
            continue
        is_claude = os.path.isdir(os.path.join(root, "projects"))
        subs = (["projects", "sessions"] if is_claude
                else ["sessions", "archived_sessions"])
        if is_claude:
            # 目录改名必须排在 JSONL 改写之前：slug 目录是靠读里面第一条记录的 cwd 反查的，
            # 先把 cwd 改了就再也匹配不上映射，目录名会留在旧值上。
            out["dirs"] += _project_dirs_pass(os.path.join(root, "projects"),
                                              mapping, apply)
        for p in _iter_jsonl(root, subs):
            if job and job.get("cancel"):
                break
            n = _jsonl_cwd_pass(p, mapping, apply)
            if n:
                out["cwd_files"] += 1
                out["cwd_lines"] += n
                if job:
                    job["current"] = os.path.basename(p)
        hist = os.path.join(root, "history.jsonl")
        if is_claude and os.path.isfile(long_path(hist)):
            out["history_lines"] += _history_pass(hist, mapping, apply)
    cj = os.path.join(home, ".claude.json")
    if os.path.isfile(long_path(cj)):
        out["claude_json_keys"] += _claude_json_pass(cj, mapping, apply)
    out["dir_count"] = len(out["dirs"])
    out["errors"] += [d["error"] for d in out["dirs"] if d.get("error")]
    return out


def plan_rewrite(mapping, home=None, roots=None):
    """干跑：命中多少目录、多少 JSONL 行、多少配置键。UI 拿这个做二次确认。"""
    return _rewrite(mapping, home, roots, apply=False)


def do_rewrite(mapping, home=None, roots=None, job=None):
    """真改。每个被改的文件先写 .bak-<时间戳>。"""
    return _rewrite(mapping, home, roots, apply=True, job=job)
