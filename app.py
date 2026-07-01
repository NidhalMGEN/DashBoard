"""Point d'entrée de la plateforme modulaire KPI_MGEN_V3.

Factory Flask : crée l'application, enregistre automatiquement tous les
blueprints déclarés dans `modules/_registry.py`, expose la liste des modules
aux templates (sidebar) et ouvre le navigateur sur la page d'accueil.

Lancement portable (Python embeddable, sans droits admin) :
    python\\python.exe app.py
"""

from __future__ import annotations

import os
import sys
import time
import threading
import webbrowser
import importlib
from pathlib import Path

# L'embeddable Python remplace sys.path via ._pth — le dossier du script n'est
# pas ajouté automatiquement. On le force pour trouver le paquet `modules`.
sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, redirect, url_for, render_template, jsonify

from modules._registry import active_modules, default_module


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


BASE_DIR = _base_dir()


def create_app() -> Flask:
    """Construit l'application Flask et enregistre les modules actifs."""
    app = Flask(
        __name__,
        template_folder=str(BASE_DIR / "templates"),
        static_folder=str(BASE_DIR / "static"),
    )
    app.config["BASE_DIR"] = BASE_DIR
    app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(32))

    # Liste des modules + statut credentials disponibles dans TOUS les
    # templates (sidebar + bandeau topbar).
    @app.context_processor
    def inject_navigation():
        try:
            from config.credentials_loader import CREDENTIALS
            cred_status = CREDENTIALS.status()
        except Exception:
            cred_status = {"file_present": False, "loaded": False,
                           "sections_total": 0, "sections_complete": 0}
        return {
            "nav_modules": active_modules(),
            "app_title": "KPI MGEN V3",
            "cred_status": cred_status,
        }

    @app.route("/admin/reload-credentials", methods=["POST", "GET"])
    def reload_credentials():
        from config.credentials_loader import CREDENTIALS
        return jsonify({"ok": True, "status": CREDENTIALS.reload()})

    # Enregistrement automatique des blueprints depuis le registre.
    for spec in active_modules():
        module = importlib.import_module(spec.import_path)
        blueprint = getattr(module, spec.blueprint)
        # On attache la spec au blueprint pour que les routes y accèdent.
        blueprint.module_spec = spec
        app.register_blueprint(blueprint)

    # Route racine -> module par défaut (dashboard).
    @app.route("/")
    def root():
        spec = default_module()
        if spec is None:
            return "Aucun module actif.", 503
        return redirect(url_for(f"{spec.key}.index"))

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "modules": [m.key for m in active_modules()]})

    # Démarre le planificateur (thread de fond) — sauf en mode test pour ne
    # pas laisser de thread persistant pendant les tests unitaires.
    if not app.testing:
        try:
            from core import scheduler
            scheduler.start(BASE_DIR)
        except Exception:
            pass

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404,
                               message="Page introuvable"), 404

    @app.errorhandler(500)
    def server_error(_e):
        return render_template("error.html", code=500,
                               message="Erreur interne du serveur"), 500

    return app


app = create_app()


def open_browser():
    time.sleep(1.2)
    try:
        webbrowser.open("http://127.0.0.1:5000")
    except Exception:
        pass


if __name__ == "__main__":
    if "--no-browser" not in sys.argv:
        threading.Thread(target=open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
