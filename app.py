# -*- coding: utf-8 -*-
"""会话转 MD —— 桌面应用入口（Flask 后端 + pywebview 原生窗口）。"""
import os
import sys
import json
import socket
import threading

from flask import Flask, request, jsonify, send_from_directory
import webview

from backend import scanner, parser, converter, migrator, cli_registry, vault


def resource_path(rel):
    """打包后从 _MEIPASS 取资源，开发时从脚本目录取。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def app_dir():
    """配置文件存放目录：打包后放 exe 同级，开发时放脚本目录（绿色版跟着走）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "config.json")
WEB_DIR = resource_path("web")

app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")
_window = None


def load_config():
    cfg = {"sources": [], "output_dir": "", "vault_custom": [], "backup_dir": ""}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception:
            pass
    if not cfg.get("sources"):
        cfg["sources"] = scanner.default_sources()
    if not cfg.get("output_dir"):
        cfg["output_dir"] = os.path.join(os.path.expanduser("~"), "会话MD导出")
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("保存配置失败:", e)


# ---------------- 路由 ----------------
@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/api/state")
def api_state():
    cfg = load_config()
    return jsonify({"sources": cfg["sources"], "output_dir": cfg["output_dir"],
                    "backup_dir": cfg.get("backup_dir") or "",
                    "home": os.path.expanduser("~"),
                    "defaults": scanner.default_sources()})


@app.route("/api/sessions")
def api_sessions():
    cfg = load_config()
    return jsonify(scanner.scan(cfg["sources"]))


@app.route("/api/session")
def api_session():
    path = request.args.get("path", "")
    source = request.args.get("source", "claude")
    if not os.path.isfile(path):
        return jsonify({"error": "文件不存在"}), 404
    events = parser.parse(path, source)
    return jsonify({"events": events})


@app.route("/api/convert", methods=["POST"])
def api_convert():
    data = request.get_json(force=True) or {}
    items = data.get("items", [])
    outdir = data.get("output_dir") or load_config()["output_dir"]
    results, used = [], set()
    for it in items:
        path = it.get("path", "")
        source = it.get("source", "claude")
        mode = it.get("mode", converter.MODE_TOOLS)
        try:
            events = parser.parse(path, source)
            meta = {"title": it.get("title"), "source": source,
                    "project": it.get("project"),
                    "created": it.get("created"), "modified": it.get("modified")}
            md = converter.convert(events, meta, mode)
            sub = os.path.join(outdir, source)
            os.makedirs(sub, exist_ok=True)
            date_prefix = (it.get("created") or it.get("modified") or "").split(" ")[0].replace(":", "-")
            base = f"{date_prefix}_{scanner.slugify(it.get('title'))}" if date_prefix \
                else scanner.slugify(it.get("title"))
            fname = base + ".md"
            n = 1
            while (os.path.join(sub, fname) in used) or os.path.exists(os.path.join(sub, fname)):
                fname = f"{base}_{n}.md"
                n += 1
            fpath = os.path.join(sub, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(md)
            used.add(fpath)
            results.append({"path": path, "ok": True, "out": fpath})
        except Exception as e:
            results.append({"path": path, "ok": False, "error": str(e)})
    return jsonify({"results": results,
                    "ok": sum(1 for r in results if r["ok"]),
                    "fail": sum(1 for r in results if not r["ok"]),
                    "output_dir": outdir})


@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json(force=True) or {}
    cfg = load_config()
    if "sources" in data:
        cfg["sources"] = data["sources"]
    if "output_dir" in data:
        cfg["output_dir"] = data["output_dir"]
    for k in ("vault_custom", "backup_dir"):
        if k in data:
            cfg[k] = data[k]
    save_config(cfg)
    return jsonify({"ok": True})


# ---------------- 迁移：跨 CLI ----------------
@app.route("/api/migrate/cross", methods=["POST"])
def api_migrate_cross():
    """把一个会话写成目标 CLI 的原生会话文件，或写成通用交接包。

    只新建文件，不动任何已有会话；工具活动一律折叠成文本摘要（绝不生成 tool_use 块）。
    """
    data = request.get_json(force=True) or {}
    items = data.get("items") or ([data["item"]] if data.get("item") else [])
    target = data.get("target") or "claude"
    opts = {"scope": data.get("scope") or migrator.SCOPE_ALL,
            "last_n": data.get("last_n") or migrator.DEFAULT_LAST_N,
            "char_cap": data.get("char_cap") or migrator.DEFAULT_CHAR_CAP,
            "with_tools": bool(data.get("with_tools", True)),
            "mode": data.get("mode") or converter.MODE_TOOLS,
            "target_cwd": data.get("target_cwd") or "",
            "output_dir": data.get("output_dir") or load_config()["output_dir"]}
    results = []
    for it in items:
        try:
            results.append(dict(migrator.migrate(it, target, opts), ok=True,
                                title=it.get("title") or ""))
        except Exception as e:
            results.append({"ok": False, "error": str(e), "title": it.get("title") or ""})
    return jsonify({"results": results,
                    "ok": sum(1 for r in results if r["ok"]),
                    "fail": sum(1 for r in results if not r["ok"])})


# ---------------- 迁移：备份 / 还原 ----------------
@app.route("/api/vault/registry")
def api_vault_registry():
    """本机 AI CLI 目录列表。不带体积（45 个目录一次全算要十几秒），体积按行单独问。"""
    cfg = load_config()
    return jsonify({"entries": cli_registry.registry(custom=cfg.get("vault_custom") or []),
                    "home": cli_registry.HOME,
                    "scopes": [{"v": vault.SCOPE_SESSIONS, "label": "仅会话数据"},
                               {"v": vault.SCOPE_ROOT, "label": "整个根目录（智能排除）"},
                               {"v": vault.SCOPE_FULL, "label": "完整不排除"}]})


@app.route("/api/vault/size")
def api_vault_size():
    root = request.args.get("root", "")
    key = request.args.get("key", "")
    if not os.path.isdir(root):
        return jsonify({"error": "目录不存在"}), 404
    rule = cli_registry.find(key, custom=load_config().get("vault_custom") or [])
    return jsonify(cli_registry.measure(root, (rule or {}).get("junk") or []))


@app.route("/api/vault/plan", methods=["POST"])
def api_vault_plan():
    """干跑：备份/还原共用，用 action 区分。UI 先给用户看清楚再让他决定跑不跑。"""
    d = request.get_json(force=True) or {}
    custom = load_config().get("vault_custom") or []
    try:
        if (d.get("action") or "backup") == "restore":
            return jsonify(vault.plan_restore(d.get("backup_dir") or "",
                                              d.get("home") or None,
                                              d.get("overrides") or {},
                                              d.get("keys") or None,
                                              d.get("conflict") or vault.CONFLICT_SKIP))
        return jsonify(vault.plan_backup(d.get("entries") or [], d.get("dest") or "",
                                         None, custom))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/vault/rewrite/plan", methods=["POST"])
def api_vault_rewrite_plan():
    """路径改写单独干跑：命中多少目录 / 多少行 / 多少配置键，二次确认用。"""
    d = request.get_json(force=True) or {}
    try:
        return jsonify(vault.plan_rewrite(d.get("mapping") or {}, d.get("home") or None,
                                          d.get("roots") or []))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/vault/backup", methods=["POST"])
def api_vault_backup():
    d = request.get_json(force=True) or {}
    try:
        job = vault.start_backup(d.get("entries") or [], d.get("dest") or "", None,
                                load_config().get("vault_custom") or [],
                                bool(d.get("zip")))
        return jsonify({"job_id": job["id"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/vault/restore", methods=["POST"])
def api_vault_restore():
    d = request.get_json(force=True) or {}
    try:
        src = d.get("backup_dir") or ""
        if src.lower().endswith(".zip"):
            src = vault.unzip_backup(src)
        job = vault.start_restore(src, d.get("home") or None, d.get("overrides") or {},
                                 d.get("keys") or None,
                                 d.get("conflict") or vault.CONFLICT_SKIP,
                                 d.get("rewrite") or None)
        return jsonify({"job_id": job["id"],
                        "backup_dir": (job.get("result") or {}).get("backup_dir") or src})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/vault/job")
def api_vault_job():
    j = vault.get_job(request.args.get("id", ""))
    return jsonify(j) if j else (jsonify({"error": "任务不存在"}), 404)


@app.route("/api/vault/cancel", methods=["POST"])
def api_vault_cancel():
    d = request.get_json(force=True) or {}
    return jsonify({"ok": vault.cancel_job(d.get("id") or "")})


class Api:
    """暴露给前端的原生能力（文件夹选择对话框）。"""
    def pick_directory(self):
        try:
            res = _window.create_file_dialog(webview.FOLDER_DIALOG)
            if res:
                return res[0] if isinstance(res, (list, tuple)) else res
        except Exception as e:
            print("选择目录失败:", e)
        return ""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    global _window
    port = _free_port()
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port,
                     threaded=True, use_reloader=False), daemon=True).start()
    _window = webview.create_window("会话转 MD", f"http://127.0.0.1:{port}",
                                    js_api=Api(), width=1240, height=800, min_size=(960, 640))
    webview.start()


if __name__ == "__main__":
    main()
