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

DEDUP_WINDOW_SEC = 2.0
last_events = {}  # name -> {"type": str, "ts": float}

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
