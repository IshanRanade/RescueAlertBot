from flask import Flask, render_template, request, redirect
import subprocess
import os
import signal

app = Flask(__name__)

BOT_PROCESS = None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    global BOT_PROCESS

    if BOT_PROCESS:
        return "Bot already running"

    email = request.form["email"]
    password = request.form["password"]
    otp = request.form["otp"]

    env = os.environ.copy()
    env["EMAIL"] = email
    env["PASSWORD"] = password
    env["OTP"] = otp

    BOT_PROCESS = subprocess.Popen(
        ["bash", "run_bot.sh"],
        env=env,
    )

    return redirect("/")


@app.route("/stop", methods=["POST"])
def stop():
    global BOT_PROCESS

    if BOT_PROCESS:
        os.kill(BOT_PROCESS.pid, signal.SIGTERM)
        BOT_PROCESS = None

    return redirect("/")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3267)
