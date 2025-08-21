const tableBody = document.querySelector("#drives tbody");
const protectedEl = document.querySelector("#protected");
const eventsEl = document.querySelector("#events");

function fmtDiskRow(d) {
  const prot = d.protected ? '<span class="badge yes">YES</span>' : '<span class="badge no">NO</span>';
  return `
    <tr id="row-${d.name}">
      <td><code>${d.path}</code></td>
      <td>${d.size || ""}</td>
      <td>${d.model || ""}</td>
      <td>${d.serial || ""}</td>
      <td>${d.tran || ""}</td>
      <td>${d.state || ""}</td>
      <td>${prot}</td>
    </tr>`;
}

function renderSnapshot(disks, protectedList) {
  protectedEl.textContent = protectedList.join(", ") || "(none)";
  tableBody.innerHTML = "";
  disks.forEach(d => {
    tableBody.insertAdjacentHTML("beforeend", fmtDiskRow(d));
  });
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
  eventsEl.prepend(li); // newest on top
  // limit size
  while (eventsEl.children.length > 200) eventsEl.removeChild(eventsEl.lastChild);
}

async function refreshOnce() {
  const res = await fetch("/api/disks");
  const data = await res.json();
  renderSnapshot(data.disks, data.protected);
}

function initSSE() {
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
  es.onerror = () => {
    // Backoff & try to reconnect
    setTimeout(() => initSSE(), 1500);
  };
}

window.addEventListener("DOMContentLoaded", async () => {
  await refreshOnce();
  initSSE();
});
