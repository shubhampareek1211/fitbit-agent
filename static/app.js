const $ = (id) => document.getElementById(id);
let sessionId = null;
let source = null;
let stepsChart = null, answerChart = null;
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

function laneFor(agent) {
  const key = agent.startsWith("Researcher") ? agent : agent;
  let lane = document.querySelector(`.lane[data-agent="${key}"]`);
  if (!lane) {
    lane = document.createElement("div");
    lane.className = `lane ${agent.split("_")[0]}`;
    lane.dataset.agent = key;
    lane.innerHTML = `<h3>${agent.replace("_", " ")}</h3><ol></ol>`;
    $("lanes").appendChild(lane);
  }
  return lane;
}

function addActivity(agent, text) {
  document.querySelectorAll(".lane.active").forEach((l) => l.classList.remove("active"));
  const lane = laneFor(agent);
  lane.classList.add("active");
  const li = document.createElement("li");
  li.textContent = text;
  lane.querySelector("ol").appendChild(li);
}

function say(role, text) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.textContent = text;
  $("thread").appendChild(div);
}

function renderMarkdown(md) {
  let html = DOMPurify.sanitize(marked.parse(md));
  html = html.replace(/\[(E\d+(?:\s*[,;]\s*E\d+)*)\]/g, (_, ids) =>
    ids.split(/\s*[,;]\s*/).map((id) => `<sup class="cite"><a href="#src-${id}">${id}</a></sup>`).join(""));
  $("answer").innerHTML = html;
  $("answer").querySelectorAll("li").forEach((li) => {
    const m = li.textContent.match(/^(E\d+)\b/);
    if (m) li.id = `src-${m[1]}`;
  });
}

function renderCalls(calls) {
  $("ncalls").textContent = calls.length;
  $("calls").innerHTML = "";
  for (const c of calls) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="who"></span> <code></code> → <span class="muted"></span>`;
    li.querySelector(".who").textContent = c.agent;
    li.querySelector("code").textContent = `${c.name}(${JSON.stringify(c.args)})`;
    li.querySelector(".muted").textContent = String(c.result).slice(0, 220);
    $("calls").appendChild(li);
  }
}

function lineChart(canvas, labels, datasets, existing) {
  if (existing) existing.destroy();
  const palette = [css("--accent"), css("--c-res"), css("--c-ver")];
  return new Chart(canvas, {
    type: "line",
    data: { labels, datasets: datasets.map((d, i) => ({ ...d, borderColor: palette[i], backgroundColor: palette[i],
      pointRadius: 0, borderWidth: 2, spanGaps: false, tension: 0.2 })) },
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: css("--muted"), boxWidth: 12 } } },
      scales: { x: { ticks: { color: css("--muted"), maxTicksLimit: 8 }, grid: { display: false } },
                y: { ticks: { color: css("--muted") }, grid: { color: css("--line") } } } },
  });
}

function ask(question) {
  if (!question.trim()) return;
  if (source) source.close();
  say("user", question);
  $("go").disabled = true;
  $("lanes").innerHTML = "";
  $("run").classList.remove("hidden");
  $("report").classList.add("hidden");
  const t0 = Date.now();
  const timer = setInterval(() => ($("timer").textContent = `${Math.round((Date.now() - t0) / 1000)}s`), 500);
  const params = new URLSearchParams({ question });
  if (sessionId) params.set("session_id", sessionId);
  source = new EventSource(`/run?${params}`);
  const finish = () => { clearInterval(timer); source.close(); $("go").disabled = false;
    document.querySelectorAll(".lane.active").forEach((l) => l.classList.remove("active")); };
  source.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === "start") sessionId = ev.session_id;
    if (ev.type === "activity") addActivity(ev.agent, ev.text);
    if (ev.type === "error") { finish(); say("bot", `Something went wrong: ${ev.message}`); }
    if (ev.type === "done") {
      finish();
      sessionId = ev.session_id;
      if (ev.status !== "answer") {
        say("bot", ev.markdown);
        $("run").classList.add("hidden");
        return;
      }
      $("report").classList.remove("hidden");
      renderMarkdown(ev.markdown);
      renderCalls(ev.tool_calls);
      const ok = ev.verify && ev.verify.passed;
      $("badge").className = `badge ${ok ? "ok" : "warn"}`;
      $("badge").textContent = ok ? `Verified · ${ev.verify.numbers_checked} numbers checked · ${ev.seconds}s`
                                  : "Partly verified, see note";
      if (ev.chart && ev.chart.dates.length > 1) {
        $("answerChartWrap").classList.remove("hidden");
        answerChart = lineChart($("answerChart"), ev.chart.dates,
          ev.chart.metrics.map((m, i) => ({ label: m, data: ev.chart.series[i] })), answerChart);
      } else $("answerChartWrap").classList.add("hidden");
      $("report").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  };
  source.onerror = () => { if ($("go").disabled) { finish(); say("bot", "Connection lost. Please try again."); } };
}

async function init() {
  const o = await (await fetch("/api/overview")).json();
  $("asof").textContent = `data through ${o.asof}`;
  $("window").textContent = `${o.first_date} → ${o.asof} · ${o.location}. "Today" for every question is ${o.asof}.`;
  for (const q of o.samples) {
    const b = document.createElement("button");
    b.className = "chip"; b.type = "button"; b.textContent = q;
    b.onclick = () => ask(q);
    $("chips").appendChild(b);
  }
  $("coverage").innerHTML = o.coverage.map((c) =>
    `<div><b>${c.metric}</b>${c.days} days · ${c.first_date.slice(5)}→${c.last_date.slice(5)}</div>`).join("");
  stepsChart = lineChart($("stepsChart"), o.steps.dates, [
    { label: "steps (merged total)", data: o.steps.total },
    { label: "watch", data: o.steps.watch },
    { label: "phone", data: o.steps.phone },
  ]);
  $("mcpurl").textContent = `${location.origin}/mcp/`;
}

$("form").onsubmit = (e) => { e.preventDefault(); const q = $("q").value; $("q").value = ""; ask(q); };
init();
