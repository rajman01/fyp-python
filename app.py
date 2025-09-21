import os
from dotenv import load_dotenv

from topographic import TopographicPlan

load_dotenv()  # reads .env into environment

from cadastral import CadastralPlan

from flask import Flask, request, jsonify

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret"


@app.get("/")
def home():
    return "<h1>Hello, Flask 👋</h1><p>You're up and running!</p>"

@app.route("/cadastral/plan", methods=["POST"])
def generate_cadastral_plan():
    data = request.get_json()

    plan = CadastralPlan(**data)
    plan.draw()

    url = plan.save()
    return jsonify({"message": "Cadastral plan generated", "filename": plan.name, "url": url}), 200

@app.route("/topographic/plan", methods=["POST"])
def generate_topographic_plan():
    data = request.get_json()

    plan = TopographicPlan(**data)
    plan.draw()

    url = plan.save()
    return jsonify({"message": "Topographic plan generated", "filename": plan.name, "url": url}), 200


@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Resource not found"}), 404

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Something went wrong on our side"}), 500

@app.errorhandler(Exception)
def handle_exception(e):
    # You can log the exception here
    app.logger.error(f"Unhandled Exception: {e}", exc_info=True)

    # Return JSON response instead of crashing
    return jsonify({"error": "An unexpected error occurred"}), 500

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
