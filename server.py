#!/usr/bin/env python3
"""gpt-image-2 生图工作台 — 零依赖本地服务。

用法:
    python3 server.py            # 默认端口 8000
    python3 server.py 9000       # 指定端口

API Key 和请求地址可以写在 .env 里，也可以直接在网页界面填写（界面优先）。
生成的图片自动保存到 outputs/ 目录，元数据记录在 outputs/history.jsonl。
"""

import base64
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(ROOT, "outputs")
HISTORY_PATH = os.path.join(OUTPUT_DIR, "history.jsonl")
HISTORY_LOCK = threading.Lock()

IMAGE_MIME = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
FMT_EXT = {"png": "png", "webp": "webp", "jpeg": "jpg"}


def load_env():
    env = {}
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def candidate_urls(base_url: str, endpoint: str) -> list:
    """根据填写的请求地址生成按优先级排列的候选端点，404 时依次回退。

    支持: https://host / https://host/v1 / https://host/v1/images/generations
    """
    split = urllib.parse.urlsplit(base_url.strip())
    query = ("?" + split.query) if split.query else ""
    url = urllib.parse.urlunsplit((split.scheme, split.netloc, split.path, "", "")).rstrip("/")
    if url.endswith("/" + endpoint):
        return [url + query]
    # 用户可能填了完整的 generations 端点，切换到 edits/models 时先剥掉
    for suffix in ("/images/generations", "/images/edits"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    url = url.rstrip("/")
    if re.search(r"/v\d+$", url):
        bare = re.sub(r"/v\d+$", "", url)
        return [f"{url}/{endpoint}{query}", f"{bare}/{endpoint}{query}"]
    return [f"{url}/v1/{endpoint}{query}", f"{url}/{endpoint}{query}"]


class _NoPostRedirect(urllib.request.HTTPRedirectHandler):
    """禁止把 POST 重定向降级成 GET（urllib 默认会丢弃 body 改发 GET）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.get_method() == "POST":
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def call_upstream(candidates, api_key, body, content_type, method="POST", timeout=600):
    """依次尝试候选端点，返回 (json数据, None) 或 (None, 错误dict)。"""
    ctx = ssl.create_default_context()
    opener = urllib.request.build_opener(
        _NoPostRedirect(), urllib.request.HTTPSHandler(context=ctx))
    last_err = None
    for url in candidates:
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": content_type, "Authorization": f"Bearer {api_key}"}
            if body is not None
            else {"Authorization": f"Bearer {api_key}"},
            method=method,
        )
        sys.stderr.write(f"[upstream] {method} {url}\n")
        try:
            with opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            sys.stderr.write(f"[upstream] HTTP {e.code} from {url}\n")
            if e.code in (301, 302, 303, 307, 308):
                loc = (e.headers or {}).get("Location", "") or "未知地址"
                return None, {"error": f"上游要求重定向（HTTP {e.code}）到 {loc}，"
                                       f"请把请求地址直接改为该地址（原地址: {url}）"}
            detail = e.read().decode("utf-8", errors="replace")
            last_err = {"error": f"上游返回 HTTP {e.code}（请求地址: {url}）", "detail": detail[:2000]}
            if e.code not in (404, 405):
                return None, last_err  # 404/405 才尝试下一个候选路径
        except Exception as e:
            return None, {"error": f"请求失败: {e}（请求地址: {url}）"}
    return None, last_err or {"error": "请求失败"}


def parse_data_url(durl: str):
    """解析 data:image/png;base64,xxx 形式的图片，返回 (bytes, mime)。"""
    m = re.match(r"^data:(image/[\w.+-]+);base64,(.+)$", durl, re.DOTALL)
    if not m:
        raise ValueError("不是有效的 base64 图片")
    mime = m.group(1).lower()
    try:
        raw = base64.b64decode(m.group(2), validate=False)
    except Exception:
        raise ValueError("base64 解码失败")
    if not raw:
        raise ValueError("图片内容为空")
    return raw, mime


def multipart_body(fields: dict, files: list):
    """用 stdlib 手工构造 multipart/form-data。files: [(name, filename, mime, bytes)]"""
    boundary = "----imgtool" + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts.append(
            (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
             f"{value}\r\n").encode("utf-8")
        )
    for name, filename, mime, raw in files:
        parts.append(
            (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
             f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
        )
        parts.append(raw)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def append_history(record: dict):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with HISTORY_LOCK:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_history():
    records = []
    with HISTORY_LOCK:
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, encoding="utf-8") as f:
                for line in f:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        continue
    known = set()
    out = []
    for rec in records:
        files = rec.get("files") or []
        known.update(files)
        alive = [n for n in files if os.path.isfile(os.path.join(OUTPUT_DIR, n))]
        if alive:
            rec["files"] = alive
            out.append(rec)
    # 没有元数据的旧图片也纳入历史（比如本工具早期版本生成的）
    if os.path.isdir(OUTPUT_DIR):
        for name in os.listdir(OUTPUT_DIR):
            ext = os.path.splitext(name)[1].lower()
            if ext in IMAGE_MIME and name not in known:
                path = os.path.join(OUTPUT_DIR, name)
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue  # 刚被并发删除
                out.append({"id": "old-" + name, "ts": mtime,
                            "prompt": None, "files": [name]})
    out.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return out[:500]


def generate(payload: dict) -> dict:
    env = load_env()
    api_key = (payload.get("apiKey") or env.get("API_KEY", "")).strip()
    base_url = (payload.get("baseUrl") or env.get("BASE_URL", "")).strip()
    if not api_key:
        return {"error": "缺少 API Key：请点击右上角「接口设置」填写，或写入 .env 的 API_KEY"}
    if not base_url:
        return {"error": "缺少请求地址：请点击右上角「接口设置」填写，或写入 .env 的 BASE_URL"}

    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        return {"error": "提示词不能为空"}
    model = (payload.get("model") or env.get("MODEL") or "gpt-image-2").strip()
    try:
        n = max(1, min(8, int(payload.get("n", 1))))
    except (TypeError, ValueError):
        n = 1
    size = str(payload.get("size") or "auto").strip().lower().replace("×", "x").replace("*", "x")
    if size != "auto" and not re.fullmatch(r"\d{2,5}x\d{2,5}", size):
        return {"error": f"尺寸格式不正确: {size}（应为 宽x高，例如 1024x1024）"}
    quality = str(payload.get("quality") or "auto").strip()
    out_fmt = str(payload.get("format") or "png").strip().lower()
    if out_fmt not in FMT_EXT:
        out_fmt = "png"
    transparent = bool(payload.get("transparent")) and out_fmt in ("png", "webp")

    fields = {"model": model, "prompt": prompt}
    if size != "auto":
        fields["size"] = size
    if quality != "auto":
        fields["quality"] = quality
    if out_fmt != "png":
        fields["output_format"] = out_fmt
    if transparent:
        fields["background"] = "transparent"

    images_in = payload.get("images") or []
    if images_in:
        # 带参考图 → images/edits（multipart）
        files = []
        for i, durl in enumerate(images_in[:16]):
            try:
                raw, mime = parse_data_url(durl)
            except ValueError as e:
                return {"error": f"参考图 {i + 1} 无法解析: {e}"}
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
            field = "image" if len(images_in) == 1 else "image[]"
            files.append((field, f"ref{i + 1}.{ext}", mime, raw))
        mp_fields = dict(fields)
        mp_fields["n"] = str(n)
        body, ctype = multipart_body(mp_fields, files)
        candidates = candidate_urls(base_url, "images/edits")
        mode = "edit"
    else:
        # 纯文生图 → images/generations（JSON）
        jf = dict(fields)
        jf["n"] = n
        body, ctype = json.dumps(jf).encode("utf-8"), "application/json"
        candidates = candidate_urls(base_url, "images/generations")
        mode = "generate"

    t0 = time.time()
    data, err = call_upstream(candidates, api_key, body, ctype)
    if err:
        return err
    elapsed = round(time.time() - t0, 1)

    ctx = ssl.create_default_context()
    ext = FMT_EXT[out_fmt]
    rid = uuid.uuid4().hex[:8]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    images = []
    for i, item in enumerate(data.get("data", []) or []):
        fname = f"{stamp}-{rid}-{i + 1}.{ext}"
        fpath = os.path.join(OUTPUT_DIR, fname)
        if item.get("b64_json"):
            try:
                raw = base64.b64decode(item["b64_json"])
            except Exception:
                continue
            with open(fpath, "wb") as f:
                f.write(raw)
            images.append({"src": f"/outputs/{fname}", "file": fname})
        elif item.get("url"):
            # 上游返回图片链接时，下载保存一份到本地
            try:
                req = urllib.request.Request(item["url"], headers={"User-Agent": "curl/8"})
                with urllib.request.urlopen(req, timeout=300, context=ctx) as r:
                    raw = r.read()
                with open(fpath, "wb") as f:
                    f.write(raw)
                images.append({"src": f"/outputs/{fname}", "file": fname})
            except Exception:
                images.append({"src": item["url"], "file": None})

    if not images:
        return {"error": "上游没有返回图片", "detail": json.dumps(data, ensure_ascii=False)[:2000]}

    record = {
        "id": f"{stamp}-{rid}", "ts": time.time(), "prompt": prompt, "model": model,
        "size": size, "quality": quality, "n": n, "mode": mode, "format": out_fmt,
        "transparent": transparent, "elapsed": elapsed,
        "files": [im["file"] for im in images if im["file"]],
    }
    append_history(record)
    return {"images": images, "record": record}


def delete_image(payload: dict) -> dict:
    name = os.path.basename(str(payload.get("file") or ""))
    ext = os.path.splitext(name)[1].lower()
    path = os.path.join(OUTPUT_DIR, name)
    if not name or ext not in IMAGE_MIME or not os.path.isfile(path):
        return {"error": "文件不存在"}
    try:
        os.remove(path)
    except FileNotFoundError:
        pass  # 已被并发删除，视为成功
    except OSError as e:
        return {"error": f"删除失败: {e}"}
    return {"ok": True}


def test_connection(payload: dict) -> dict:
    env = load_env()
    api_key = (payload.get("apiKey") or env.get("API_KEY", "")).strip()
    base_url = (payload.get("baseUrl") or env.get("BASE_URL", "")).strip()
    if not api_key or not base_url:
        return {"ok": False, "msg": "请先填写 API Key 和请求地址"}
    ctx = ssl.create_default_context()
    for url in candidate_urls(base_url, "models"):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            ids = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
            image_models = [i for i in ids if "image" in i.lower()]
            return {"ok": True, "models": len(ids), "imageModels": image_models[:8]}
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"ok": False, "msg": f"API Key 无效或无权限（HTTP {e.code}）"}
            if e.code in (404, 405):
                continue
            return {"ok": False, "msg": f"上游返回 HTTP {e.code}"}
        except Exception as e:
            return {"ok": False, "msg": f"无法连接: {e}"}
    return {"ok": False, "soft": True,
            "msg": "接口没有 /models 端点，无法预检；Key 和地址可能仍然有效，直接生图试试"}


def get_config() -> dict:
    env = load_env()
    return {
        "envKey": bool(env.get("API_KEY")),
        "envUrl": env.get("BASE_URL", ""),
        "model": env.get("MODEL", "gpt-image-2"),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _send(self, code, content_type, body: bytes, extra_headers=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(ROOT, "index.html"), "rb") as f:
                self._send(200, "text/html; charset=utf-8", f.read())
        elif path == "/history":
            self._send_json({"records": load_history()})
        elif path == "/config":
            self._send_json(get_config())
        elif path == "/favicon.ico":
            self._send(204, "text/plain", b"")
        elif path.startswith("/outputs/"):
            # 先解码再取 basename，防止 ..%2F 之类的编码穿越
            name = os.path.basename(urllib.parse.unquote(path))
            ext = os.path.splitext(name)[1].lower()
            fpath = os.path.join(OUTPUT_DIR, name)
            if ext in IMAGE_MIME:
                try:
                    with open(fpath, "rb") as f:
                        data = f.read()
                except OSError:
                    self._send(404, "text/plain", b"not found")
                    return
                self._send(200, IMAGE_MIME[ext], data,
                           {"Cache-Control": "public, max-age=31536000, immutable"})
            else:
                self._send(404, "text/plain", b"not found")
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if length > 256 * 1024 * 1024:
            self._send_json({"error": "请求体过大"}, 413)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send_json({"error": "bad request"}, 400)
            return
        if self.path == "/generate":
            try:
                result = generate(payload)
            except Exception as e:
                result = {"error": f"服务内部错误: {e}"}
            self._send_json(result, 200 if "images" in result else 502)
        elif self.path == "/delete":
            try:
                self._send_json(delete_image(payload))
            except Exception as e:
                self._send_json({"error": f"服务内部错误: {e}"})
        elif self.path == "/test":
            try:
                self._send_json(test_connection(payload))
            except Exception as e:
                self._send_json({"ok": False, "msg": f"服务内部错误: {e}"})
        else:
            self._send(404, "text/plain", b"not found")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"✅ 服务已启动: http://127.0.0.1:{port}")
    print(f"   图片保存目录: {OUTPUT_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
