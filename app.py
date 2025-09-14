from flask import Flask

app = Flask(__name__)
app.config["SECRET_KEY"] = "secret"


@app.get("/")
def home():
    return "<h1>Hello, Flask 👋</h1><p>You're up and running!</p>"

@app.route("/plans", methods=["POST"])
def auto_plan():
    return ""

if __name__ == '__main__':
    app.run()
