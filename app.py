# -*- coding: utf-8 -*-
"""会话转 MD —— 桌面应用入口（Flask 后端 + pywebview 原生窗口）。"""
import os
import sys
import json
import socket
import threading

from flask import Flask, request, jsonify, send_from_directory
import webview

from backend import scanner, parser, converter


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
    cfg = {"sources": [], "output_dir": ""}
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
    save_config(cfg)
    return jsonify({"ok": True})


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
