const form = document.querySelector("#job-form");
const statusBox = document.querySelector("#status");
const resultBox = document.querySelector("#results");
const recentBox = document.querySelector("#recent-jobs");
const saveConfig = document.querySelector("#save-config");

const fields = ["provider", "base_url", "model", "api_key"];
const recentJobsKey = "pdfExerciseMakerRecentJobs";

function loadConfig() {
  const raw = localStorage.getItem("pdfExerciseMakerConfig");
  if (!raw) return;
  try {
    const config = JSON.parse(raw);
    for (const name of fields) {
      if (config[name]) document.querySelector(`[name="${name}"]`).value = config[name];
    }
    saveConfig.checked = true;
  } catch {}
}

function saveConfigIfNeeded() {
  if (!saveConfig.checked) {
    localStorage.removeItem("pdfExerciseMakerConfig");
    return;
  }
  const config = {};
  for (const name of fields) {
    config[name] = document.querySelector(`[name="${name}"]`).value;
  }
  localStorage.setItem("pdfExerciseMakerConfig", JSON.stringify(config));
}

function loadRecentJobs() {
  try {
    return JSON.parse(localStorage.getItem(recentJobsKey) || "[]");
  } catch {
    return [];
  }
}

function saveRecentJob(jobId) {
  const jobs = loadRecentJobs().filter((id) => id !== jobId);
  jobs.unshift(jobId);
  localStorage.setItem(recentJobsKey, JSON.stringify(jobs.slice(0, 8)));
  renderRecentJobs();
}

function renderRecentJobs() {
  const jobs = loadRecentJobs();
  if (!jobs.length) {
    recentBox.innerHTML = `<p class="hint">暂无最近任务。</p>`;
    return;
  }
  recentBox.innerHTML = jobs
    .map((id) => `<button type="button" class="link-button" data-job-id="${id}">${id.slice(0, 8)} 查询结果</button>`)
    .join("");
  recentBox.querySelectorAll("[data-job-id]").forEach((button) => {
    button.addEventListener("click", () => poll(button.dataset.jobId));
  });
}

function renderJob(job) {
  const events = job.events.map((event) => `<li><span>${event.created_at}</span>${event.message}</li>`).join("");
  const tokenUsage = job.token_usage
    ? `<p><strong>Token：</strong>输入 ${job.token_usage.input_tokens ?? 0}，输出 ${job.token_usage.output_tokens ?? 0}，总计 ${job.token_usage.total_tokens ?? 0}</p>`
    : "";
  const queueText = job.queue_position === 0
    ? "正在处理"
    : (job.queue_position ? `排队第 ${job.queue_position} 位` : "不在队列中");
  statusBox.innerHTML = `
    <div class="progress"><div style="width:${job.progress}%"></div></div>
    <p><strong>状态：</strong>${job.status} <strong>进度：</strong>${job.progress}%</p>
    <p><strong>队列：</strong>${queueText} <strong>当前活跃任务：</strong>${job.active_jobs ?? 0}</p>
    ${tokenUsage}
    ${job.error ? `<p class="error">${job.error}</p>` : ""}
    <ul class="events">${events}</ul>
  `;
  if (job.status === "completed") {
    const labels = {
      student_pdf: "下载无答案习题",
      answer_pdf: "下载答案详解",
      worksheet_json: "下载结构化数据",
      transcript: "下载转录底稿",
      token_usage: "下载 Token 用量"
    };
    const links = Object.entries(job.artifacts)
      .map(([kind, href]) => `<a class="button" href="${href}" target="_blank">${labels[kind] || kind}</a>`)
      .join("");
    resultBox.innerHTML = `<h2>下载结果</h2><div class="downloads">${links}</div>`;
  }
}

async function poll(jobId) {
  const response = await fetch(`/api/jobs/${jobId}`);
  const job = await response.json();
  renderJob(job);
  if (job.status !== "completed" && job.status !== "failed") {
    setTimeout(() => poll(jobId), 2500);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  saveConfigIfNeeded();
  resultBox.innerHTML = "";
  statusBox.innerHTML = "<p>正在上传...</p>";
  const response = await fetch("/api/jobs", {
    method: "POST",
    body: new FormData(form),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: "提交失败" }));
    statusBox.innerHTML = `<p class="error">${error.detail || "提交失败"}</p>`;
    return;
  }
  const { job_id } = await response.json();
  saveRecentJob(job_id);
  poll(job_id);
});

loadConfig();
renderRecentJobs();
