import sys
import json
import time
import queue
import threading
import webbrowser
import os
from pathlib import Path

# L'embeddable Python remplace sys.path via ._pth — le dossier du script n'est
# pas ajoute automatiquement. On le force ici pour trouver pipeline_runner etc.
sys.path.insert(0, str(Path(__file__).parent))

if getattr(sys, "frozen", False):
    BASE_DIR     = Path(sys.executable).parent
    TEMPLATE_DIR = BASE_DIR / "templates"
    from flask import Flask, render_template, request, Response, jsonify
    app = Flask(__name__, template_folder=str(TEMPLATE_DIR))
else:
    BASE_DIR = Path(__file__).parent
    from flask import Flask, render_template, request, Response, jsonify
    app = Flask(__name__)

from pipeline_runner import PipelineRunner

log_queue: queue.Queue = queue.Queue()
runner: PipelineRunner | None = None
_runner_thread: threading.Thread | None = None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    global runner, _runner_thread, log_queue
    data = request.get_json(silent=True) or {}
    pg_user     = data.get("pg_user", "")
    pg_password = data.get("pg_password", "")

    if not pg_user or not pg_password:
        return jsonify({"error": "Identifiants manquants"}), 400

    if runner and runner.running:
        return jsonify({"error": "Pipeline déjà en cours"}), 409

    # Vider la queue
    while not log_queue.empty():
        try:
            log_queue.get_nowait()
        except queue.Empty:
            break

    runner = PipelineRunner(pg_user, pg_password, log_queue)
    _runner_thread = threading.Thread(target=runner.run, daemon=True)
    _runner_thread.start()
    return jsonify({"ok": True})


@app.route("/stream")
def stream():
    def event_generator():
        keepalive_interval = 15
        last_ka = time.time()
        while True:
            try:
                msg = log_queue.get(timeout=1)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                if msg.get("type") == "done":
                    break
            except queue.Empty:
                # keep-alive SSE pour éviter la déconnexion
                if time.time() - last_ka >= keepalive_interval:
                    yield ": keep-alive\n\n"
                    last_ka = time.time()

    return Response(
        event_generator(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/resume", methods=["POST"])
def resume():
    global runner
    data     = request.get_json(silent=True) or {}
    pause_id = data.get("pause_id", "")
    if runner:
        runner.resume(pause_id)
        return jsonify({"ok": True})
    return jsonify({"error": "Aucun pipeline actif"}), 400


@app.route("/status")
def status():
    if runner is None:
        return jsonify({"state": "idle", "progress": 0, "steps": []})
    steps = [
        {"id": s.id, "label": s.label, "status": s.status.value}
        for s in runner.STEPS
    ]
    return jsonify({
        "state":    "running" if runner.running else ("done" if runner.success else "error"),
        "progress": runner.progress,
        "steps":    steps,
    })


@app.route("/report")
def report():
    from report_generator import generate
    output_dir = BASE_DIR / "Output"
    assets_dir = BASE_DIR / "assets"

    # Cherche le JSON courant
    json_files = sorted(output_dir.glob("*_Modele_clean.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not json_files:
        return jsonify({"error": "Aucun fichier *_Modele_clean.json dans Output/"}), 404

    try:
        html_path = generate(json_files[0], output_dir, assets_dir)
        html_content = html_path.read_text(encoding="utf-8")
        return jsonify({"ok": True, "html": html_content, "path": str(html_path)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/open-folder")
def open_folder():
    path = request.args.get("path", "")
    full = BASE_DIR / path
    if full.exists() and sys.platform == "win32":
        os.startfile(str(full))
    return jsonify({"ok": True})


@app.route("/shutdown", methods=["POST"])
def shutdown():
    def stop():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=stop, daemon=True).start()
    return jsonify({"ok": True})


def open_browser():
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
