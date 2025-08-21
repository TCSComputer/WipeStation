#!/usr/bin/env python3
import json
import queue
import threading
import time
import subprocess
import re
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request
import pyudev

app = Flask(__name__)

# ---- SAFETY: NEVER touch the boot disk(s) ----
PROTECTED_DISKS = {"sda"}  # expand later if needed

# Known non-physical devices to ignore
IGNORE_PREFIXES = ("loop", "md", "dm-", "zram", "sr", "ram")

# In-memory state
state_lock = threading.Lock()
disks = {}   # key: "sdb", value: dict with details
events_broker = None  # initialized below

DEDUP_WINDOW_SEC = 2.0
last_events = {}  # name -> {"type": str, "ts": float}

# Wipe job tracking
job_lock = threading.Lock()
jobs = {}  # key: disk name -> job dict

def run_cmd_json(cmd):
    out = subprocess.check_output(cmd, text=True)
    return json.loads(out)


def scan_disks():
    result = {}
    try:
        # OLD (bad): ["lsblk", "-J", "-O", "-o", "..."]
        data = run_cmd_json([
            "lsblk", "-J",
            "-o", "NAME,TYPE,SIZE,MODEL,SERIAL,VENDOR,WWN,TRAN,STATE"
        ])
        for blk in data.get("blockdevices", []):
            if blk.get("type") != "disk":
                continue
            name = blk.get("name", "")
            if not name or name in PROTECTED_DISKS:
                continue
            if name.startswith(IGNORE_PREFIXES):
                continue

            info = {
                "name": name,
                "path": f"/dev/{name}",
                "size": blk.get("size"),
                "model": (blk.get("model") or "").strip(),
                "serial": (blk.get("serial") or "").strip(),
                "vendor": (blk.get("vendor") or "").strip(),
                "wwn": (blk.get("wwn") or "").strip(),
                "tran": (blk.get("tran") or "").strip(),    # sata/usb/etc
                "state": (blk.get("state") or "").strip(),  # may be empty on some devices
                "protected": name in PROTECTED_DISKS,
            }
            result[name] = info
    except Exception as e:
        print(f"[scan_disks] Error: {e}")
    return result



class EventBroker:
    """
    Simple SSE broadcaster. Each client gets its own Queue.
    """
    def __init__(self):
        self.clients = []
        self.lock = threading.Lock()

    def register(self):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
        return q

    def unregister(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def publish(self, event):
        # event is a dict that will be JSON-serialized
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

def should_publish(name: str, action: str) -> bool:
    """
    Return False if we saw the same action for this disk within DEDUP_WINDOW_SEC.
    """
    now = time.time()
    le = last_events.get(name)
    if le and le["type"] == action and (now - le["ts"]) < DEDUP_WINDOW_SEC:
        return False
    last_events[name] = {"type": action, "ts": now}
    return True

def is_rotational(name: str) -> bool:
    try:
        with open(f"/sys/block/{name}/queue/rotational", "r") as f:
            return f.read().strip() == "1"
    except Exception:
        return True  # assume HDD if unknown

def device_size_bytes(devpath: str) -> int:
    # Use blockdev --getsize64 for exact bytes
    try:
        out = subprocess.check_output(["blockdev", "--getsize64", devpath], text=True).strip()
        return int(out)
    except Exception:
        return 0

def start_wipe_job(name: str, level: str):
    if name in PROTECTED_DISKS:
        raise RuntimeError("Refusing to wipe a protected disk")
    dev = f"/dev/{name}"
    rot = is_rotational(name)
    size = device_size_bytes(dev)

    job = {
        "disk": name,
        "device": dev,
        "level": level,
        "rotational": rot,
        "size": size,
        "started": time.time(),
        "bytes": 0,
        "percent": 0.0,
        "status": "running",
        "log": [],
        "pid": None,
        "method": None,
    }

    def publish():
        events_broker.publish({"type": "job", "job": {k: (int(v) if isinstance(v, bool) else v) for k, v in job.items()}, "ts": time.time()})

    def log(msg):
        job["log"].append(msg)
        publish()

    def set_progress(done_bytes):
        job["bytes"] = done_bytes
        if job["size"] > 0:
            job["percent"] = max(0.0, min(100.0, (done_bytes / job["size"]) * 100))
        publish()

    def worker():
        try:
            # --- Select method by media type & level ---
            if rot:
                # HDD
                if level == "low":
                    job["method"] = "dd zero (1 pass)"
                    cmd = ["dd", f"if=/dev/zero", f"of={dev}", "bs=16M", "oflag=direct", "status=progress"]
                    rc = stream_cmd(cmd, set_progress, log)
                elif level == "med":
                    job["method"] = "dd zero (1) + dd random (1)"
                    # zeros
                    rc = stream_cmd(["dd", "if=/dev/zero", f"of={dev}", "bs=16M", "oflag=direct", "status=progress"], set_progress, log)
                    if rc != 0: raise RuntimeError("zero pass failed")
                    # random
                    rc = stream_cmd(["dd", "if=/dev/urandom", f"of={dev}", "bs=4M", "oflag=direct", "status=progress"], set_progress, log)
                else:
                    job["method"] = "shred -v -n 7 -z (DoD-like)"
                    cmd = ["shred", "-v", "-n", "7", "-z", dev]
                    rc = stream_cmd(cmd, set_progress, log)
            else:
                # SSD
                if level == "low":
                    job["method"] = "blkdiscard (full device TRIM)"
                    log("Running blkdiscard (no incremental progress)")
                    rc = subprocess.call(["blkdiscard", dev])
                    if rc == 0:
                        set_progress(size)
                elif level == "med":
                    job["method"] = "blkdiscard + dd zero (1)"
                    log("blkdiscard (phase 1)")
                    rc = subprocess.call(["blkdiscard", dev])
                    if rc != 0: raise RuntimeError("blkdiscard failed")
                    log("dd zero (phase 2)")
                    rc = stream_cmd(["dd", "if=/dev/zero", f"of={dev}", "bs=16M", "oflag=direct", "status=progress"], set_progress, log)
                else:
                    # Try ATA Secure Erase; fall back if unsupported
                    job["method"] = "hdparm secure-erase (enhanced if avail) or fallback to blkdiscard"
                    log("Checking drive security state...")
                    try:
                        sec = subprocess.check_output(["hdparm", "-I", dev], text=True, stderr=subprocess.STDOUT)
                    except Exception as e:
                        sec = str(e)
                    frozen = "not\tenabled" not in sec and "frozen" in sec.lower()
                    enhanced = "Enhanced erase" in sec

                    if "supported" in sec and "enabled" in sec:
                        # If security already enabled with unknown password, we cannot proceed safely.
                        log("Security feature set is already ENABLED; cannot set temp password safely. Falling back.")
                        rc = 1
                    else:
                        # Try to set a temp password and erase
                        try:
                            subprocess.check_call(["hdparm", "--user-master", "u", "--security-set-pass", "p3lt3ch", dev])
                            if enhanced:
                                log("Running hdparm --security-erase-enhanced ... (no incremental progress)")
                                rc = subprocess.call(["hdparm", "--user-master", "u", "--security-erase-enhanced", "p3lt3ch", dev])
                            else:
                                log("Running hdparm --security-erase ... (no incremental progress)")
                                rc = subprocess.call(["hdparm", "--user-master", "u", "--security-erase", "p3lt3ch", dev])
                            # Clear password if needed
                            try:
                                subprocess.call(["hdparm", "--user-master", "u", "--security-disable", "p3lt3ch", dev])
                            except Exception:
                                pass
                            if rc == 0:
                                set_progress(size)
                        except subprocess.CalledProcessError as e:
                            log(f"hdparm secure erase failed: {e}")
                            rc = 1

                    if rc != 0:
                        log("Falling back to blkdiscard")
                        rc = subprocess.call(["blkdiscard", dev])
                        if rc == 0:
                            set_progress(size)

            if rc == 0:
                job["status"] = "done"
                set_progress(size if size else job["bytes"])
            else:
                job["status"] = "error"
                publish()
        except Exception as ex:
            job["status"] = f"error: {ex}"
            publish()

    with job_lock:
        if name in jobs and jobs[name]["status"] == "running":
            raise RuntimeError("A wipe is already running for this disk")
        jobs[name] = job

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    publish()
    return job


def stream_cmd(cmd, progress_cb, line_cb=None):
    """
    Run a command and stream stderr for progress parsing.
    dd writes progress to stderr with 'status=progress'.
    shred -v writes bytes/pass info to stderr.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    total_bytes = None
    # Read stderr live
    for line in proc.stderr:
        if line_cb:
            line_cb(line)
        # Try to catch dd status lines like: '123456789 bytes (123 MB, ...) copied'
        m = re.search(r'(\d+)\s+bytes', line)
        if m:
            try:
                done = int(m.group(1))
                progress_cb(done)
            except Exception:
                pass
    proc.wait()
    return proc.returncode


def udev_monitor_thread():
    """
    Watches for block device add/remove/change and emits de-duplicated events.
    """
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by("block")

    for device in iter(monitor.poll, None):
        try:
            action = device.action  # 'add', 'remove', 'change'
            devtype = device.get("DEVTYPE")  # 'disk', 'partition'
            name = device.sys_name  # e.g. 'sdb'

            # Only whole disks
            if devtype != "disk":
                continue
            # Never consider protected/non-physical
            if name in PROTECTED_DISKS or name.startswith(IGNORE_PREFIXES):
                continue

            # Debounce: skip rapid duplicates of the same action
            if not should_publish(name, action):
                continue

            if action == "add":
                # If we already know this disk, suppress duplicate add
                with state_lock:
                    already = name in disks
                if already:
                    # treat as a change/update instead
                    snapshot = scan_disks()
                    with state_lock:
                        if name in snapshot:
                            disks[name] = snapshot[name]
                            events_broker.publish({"type": "change", "disk": disks[name], "ts": time.time()})
                    continue

                snapshot = scan_disks()
                with state_lock:
                    if name in snapshot:
                        disks[name] = snapshot[name]
                        events_broker.publish({"type": "add", "disk": disks[name], "ts": time.time()})

            elif action == "change":
                # Sometimes only 'change' fires on first init; treat as add if unknown
                snapshot = scan_disks()
                with state_lock:
                    if name not in snapshot:
                        # nothing to do
                        continue
                    first_time = name not in disks
                    disks[name] = snapshot[name]
                    events_broker.publish({
                        "type": "add" if first_time else "change",
                        "disk": disks[name],
                        "ts": time.time()
                    })

            elif action == "remove":
                with state_lock:
                    if name in disks:
                        removed = disks.pop(name)
                        events_broker.publish({"type": "remove", "disk": removed, "ts": time.time()})
                # clear last-event tracking so a new insert isn't throttled
                last_events.pop(name, None)

        except Exception as e:
            print(f"[udev_monitor_thread] Error: {e}")



@app.route("/")
def index():
    return render_template("index.html", protected=list(PROTECTED_DISKS))


@app.route("/api/disks")
def api_disks():
    with state_lock:
        current = list(disks.values())
    return jsonify({"disks": current, "protected": list(PROTECTED_DISKS)})


@app.route("/events")
def sse():
    def stream():
        q = events_broker.register()
        try:
            # Send initial full snapshot to client
            with state_lock:
                snapshot = list(disks.values())
            init = {"type": "snapshot", "disks": snapshot, "ts": time.time()}
            yield f"data: {json.dumps(init)}\n\n"

            while True:
                event = q.get()
                yield f"data: {json.dumps(event)}\n\n"
        except GeneratorExit:
            pass
        finally:
            events_broker.unregister(q)

    return Response(stream(), mimetype="text/event-stream")


@app.route("/api/wipe/<name>", methods=["POST"])
def api_wipe(name):
    level = request.args.get("level", "low").lower()
    if level not in ("low", "med", "high"):
        return jsonify({"error": "level must be one of: low, med, high"}), 400
    if name in PROTECTED_DISKS:
        return jsonify({"error": "protected disk"}), 400
    try:
        job = start_wipe_job(name, level)
        return jsonify({"job": job})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/jobs")
def api_jobs():
    with job_lock:
        return jsonify({"jobs": list(jobs.values())})

@app.route("/events/jobs")
def sse_jobs():
    def stream():
        q = events_broker.register()
        try:
            # send initial
            with job_lock:
                snapshot = list(jobs.values())
            yield f"data: {json.dumps({'type': 'jobs_snapshot', 'jobs': snapshot, 'ts': time.time()})}\n\n"
            while True:
                evt = q.get()
                if evt.get("type") in ("job",):
                    yield f"data: {json.dumps(evt)}\n\n"
        except GeneratorExit:
            pass
        finally:
            events_broker.unregister(q)
    return Response(stream(), mimetype="text/event-stream")

def bootstrap_initial_state():
    with state_lock:
        disks.clear()
        disks.update(scan_disks())


def main():
    global events_broker
    events_broker = EventBroker()

    # Initial disk inventory
    bootstrap_initial_state()

    # prevent immediate throttling on startup snapshot
    for _name in list(disks.keys()):
        last_events.pop(_name, None)

    # Start udev watcher
    t = threading.Thread(target=udev_monitor_thread, daemon=True)
    t.start()

    # Run Flask
    # For development: plain HTTP on 0.0.0.0:8080
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)


if __name__ == "__main__":
    main()
