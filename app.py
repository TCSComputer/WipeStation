#!/usr/bin/env python3
# TCS Wipe Station - Flask backend
# - Disk hotplug detection w/ pyudev
# - SSE event streams for disks and jobs
# - Wipe job orchestration via root-only helper (/usr/local/bin/wipectl)

import os
import re
import json
import uuid
import time
import queue
import signal
import threading
import subprocess
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request
import pyudev

# ------------------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------------------

app = Flask(__name__)

# SAFETY: Never allow wiping protected disks
PROTECTED_DISKS = {"sda"}  # expand as needed, e.g., {"sda","nvme0n1"}

# Ignore these "disks" (non-physical, virtual, or partitions only)
IGNORE_PREFIXES = ("loop", "md", "dm-", "zram", "sr", "ram")

# Event dedup window for udev (seconds)
DEDUP_WINDOW_SEC = 2.0

# In-memory state
state_lock = threading.Lock()
disks = {}       # key: "sdb", value: dict with details
last_events = {} # name -> {"type": str, "ts": float}

# Wipe jobs
job_lock = threading.Lock()
jobs = {}        # job_id -> job dict
disk_running = {} # disk name -> job_id (to prevent concurrent wipes on same disk)

# Global event broker for SSE
events_broker = None


# ------------------------------------------------------------------------------
# Utilities
# ------------------------------------------------------------------------------

def run_cmd_json(cmd):
    out = subprocess.check_output(cmd, text=True)
    return json.loads(out)


def enrich_serial_from_udev(name: str, current_serial: str) -> str:
    """
    Try to pull ID_SERIAL[_SHORT] from udev if lsblk serial is empty.
    """
    if current_serial:
        return current_serial
    try:
        ctx = pyudev.Context()
        dev = ctx.device_from_device_file(f"/dev/{name}")
        for key in ("ID_SERIAL_SHORT", "ID_SERIAL"):
            val = dev.properties.get(key, "").strip()
            if val:
                return val
    except Exception:
        pass
    return ""


def scan_disks():
    """
    Return a dict of current disks keyed by device name (e.g., 'sdb'),
    using lsblk in bytes mode and enriching serial from udev.
    """
    result = {}
    try:
        data = run_cmd_json([
            "lsblk", "-J", "-b",  # bytes for numeric size
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
                "size": blk.get("size") or 0,  # bytes (int or str convertible)
                "model": (blk.get("model") or "").strip(),
                "serial": (blk.get("serial") or "").strip(),
                "vendor": (blk.get("vendor") or "").strip(),
                "wwn": (blk.get("wwn") or "").strip(),
                "tran": (blk.get("tran") or "").strip(),   # sata/usb/etc
                "state": (blk.get("state") or "").strip(), # may be empty
                "protected": name in PROTECTED_DISKS,
            }
            # ensure integer size
            try:
                info["size"] = int(info["size"])
            except Exception:
                info["size"] = 0

            # Enrich serial from udev if empty
            info["serial"] = enrich_serial_from_udev(name, info["serial"])
            result[name] = info
    except Exception as e:
        print(f"[scan_disks] Error: {e}")
    return result


def is_rotational(name: str) -> bool:
    try:
        with open(f"/sys/block/{name}/queue/rotational", "r") as f:
            return f.read().strip() == "1"
    except Exception:
        # default to HDD if unknown
        return True


def device_size_bytes(devpath: str) -> int:
    """
    Return device size in bytes with multiple fallbacks.
    """
    # 1) lsblk -nb
    try:
        out = subprocess.check_output(
            ["lsblk", "-nb", "-o", "SIZE", devpath],
            text=True
        ).strip()
        if out.isdigit():
            return int(out)
    except Exception:
        pass

    # 2) blockdev (try multiple locations)
    for bd in ("blockdev", "/sbin/blockdev", "/usr/sbin/blockdev"):
        try:
            out = subprocess.check_output([bd, "--getsize64", devpath], text=True).strip()
            if out.isdigit():
                return int(out)
        except Exception:
            continue

    # 3) sysfs: sectors * logical_block_size
    try:
        name = os.path.basename(devpath)
        with open(f"/sys/block/{name}/size", "r") as f:
            sectors = int(f.read().strip())
        lbs = 512
        try:
            with open(f"/sys/block/{name}/queue/logical_block_size", "r") as f:
                lbs = int(f.read().strip())
        except Exception:
            pass
        return sectors * lbs
    except Exception:
        return 0


def should_publish(name: str, action: str) -> bool:
    """
    Deduplicate rapid identical udev events per disk.
    """
    now = time.time()
    le = last_events.get(name)
    if le and le["type"] == action and (now - le["ts"]) < DEDUP_WINDOW_SEC:
        return False
    last_events[name] = {"type": action, "ts": now}
    return True


class EventBroker:
    """
    SSE broadcaster. Each client gets its own Queue.
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

    def publish(self, event):   # <-- needs self, event
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass



# ------------------------------------------------------------------------------
# Hot-plug monitoring (udev)
# ------------------------------------------------------------------------------

def udev_monitor_thread():
    """
    Watch block device add/remove/change, update state, and publish de-duped events.
    """
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by("block")

    for device in iter(monitor.poll, None):
        try:
            action = device.action        # 'add', 'remove', 'change'
            devtype = device.get("DEVTYPE")  # 'disk', 'partition'
            name = device.sys_name        # e.g., 'sdb'

            # Only whole disks
            if devtype != "disk":
                continue
            # Never consider protected/non-physical
            if name in PROTECTED_DISKS or name.startswith(IGNORE_PREFIXES):
                continue

            # debounce
            if not should_publish(name, action):
                continue

            if action == "add":
                snapshot = scan_disks()
                with state_lock:
                    if name not in disks and name in snapshot:
                        disks[name] = snapshot[name]
                        events_broker.publish({"type": "add", "disk": disks[name], "ts": time.time()})

            elif action == "change":
                snapshot = scan_disks()
                with state_lock:
                    if name in snapshot:
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
                last_events.pop(name, None)

        except Exception as e:
            print(f"[udev_monitor_thread] Error: {e}")


# ------------------------------------------------------------------------------
# Flask routes - pages & APIs
# ------------------------------------------------------------------------------

@app.route("/")
def index():
    # If you have a Jinja template (templates/index.html), render it;
    # otherwise send a minimal HTML so the service stays functional.
    try:
        return render_template("index.html", protected=list(PROTECTED_DISKS))
    except Exception:
        return (
            "<!doctype html><title>Wipe Station</title>"
            "<h1>Wipe Station API</h1>"
            "<p>Use /api/disks, /api/wipe/&lt;sdX&gt;?level=low|med|high, /events, /events/jobs</p>",
            200,
            {"Content-Type": "text/html"},
        )


@app.route("/api/disks")
def api_disks():
    with state_lock:
        current = list(disks.values())
    return jsonify({"disks": current, "protected": list(PROTECTED_DISKS)})


@app.route("/events")
def sse_disks():
    def stream():
        q = events_broker.register()
        try:
            # initial snapshot
            with state_lock:
                snapshot = list(disks.values())
            yield f"data: {json.dumps({'type':'snapshot','disks':snapshot,'ts':time.time()})}\n\n"
            while True:
                event = q.get()
                # forward disk events only
                if event.get("type") in ("snapshot", "add", "change", "remove"):
                    yield f"data: {json.dumps(event)}\n\n"
        except GeneratorExit:
            pass
        finally:
            events_broker.unregister(q)
    return Response(stream(), mimetype="text/event-stream")


# ------------------------------------------------------------------------------
# Wipe job engine
# ------------------------------------------------------------------------------

def stream_cmd(cmd, progress_cb, line_cb=None):
    """
    Run a command and stream stderr lines; parse '... bytes' to update progress.
    Return process exit code.
    """
    # Launch in its own process group so we can cancel later if needed
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        preexec_fn=os.setsid
    )
    # track pid via the callback (caller may stash it in job dict separately)
    for line in proc.stderr:
        line = line.rstrip("\n")
        if line_cb:
            line_cb(line)
        # Parse '123456 bytes ...' from dd/shred
        m = re.search(r'(\d+)\s+bytes', line)
        if m:
            try:
                done = int(m.group(1))
                progress_cb(done)
            except Exception:
                pass
    proc.wait()
    return proc.returncode


@app.route("/api/wipe/<name>", methods=["POST"])
def api_wipe(name):
    # Validate device name strictly: sd[a-z]
    if not re.fullmatch(r"sd[a-z]", name):
        return jsonify({"error": "invalid device name"}), 400
    if name in PROTECTED_DISKS:
        return jsonify({"error": "protected disk"}), 400

    level = request.args.get("level", "low").lower()
    if level not in ("low", "med", "high"):
        return jsonify({"error": "level must be one of: low, med, high"}), 400

    # prevent concurrent wipe on same disk
    with job_lock:
        if name in disk_running:
            jid = disk_running[name]
            job = jobs.get(jid)
            return jsonify({"error": f"disk {name} already has running job {jid}", "job": job}), 400

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
            # initial snapshot
            with job_lock:
                snapshot = list(jobs.values())
            yield f"data: {json.dumps({'type':'jobs_snapshot','jobs':snapshot,'ts':time.time()})}\n\n"
            while True:
                evt = q.get()
                if evt.get("type") in ("job",):
                    yield f"data: {json.dumps(evt)}\n\n"
        except GeneratorExit:
            pass
        finally:
            events_broker.unregister(q)
    return Response(stream(), mimetype="text/event-stream")


def start_wipe_job(name: str, level: str):
    if name in PROTECTED_DISKS:
        raise RuntimeError("Refusing to wipe a protected disk")

    dev = f"/dev/{name}"
    rot = is_rotational(name)
    size = device_size_bytes(dev)  # in bytes

    # Snapshot model/serial at job start (helps in UI modals and logs)
    with state_lock:
        disk_meta = disks.get(name, {})
    model = disk_meta.get("model") or ""
    serial = disk_meta.get("serial") or ""
    tran = disk_meta.get("tran") or ""

    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
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
        # helpful for UI
        "model": model,
        "serial": serial,
        "bus": tran,
        # runtime metrics
        "mbps": 0.0,
        "eta_sec": None,
    }

    # ---- helpers to notify UI & console ----
    def publish():
        job_view = dict(job)
        job_view["last_log"] = job["log"][-1] if job["log"] else ""
        events_broker.publish({"type": "job", "job": job_view, "ts": time.time()})

    def logline(msg: str):
        print(f"[JOB {name}] {msg}", flush=True)
        job["log"].append(msg)
        publish()

    def set_progress(done_bytes: int):
        job["bytes"] = done_bytes
        if job["size"] > 0:
            job["percent"] = max(0.0, min(100.0, (done_bytes / job["size"]) * 100))
        # speed + eta
        elapsed = max(1e-6, time.time() - job["started"])
        job["mbps"] = (job["bytes"] / elapsed) / (1024 * 1024)
        if job["size"] > 0 and job["bytes"] > 0:
            remaining = max(0, job["size"] - job["bytes"])
            rate_bps = job["mbps"] * 1024 * 1024
            if rate_bps > 0:
                job["eta_sec"] = int(remaining / rate_bps)
        else:
            job["eta_sec"] = None
        # console debug
        print(f"[JOB {name}] progress {job['percent']:.1f}% ({job['bytes']}/{job['size']} bytes)", flush=True)
        publish()

    # ---- the worker thread that runs the wipe ----
    def worker():
        try:
            # Choose method per media type & level (via root-only helper)
            if rot:
                # ---------------- HDD ----------------
                if level == "low":
                    job["method"] = "dd zero (1 pass) via wipectl"
                    cmd = ["sudo", "-n", "/usr/local/bin/wipectl", "hdd-zero", dev]
                    rc = stream_cmd(cmd, set_progress, logline)
                    # dd may exit non-zero at end-of-device; treat as success if >= 99.9%
                    if rc != 0 and job["bytes"] and job["size"] and job["bytes"] >= job["size"] * 0.999:
                        logline("dd ended with ENOSPC at end-of-device; treating as success")
                        rc = 0

                elif level == "med":
                    job["method"] = "zero + random (2 passes) via wipectl"
                    # zero pass
                    rc = stream_cmd(["sudo", "-n", "/usr/local/bin/wipectl", "hdd-zero", dev], set_progress, logline)
                    if rc != 0 and job["bytes"] and job["size"] and job["bytes"] >= job["size"] * 0.999:
                        logline("dd ended with ENOSPC at end-of-device; treating zero pass as success")
                        rc = 0
                    if rc != 0:
                        raise RuntimeError("zero pass failed")
                    # random pass
                    rc = stream_cmd(["sudo", "-n", "/usr/local/bin/wipectl", "hdd-random", dev], set_progress, logline)

                else:
                    job["method"] = "DoD 7-pass via wipectl (shred)"
                    cmd = ["sudo", "-n", "/usr/local/bin/wipectl", "hdd-dod", dev]
                    rc = stream_cmd(cmd, set_progress, logline)

            else:
                # ---------------- SSD ----------------
                if level == "low":
                    job["method"] = "blkdiscard via wipectl"
                    logline("Running blkdiscard (no incremental progress)")
                    rc = subprocess.call(["sudo", "-n", "/usr/local/bin/wipectl", "ssd-discard", dev])
                    if rc == 0:
                        set_progress(size)

                elif level == "med":
                    job["method"] = "blkdiscard + dd zero via wipectl"
                    # helper does discard then dd zero; dd progress parsed
                    rc = stream_cmd(["sudo", "-n", "/usr/local/bin/wipectl", "ssd-discard-zero", dev], set_progress, logline)
                    if rc != 0 and job["bytes"] and job["size"] and job["bytes"] >= job["size"] * 0.999:
                        logline("dd ended with ENOSPC at end-of-device; treating as success")
                        rc = 0

                else:
                    job["method"] = "secure erase via wipectl (fallback blkdiscard)"
                    logline("Attempting ATA Secure Erase; will fall back if unsupported/frozen.")
                    rc = subprocess.call(["sudo", "-n", "/usr/local/bin/wipectl", "ssd-secure-erase", dev])
                    if rc == 0:
                        set_progress(size)

            # finalize status
            if rc == 0:
                job["status"] = "done"
                # ensure bar reaches 100% for UI
                if job["size"] and job["bytes"] < job["size"]:
                    set_progress(job["size"])
                publish()
            else:
                job["status"] = "error"
                publish()

        except Exception as ex:
            job["status"] = f"error: {ex}"
            publish()
        finally:
            # persist log line for audit (JSONL monthly file)
            try:
                logdir = Path("/var/log/TCS-wiper")
                logdir.mkdir(parents=True, exist_ok=True)
                job_copy = dict(job)
                job_copy["finished"] = time.time()
                with open(logdir / f"jobs-{time.strftime('%Y-%m')}.log", "a") as f:
                    f.write(json.dumps(job_copy) + "\n")
            except Exception as e:
                print(f"[JOB {name}] failed to write audit log: {e}", flush=True)
            # mark disk as free
            with job_lock:
                if disk_running.get(name) == job_id:
                    disk_running.pop(name, None)

    # register and start
    with job_lock:
        if name in disk_running:
            raise RuntimeError(f"disk {name} already has a running job")
        jobs[job_id] = job
        disk_running[name] = job_id

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    publish()
    return job


# ------------------------------------------------------------------------------
# App bootstrap
# ------------------------------------------------------------------------------

def bootstrap_initial_state():
    with state_lock:
        disks.clear()
        disks.update(scan_disks())


def main():
    global events_broker
    events_broker = EventBroker()

    # Initial snapshot
    bootstrap_initial_state()
    # Prevent immediate throttling on startup
    for _name in list(disks.keys()):
        last_events.pop(_name, None)

    # Start udev watcher
    t = threading.Thread(target=udev_monitor_thread, daemon=True)
    t.start()

    # Run Flask (development server; use systemd + gunicorn/uwsgi for prod)
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)


if __name__ == "__main__":
    main()