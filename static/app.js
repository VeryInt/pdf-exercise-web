const form = document.querySelector("#job-form");
const fileInput = document.querySelector("#file-input");
const dropzone = document.querySelector("#dropzone");
const browseFile = document.querySelector("#browse-file");
const removeFile = document.querySelector("#remove-file");
const filePreview = document.querySelector("#file-preview");
const fileName = document.querySelector("#file-name");
const fileDetail = document.querySelector("#file-detail");
const saveConfig = document.querySelector("#save-config");
const clientIdField = document.querySelector("#client-id-field");
const apiKeyInput = document.querySelector("#api-key-input");
const toggleApiKey = document.querySelector("#toggle-api-key");
const testConnection = document.querySelector("#test-connection");
const submitButton = document.querySelector("#submit-button");
const recentBox = document.querySelector("#recent-jobs");
const clearRecent = document.querySelector("#clear-recent");

const configFields = ["provider", "base_url", "model", "api_key"];
const configKey = "pdfExerciseMakerConfig";
const clientIdKey = "pdfExerciseMakerClientId";
const recentJobsKey = "pdfExerciseMakerRecentJobs";
const sessionStatsKey = "pdfExerciseMakerSessionStats";
const serviceTokenKey = "pdfExerciseMakerServiceToken";

let pollTimer = null;
let currentJobId = null;
let currentJobStartedAt = null;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function getClientId() {
  let clientId = localStorage.getItem(clientIdKey);
  if (!clientId) {
    clientId = crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    localStorage.setItem(clientIdKey, clientId);
  }
  return clientId;
}

function captureServiceToken() {
  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const token = (fragment.get("token") || "").trim();
  if (token) {
    sessionStorage.setItem(serviceTokenKey, token);
    history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  }
  return sessionStorage.getItem(serviceTokenKey) || "";
}

function getServiceToken() {
  return sessionStorage.getItem(serviceTokenKey) || "";
}

function headers() {
  const result = { "X-Client-Id": getClientId() };
  const serviceToken = getServiceToken();
  if (serviceToken) result["X-Service-Token"] = serviceToken;
  return result;
}

function setSharedAccessState(enabled) {
  const banner = document.querySelector("#shared-access-banner");
  banner.hidden = !enabled;
  for (const name of ["provider", "base_url", "model", "api_key"]) {
    const field = document.querySelector(`[name="${name}"]`);
    if (field) field.disabled = enabled;
  }
  apiKeyInput.required = !enabled;
  saveConfig.closest(".save-config").hidden = enabled;
}

function readJsonStorage(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key) || JSON.stringify(fallback));
  } catch {
    return fallback;
  }
}

function writeJsonStorage(key, value) {
  localStorage.setItem(key, JSON.stringify(value));
}

function subjectLabel(value) {
  return {
    auto: "自动",
    english: "英语",
    math: "数学",
    physics: "物理",
    other: "其他",
  }[value] || value || "自动";
}

function statusLabel(status, progress) {
  if (status === "queued") return `排队中 · ${progress ?? 0}%`;
  if (status === "running") return `运行中 · ${progress ?? 0}%`;
  if (status === "completed") return "已完成";
  if (status === "failed") return "失败";
  return status || "未知";
}

function statusDotClass(status) {
  if (status === "completed") return "dot ok";
  if (status === "failed") return "dot";
  if (status === "running" || status === "queued") return "dot accent-bg";
  return "dot";
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  if (sameDay) {
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  }
  return date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

function formatDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return "00:00";
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const remain = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remain).padStart(2, "0")}`;
}

function formatCountdown(expiresAt) {
  if (!expiresAt) return "-- : -- : --";
  const diff = new Date(expiresAt).getTime() - Date.now();
  if (!Number.isFinite(diff) || diff <= 0) return "已过期";
  const totalSeconds = Math.floor(diff / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return `${String(hours).padStart(2, "0")} : ${String(minutes).padStart(2, "0")} : ${String(seconds).padStart(2, "0")}`;
}

function loadConfig() {
  const config = readJsonStorage(configKey, null);
  if (!config) return;
  for (const name of configFields) {
    const field = document.querySelector(`[name="${name}"]`);
    if (field && config[name]) field.value = config[name];
  }
  saveConfig.checked = true;
}

function saveConfigIfNeeded() {
  if (!saveConfig.checked) {
    localStorage.removeItem(configKey);
    return;
  }
  const config = {};
  for (const name of configFields) {
    const field = document.querySelector(`[name="${name}"]`);
    config[name] = field ? field.value : "";
  }
  writeJsonStorage(configKey, config);
}

function loadRecentJobs() {
  const raw = readJsonStorage(recentJobsKey, []);
  return Array.isArray(raw) ? raw : [];
}

function saveRecentJob(record) {
  const jobs = loadRecentJobs().filter((job) => job.job_id !== record.job_id);
  jobs.unshift({ ...record, updated_at_local: new Date().toISOString() });
  writeJsonStorage(recentJobsKey, jobs.slice(0, 8));
  renderRecentJobs();
  updateSessionStats();
}

function patchRecentJob(job) {
  const jobs = loadRecentJobs();
  const index = jobs.findIndex((item) => item.job_id === job.id);
  if (index === -1) return;
  jobs[index] = {
    ...jobs[index],
    status: job.status,
    progress: job.progress,
    subject: job.subject,
    original_filename: job.original_filename || jobs[index].original_filename,
    created_at: job.created_at || jobs[index].created_at,
    updated_at_local: new Date().toISOString(),
  };
  writeJsonStorage(recentJobsKey, jobs.slice(0, 8));
  renderRecentJobs();
  updateSessionStats();
}

function updateSessionStats() {
  const jobs = loadRecentJobs();
  const stats = readJsonStorage(sessionStatsKey, { created: 0 });
  document.querySelector("#session-created").textContent = String(stats.created || jobs.length || 0);
  document.querySelector("#session-running").textContent = String(
    jobs.filter((job) => job.status === "queued" || job.status === "running").length,
  );
  document.querySelector("#session-recent").textContent = String(jobs.length);
}

function incrementCreatedSessionCount() {
  const stats = readJsonStorage(sessionStatsKey, { created: 0 });
  stats.created = (stats.created || 0) + 1;
  writeJsonStorage(sessionStatsKey, stats);
  updateSessionStats();
}

function renderRecentJobs() {
  const jobs = loadRecentJobs();
  if (!jobs.length) {
    recentBox.innerHTML = `<div class="recent-row"><div class="muted">暂无最近任务。</div></div>`;
    return;
  }

  const rows = jobs
    .map((job, index) => {
      const id = escapeHtml(job.job_id || "");
      const filename = escapeHtml(job.original_filename || "未命名文件");
      const shortId = escapeHtml((job.job_id || "").slice(0, 8));
      const status = escapeHtml(statusLabel(job.status, job.progress));
      const subject = escapeHtml(subjectLabel(job.subject));
      const created = escapeHtml(formatTime(job.created_at || job.updated_at_local));
      const dotClass = statusDotClass(job.status);
      const actionText = job.status === "failed" ? "重试" : "查看";
      return `
        <div class="recent-row">
          <div class="mono num muted">${String(index + 1).padStart(2, "0")}</div>
          <div class="recent-file">
            <strong>${filename}</strong>
            <span class="mono">${shortId}</span>
          </div>
          <div>${subject}</div>
          <div><span class="status-pill"><span class="${dotClass}"></span>${status}</span></div>
          <div class="mono muted">${created}</div>
          <div><button class="text-link" type="button" data-job-id="${id}">${actionText}</button></div>
        </div>
      `;
    })
    .join("");

  recentBox.innerHTML = `
    <div class="recent-row header">
      <div>#</div>
      <div>文件 / Job</div>
      <div>学科</div>
      <div>状态</div>
      <div>创建时间</div>
      <div>操作</div>
    </div>
    ${rows}
  `;
  recentBox.querySelectorAll("[data-job-id]").forEach((button) => {
    button.addEventListener("click", () => poll(button.dataset.jobId));
  });
}

function updateFilePreview(file) {
  if (!file) {
    filePreview.classList.add("is-empty");
    fileName.textContent = "尚未选择文件";
    fileDetail.textContent = "";
    removeFile.hidden = true;
    return;
  }
  filePreview.classList.remove("is-empty");
  fileName.textContent = file.name;
  fileDetail.textContent = `· ${formatBytes(file.size)} · ${file.type || "未知类型"}`;
  removeFile.hidden = false;
}

function setFile(file) {
  if (!file) {
    fileInput.value = "";
    updateFilePreview(null);
    return;
  }
  const transfer = new DataTransfer();
  transfer.items.add(file);
  fileInput.files = transfer.files;
  updateFilePreview(file);
}

function renderTimeline(events, status) {
  if (!events || !events.length) {
    return `<li class="muted">提交任务后会在这里显示处理进度。</li>`;
  }
  return events
    .map((event, index) => {
      const cls = index === events.length - 1 && status !== "completed" && status !== "failed" ? "current" : "done";
      return `<li class="${cls}"><b>${escapeHtml(event.message)}</b><time>${escapeHtml(formatTime(event.created_at))}</time></li>`;
    })
    .join("");
}

function renderDownloads(job) {
  const labels = {
    student_pdf: ["无答案习题 PDF", "给孩子练习用 · 仅题面"],
    answer_pdf: ["答案详解 PDF", "题目 + 答案 + 解析"],
    worksheet_json: ["结构化数据", "json"],
    transcript: ["转录底稿", "md"],
    token_usage: ["Token 用量", "json"],
  };
  const order = ["student_pdf", "answer_pdf", "worksheet_json", "transcript", "token_usage"];
  const completed = job && job.status === "completed";
  const artifacts = job?.artifacts || {};

  document.querySelector("#result-state").textContent = completed ? "可下载" : "待完成";
  document.querySelector("#download-list").innerHTML = order
    .map((kind) => {
      const [title, subtitle] = labels[kind];
      const href = artifacts[kind];
      const disabled = !completed || !href;
      const action = disabled
        ? `<button class="btn small disabled" type="button" disabled>下载</button>`
        : `<a class="btn small" href="${escapeHtml(href)}" target="_blank" rel="noreferrer">下载</a>`;
      return `
        <li>
          <div>
            <strong>${title}</strong>
            <span>${subtitle}</span>
          </div>
          ${action}
        </li>
      `;
    })
    .join("");
  const expiry = document.querySelector("#expiry-countdown");
  if (completed && job.expires_at) {
    expiry.dataset.expiresAt = job.expires_at;
    expiry.textContent = formatCountdown(job.expires_at);
  } else {
    delete expiry.dataset.expiresAt;
    expiry.textContent = "-- : -- : --";
  }
}

function renderJob(job) {
  currentJobId = job.id;
  patchRecentJob(job);

  const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
  const queueText =
    job.queue_position === 0
      ? "排队 #0 · 正在处理"
      : job.queue_position
        ? `排队 #${job.queue_position} · 等待处理`
        : "不在队列中";
  const phase =
    job.status === "completed"
      ? "生成完成"
      : job.status === "failed"
        ? "生成失败"
        : job.status === "queued"
          ? "等待处理…"
          : "处理中…";

  document.querySelector("#status-title").textContent = statusLabel(job.status, progress);
  document.querySelector("#status-dot").className = statusDotClass(job.status);
  document.querySelector("#status-job-id").textContent = `job · ${job.id.slice(0, 8)}`;
  document.querySelector("#status-phase").textContent = phase;
  document.querySelector("#status-progress").textContent = String(progress);
  document.querySelector("#progress-bar").style.width = `${progress}%`;
  document.querySelector("#queue-text").textContent = `${queueText} · 当前活跃 ${job.active_jobs ?? 0}`;
  document.querySelector("#elapsed-text").textContent = currentJobStartedAt
    ? `已耗时 ${formatDuration(Date.now() - currentJobStartedAt)}`
    : "每 2.5s 自动刷新";
  document.querySelector("#event-timeline").innerHTML = renderTimeline(job.events, job.status);

  const error = document.querySelector("#error-message");
  if (job.error) {
    error.textContent = job.error;
    error.hidden = false;
  } else {
    error.hidden = true;
  }

  const tokenPanel = document.querySelector("#token-panel");
  const tokenValues = tokenPanel.querySelectorAll("b");
  if (job.token_usage) {
    tokenPanel.classList.remove("is-empty");
    tokenValues[0].textContent = Number(job.token_usage.input_tokens ?? 0).toLocaleString("zh-CN");
    tokenValues[1].textContent = Number(job.token_usage.output_tokens ?? 0).toLocaleString("zh-CN");
    tokenValues[2].textContent = Number(job.token_usage.total_tokens ?? 0).toLocaleString("zh-CN");
  } else {
    tokenPanel.classList.add("is-empty");
    tokenValues.forEach((node) => {
      node.textContent = "0";
    });
  }

  renderDownloads(job);
}

function renderSubmitError(message) {
  document.querySelector("#status-title").textContent = "提交失败";
  document.querySelector("#status-dot").className = "dot";
  document.querySelector("#status-phase").textContent = "请检查表单";
  document.querySelector("#status-progress").textContent = "0";
  document.querySelector("#progress-bar").style.width = "0";
  document.querySelector("#queue-text").textContent = "未创建任务";
  document.querySelector("#event-timeline").innerHTML = `<li class="current"><b>${escapeHtml(message)}</b></li>`;
  const error = document.querySelector("#error-message");
  error.textContent = message;
  error.hidden = false;
}

async function poll(jobId) {
  clearTimeout(pollTimer);
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, { headers: headers() });
    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: "任务查询失败" }));
      throw new Error(error.detail || "任务查询失败");
    }
    const job = await response.json();
    renderJob(job);
    if (job.status !== "completed" && job.status !== "failed") {
      pollTimer = setTimeout(() => poll(jobId), 2500);
    }
  } catch (error) {
    renderSubmitError(error.message);
  }
}

async function refreshSystemStatus() {
  try {
    const response = await fetch("/api/status", { headers: headers() });
    if (!response.ok) return;
    const status = await response.json();
    document.querySelector("#active-jobs").textContent = `${status.active_jobs}/${status.max_active_jobs}`;
    document.querySelector("#ip-quota").textContent = status.hourly_limit_exempt
      ? "共享授权 · 不限小时次数"
      : `${status.hourly_jobs_for_ip} / ${status.max_jobs_per_ip_per_hour}`;
    document.querySelector("#max-upload").textContent = String(status.max_upload_mb);
    document.querySelector("#queue-limit").textContent = String(status.max_active_jobs);
    document.querySelector("#ip-active-limit").textContent = String(status.max_active_jobs_per_ip);
    const location = [status.visitor_country, status.visitor_as_name].filter(Boolean).join(" · ");
    document.querySelector("#visitor-location").textContent = `访问来源：${location || "未知"}`;
    setSharedAccessState(Boolean(status.shared_access_authorized));
  } catch {}
}

fileInput.addEventListener("change", () => {
  updateFilePreview(fileInput.files[0]);
});

browseFile.addEventListener("click", () => fileInput.click());

removeFile.addEventListener("click", () => setFile(null));

dropzone.addEventListener("click", (event) => {
  if (event.target === browseFile || event.target === fileInput) return;
  fileInput.click();
});

dropzone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    fileInput.click();
  }
});

["dragenter", "dragover"].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add("is-dragover");
  });
});

["dragleave", "drop"].forEach((name) => {
  dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-dragover");
  });
});

dropzone.addEventListener("drop", (event) => {
  const file = event.dataTransfer.files[0];
  if (file) setFile(file);
});

toggleApiKey.addEventListener("click", () => {
  const hidden = apiKeyInput.type === "password";
  apiKeyInput.type = hidden ? "text" : "password";
  toggleApiKey.textContent = hidden ? "隐藏" : "显示";
});

testConnection.addEventListener("click", () => {
  renderSubmitError(
    document.querySelector("#shared-access-banner").hidden
      ? "当前版本会在提交任务时验证模型连接。"
      : "共享 AI 配置已由服务端启用，会在提交任务时验证。",
  );
});

clearRecent.addEventListener("click", () => {
  localStorage.removeItem(recentJobsKey);
  renderRecentJobs();
  updateSessionStats();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  saveConfigIfNeeded();
  clearTimeout(pollTimer);
  submitButton.disabled = true;
  currentJobStartedAt = Date.now();
  document.querySelector("#status-title").textContent = "正在上传";
  document.querySelector("#status-phase").textContent = "上传中…";
  document.querySelector("#status-progress").textContent = "0";
  document.querySelector("#progress-bar").style.width = "0";
  document.querySelector("#event-timeline").innerHTML = `<li class="current"><b>正在上传文件并创建任务。</b></li>`;
  renderDownloads(null);

  try {
    clientIdField.value = getClientId();
    const data = new FormData(form);
    data.set("client_id", getClientId());
    const response = await fetch("/api/jobs", {
      method: "POST",
      body: data,
      headers: headers(),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({ detail: "提交失败" }));
      throw new Error(error.detail || "提交失败");
    }
    const result = await response.json();
    const file = fileInput.files[0];
    incrementCreatedSessionCount();
    saveRecentJob({
      job_id: result.job_id,
      original_filename: file ? file.name : "未命名文件",
      subject: data.get("subject"),
      status: "queued",
      progress: 0,
      created_at: new Date().toISOString(),
    });
    await refreshSystemStatus();
    poll(result.job_id);
  } catch (error) {
    renderSubmitError(error.message);
  } finally {
    submitButton.disabled = false;
  }
});

setInterval(() => {
  if (currentJobId && currentJobStartedAt) {
    document.querySelector("#elapsed-text").textContent = `已耗时 ${formatDuration(Date.now() - currentJobStartedAt)}`;
  }
  const expiry = document.querySelector("#expiry-countdown");
  if (expiry.dataset.expiresAt) expiry.textContent = formatCountdown(expiry.dataset.expiresAt);
}, 1000);

captureServiceToken();
clientIdField.value = getClientId();
if (getServiceToken()) apiKeyInput.required = false;
loadConfig();
renderRecentJobs();
renderDownloads(null);
updateFilePreview(fileInput.files[0]);
updateSessionStats();
refreshSystemStatus();
