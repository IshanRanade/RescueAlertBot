from flask import Flask, render_template, request, redirect, jsonify
import subprocess
import os
import sys
import time
from threading import Thread, Event, Lock

app = Flask(__name__)

# ---------------- STATUS STRINGS ---------------- #
STATUS_RUNNING = "RUNNING"
STATUS_STOPPED = "STOPPED"

BOT_PROCESS = None
BOT_LOCK = Lock()  # Lock to prevent multiple starts
TIMER_THREAD = None
TIMER_STOP_EVENT = Event()
TIME_LEFT = 0
TIME_LOCK = Lock()
TIMER_DURATION = 3 * 60 * 60  # 3 hours


# ---------------- BOT ---------------- #

def start_bot_process(env):
    global BOT_PROCESS
    with BOT_LOCK:
        if BOT_PROCESS and BOT_PROCESS.poll() is None:
            print("Bot already running (inside start_bot_process).", flush=True)
            return

        # Reset timer automatically when bot starts
        with TIME_LOCK:
            global TIME_LEFT
            TIME_LEFT = TIMER_DURATION
        TIMER_STOP_EVENT.clear()

        BOT_PROCESS = subprocess.Popen(
            ["python", "-u", "sevaro_bot.py"],  # -u = unbuffered logs
            env=env,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )

    BOT_PROCESS.wait()
    with BOT_LOCK:
        BOT_PROCESS = None
    print("Bot stopped.", flush=True)

    # Set timer to 0 immediately when bot dies/crashes
    with TIME_LOCK:
        TIME_LEFT = 0


# ---------------- TIMER ---------------- #

def timer_loop():
    global TIME_LEFT

    # Initialize timer if not already set
    with TIME_LOCK:
        if TIME_LEFT <= 0:
            TIME_LEFT = TIMER_DURATION

    while True:
        time.sleep(1)

        if TIMER_STOP_EVENT.is_set():
            break

        with BOT_LOCK:
            proc = BOT_PROCESS

        # Stop timer if bot is not running
        if proc is None or proc.poll() is not None:
            with TIME_LOCK:
                TIME_LEFT = 0
            break

        with TIME_LOCK:
            if TIME_LEFT <= 0:
                break
            TIME_LEFT -= 1

    # Auto stop when time ends
    with BOT_LOCK:
        if BOT_PROCESS and BOT_PROCESS.poll() is None:
            print(f"Auto-stopping bot after {TIMER_DURATION} seconds.", flush=True)
            BOT_PROCESS.terminate()
            BOT_PROCESS.wait(timeout=10)

    with TIME_LOCK:
        TIME_LEFT = 0


# ---------------- ROUTES ---------------- #

@app.route("/")
def index():
    with TIME_LOCK:
        t = TIME_LEFT
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)

    with BOT_LOCK:
        status = STATUS_RUNNING if BOT_PROCESS and BOT_PROCESS.poll() is None else STATUS_STOPPED

    return render_template("index.html", status=status, hours=h, minutes=m, seconds=s)


@app.route("/start", methods=["POST"])
def start():
    print("Starting bot...", flush=True)

    global TIMER_THREAD

    env = os.environ.copy()
    env["EMAIL"] = request.form["email"]
    env["PASSWORD"] = request.form["password"]
    env["OTP"] = request.form["otp"]

    # Start bot thread safely
    Thread(target=start_bot_process, args=(env,), daemon=True).start()

    # Start timer thread if not already running
    if TIMER_THREAD is None or not TIMER_THREAD.is_alive():
        TIMER_THREAD = Thread(target=timer_loop, daemon=True)
        TIMER_THREAD.start()

    return redirect("/")


@app.route("/stop", methods=["POST"])
def stop():
    TIMER_STOP_EVENT.set()

    with BOT_LOCK:
        if BOT_PROCESS and BOT_PROCESS.poll() is None:
            BOT_PROCESS.terminate()
            BOT_PROCESS.wait(timeout=10)

    # Set timer to 0 when bot is stopped
    with TIME_LOCK:
        global TIME_LEFT
        TIME_LEFT = 0

    return redirect("/")


@app.route("/refresh_timer", methods=["POST"])
def refresh_timer():
    with BOT_LOCK:
        bot_running = BOT_PROCESS is not None and BOT_PROCESS.poll() is None

    if bot_running:
        with TIME_LOCK:
            global TIME_LEFT
            TIME_LEFT = TIMER_DURATION
        TIMER_STOP_EVENT.clear()

    return redirect("/")


@app.route("/status")
def status():
    with TIME_LOCK:
        t = TIME_LEFT
    h, r = divmod(t, 3600)
    m, s = divmod(r, 60)

    with BOT_LOCK:
        status = STATUS_RUNNING if BOT_PROCESS and BOT_PROCESS.poll() is None else STATUS_STOPPED

    return jsonify(status=status, hours=h, minutes=m, seconds=s)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3267)
