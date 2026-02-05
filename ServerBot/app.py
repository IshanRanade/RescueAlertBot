from flask import Flask, render_template, request, redirect, jsonify
import subprocess
import os
import time
from threading import Thread, Event, Lock

app = Flask(__name__)

BOT_PROCESS = None
BOT_THREAD = None
TIMER_THREAD = None
TIMER_STOP_EVENT = Event()
TIME_LEFT = 0
TIME_LOCK = Lock()
TIMER_DURATION = 3 * 60 * 60  # 3 hours in seconds


def run_bot_with_logging(env):
    global BOT_PROCESS
    BOT_PROCESS = subprocess.Popen(
        ["python", "sevaro_bot.py"],
        env=env,
        stdout=sys.stdout,
        stderr=sys.stderr,
        bufsize=1,
        universal_newlines=True
    )
    BOT_PROCESS.wait()
    BOT_PROCESS = None
    print("Bot has stopped.", flush=True)


def timer_thread():
    global TIME_LEFT, BOT_PROCESS
    with TIME_LOCK:
        TIME_LEFT = TIMER_DURATION

    while True:
        time.sleep(1)

        with TIME_LOCK:
            if TIMER_STOP_EVENT.is_set() or TIME_LEFT <= 0:
                TIME_LEFT = 0
                break
            TIME_LEFT -= 1

        # If bot crashed, stop timer
        if BOT_PROCESS is None or (BOT_PROCESS and BOT_PROCESS.poll() is not None):
            with TIME_LOCK:
                TIME_LEFT = 0
            break

    # Auto-stop the bot if time runs out
    if BOT_PROCESS and BOT_PROCESS.poll() is None:
        print("Auto-stopping bot after 3 hours.")
        BOT_PROCESS.terminate()
        BOT_PROCESS.wait()

    with TIME_LOCK:
        TIME_LEFT = 0


@app.route("/")
def index():
    with TIME_LOCK:
        t = TIME_LEFT
    hours, remainder = divmod(t, 3600)
    minutes, seconds = divmod(remainder, 60)
    status = "running" if BOT_PROCESS and BOT_PROCESS.poll() is None else "stopped"
    return render_template("index.html", status=status,
                           hours=hours, minutes=minutes, seconds=seconds)


@app.route("/start", methods=["POST"])
def start():
    global BOT_THREAD, TIMER_THREAD, TIMER_STOP_EVENT

    if BOT_PROCESS and BOT_PROCESS.poll() is None:
        return "Bot already running"

    email = request.form["email"]
    password = request.form["password"]
    otp = request.form["otp"]

    env = os.environ.copy()
    env["EMAIL"] = email
    env["PASSWORD"] = password
    env["OTP"] = otp

    TIMER_STOP_EVENT.clear()

    # Start bot thread
    BOT_THREAD = Thread(target=run_bot_with_logging, args=(env,), daemon=True)
    BOT_THREAD.start()

    # Start timer thread
    TIMER_THREAD = Thread(target=timer_thread, daemon=True)
    TIMER_THREAD.start()

    return redirect("/")


@app.route("/stop", methods=["POST"])
def stop():
    global TIMER_STOP_EVENT
    TIMER_STOP_EVENT.set()

    if BOT_PROCESS and BOT_PROCESS.poll() is None:
        BOT_PROCESS.terminate()
        BOT_PROCESS.wait()

    return redirect("/")


@app.route("/refresh_timer", methods=["POST"])
def refresh_timer():
    global TIME_LEFT, TIMER_STOP_EVENT
    if BOT_PROCESS and BOT_PROCESS.poll() is None:
        with TIME_LOCK:
            TIME_LEFT = TIMER_DURATION
        TIMER_STOP_EVENT.clear()
    return redirect("/")


@app.route("/status")
def status():
    with TIME_LOCK:
        t = TIME_LEFT
    hours, remainder = divmod(t, 3600)
    minutes, seconds = divmod(remainder, 60)
    status = "running" if BOT_PROCESS and BOT_PROCESS.poll() is None else "stopped"
    return jsonify({
        "status": status,
        "hours": hours,
        "minutes": minutes,
        "seconds": seconds
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3267)
