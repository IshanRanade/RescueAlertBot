from flask import Flask, render_template, request, redirect, jsonify
import subprocess
import signal
import requests
import os
import sys
import time
from threading import Thread, Event, Lock

app = Flask(__name__)

BOT_PROCESS = None
BOT_LOCK = Lock()
TIMER_THREAD = None
TIMER_THREAD_LOCK = Lock()  # Separate lock for timer thread creation
TIMER_STOP_EVENT = Event()
TIME_LEFT = 0
TIME_LOCK = Lock()
TIMER_DURATION = 60 * 60  # 1 hour
WARNING_TIME = 5 * 60  # 5 minutes before expiry
WARNING_SENT = False


def send_telegram(msg):
    """Send a Telegram notification. Returns True on success, False on failure."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not token or not chat_id:
        print(f"Telegram disabled: {msg}", flush=True)
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
        if r.ok:
            print(f"📱 Telegram sent: {msg}", flush=True)
            return True
        else:
            print(f"Telegram failed ({r.status_code}): {r.text}", flush=True)
            return False
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)
        return False


# ---------------- HELPERS ---------------- #

def is_bot_running():
    """Check if bot process is currently running (must hold BOT_LOCK)."""
    return BOT_PROCESS is not None and BOT_PROCESS.poll() is None


def get_status_data():
    """Get current status and time remaining."""
    with TIME_LOCK:
        t = TIME_LEFT
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)

    with BOT_LOCK:
        status = "RUNNING" if is_bot_running() else "STOPPED"

    return {"status": status, "hours": h, "minutes": m, "seconds": s}


def reset_timer():
    """Reset timer to full duration."""
    global TIME_LEFT, WARNING_SENT
    with TIME_LOCK:
        TIME_LEFT = TIMER_DURATION
    WARNING_SENT = False
    TIMER_STOP_EVENT.clear()


def kill_bot_process():
    """Forcefully kill bot process and all children. Must hold BOT_LOCK."""
    if not is_bot_running():
        return

    try:
        # Kill entire process group (bot + browser children)
        os.killpg(BOT_PROCESS.pid, signal.SIGTERM)
        BOT_PROCESS.wait(timeout=5)
    except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
        pass

    # Force kill if still alive
    if is_bot_running():
        try:
            os.killpg(BOT_PROCESS.pid, signal.SIGKILL)
            BOT_PROCESS.wait(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
            pass


# ---------------- BOT ---------------- #

def start_bot_process(env):
    global BOT_PROCESS, TIME_LEFT, WARNING_SENT
    with BOT_LOCK:
        if is_bot_running():
            print("Bot already running.", flush=True)
            return
        reset_timer()
        WARNING_SENT = False
        BOT_PROCESS = subprocess.Popen(
            ["python", "-u", "sevaro_bot.py"],
            env=env,
            stdout=sys.stdout,
            stderr=sys.stderr,
            start_new_session=True,  # Create process group for clean kills
        )

    if not send_telegram("🟢 Bot started and watching for rescue cases."):
        print("❌ Failed to send start notification. Killing bot.", flush=True)
        with BOT_LOCK:
            kill_bot_process()
            BOT_PROCESS = None
        with TIME_LOCK:
            TIME_LEFT = 0
        return

    BOT_PROCESS.wait()
    with BOT_LOCK:
        BOT_PROCESS = None
    print("Bot stopped.", flush=True)

    send_telegram("🔴 Bot has stopped.")

    with TIME_LOCK:
        TIME_LEFT = 0


# ---------------- TIMER ---------------- #

def timer_loop():
    global TIME_LEFT, WARNING_SENT

    with TIME_LOCK:
        if TIME_LEFT <= 0:
            TIME_LEFT = TIMER_DURATION

    while True:
        time.sleep(1)

        if TIMER_STOP_EVENT.is_set():
            break

        with BOT_LOCK:
            proc = BOT_PROCESS

        if proc is None or proc.poll() is not None:
            with TIME_LOCK:
                TIME_LEFT = 0
            break

        with TIME_LOCK:
            if TIME_LEFT <= 0:
                break
            TIME_LEFT -= 1
            current_time = TIME_LEFT

        # Send 5-minute warning
        if current_time == WARNING_TIME and not WARNING_SENT:
            WARNING_SENT = True
            send_telegram("⚠️ Bot timer expires in 5 minutes! Refresh to extend.")

    with BOT_LOCK:
        if is_bot_running():
            print(f"Auto-stopping bot after {TIMER_DURATION} seconds.", flush=True)
            send_telegram("⏰ Bot timer expired. Stopping bot.")
            kill_bot_process()

    with TIME_LOCK:
        TIME_LEFT = 0


# ---------------- ROUTES ---------------- #

@app.route("/")
def index():
    data = get_status_data()
    return render_template("index.html", **data)


@app.route("/start", methods=["POST"])
def start():
    print("Starting bot...", flush=True)

    env = os.environ.copy()
    env["EMAIL"] = request.form["email"]
    env["PASSWORD"] = request.form["password"]
    env["OTP"] = request.form["otp"]
    env["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    env["TELEGRAM_CHAT_ID"] = os.environ.get("TELEGRAM_CHAT_ID", "")

    Thread(target=start_bot_process, args=(env,), daemon=True).start()

    # Protected timer thread creation to prevent duplicates
    with TIMER_THREAD_LOCK:
        global TIMER_THREAD
        if TIMER_THREAD is None or not TIMER_THREAD.is_alive():
            TIMER_THREAD = Thread(target=timer_loop, daemon=True)
            TIMER_THREAD.start()

    return redirect("/")


@app.route("/stop", methods=["POST"])
def stop():
    global TIME_LEFT
    TIMER_STOP_EVENT.set()

    with BOT_LOCK:
        kill_bot_process()

    with TIME_LOCK:
        TIME_LEFT = 0

    return redirect("/")


@app.route("/refresh_timer", methods=["POST"])
def refresh_timer():
    with BOT_LOCK:
        if is_bot_running():
            reset_timer()
    return redirect("/")


@app.route("/status")
def status():
    return jsonify(get_status_data())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3267)
