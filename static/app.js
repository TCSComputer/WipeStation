// TCS Wipe Station Frontend

const tableBody   = document.querySelector("#drives tbody");
const protectedEl = document.querySelector("#protected");
const eventsEl    = document.querySelector("#events");

const modalEl     = document.getElementById("job-finished");
const jfTitleEl   = document.getElementById("jf-title");
const jfBodyEl    = document.getElementById("jf-body");
const jfCloseEl   = document.getElementById("jf-close");
const jfPrintEl   = document.getElementById("jf-print");

// Track running jobs by disk to disable buttons & avoid multi-start
const runningByDisk = new Map();

function bytesToGB(n) {
  if (n == null || isNaN(n)) return "";
  return (n / 1e9).toFixed(1) + " GB";
}

function fmtDiskRow(d) {
  const prot = d.protected ? '<span class="badge yes">YES</span>' : '<span class="badge no">NO</span>';
  const disabled = d.protected ? 'disabled' : '';
  const last4 = (d.serial || "").slice(-4);
  // If a job is running for this disk, disable buttons
  const jobRunning = runningByDisk.get(d.name) === true ? "disabled" : "";

  return `
    <tr id="row-${d.name}">
      <td><code>${d.path}</code></td>
      <td>${bytesToGB(d.size)}</td>
      <td>${d.model || ""}</td>
      <td>${d.serial || "(unknown)"}${last4 ? ` <span class="badge serial4">${last4}</span>` : ""}</td>
      <td>${d.tran || ""}</td>
      <td>${d.state || ""}</td>
      <td>${prot}</td>
      <td class="actions">
        <div class="actions-stack">
          <div>
            <button class="btn act" data-disk="${d.name}" data-level="low"  ${disabled} ${jobRunning}>Low</button>
            <button class="btn act" data-disk="${d.name}" data-level="med"  ${disabled} ${jobRunning}>Med</button>
            <button class="btn act" data-disk="${d.name}" data-level="high" ${disabled} ${jobRunning}>High</button>
          </div>
          <div class="progress"><div class="bar" id="bar-${d.name}" style="width:0%"></div></div>
          <div class="small" id="meta-${d.name}"></div>
        </div>
      </td>
    </tr>`;
}

async function startWipe(name, level) {
  const res = await fetch(`/api/wipe/${name}?level=${level}`, { method: "POST" });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }
  // mark as running to disable action buttons
  runningByDisk.set(name, true);
  updateRowDisabled(name, true);
}

function updateRowDisabled(disk, on) {
  const row = document.getElementById(`row-${disk}`);
  if (!row) return;
  row.querySelectorAll(".btn.act").forEach(btn => btn.disabled = !!on);
}

document.addEventListener("click", async (e) => {
  const b = e.target.closest(".act");
  if (!b) return;
  const disk  = b.dataset.disk;
  const level = b.dataset.level;

  // Pull last 4 of serial from the row for safer confirmation
  const serialCell = document.getElementById(`row-${disk}`)?.children?.[3]?.textContent || "";
  const m = serialCell.match(/(\w{4})\s*$/);
  const last4 = m ? m[1] : "";

  let promptMsg = `CONFIRM WIPE (${level.toUpperCase()}) on /dev/${disk}\n\nType the LAST 4 of the serial to proceed`;
  if (last4) promptMsg += ` [${last4}]`;
  const typed = window.prompt(promptMsg, "");
  if (!typed || (last4 && typed.trim() !== last4)) { alert("Canceled."); return; }

  await startWipe(disk, level);
});

function initJobSSE() {
  const es = new EventSource("/events/jobs");
  es.onmessage = (msg) => {
    const evt = JSON.parse(msg.data);
    if (evt.type === "jobs_snapshot") {
      evt.jobs.forEach(updateJobUI);
    } else if (evt.type === "job") {
      updateJobUI(evt.job);
    }
  };
  es.onerror = () => setTimeout(initJobSSE, 1500);
}

function renderMeta(job) {
  const pct    = (job.percent || 0).toFixed(1) + "%";
  const doneGB = job.bytes ? (job.bytes / 1e9).toFixed(2) + " GB" : "…";
  const sizeGB = job.size ? (job.size / 1e9).toFixed(1) + " GB" : "unknown";
  const spd    = job.mbps ? `${job.mbps.toFixed(1)} MB/s` : "–";
  const eta    = (job.eta_sec != null) ? `${Math.max(0, Math.floor(job.eta_sec/60))}m` : "–";
  const state  = (job.status || "").toString();
  const stateClass = state.startsWith("error") ? "error" : (state === "done" ? "done" : "running");

  // Suppress noisy '... bytes copied ...' lines in the tail
  const noisy = /(\d+)\s+bytes.*copied|records in|records out/i;
  const last  = job.last_log && !noisy.test(job.last_log) ? job.last_log : "";

  return `
    <div class="meta-block">
      <div class="meta-line"><strong>[${(job.level||"?").toUpperCase()} • ${job.method || "…"}]</strong></div>
      <div class="meta-line">${doneGB} / ${sizeGB} — ${pct}</div>
      <div class="meta-line">
        <span class="badge state ${stateClass}">${state}</span>
        ${spd !== "–" ? ` • ${spd}` : ""}${eta !== "–" ? ` • ETA ${eta}` : ""}
      </div>
      ${last ? `<div class="meta-line small">${last}</div>` : ""}
    </div>
  `;
}

function updateJobUI(job) {
  const bar  = document.getElementById(`bar-${job.disk}`);
  const meta = document.getElementById(`meta-${job.disk}`);
  const row  = document.getElementById(`row-${job.disk}`);
  if (!bar || !meta || !row) return;

  const pctNum = (job.percent || 0);
  bar.style.width = `${pctNum.toFixed(1)}%`;
  meta.innerHTML = renderMeta(job);

  // Row height/disabled buttons while running
  if (job.status === "running") {
    runningByDisk.set(job.disk, true);
    updateRowDisabled(job.disk, true);
    row.classList.add("row-job");
  } else if (job.status === "done" || String(job.status).startsWith("error")) {
    runningByDisk.set(job.disk, false);
    updateRowDisabled(job.disk, false);
    row.classList.remove("row-job");
    showFinished(job);
  }
}

function showFinished(job) {
  if (!modalEl) return;
  jfTitleEl.textContent = (job.status === "done") ? "Wipe Completed" : "Wipe Failed";
  const sizeGB = job.size ? (job.size / 1e9).toFixed(2) + " GB" : "unknown";
  const mbps   = job.mbps ? job.mbps.toFixed(1) + " MB/s" : "-";
  const finishedAt = new Date().toLocaleString();
  jfBodyEl.textContent =
`Device: ${job.device}
Model: ${job.model || "-"}
Serial: ${job.serial || "-"}
Level: ${job.level?.toUpperCase() || "-"}
Method: ${job.method || "-"}
Size: ${sizeGB}
Avg speed: ${mbps}
Finished: ${finishedAt}
Result: ${job.status}
Last log: ${job.last_log || ""}`;
  modalEl.classList.remove("hidden");

  jfCloseEl.onclick = () => modalEl.classList.add("hidden");
  // Temporary print: this prints the page. Later: call a backend /api/jobs/<id>/print for a proper CUPS printout.
  jfPrintEl.onclick = () => window.print();
}

function renderSnapshot(disks, protectedList) {
  protectedEl.textContent = protectedList.join(", ") || "(none)";
  tableBody.innerHTML = "";
  disks.forEach(d => tableBody.insertAdjacentHTML("beforeend", fmtDiskRow(d)));
}

function upsertRow(d) {
  const id = `row-${d.name}`;
  const existing = document.getElementById(id);
  if (existing) {
    existing.outerHTML = fmtDiskRow(d);
    document.getElementById(id).classList.add("row-add");
    setTimeout(() => document.getElementById(id)?.classList.remove("row-add"), 2000);
  } else {
    tableBody.insertAdjacentHTML("beforeend", fmtDiskRow(d));
    document.getElementById(id).classList.add("row-add");
    setTimeout(() => document.getElementById(id)?.classList.remove("row-add"), 2000);
  }
}

function removeRow(d) {
  const id = `row-${d.name}`;
  const row = document.getElementById(id);
  if (row) row.remove();
}

function logEvent(evt) {
  const li = document.createElement("li");
  const ts = new Date(evt.ts * 1000).toLocaleString();
  if (evt.type === "snapshot") {
    li.textContent = `[${ts}] snapshot: ${evt.disks.length} disk(s)`;
  } else {
    li.textContent = `[${ts}] ${evt.type}: ${evt.disk.path} (${evt.disk.model || ""} ${evt.disk.serial || ""})`;
  }
  eventsEl.prepend(li);
  while (eventsEl.children.length > 200) eventsEl.removeChild(eventsEl.lastChild);
}

async function refreshOnce() {
  const res = await fetch("/api/disks");
  const data = await res.json();
  renderSnapshot(data.disks, data.protected);
}

function initDiskSSE() {
  const es = new EventSource("/events");
  es.onmessage = (msg) => {
    const evt = JSON.parse(msg.data);
    logEvent(evt);
    if (evt.type === "snapshot") {
      renderSnapshot(evt.disks, []);
    } else if (evt.type === "add" || evt.type === "change") {
      upsertRow(evt.disk);
    } else if (evt.type === "remove") {
      removeRow(evt.disk);
    }
  };
  es.onerror = () => setTimeout(initDiskSSE, 1500);
}

window.addEventListener("DOMContentLoaded", async () => {
  await refreshOnce();
  initDiskSSE();
  initJobSSE();
});
