from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import yt_dlp
import os, uuid, threading, time, glob

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

tasks = {}
tasks_lock = threading.Lock()

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def fmt_duration(secs):
    if not secs:
        return "Unknown"
    secs = int(secs)
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_ydl_opts(cookies=None, cookie_format="header"):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "http_headers": {"User-Agent": USER_AGENT},
        "extractor_args": {"youtube": {"skip": ["dash", "hls"]}},
    }
    if cookies and cookies.strip():
        if cookie_format == "netscape":
            cpath = os.path.join(DOWNLOAD_DIR, f"ck_{uuid.uuid4().hex}.txt")
            with open(cpath, "w") as f:
                f.write(cookies)
            opts["cookiefile"] = cpath
        else:
            opts["http_headers"]["Cookie"] = cookies
    return opts


def find_downloaded_file(base_path):
    for ext in ["mp4", "mp3", "m4a", "webm", "mkv", "opus", "ogg"]:
        p = f"{base_path}.{ext}"
        if os.path.exists(p):
            return p
    matches = glob.glob(f"{base_path}*")
    return matches[0] if matches else None


def cleanup_old_files():
    now = time.time()
    for f in os.listdir(DOWNLOAD_DIR):
        fp = os.path.join(DOWNLOAD_DIR, f)
        try:
            if os.path.isfile(fp) and now - os.path.getmtime(fp) > 1800:
                os.remove(fp)
        except Exception:
            pass


@app.route("/")
def index():
    return jsonify({"status": "running", "message": "YVD Backend chal raha hai!", "yt_dlp": yt_dlp.version.__version__})


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "yt_dlp": yt_dlp.version.__version__})


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    cookies = data.get("cookies", "")
    cookie_fmt = data.get("cookie_format", "header")

    if not url:
        return jsonify({"success": False, "error": "URL nahi diya"}), 400

    try:
        opts = build_ydl_opts(cookies, cookie_fmt)
        opts["skip_download"] = True

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return jsonify({"success": False, "error": "Info nahi mili"}), 404

        seen, qualities = set(), []
        for fmt in info.get("formats", []):
            h = fmt.get("height")
            if h and fmt.get("vcodec", "none") != "none":
                lbl = f"{h}p"
                if lbl not in seen:
                    seen.add(lbl)
                    qualities.append((h, lbl))
        qualities.sort(reverse=True)
        quality_labels = [q[1] for q in qualities[:8]]
        for q in ["1080p", "720p", "480p", "360p"]:
            if q not in quality_labels:
                quality_labels.append(q)

        thumb = info.get("thumbnail", "")
        if not thumb and info.get("thumbnails"):
            thumb = max(info["thumbnails"], key=lambda t: t.get("width", 0) * t.get("height", 0), default={}).get("url", "")

        return jsonify({
            "success": True,
            "data": {
                "title": info.get("title", "Unknown"),
                "duration": fmt_duration(info.get("duration")),
                "channel": info.get("uploader") or info.get("channel", "Unknown"),
                "thumbnail": thumb,
                "video_id": info.get("id", uuid.uuid4().hex[:8]),
                "qualities": quality_labels,
            }
        })

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Sign in" in msg or "login" in msg.lower():
            return jsonify({"success": False, "error": "Age-restricted. Settings mein cookies daalo!"}), 403
        if "Private" in msg:
            return jsonify({"success": False, "error": "Private video. Cookies set karo."}), 403
        return jsonify({"success": False, "error": msg[:300]}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)[:300]}), 500


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    fmt = data.get("format", "video")
    quality = data.get("quality", "720p")
    cookies = data.get("cookies", "")
    cookie_fmt = data.get("cookie_format", "header")

    if not url:
        return jsonify({"success": False, "error": "URL nahi diya"}), 400

    task_id = uuid.uuid4().hex
    out_base = os.path.join(DOWNLOAD_DIR, task_id)
    q_num = quality.replace("p", "") if quality else "720"

    with tasks_lock:
        tasks[task_id] = {"status": "starting", "percent": 0, "file": None, "error": None}

    t = threading.Thread(target=_do_download, args=(task_id, url, fmt, q_num, cookies, cookie_fmt, out_base), daemon=True)
    t.start()

    for _ in range(360):
        time.sleep(1)
        with tasks_lock:
            task = tasks.get(task_id, {})
        if task.get("status") == "complete":
            return jsonify({"success": True, "download_url": f"/api/file/{task_id}"})
        if task.get("status") == "error":
            return jsonify({"success": False, "error": task.get("error", "Download fail")}), 500

    return jsonify({"success": False, "error": "Timeout"}), 408


def _do_download(task_id, url, fmt, q_num, cookies, cookie_fmt, out_base):
    def hook(d):
        if d["status"] == "downloading":
            try:
                pct = float(d.get("_percent_str", "0%").strip().replace("%", ""))
            except Exception:
                pct = 0
            with tasks_lock:
                if task_id in tasks:
                    tasks[task_id].update({"status": "downloading", "percent": int(pct)})

    try:
        opts = build_ydl_opts(cookies, cookie_fmt)
        opts["progress_hooks"] = [hook]
        opts["outtmpl"] = out_base + ".%(ext)s"

        if fmt == "mp3":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        elif fmt == "audio":
            opts["format"] = "bestaudio/best"
        else:
            opts["format"] = "bestvideo+bestaudio/best"
            opts["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        file_path = find_downloaded_file(out_base)
        if file_path:
            with tasks_lock:
                tasks[task_id].update({"status": "complete", "percent": 100, "file": file_path})
        else:
            raise FileNotFoundError("File nahi mili")

    except Exception as e:
        with tasks_lock:
            if task_id in tasks:
                tasks[task_id].update({"status": "error", "error": str(e)[:300]})


@app.route("/api/file/<task_id>")
def serve_file(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task or not task.get("file"):
        return jsonify({"error": "File nahi mili"}), 404
    fp = task["file"]
    if not os.path.exists(fp):
        return jsonify({"error": "File nahi hai"}), 404
    return send_file(fp, as_attachment=True, download_name=os.path.basename(fp))


@app.route("/api/status/<task_id>")
def get_status(task_id):
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return jsonify({"status": "not_found"}), 404
    resp = {"status": task["status"], "percent": task.get("percent", 0)}
    if task["status"] == "complete":
        resp["file_url"] = f"/api/file/{task_id}"
    elif task["status"] == "error":
        resp["error"] = task.get("error", "Unknown")
    return jsonify(resp)


threading.Thread(target=lambda: [time.sleep(1800) or cleanup_old_files() for _ in iter(int, 1)], daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
