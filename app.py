#!/usr/bin/env python3
import json
import queue
import threading
import time
import subprocess
from pathlib import Path

from flask import Flask, Response, jsonify, render_template
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


def udev_monitor_thread():
    """
    Watches for block device add/remove using pyudev and updates state.
    """
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by("block")
    for device in iter(monitor.poll, None):
        try:
            action = device.action  # 'add', 'remove', 'change'
            devtype = device.get("DEVTYPE")  # 'disk', 'partition'
            name = device.sys_name  # e.g. 'sdb' or 'sdb1'

            # We only care about whole disks, not partitions
            if devtype != "disk":
                continue
            # Ignore protected and non-physical names
            if name in PROTECTED_DISKS or name.startswith(IGNORE_PREFIXES):
                continue

            if action == "add":
                # Refresh details for this disk
                new_snapshot = scan_disks()
                with state_lock:
                    # Merge single disk (if present) into state
                    if name in new_snapshot:
                        disks[name] = new_snapshot[name]
                        evt = {"type": "add", "disk": disks[name], "ts": time.time()}
                        events_broker.publish(evt)

            elif action == "remove":
                with state_lock:
                    if name in disks:
                        removed = disks.pop(name)
                        evt = {"type": "remove", "disk": removed, "ts": time.time()}
                        events_broker.publish(evt)

            elif action == "change":
                # Re-scan this disk’s info
                snapshot = scan_disks()
                with state_lock:
                    if name in snapshot:
                        disks[name] = snapshot[name]
                        evt = {"type": "change", "disk": disks[name], "ts": time.time()}
                        events_broker.publish(evt)
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


def bootstrap_initial_state():
    with state_lock:
        disks.clear()
        disks.update(scan_disks())


def main():
    global events_broker
    events_broker = EventBroker()

    # Initial disk inventory
    bootstrap_initial_state()

    # Start udev watcher
    t = threading.Thread(target=udev_monitor_thread, daemon=True)
    t.start()

    # Run Flask
    # For development: plain HTTP on 0.0.0.0:8080
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)


if __name__ == "__main__":
    main()
