// Renders Pulse chart specs: Apache ECharts options built by pulse/charts.py from verified evidence.
// The page only draws them. Colour tokens such as "@accent" resolve to the CSS variable --accent, so charts follow
// the light/dark theme and are redrawn when the system theme changes.
const Charts = (() => {
  const live = []; // { el, spec, chart }
  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const resolve = (x) =>
    Array.isArray(x) ? x.map(resolve)
      : x && typeof x === "object" ? Object.fromEntries(Object.entries(x).map(([k, v]) => [k, resolve(v)]))
        : typeof x === "string" && /^@[a-z0-9-]+$/.test(x) ? cssVar(`--${x.slice(1)}`) : x;

  function theme() {
    const muted = cssVar("--muted"), line = cssVar("--line"), ink = cssVar("--ink"), surface = cssVar("--surface");
    const axis = {
      axisLine: { lineStyle: { color: line } }, axisTick: { show: false },
      axisLabel: { color: muted }, splitLine: { lineStyle: { color: line } }, nameTextStyle: { color: muted },
    };
    return {
      color: ["--accent", "--c-res", "--c-ver", "--c-gate", "--ok"].map(cssVar),
      backgroundColor: "transparent",
      textStyle: { color: muted, fontFamily: "Figtree, system-ui, -apple-system, 'Segoe UI', sans-serif" },
      legend: { textStyle: { color: muted }, itemWidth: 14, itemHeight: 8 },
      tooltip: { backgroundColor: surface, borderColor: line, textStyle: { color: ink } },
      categoryAxis: { ...axis, splitLine: { show: false } },
      valueAxis: { ...axis, axisLine: { show: false } },
    };
  }

  // Custom-series renderers named in specs ("renderItem": "timeline"): specs stay plain JSON, the drawing code lives here.
  const RENDERERS = {
    // One horizontal block per item, value = [row, from, to, ...]: the workout timeline.
    timeline(params, api) {
      const row = api.value(0), a = api.coord([api.value(1), row]), b = api.coord([api.value(2), row]);
      const h = api.size([0, 1])[1] * 0.6;
      const rect = echarts.graphic.clipRectByRect(
        { x: a[0], y: a[1] - h / 2, width: Math.max(3, b[0] - a[0]), height: h },
        { x: params.coordSys.x, y: params.coordSys.y, width: params.coordSys.width, height: params.coordSys.height });
      return rect && { type: "rect", shape: { ...rect, r: 2 },
        style: api.style({ stroke: cssVar("--surface"), lineWidth: 1 }) };
    },
  };

  function draw(item) {
    if (item.chart) item.chart.dispose();
    echarts.registerTheme("pulse", theme());
    item.chart = echarts.init(item.el, "pulse", { renderer: "svg" });
    const option = resolve(item.spec.option);
    for (const s of [].concat(option.series || [])) {
      if (s.type === "custom" && typeof s.renderItem === "string") s.renderItem = RENDERERS[s.renderItem];
    }
    item.chart.setOption({ aria: { enabled: true }, animationDuration: 300, ...option });
  }

  function table(t) {  // the chart's data as a table: the view that doesn't depend on telling colours apart
    const details = document.createElement("details");
    details.className = "data";
    details.innerHTML = '<summary>Data</summary><div class="tablewrap"><table><thead><tr></tr></thead><tbody></tbody></table></div>';
    const head = details.querySelector("thead tr"), body = details.querySelector("tbody");
    for (const c of t.columns) head.appendChild(Object.assign(document.createElement("th"), { textContent: c }));
    for (const r of t.rows) {
      const tr = body.insertRow();
      for (const v of r) {
        const td = tr.insertCell();
        td.textContent = v === null || v === undefined ? "–" : String(v);
        if (typeof v === "number") td.className = "num";
      }
    }
    return details;
  }

  function figure(spec) {
    const fig = document.createElement("figure");
    fig.className = "fig";
    fig.innerHTML = '<h3></h3><p class="sub"></p><div class="chart"></div><figcaption></figcaption>';
    fig.querySelector("h3").textContent = spec.title;
    fig.querySelector(".sub").textContent = spec.subtitle || "";
    fig.querySelector(".chart").style.height = `${spec.height || 260}px`;
    const cap = fig.querySelector("figcaption");
    cap.textContent = spec.caption || "";
    for (const id of spec.evidence || []) {  // link each evidence id in the caption to its Sources entry
      cap.innerHTML = cap.innerHTML.replace(new RegExp(`\\b${id}\\b`), `<a href="#src-${id}">${id}</a>`);
    }
    if (spec.table) fig.insertBefore(table(spec.table), cap);
    return fig;
  }

  function render(container, specs) {
    for (let i = live.length - 1; i >= 0; i--) {
      if (container.contains(live[i].el)) { live[i].chart.dispose(); live.splice(i, 1); }
    }
    container.innerHTML = "";
    for (const spec of specs || []) {
      const fig = figure(spec);
      container.appendChild(fig);
      const item = { el: fig.querySelector(".chart"), spec, chart: null };
      live.push(item);
      draw(item);
    }
  }

  window.addEventListener("resize", () => live.forEach((i) => i.chart && i.chart.resize()));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => live.forEach(draw));
  return { render };
})();
