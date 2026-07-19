const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const folderInput = document.getElementById("folderInput");
const btnPickFiles = document.getElementById("btnPickFiles");
const btnPickFolder = document.getElementById("btnPickFolder");
const queueSection = document.getElementById("queueSection");
const queueList = document.getElementById("queueList");
const queueSummary = document.getElementById("queueSummary");

const MAX_PARALLEL_UPLOADS = 2;
const STAGE_LABELS = {
  queued: "Queued",
  uploading_bunny: "Uploading to Bunny",
  transcoding: "Transcoding",
  downloading_hls: "Downloading HLS",
  uploading_r2: "Uploading to R2",
  done: "Done",
  error: "Error",
};

const items = new Map(); // clientId -> { file, relativePath, el, jobId, pollTimer }
let pending = [];
let activeUploads = 0;

/* ---------- selection handlers ---------- */

btnPickFiles.addEventListener("click", () => fileInput.click());
btnPickFolder.addEventListener("click", () => folderInput.click());

fileInput.addEventListener("change", (e) => {
  addFiles([...e.target.files].map((f) => ({ file: f, relativePath: f.name })));
  fileInput.value = "";
});

folderInput.addEventListener("change", (e) => {
  addFiles(
    [...e.target.files].map((f) => ({
      file: f,
      relativePath: f.webkitRelativePath || f.name,
    }))
  );
  folderInput.value = "";
});

["dragenter", "dragover"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    e.stopPropagation();
    dropzone.classList.add("dragover");
  })
);

["dragleave", "drop"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    e.stopPropagation();
    dropzone.classList.remove("dragover");
  })
);

dropzone.addEventListener("drop", async (e) => {
  const dt = e.dataTransfer;
  const collected = [];
  const entries = [...dt.items]
    .map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
    .filter(Boolean);

  if (entries.length) {
    for (const entry of entries) {
      await walkEntry(entry, "", collected);
    }
  } else {
    // Fallback: browsers without entry API support
    [...dt.files].forEach((f) => collected.push({ file: f, relativePath: f.name }));
  }
  addFiles(collected);
});

function walkEntry(entry, path, collected) {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file((file) => {
        collected.push({ file, relativePath: path + file.name });
        resolve();
      });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const allEntries = [];
      const readBatch = () => {
        reader.readEntries(async (batch) => {
          if (!batch.length) {
            for (const child of allEntries) {
              await walkEntry(child, path + entry.name + "/", collected);
            }
            resolve();
          } else {
            allEntries.push(...batch);
            readBatch();
          }
        });
      };
      readBatch();
    } else {
      resolve();
    }
  });
}

/* ---------- queue management ---------- */

function isVideoFile(file) {
  if (file.type && file.type.startsWith("video/")) return true;
  return /\.(mp4|mov|mkv|avi|webm|m4v|flv|wmv|ts)$/i.test(file.name);
}

function addFiles(entries) {
  const videoEntries = entries.filter((e) => isVideoFile(e.file));
  if (!videoEntries.length) return;

  queueSection.hidden = false;

  videoEntries.forEach(({ file, relativePath }) => {
    const clientId = `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const el = renderItem(clientId, relativePath, file.size);
    items.set(clientId, { file, relativePath, el, jobId: null });
    pending.push(clientId);
  });

  updateSummary();
  pumpQueue();
}

function pumpQueue() {
  while (activeUploads < MAX_PARALLEL_UPLOADS && pending.length) {
    const clientId = pending.shift();
    activeUploads++;
    uploadItem(clientId).finally(() => {
      activeUploads--;
      pumpQueue();
    });
  }
}

function updateSummary() {
  const total = items.size;
  const done = [...items.values()].filter((i) => i.el.dataset.stage === "done").length;
  const errored = [...items.values()].filter((i) => i.el.dataset.stage === "error").length;
  queueSummary.textContent = `${done}/${total} done${errored ? `, ${errored} failed` : ""}`;
}

/* ---------- rendering ---------- */

function renderItem(clientId, relativePath, size) {
  const el = document.createElement("div");
  el.className = "item";
  el.dataset.stage = "queued";
  el.innerHTML = `
    <div class="item-top">
      <div>
        <div class="item-name">${escapeHtml(baseName(relativePath))}</div>
        <div class="item-path">${escapeHtml(relativePath)}</div>
      </div>
      <div class="item-stage" data-role="stage">Queued</div>
    </div>
    <div class="bar-track"><div class="bar-fill" data-role="bar"></div></div>
    <div class="item-meta">
      <span data-role="size">${formatSize(size)}</span>
      <span data-role="pct">0%</span>
    </div>
    <div class="item-error" data-role="error" hidden></div>
  `;
  queueList.prepend(el);
  return el;
}

function setItemStage(clientId, stage, pct, errorMsg) {
  const item = items.get(clientId);
  if (!item) return;
  const { el } = item;
  el.dataset.stage = stage;
  el.querySelector('[data-role="stage"]').textContent = STAGE_LABELS[stage] || stage;
  el.querySelector('[data-role="stage"]').className = `item-stage ${stage === "done" ? "done" : stage === "error" ? "error" : ""}`;
  const bar = el.querySelector('[data-role="bar"]');
  bar.style.width = `${pct}%`;
  bar.className = `bar-fill ${stage === "done" ? "done" : stage === "error" ? "error" : ""}`;
  el.querySelector('[data-role="pct"]').textContent = `${Math.round(pct)}%`;
  const errEl = el.querySelector('[data-role="error"]');
  if (errorMsg) {
    errEl.hidden = false;
    errEl.textContent = errorMsg;
  }
  updateSummary();
}

function baseName(path) {
  const parts = path.split("/");
  return parts[parts.length - 1];
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let v = bytes;
  let u = -1;
  do {
    v /= 1024;
    u++;
  } while (v >= 1024 && u < units.length - 1);
  return `${v.toFixed(1)} ${units[u]}`;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

/* ---------- upload + poll ---------- */

function uploadItem(clientId) {
  const item = items.get(clientId);
  return new Promise((resolve) => {
    const form = new FormData();
    form.append("file", item.file);
    form.append("relativePath", item.relativePath);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");

    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) {
        const pct = (e.loaded / e.total) * 10; // upload-to-server is 0-10% of overall bar
        setItemStage(clientId, "uploading_bunny", pct);
      }
    });

    xhr.onload = () => {
      if (xhr.status === 202) {
        const data = JSON.parse(xhr.responseText);
        item.jobId = data.job_id;
        pollStatus(clientId);
      } else {
        let msg = "Upload failed";
        try {
          msg = JSON.parse(xhr.responseText).error || msg;
        } catch (_) {}
        setItemStage(clientId, "error", 0, msg);
      }
      resolve();
    };

    xhr.onerror = () => {
      setItemStage(clientId, "error", 0, "Network error during upload");
      resolve();
    };

    xhr.send(form);
  });
}

function pollStatus(clientId) {
  const item = items.get(clientId);
  if (!item || !item.jobId) return;

  const tick = async () => {
    try {
      const resp = await fetch(`/api/status/${item.jobId}`);
      if (!resp.ok) throw new Error("status check failed");
      const data = await resp.json();
      setItemStage(clientId, data.stage, data.progress, data.error);
      if (data.stage !== "done" && data.stage !== "error") {
        item.pollTimer = setTimeout(tick, 2000);
      }
    } catch (err) {
      item.pollTimer = setTimeout(tick, 4000);
    }
  };
  tick();
}
