# 🔒 TCSComputer Disk Wipe Utility

## Overview

This project provides a **web-based secure disk wiping tool** designed for IT shops, refurbishers, and technicians who need a **repeatable, auditable, and safe way to sanitize storage devices**.  
It combines a **Flask-based web UI** (for usability and progress tracking) with a minimal **root-only helper (`wipectl`)** (for safety and control).

The design goal is to protect technicians and end-users from dangerous mistakes (like wiping the system boot disk) while still providing maximum flexibility in handling both **HDDs** and **SSDs** with methods appropriate to each medium.

---

## ✨ Key Features

- **Web UI for Wipes**  
  Initiate disk wipes from a browser with simple buttons (Low, Medium, High).

- **Live Progress Tracking**  
  Output from `dd`, `shred`, or `hdparm` is streamed in real time so you can see % complete, GB written, and estimated time.

- **Device Safety Guardrails**  
  - Refuses to wipe system disks (`/dev/sda` by default).  
  - Validates device paths.  
  - Prevents wiping mounted partitions.  
  - Distinguishes HDD vs SSD for method selection.  

- **Multiple Wipe Levels**  
  - **Low:** 1-pass zero fill (fastest, suitable for quick reuse).  
  - **Medium:** 1-pass random fill.  
  - **High:** DoD-style 7-pass overwrite or ATA Secure Erase (SSD).  

- **SSD-Aware Operations**  
  - Full-device TRIM/discard (`blkdiscard`).  
  - Secure Erase via `hdparm` if supported.  
  - Fallback to TRIM+zero if enhanced erase is unavailable.  

- **Least-Privilege Design**  
  - Web process runs as an unprivileged user.  
  - Only the whitelisted helper (`/usr/local/bin/wipectl`) runs with root rights via `sudo`.  
  - Helper has minimal, auditable logic with no arbitrary command execution.  

- **Extensible Architecture**  
  - Easy to add more wipe profiles in the future.  
  - Logs can be extended to generate **wipe certificates** (per device, per date).  

---

## 🚀 Quick Start

1. **Install requirements**  
   - Python 3 + Flask  
   - Linux with `dd`, `shred`, `blkdiscard`, `hdparm`  

2. **Set up the root helper**  
   See [Root-Only Wipe Helper](#root-only-wipe-helper-usrlocalbinwipectl).

3. **Run the web app**  
   ```bash
   flask run --host=0.0.0.0 --port=5000
   ```

4. **Open in browser**  
   Visit `http://<server-ip>:5000` and select a disk + wipe level.

---

## 🔮 Future Directions

This project is intentionally modular. Future improvements may include:

- **Audit Logging**  
  JSON or CSV log of wipe history (serial number, method, duration, operator).  

- **Certificate Generator**  
  Automatically generate printable PDF certificates for client records.  

- **Multi-Drive Parallel Wipes**  
  Queue or parallelize jobs for batch sanitization of many disks.  

- **Role-Based Access Control**  
  Web authentication so only authorized technicians can initiate wipes.  

- **API Mode**  
  REST endpoints for integration into larger IT shop workflows or automation scripts.  

---

## ⚠️ Disclaimer

This tool is destructive by design.  
Always double-check target devices before starting a wipe.  
The authors assume **no liability** for data loss resulting from misuse.

---


## Root-Only Wipe Helper (`/usr/local/bin/wipectl`)

**Why this exists:**  
The web app runs as an unprivileged user and must never hold raw write access to block devices. Destructive operations (zeroing, random fills, Secure Erase, TRIM/discard) are delegated to a tiny, tightly-scoped **root helper** invoked via `sudo`. This keeps the web process low-privilege while still enabling wipes, and gives us a single, auditable choke-point with strong guardrails.

**What it does (safeguards):**
- Refuses **protected devices** (defaults to `sda`, the boot disk).
- Verifies the target looks like a whole SATA disk (`/dev/sdX`) and **is a block device**.
- Ensures the disk and its partitions are **not mounted**.
- Performs only a short, whitelisted set of subcommands (no arbitrary shell).
- Preserves **progress output on stderr** so the web UI can display live %/bytes.

### Install the helper

```bash
sudo tee /usr/local/bin/wipectl >/dev/null <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

# ---- CONFIG ----
PROTECTED=("sda")      # NEVER allow wiping these (add more if needed)
DD_BS_ZERO="16M"       # dd zero block size
DD_BS_RAND="4M"        # dd random block size

usage() {
  cat >&2 <<USAGE
Usage:
  wipectl hdd-zero /dev/sdX          # 1-pass zeros (HDD)
  wipectl hdd-random /dev/sdX        # 1-pass random (HDD)
  wipectl hdd-dod /dev/sdX           # DoD-like: shred -v -n 7 -z (HDD)
  wipectl ssd-discard /dev/sdX       # Full-device TRIM (SSD)
  wipectl ssd-discard-zero /dev/sdX  # TRIM then 1-pass zeros (SSD)
  wipectl ssd-secure-erase /dev/sdX  # ATA Secure Erase (fallback: TRIM)

Notes:
  - Must be run as root (via sudo).
  - /dev/sdX must exist and NOT be mounted.
  - Refuses protected devices: ${PROTECTED[*]}
USAGE
  exit 2
}

die() { echo "ERROR: $*" >&2; exit 1; }

need_root() { [ "$(id -u)" -eq 0 ] || die "must run as root"; }

check_dev() {
  local dev="$1"
  [[ "$dev" =~ ^/dev/sd[a-z]$ ]] || die "invalid device: $dev (must be whole disk like /dev/sdb)"
  [ -b "$dev" ] || die "not a block device: $dev"
  local base; base="$(basename "$dev")"
  for p in "${PROTECTED[@]}"; do
    [ "$base" = "$p" ] && die "device $dev is protected"
  done
  # refuse if mounted anywhere
  if lsblk -nro MOUNTPOINT "$dev" | grep -q .; then
    die "device $dev has mountpoints; unmount partitions first"
  fi
  # also check child partitions
  if lsblk -nr "$dev" | awk '{print $1}' | grep -qE '^sd[a-z][0-9]+'; then
    while read -r part; do
      mp="$(lsblk -nro MOUNTPOINT "/dev/$part" || true)"
      [ -n "$mp" ] && die "partition /dev/$part is mounted at $mp; unmount first"
    done < <(lsblk -nr "$dev" | awk '{print $1}')
  fi
}

is_rotational() {
  local dev="$1"; local base; base="$(basename "$dev")"
  [ "$(cat /sys/block/"$base"/queue/rotational 2>/dev/null || echo 1)" -eq 1 ]
}

cmd="${1:-}"; dev="${2:-}"

[ -z "${cmd}" ] && usage
[ -z "${dev}" ] && usage

need_root
check_dev "$dev"

case "$cmd" in
  hdd-zero)
    is_rotational "$dev" || echo "Note: target reports SSD; proceeding per request." >&2
    exec dd if=/dev/zero "of=$dev" bs="$DD_BS_ZERO" oflag=direct status=progress
    ;;

  hdd-random)
    is_rotational "$dev" || echo "Note: target reports SSD; proceeding per request." >&2
    exec dd if=/dev/urandom "of=$dev" bs="$DD_BS_RAND" oflag=direct status=progress
    ;;

  hdd-dod)
    is_rotational "$dev" || echo "Note: target reports SSD; proceeding per request." >&2
    exec shred -v -n 7 -z "$dev"
    ;;

  ssd-discard)
    ! is_rotational "$dev" || echo "Note: target reports HDD; proceeding per request." >&2
    exec blkdiscard "$dev"
    ;;

  ssd-discard-zero)
    ! is_rotational "$dev" || echo "Note: target reports HDD; proceeding per request." >&2
    blkdiscard "$dev"
    exec dd if=/dev/zero "of=$dev" bs="$DD_BS_ZERO" oflag=direct status=progress
    ;;

  ssd-secure-erase)
    ! is_rotational "$dev" || echo "Note: target reports HDD; proceeding per request." >&2
    PW="p3lt3ch"
    if hdparm -I "$dev" 2>&1 | grep -q "Security:\|supported"; then
      if hdparm --user-master u --security-set-pass "$PW" "$dev" >/dev/null 2>&1; then
        if hdparm -I "$dev" | grep -q "Enhanced erase"; then
          echo "Running hdparm --security-erase-enhanced ..." >&2
          hdparm --user-master u --security-erase-enhanced "$PW" "$dev"
        else
          echo "Running hdparm --security-erase ..." >&2
          hdparm --user-master u --security-erase "$PW" "$dev"
        fi
        hdparm --user-master u --security-disable "$PW" "$dev" >/dev/null 2>&1 || true
        exit 0
      else
        echo "security-set-pass failed (frozen/enabled?) — falling back to blkdiscard" >&2
      fi
    else
      echo "secure erase unsupported — falling back to blkdiscard" >&2
    fi
    exec blkdiscard "$dev"
    ;;

  *)
    usage
    ;;
esac
EOF

sudo chmod 0755 /usr/local/bin/wipectl
```

### Allow the web user to run it via `sudo` (no password)

> Replace `tcstech` below if your web process runs as a different user.

```bash
# Create a sudoers snippet (validated with visudo)
echo 'Cmnd_Alias WIPECTL=/usr/local/bin/wipectl *' | sudo tee /etc/sudoers.d/wipectl >/dev/null
echo 'tcstech ALL=(root) NOPASSWD: WIPECTL' | sudo tee -a /etc/sudoers.d/wipectl >/dev/null
sudo chmod 0440 /etc/sudoers.d/wipectl
sudo visudo -cf /etc/sudoers.d/wipectl
```

If `visudo` reports **parsed OK**, you’re set.

### How the web app uses it

The Flask backend never calls `dd`, `shred`, `blkdiscard`, or `hdparm` directly.  
Instead it executes one of:

```
sudo -n /usr/local/bin/wipectl hdd-zero /dev/sdX
sudo -n /usr/local/bin/wipectl hdd-random /dev/sdX
sudo -n /usr/local/bin/wipectl hdd-dod /dev/sdX
sudo -n /usr/local/bin/wipectl ssd-discard /dev/sdX
sudo -n /usr/local/bin/wipectl ssd-discard-zero /dev/sdX
sudo -n /usr/local/bin/wipectl ssd-secure-erase /dev/sdX
```

The helper prints progress to **stderr** (e.g., `dd … status=progress`, `shred -v`), which the app parses to update the on-screen progress bar.

### Security notes

- Keep the **whitelist** tight. Do not add generic shells/flags.
- Extend the `PROTECTED=(…)` array if this host has additional system disks.
- The sudoers entry allows only `/usr/local/bin/wipectl` with arguments; it does **not** grant general root access.
- Consider running the web app as a dedicated user (e.g., `wipeweb`) and scoping the sudoers rule to that user instead of a human account.
