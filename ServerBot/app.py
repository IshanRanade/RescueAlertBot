from flask import Flask, render_template, request, redirect
import subprocess
import os
import signal
from threading import Thread

app = Flask(__name__)

BOT_PROCESS = None  # this will hold the subprocess.Popen object
BOT_THREAD = None   # this will hold the thread


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    global BOT_PROCESS, BOT_THREAD

    if BOT_PROCESS and BOT_PROCESS.poll() is None:
        return "Bot already running"

    email = request.form["email"]
    password = request.form["password"]
    otp = request.form["otp"]

    env = os.environ.copy()
    env["EMAIL"] = email
    env["PASSWORD"] = password
    env["OTP"] = otp

    def run_bot():
        global BOT_PROCESS
        BOT_PROCESS = subprocess.Popen(["python", "sevaro_bot.py"], env=env)
        BOT_PROCESS.wait()  # wait until it exits
        BOT_PROCESS = None  # reset after completion

    BOT_THREAD = Thread(target=run_bot, daemon=True)
    BOT_THREAD.start()

    return redirect("/")


@app.route("/stop", methods=["POST"])
def stop():
    global BOT_PROCESS, BOT_THREAD

    if BOT_PROCESS and BOT_PROCESS.poll() is None:
        BOT_PROCESS.terminate()  # sends SIGTERM
        BOT_PROCESS.wait()       # wait for it to exit
        BOT_PROCESS = None

    BOT_THREAD = None

    return redirect("/")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3267)
