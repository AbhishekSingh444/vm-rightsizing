/* Front end for the right-sizing results. Reads window.DATA (built by build_data.py). */
(function () {
  "use strict";
  const D = window.DATA;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const f = (v, d = 1) => (v === "" || v == null || Number.isNaN(+v) ? "–" : (+v).toFixed(d));
  const pct = (v, d = 1) => (v === "" || v == null ? "–" : (100 * v).toFixed(d) + "%");
  const big = (v) => (+v).toLocaleString("en-US", { maximumFractionDigits: 0 });
  const tip = $("#tip");

  /* ---------------- tables ---------------- */
  function renderTable(el, rows, cols, opts = {}) {
    if (typeof el === "string") el = $(el);
    if (!cols) cols = Object.keys(rows[0] || {}).map((k) => ({ k }));
    const head = "<tr>" + cols.map((c) => `<th class="${c.num ? "num" : ""}">${c.label ?? c.k}</th>`).join("") + "</tr>";
    const limit = opts.limit ?? rows.length;
    const body = rows.slice(0, limit).map((r) =>
      `<tr class="${opts.hl && opts.hl(r) ? "hl" : ""}">` + cols.map((c) => {
        const v = c.fmt ? c.fmt(r[c.k], r) : esc(r[c.k]);
        return `<td class="${c.num ? "num" : ""}">${v}</td>`;
      }).join("") + "</tr>").join("");
    el.innerHTML = head + body;
  }

  /* ---------------- segmented controls ---------------- */
  function seg(el, options, value, onChange) {
    if (typeof el === "string") el = $(el);
    el.innerHTML = options.map(([v, l]) => `<button data-v="${esc(v)}">${esc(l)}</button>`).join("");
    const set = (v) => { $$("button", el).forEach((b) => b.classList.toggle("on", b.dataset.v === String(v))); };
    el.onclick = (e) => { const b = e.target.closest("button"); if (!b) return; set(b.dataset.v); onChange(b.dataset.v); };
    set(value);
  }

  /* ---------------- SVG charting ---------------- */
  const NS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function niceTicks(min, max, n = 5) {
    const span = max - min || 1;
    const step0 = span / n, mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= n) || 10 * mag;
    const out = [];
    for (let v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  }
  function scale(dom, rng, log) {
    if (log) {
      const a = Math.log10(dom[0]), b = Math.log10(dom[1]);
      return (v) => rng[0] + ((Math.log10(Math.max(v, dom[0])) - a) / (b - a)) * (rng[1] - rng[0]);
    }
    return (v) => rng[0] + ((v - dom[0]) / (dom[1] - dom[0])) * (rng[1] - rng[0]);
  }
  function showTip(evt, html, host) {
    tip.innerHTML = html;
    tip.style.display = "block";
    const r = tip.getBoundingClientRect();
    let x = evt.clientX + 14, y = evt.clientY + 14;
    if (x + r.width > innerWidth - 8) x = evt.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = evt.clientY - r.height - 14;
    tip.style.left = x + scrollX + "px";
    tip.style.top = y + scrollY + "px";
    tip.style.position = "absolute";
  }
  const hideTip = () => { tip.style.display = "none"; };

  /* Generic x/y plot. layers: band, area, line, hline, rects, points, labels */
  function plot(el, o) {
    if (typeof el === "string") el = $(el);
    const W = Math.max(el.clientWidth || 600, 280), H = o.height || 340;
    const m = { l: 56, r: o.right ?? 24, t: 12, b: 44 };
    el.innerHTML = "";
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" }, el);
    if (o.title) svgEl("title", {}, svg).textContent = o.title;
    const sx = scale(o.x, [m.l, W - m.r], o.xLog), sy = scale(o.y, [H - m.b, m.t], o.yLog);
    const g = svgEl("g", { class: "grid" }, svg);
    const xt = o.xTicks || (o.xLog ? logTicks(o.x) : niceTicks(o.x[0], o.x[1], Math.max(3, Math.floor(W / 90))));
    const yt = o.yTicks || niceTicks(o.y[0], o.y[1], 5);
    yt.forEach((v) => {
      svgEl("line", { x1: m.l, x2: W - m.r, y1: sy(v), y2: sy(v) }, g);
      svgEl("text", { x: m.l - 8, y: sy(v) + 4, "text-anchor": "end" }, svg).textContent = (o.yFmt || String)(v);
    });
    xt.forEach((v) => {
      svgEl("text", { x: sx(v), y: H - m.b + 16, "text-anchor": "middle" }, svg).textContent = (o.xFmt || String)(v);
    });
    svgEl("line", { x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b, stroke: css("--axis") }, svg);
    if (o.xLabel) svgEl("text", { x: (m.l + W - m.r) / 2, y: H - 6, "text-anchor": "middle", class: "lbl" }, svg).textContent = o.xLabel;
    if (o.yLabel) svgEl("text", { x: 14, y: (m.t + H - m.b) / 2, "text-anchor": "middle", class: "lbl", transform: `rotate(-90 14 ${(m.t + H - m.b) / 2})` }, svg).textContent = o.yLabel;

    const hits = [];
    (o.layers || []).forEach((L) => {
      const col = L.color || css("--s1");
      if (L.type === "band") {
        svgEl("rect", { x: sx(L.x0), y: m.t, width: sx(L.x1) - sx(L.x0), height: H - m.b - m.t, fill: css("--band") }, svg);
        if (L.label) svgEl("text", { x: sx(L.x0) + 6, y: m.t + 14, class: "lbl" }, svg).textContent = L.label;
      } else if (L.type === "area") {
        const up = L.pts.map((p) => `${sx(p.x)},${sy(p.hi)}`), dn = L.pts.slice().reverse().map((p) => `${sx(p.x)},${sy(p.lo)}`);
        svgEl("polygon", { points: up.concat(dn).join(" "), fill: L.fill || css("--surface-2"), stroke: "none" }, svg);
      } else if (L.type === "line") {
        const d = L.pts.map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join("");
        svgEl("path", { d, fill: "none", stroke: col, "stroke-width": L.width || 2, "stroke-dasharray": L.dash || "", "stroke-linejoin": "round" }, svg);
        if (L.cross) crosshair(svg, L, sx, sy, m, H, W);
      } else if (L.type === "hline") {
        svgEl("line", { x1: m.l, x2: W - m.r, y1: sy(L.y), y2: sy(L.y), stroke: col, "stroke-width": L.width || 2, "stroke-dasharray": L.dash || "" }, svg);
        if (L.label) svgEl("text", { x: W - m.r + 6, y: sy(L.y) + 4, class: "dl" }, svg).textContent = L.label;
      } else if (L.type === "rects") {
        L.items.forEach((r) => {
          const x0 = sx(r.x0) + 1, x1 = sx(r.x1) - 1, y = sy(r.y), y0 = sy(o.y[0]);
          const h = Math.max(0, y0 - y);
          const rect = svgEl("path", { d: roundTop(x0, y, Math.max(1, x1 - x0), h, 3), fill: r.color || col }, svg);
          hits.push([rect, r.tip]);
        });
      } else if (L.type === "points") {
        L.pts.forEach((p) => {
          const c = p.color || col;
          if (p.xlo != null) svgEl("line", { x1: sx(p.xlo), x2: sx(p.xhi), y1: sy(p.y), y2: sy(p.y), stroke: c, "stroke-width": 1.2, opacity: 0.6 }, svg);
          if (p.ylo != null) svgEl("line", { x1: sx(p.x), x2: sx(p.x), y1: sy(p.ylo), y2: sy(p.yhi), stroke: c, "stroke-width": 1.2, opacity: 0.6 }, svg);
          const r = p.r || L.r || 5;
          const shape = p.shape === "sq"
            ? svgEl("rect", { x: sx(p.x) - r, y: sy(p.y) - r, width: 2 * r, height: 2 * r, rx: 1.5 }, svg)
            : svgEl("circle", { cx: sx(p.x), cy: sy(p.y), r }, svg);
          shape.setAttribute("fill", p.hollow ? css("--surface") : c);
          shape.setAttribute("stroke", p.hollow ? c : css("--surface"));
          shape.setAttribute("stroke-width", 2);
          if (p.label) svgEl("text", { x: sx(p.x) + (p.lx ?? 8), y: sy(p.y) + (p.ly ?? -7), class: "dl" }, svg).textContent = p.label;
          if (p.tip) {
            const hit = svgEl("circle", { cx: sx(p.x), cy: sy(p.y), r: 12, fill: "transparent" }, svg);
            hits.push([hit, p.tip]);
          }
        });
      }
    });
    hits.forEach(([n, html]) => {
      if (!html) return;
      n.style.cursor = "pointer";
      n.addEventListener("mousemove", (e) => showTip(e, html));
      n.addEventListener("mouseleave", hideTip);
    });
  }
  function roundTop(x, y, w, h, r) {
    r = Math.min(r, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }
  function logTicks(dom) {
    const out = [];
    for (let e = Math.floor(Math.log10(dom[0])); e <= Math.ceil(Math.log10(dom[1])); e++) {
      const v = Math.pow(10, e);
      if (v >= dom[0] * 0.999 && v <= dom[1] * 1.001) out.push(v);
    }
    return out;
  }
  function crosshair(svg, L, sx, sy, m, H, W) {
    const vline = svgEl("line", { y1: m.t, y2: H - m.b, stroke: css("--muted"), "stroke-width": 1, visibility: "hidden" }, svg);
    const dot = svgEl("circle", { r: 4, fill: L.color, stroke: css("--surface"), "stroke-width": 2, visibility: "hidden" }, svg);
    const ov = svgEl("rect", { x: m.l, y: m.t, width: W - m.l - (W - sx(L.pts[L.pts.length - 1].x)), height: H - m.b - m.t, fill: "transparent" }, svg);
    const xs = L.pts.map((p) => sx(p.x));
    ov.addEventListener("mousemove", (e) => {
      const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY;
      const loc = pt.matrixTransform(svg.getScreenCTM().inverse());
      let lo = 0, hi = xs.length - 1;
      while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (xs[mid] < loc.x) lo = mid; else hi = mid; }
      const i = Math.abs(xs[lo] - loc.x) < Math.abs(xs[hi] - loc.x) ? lo : hi;
      const p = L.pts[i];
      [vline].forEach((n) => { n.setAttribute("x1", xs[i]); n.setAttribute("x2", xs[i]); n.setAttribute("visibility", "visible"); });
      dot.setAttribute("cx", xs[i]); dot.setAttribute("cy", sy(p.y)); dot.setAttribute("visibility", "visible");
      showTip(e, L.cross(p));
    });
    ov.addEventListener("mouseleave", () => { vline.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip(); });
  }

  /* Horizontal bars with optional CI whiskers; handles negative values. */
  function hbars(el, items, o = {}) {
    if (typeof el === "string") el = $(el);
    const W = Math.max(el.clientWidth || 600, 280), rowH = o.rowH || 30, labW = o.labW || 150;
    const H = items.length * rowH + 34;
    const vals = items.flatMap((d) => [d.value, d.lo ?? d.value, d.hi ?? d.value, 0]);
    const dom = o.domain || [Math.min(...vals), Math.max(...vals)];
    const pad = (dom[1] - dom[0]) * 0.04;
    const sx = scale([dom[0] - (dom[0] < 0 ? pad : 0), dom[1] + pad], [labW, W - 56]);
    el.innerHTML = "";
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` }, el);
    const g = svgEl("g", { class: "grid" }, svg);
    niceTicks(dom[0], dom[1], Math.max(3, Math.floor((W - labW) / 90))).forEach((v) => {
      svgEl("line", { x1: sx(v), x2: sx(v), y1: 4, y2: H - 26 }, g);
      svgEl("text", { x: sx(v), y: H - 10, "text-anchor": "middle" }, svg).textContent = (o.fmt || String)(v);
    });
    svgEl("line", { x1: sx(0), x2: sx(0), y1: 4, y2: H - 26, stroke: css("--axis"), "stroke-width": 1.5 }, svg);
    items.forEach((d, i) => {
      const y = 8 + i * rowH, bh = rowH - 12, x0 = sx(0), x1 = sx(d.value ?? 0);
      svgEl("text", { x: labW - 10, y: y + bh / 2 + 4, "text-anchor": "end", class: "lbl" }, svg).textContent = d.label;
      if (d.value == null || d.value === "") {
        svgEl("text", { x: x0 + 6, y: y + bh / 2 + 4 }, svg).textContent = d.note || "n/a";
      } else {
        const x = Math.min(x0, x1), w = Math.max(1, Math.abs(x1 - x0));
        svgEl("rect", { x, y, width: w, height: bh, rx: 3, fill: d.color || css("--s1") }, svg);
      }
      if (d.lo != null && d.hi != null && d.lo !== "") {
        const cy = y + bh / 2;
        svgEl("line", { x1: sx(d.lo), x2: sx(d.hi), y1: cy, y2: cy, stroke: css("--ink"), "stroke-width": 1.3 }, svg);
        [d.lo, d.hi].forEach((v) => svgEl("line", { x1: sx(v), x2: sx(v), y1: cy - 5, y2: cy + 5, stroke: css("--ink"), "stroke-width": 1.3 }, svg));
      }
      if (d.value != null && d.value !== "") {
        // label sits right of the bar (or of the zero line for negatives), clear of the whisker
        const xe = Math.max(x0, x1, d.hi != null && d.hi !== "" ? sx(d.hi) : x1);
        const t = svgEl("text", { x: xe + 6, y: y + bh / 2 + 4, "text-anchor": "start", class: "dl" }, svg);
        t.textContent = d.text ?? (o.fmt || String)(d.value);
        if (d.bold) t.setAttribute("font-weight", "800");
      }
      const hit = svgEl("rect", { x: 0, y: y - 4, width: W, height: rowH, fill: "transparent" }, svg);
      if (d.tip) { hit.addEventListener("mousemove", (e) => showTip(e, d.tip)); hit.addEventListener("mouseleave", hideTip); }
    });
  }
  function legend(el, items) {
    if (typeof el === "string") el = $(el);
    el.innerHTML = items.map((it) => {
      const style = it.hollow ? `background:transparent;border:2px solid ${it.color}` : `background:${it.color}`;
      return `<span><i class="${it.kind || ""}" style="${style}"></i>${esc(it.label)}</span>`;
    }).join("");
  }

  /* ---------------- routing ---------------- */
  const renders = {};
  const once = {};
  function route() {
    const id = (location.hash || "#overview").slice(1);
    const page = $("#" + CSS.escape(id)) && $("#" + CSS.escape(id)).classList.contains("page") ? id : "overview";
    $$("section.page").forEach((s) => s.classList.toggle("on", s.id === page));
    $$("nav.side a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === "#" + page));
    $("#side").classList.remove("open");
    if (renders[page]) requestAnimationFrame(() => renders[page]());
    scrollTo(0, 0);
  }
  addEventListener("hashchange", route);
  let rt;
  addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { const id = (location.hash || "#overview").slice(1); if (renders[id]) renders[id](); }, 150); });
  $("#menuBtn").onclick = () => $("#side").classList.toggle("open");

  /* ---------------- static fills ---------------- */
  $$("pre[data-ev]").forEach((p) => { p.textContent = D.evidence[p.dataset.ev] || "(not captured)"; });
  // one full-width figure per section; a switch when there is more than one
  $$(".figsw").forEach((box) => {
    const figs = JSON.parse(box.dataset.figs);
    box.innerHTML = (figs.length > 1 ? `<div class="seg"></div>` : "") +
      `<figure><a target="_blank"><img loading="lazy"></a><figcaption></figcaption></figure>`;
    const show = (n) => {
      const [file, cap] = figs.find((x) => x[0] === n);
      $("img", box).src = $("a", box).href = `figures/${file}.png`;
      $("img", box).alt = cap;
      $("figcaption", box).textContent = cap + " — click to open full size";
    };
    if (figs.length > 1) seg($(".seg", box), figs.map(([n, c]) => [n, c]), figs[0][0], show);
    show(figs[0][0]);
  });

  const BL = (scope, method) => D.baseline.find((r) => r.scope === scope && r.method === method);
  const FCI = (scope, res, method, eps) => D.frontierCI.find((r) => r.scope === scope && r.resource === res && r.method === method && (eps == null || r.eps === eps));
  const gapRow = (scope, res) => D.gaps.find((g) => g.scope === scope && g.resource === res && g.comparator === "peak_capped");

  /* ---------------- overview ---------------- */
  const tile = (v, k, n) => `<div class="tile"><div class="v">${v}</div><div class="k">${k}</div>${n ? `<div class="n">${n}</div>` : ""}</div>`;
  $("#heroTiles").innerHTML =
    tile("792", "usable VMs of 1,250", "425 idle · 367 non-idle · 30 days each") +
    tile("37.6%", "vCPU-hours reclaimed by peak", "92.8% of VMs stay violation-free") +
    tile(f(gapRow("all_vms", "mem").gap_pp, 1) + " pp", "M(ε) vs peak_capped, memory", "at matched risk; CI excludes 0") +
    tile("50 / 50", "kernel verdicts reproduced", "like-for-like memory rule, 5 waves");

  const OBJ = [
    ["O1", "Analyse real VM telemetry and measure the gap between allocated and used capacity.", "Sizing to the observed peak alone frees 37.6% of vCPU-hours and 33.0% of GB-hours; 425 of 792 VMs are idle.", "#objectives"],
    ["O2", "Build a recommender that picks a flavour for each VM from its usage history.", "Peak, P95 and M(ε) recommend a catalog flavour for all 792 VMs; 125/125 tests pass.", "#outputs"],
    ["O3", "Model the probability that each resized VM runs out of resources in the next window.", "Per-VM tail model (u, ξ, θ) gives P(exhaustion); checked against 9 held-out days.", "#features"],
    ["O4", "Show reclaimed capacity against violation risk as a frontier an operator can choose from.", "ε swept 0.001–0.6, plotted against peak, peak_capped and P95 with 95% CIs.", "#comparative"],
  ];
  $("#objCards").innerHTML = OBJ.map(([o, goal, ev, link]) =>
    `<div class="card obj-card"><span class="badge good done">Achieved</span><span class="otag">${o}</span><p style="margin:8px 0 6px"><b>${goal}</b></p><div class="small">${ev} <a href="${link}">See evidence →</a></div></div>`).join("");

  const STEPS = [
    ["Restated the problem and pre-registered hypotheses H1–H3 with fixed thresholds.", ""],
    ["Surveyed recent papers into a gap table: method, dataset, metric, limitation.", ""],
    ["Downloaded the Bitbrains GWA-T-12 trace: 1,250 VMs, 5-minute samples, 30 days.", "O1"],
    ["Filtered bad files with four data-quality checks, leaving 792 usable VMs.", "O1"],
    ["Measured allocated vs used CPU and memory for every VM.", "O1"],
    ["Split each trace into 21 days to learn from and 9 days to test on.", "O2"],
    ["Built the baselines (peak, P95) and snapped every result to the flavour catalog.", "O2"],
    ["Built M(ε): tail model per VM, then the smallest flavour with risk ≤ ε.", "O2 O3"],
    ["Replayed the 9 hidden days and counted every memory and CPU violation.", "O3"],
    ["Swept ε from 0.001 to 0.6 to draw the risk frontier against the baselines.", "O4"],
    ["Ran the ablation: switched off one component at a time.", "O4"],
    ["Replayed 50 VMs on a Kubernetes cluster; the kernel agreed on 50 of 50.", ""],
    ["Added bootstrap intervals, six time splits and a second fleet (Rnd).", ""],
    ["Wrote the paper and passed all 125 tests.", ""],
  ];
  $("#steps").innerHTML = STEPS.map(([t, o]) => `<li><span class="txt">${t}</span>${o ? o.split(" ").map((x) => `<span class="otag">${x}</span>`).join(" ") : ""}</li>`).join("");

  const R = (rev, comp, marks, status, cls, link, ev) => ({ rev, comp, marks, status, cls, link, ev });
  const rubric = [
    R("1", "Literature survey", 3, "Complete", "good", "#literature", `${D.gap.filter((r) => !/pre-2021/.test(r.Paper)).length} papers from 2021+ (plus ${D.gap.filter((r) => /pre-2021/.test(r.Paper)).length} pre-2021 anchors) in a gap table: paper, method, dataset, metric, limitation`),
    R("1", "Problem statement & hypothesis", 2, "Complete", "good", "#hypothesis", "Restated problem; H1–H3 in the form “X reduces M by ≥N% vs B under C”"),
    R("1", "Testbed / environment evidence", 3, "Complete", "good", "#testbed", "kubectl get nodes / pods / describe captures, OOMKilled pods, image build"),
    R("1", "Baseline reproduced", 2, "Complete", "good", "#baseline", "Peak and P95 on 792 VMs, with numbers and bootstrap CIs"),
    R("2", "Objectives O1 and O2 evidenced", 4, "Complete", "good", "#objectives", "Utilisation-gap analysis (O1); flavour recommender (O2); Git history; 125/125 tests"),
    R("2", "Comparative results vs baseline", 3, "Complete", "good", "#comparative", "Interactive frontier: M(ε) and every baseline on the same axes, with CIs"),
    R("2", "Ablation or parameter sweep", 2, "Complete", "good", "#ablation", "7 arms, one switch each; ε sweep; paired bootstrap shifts"),
    R("2", "Threats to validity", 1, "Complete", "good", "#threats", "Construct / internal / external / conclusion, with bias direction"),
    R("3", "All objectives evidenced", 3, "Ready", "good", "#overview", "O1–O4; 2 fleets; 6 splits; mean and SD across splits"),
    R("3", "Statistical soundness", 2, "Ready", "good", "#stats", "95% bootstrap CIs (B = 10,000), Wilson intervals, paired comparisons"),
    R("3", "Live demonstration", 2, "Ready", "good", "#demo", "run.bat demo / demo-vm / test run live"),
    R("3", "Documentation of findings", 3, "Paper ready", "good", "#demo", "Paper submitted separately; demo video still to record"),
  ];
  renderTable("#rubricTable", rubric, [
    { k: "rev", label: "Review" }, { k: "comp", label: "Component", fmt: (v, r) => `<a href="${r.link}">${esc(v)}</a>` },
    { k: "marks", label: "Marks", num: true }, { k: "status", label: "Status", fmt: (v, r) => `<span class="badge ${r.cls}">${v}</span>` },
    { k: "ev", label: "Evidence on this site" },
  ]);

  /* ---------------- literature ---------------- */
  function drawGap() {
    const q = $("#gapSearch").value.toLowerCase();
    const rows = D.gap.filter((r) => !q || Object.values(r).join(" ").toLowerCase().includes(q));
    $("#gapCount").textContent = `${rows.length} of ${D.gap.length} papers`;
    renderTable("#gapTable", rows, [
      { k: "Ref" }, { k: "Paper" }, { k: "Method" }, { k: "Dataset / Testbed", label: "Dataset / testbed" }, { k: "Metric" }, { k: "Limitation" },
    ]);
  }
  $("#gapSearch").oninput = drawGap; drawGap();

  /* ---------------- testbed ---------------- */
  $("#testbedTiles").innerHTML = tile("1", "kind node, Ready", "v1.33.1 control plane") + tile("50", "VMs replayed as pods", "5 sequential waves, S = 1/16") +
    tile("26", "pods OOMKilled by kernel", "0 evictions") + tile("+80 MiB", "agent margin per pod", "limit = recommendation + margin");

  /* ---------------- baseline ---------------- */
  const MCOL = { peak: "--s3", peak_capped: "--s3", p95: "--s2", evt: "--s1", as_provisioned: "--muted" };
  const MNAME = { as_provisioned: "As-provisioned (B0)", peak: "Peak (B1)", p95: "P95 (B2)", peak_capped: "peak_capped", evt: "M(ε)" };
  let baseScope = "all_vms";
  seg("#baseScope", [["all_vms", "All 792 VMs"], ["non_idle", "Non-idle 367 VMs"]], baseScope, (v) => { baseScope = v; renders.baseline(); });
  renders.baseline = () => {
    const items = [], safe = [];
    ["peak", "p95"].forEach((m) => ["cpu", "mem"].forEach((res) => {
      const r = FCI(baseScope, res, m);
      const name = `${MNAME[m]} · ${res === "cpu" ? "vCPU-h" : "GB-h"}`;
      items.push({ label: name, value: r.recl_pct, lo: r.recl_lo, hi: r.recl_hi, color: css(MCOL[m]), text: f(r.recl_pct) + "%",
        tip: `<b>${name}</b><br>${f(r.recl_pct, 2)}% reclaimed<br>95% CI [${f(r.recl_lo, 2)}, ${f(r.recl_hi, 2)}]` });
      safe.push({ label: `${MNAME[m]} · ${res === "cpu" ? "CPU" : "memory"}`, value: 100 * (1 - r.viol_frac), lo: 100 * (1 - r.viol_hi), hi: 100 * (1 - r.viol_lo),
        color: css(MCOL[m]), text: f(100 * (1 - r.viol_frac)) + "%",
        tip: `<b>${MNAME[m]}, ${res}</b><br>${r.vms - r.vms_with_violation} of ${r.vms} VMs violation-free<br>95% CI [${f(100 * (1 - r.viol_hi))}, ${f(100 * (1 - r.viol_lo))}]%` });
    }));
    hbars("#baseReclChart", items, { fmt: (v) => v + "%", domain: [0, 80], labW: 140 });
    hbars("#baseSafeChart", safe, { fmt: (v) => v + "%", domain: [0, 100], labW: 140 });
  };
  renderTable("#baseTable", D.baseline, [
    { k: "scope" }, { k: "method", fmt: (v) => esc(MNAME[v] || v) }, { k: "vms", label: "VMs", num: true },
    { k: "recl_vcpu_hours", label: "Reclaimed vCPU-h", num: true, fmt: big }, { k: "recl_vcpu_pct", label: "%", num: true, fmt: (v) => f(v, 2) },
    { k: "recl_mem_gb_hours", label: "Reclaimed GB-h", num: true, fmt: big }, { k: "recl_mem_pct", label: "%", num: true, fmt: (v) => f(v, 2) },
    { k: "cpu_events", label: "CPU events", num: true }, { k: "mem_events", label: "Mem events", num: true },
    { k: "exhausted_minutes", label: "Exhausted min", num: true, fmt: big }, { k: "vms_with_violation", label: "VMs violating", num: true },
    { k: "pct_vms_safe", label: "% safe", num: true, fmt: (v) => f(v, 1) },
  ]);

  /* ---------------- raw input ---------------- */
  const rawIds = Object.keys(D.rawVms || {});
  let rawVm = rawIds.includes("1226") ? "1226" : rawIds[0], rawRes = "mem";
  if (rawIds.length) {
    seg("#rawVmSeg", rawIds.map((v) => [v, `VM ${v}`]), rawVm, (v) => { rawVm = v; renders.raw(); });
    seg("#rawResSeg", [["mem", "Memory"], ["cpu", "CPU"]], rawRes, (v) => { rawRes = v; renders.raw(); });
  }
  renders.raw = () => {
    if (!rawIds.length) { $("#rawChart").textContent = "Raw demo files not present (data/demo_input)."; return; }
    const v = D.rawVms[rawVm];
    const pol = D.demoPolicies.filter((p) => String(p.vm_id) === rawVm);
    const perVmOrig = D.perVm.find((r) => String(r.vm_id) === rawVm && r.method === "as_provisioned");
    const orig = rawRes === "cpu" ? v.cores : (perVmOrig ? perVmOrig.orig_mem_gb : v.mem_cap_gb);
    // raw rows are ";"+TAB delimited; split and trim into a table
    const cells = (ln) => ln.split(";").map((c) => c.trim());
    const hdr = cells(v.head[0]);
    renderTable("#rawHead", v.head.slice(1).map((ln) => Object.fromEntries(cells(ln).map((c, i) => [hdr[i], c]))),
      hdr.map((k) => ({ k, num: true })));
    $("#rawTiles").innerHTML = tile(big(v.samples), "raw samples", "every 300 s") + tile(f(v.span_days, 1) + " d", "trace span", "21 d observe · 9 d evaluate") +
      tile(v.cores + " vCPU", "provisioned CPU") + tile(f(orig && rawRes === "mem" ? orig : v.mem_cap_gb, 1) + " GB", "provisioned memory");
    const idx = rawRes === "cpu" ? 1 : 2, unit = rawRes === "cpu" ? "cores" : "GB";
    const pts = v.series.map((s) => ({ x: s[0], y: s[idx] }));
    const al = (name) => { const p = pol.find((q) => q.policy === name); return p ? p[rawRes + "_alloc"] : null; };
    const refs = [["peak", "--s3", "peak"], ["p95", "--s2", "P95"], ["M(0.01)", "--s1", "M(0.01)"]].map(([n, c, l]) => ({ y: al(n), color: css(c), label: l })).filter((r) => r.y != null);
    const ymax = Math.max(orig, ...pts.map((p) => p.y), ...refs.map((r) => r.y)) * 1.08;
    $("#rawChartTitle").textContent = `VM ${rawVm}: ${rawRes === "cpu" ? "CPU demand (cores)" : "memory demand (GB)"} over 30 days`;
    legend("#rawLegend", [{ label: "Demand", color: css("--ink-2"), kind: "ln" }, { label: "Original allocation", color: css("--muted"), kind: "ln" }]
      .concat(refs.map((r) => ({ label: r.label + " recommendation", color: r.color, kind: "ln" }))));
    // one line per distinct allocation, so coinciding recommendations share a label
    const groups = [];
    refs.forEach((r) => { const g = groups.find((x) => x.y === r.y); if (g) g.label += "/" + r.label; else groups.push(Object.assign({}, r)); });
    plot("#rawChart", {
      height: 360, right: 70, x: [0, Math.ceil(v.span_days)], y: [0, ymax], xLabel: "Day of trace", yLabel: unit,
      xFmt: (d) => d + " d", yFmt: (y) => (+y.toFixed(2)).toString(),
      layers: [
        { type: "band", x0: 21, x1: v.span_days, label: "Evaluation window (held out)" },
        { type: "line", pts, color: css("--ink-2"), width: 1.2, cross: (p) => `<b>Day ${f(p.x, 2)}</b><br>${f(p.y, 3)} ${unit}` },
        { type: "hline", y: orig, color: css("--muted"), dash: "5 4", width: 1.5, label: "orig" },
        ...groups.map((r) => ({ type: "hline", y: r.y, color: r.color, width: 2, label: r.label })),
      ],
    });
    renderTable("#rawPolicyTable", pol, [
      { k: "policy" }, { k: "cpu_alloc", label: "vCPU", num: true }, { k: "mem_alloc", label: "GB", num: true },
      { k: "cpu_required", label: "needs CPU", num: true, fmt: (x) => f(x, 2) }, { k: "mem_required", label: "needs GB", num: true, fmt: (x) => f(x, 2) },
      { k: "mem_pred_p", label: "pred P(mem)", num: true, fmt: (x) => f(x, 4) },
      { k: "cpu_events", label: "CPU events", num: true }, { k: "mem_events", label: "Mem events", num: true },
      { k: "cpu_recl_hours", label: "recl vCPU-h", num: true }, { k: "mem_recl_hours", label: "recl GB-h", num: true },
      { k: "mem_events", label: "Outcome", fmt: (x, r) => (r.cpu_events + r.mem_events ? `<span class="badge bad">violated</span>` : `<span class="badge good">safe</span>`) },
    ]);
  };

  /* ---------------- processed ---------------- */
  const cascade = [["Trace files", 1250], ["Bad CPU capacity", -103], ["Below catalog floor (< 2 GB)", -347], ["Capacity changes mid-trace", -2], ["Too short", -6], ["Usable VMs", 792]];
  $("#cascadeTiles").innerHTML = tile("1,250", "trace files") + tile("458", "excluded by gates", "103 + 347 + 2 + 6") + tile("792", "usable VMs", "30.0 days each") + tile("425 / 367", "idle / non-idle", "idle: P99 < 5% of allocation");
  renders.processed = () => {
    hbars("#cascadeChart", cascade.map(([l, v], i) => ({ label: l, value: Math.abs(v), color: v < 0 ? css("--s2") : css("--s1"), text: (v < 0 ? "−" : "") + big(Math.abs(v)) })), { fmt: big, labW: 210 });
  };
  function drawDs() {
    const q = $("#dsSearch").value.trim().toLowerCase();
    const rows = D.dataset.filter((r) => !q || String(r.file).toLowerCase().includes(q));
    $("#dsCount").textContent = `${rows.length} of ${D.dataset.length} files` + (rows.length > 300 ? " (first 300 shown)" : "");
    renderTable("#dsTable", rows, [
      { k: "file" }, { k: "samples", num: true }, { k: "interval_s", label: "interval s", num: true }, { k: "span_days", label: "days", num: true, fmt: (v) => f(v, 1) },
      { k: "cores", num: true }, { k: "mem_cap_gb", label: "mem GB", num: true, fmt: (v) => f(v, 2) },
      { k: "cpu_mean_util", label: "CPU mean", num: true, fmt: (v) => pct(v) }, { k: "cpu_p95_util", label: "CPU p95", num: true, fmt: (v) => pct(v) },
      { k: "cpu_max_util", label: "CPU max", num: true, fmt: (v) => pct(v) },
      { k: "mem_mean_util", label: "mem mean", num: true, fmt: (v) => pct(v) }, { k: "mem_p95_util", label: "mem p95", num: true, fmt: (v) => pct(v) },
      { k: "mem_max_util", label: "mem max", num: true, fmt: (v) => pct(v) },
      { k: "cpu_over_100", label: "flags", fmt: (v, r) => [r.cpu_over_100 === "True" && "CPU>100%", r.mem_pinned === "True" && "mem pinned", r.mem_near_zero === "True" && "mem≈0"].filter(Boolean).join(", ") },
    ], { limit: 300 });
  }
  $("#dsSearch").oninput = drawDs; drawDs();

  /* ---------------- features ---------------- */
  const med = (a) => { const s = a.filter((x) => typeof x === "number").sort((x, y) => x - y); const h = s.length >> 1; return !s.length ? NaN : s.length % 2 ? s[h] : (s[h - 1] + s[h]) / 2; };
  const F = D.features;
  const xiC = F.map((r) => r.cpu_xi).filter((x) => typeof x === "number"), xiM = F.map((r) => r.mem_xi).filter((x) => typeof x === "number");
  const est = (k) => F.filter((r) => r[k + "_est"]);
  $("#featTiles").innerHTML = tile(f(med(est("cpu").map((r) => r.cpu_theta)), 3), "median θ, CPU", `${est("cpu").length} estimated · trials 2,592 → ` + big(med(est("cpu").map((r) => r.cpu_trials)))) +
    tile(f(med(est("mem").map((r) => r.mem_theta)), 3), "median θ, memory", `${est("mem").length} estimated · trials 2,592 → ` + big(med(est("mem").map((r) => r.mem_trials)))) +
    tile(`${xiC.filter((x) => x >= 1).length} / ${xiC.length}`, "CPU fits with ξ ≥ 1", "infinite-mean tail") +
    tile(`${xiM.filter((x) => x >= 1).length} / ${xiM.length}`, "memory fits with ξ ≥ 1", "CPU falls back to peak on 140 VMs");
  let featRes = "cpu";
  seg("#featResSeg", [["cpu", "CPU"], ["mem", "Memory"]], featRes, (v) => { featRes = v; renders.features(); });
  function hist(el, vals, lo, hi, nb, color, fmtX, label) {
    const w = (hi - lo) / nb, bins = Array(nb).fill(0);
    vals.forEach((v) => { const i = Math.min(nb - 1, Math.max(0, Math.floor((Math.min(Math.max(v, lo), hi - 1e-9) - lo) / w))); bins[i]++; });
    plot(el, { height: 240, x: [lo, hi], y: [0, Math.max(...bins) * 1.1], xLabel: label, yLabel: "VMs", xFmt: fmtX,
      layers: [{ type: "rects", items: bins.map((c, i) => ({ x0: lo + i * w, x1: lo + (i + 1) * w, y: c, color, tip: `<b>${f(lo + i * w, 2)} – ${f(lo + (i + 1) * w, 2)}</b><br>${c} VMs` })) }] });
  }
  renders.features = () => {
    const col = featRes === "cpu" ? css("--s1") : css("--s2");
    const n1 = F.length - est(featRes).length;
    hist("#thetaHist", est(featRes).map((r) => r[featRes + "_theta"]), 0, 1, 20, col, (v) => v.toFixed(1),
      `θ (${featRes}), estimated only` + (n1 ? ` — ${n1} VMs not fitted, θ = 1 used` : ""));
    hist("#xiHist", featRes === "cpu" ? xiC : xiM, -2, 3, 25, col, (v) => v.toFixed(0), `ξ (${featRes})`);
  };
  function drawFeat() {
    const q = $("#featSearch").value.trim();
    const rows = F.filter((r) => !q || String(r.vm_id).startsWith(q));
    $("#featCount").textContent = `${rows.length} VMs`;
    renderTable("#featTable", rows, [
      { k: "vm_id", label: "VM", num: true }, { k: "idle", fmt: (v) => (v ? "idle" : "") },
      { k: "cpu_u", label: "CPU u", num: true, fmt: (v) => f(v, 4) }, { k: "cpu_xi", label: "CPU ξ", num: true, fmt: (v) => f(v, 3) },
      { k: "cpu_theta", label: "CPU θ", num: true, fmt: (v) => f(v, 3) }, { k: "cpu_nexc", label: "CPU exceed", num: true },
      { k: "cpu_trials", label: "CPU trials", num: true, fmt: (v) => f(v, 0) },
      { k: "mem_u", label: "mem u", num: true, fmt: (v) => f(v, 4) }, { k: "mem_xi", label: "mem ξ", num: true, fmt: (v) => f(v, 3) },
      { k: "mem_theta", label: "mem θ", num: true, fmt: (v) => f(v, 3) }, { k: "mem_nexc", label: "mem exceed", num: true },
      { k: "mem_trials", label: "mem trials", num: true, fmt: (v) => f(v, 0) },
    ], { limit: 400 });
  }
  $("#featSearch").oninput = drawFeat; drawFeat();

  /* ---------------- outputs ---------------- */
  const outMethods = [...new Set(D.perVm.map((r) => r.method))];
  let outMethod = outMethods.includes("p95") ? "p95" : outMethods[0];
  seg("#outMethod", outMethods.map((m) => [m, MNAME[m] || m]), outMethod, (v) => { outMethod = v; drawOut(); });
  function drawOut() {
    const q = $("#outSearch").value.trim(), vo = $("#outViol").checked;
    const rows = D.perVm.filter((r) => r.method === outMethod && (!q || String(r.vm_id).startsWith(q)) && (!vo || r.any_violation === "True"));
    $("#outCount").textContent = `${rows.length} VMs` + (rows.length > 400 ? " (first 400 shown)" : "");
    renderTable("#outTable", rows, [
      { k: "vm_id", label: "VM", num: true }, { k: "idle", fmt: (v) => (v === "True" ? "idle" : "") },
      { k: "orig_cores", label: "orig vCPU", num: true }, { k: "orig_mem_gb", label: "orig GB", num: true, fmt: (v) => f(v, 1) },
      { k: "alloc_cores", label: "rec vCPU", num: true }, { k: "alloc_mem_gb", label: "rec GB", num: true, fmt: (v) => f(v, 0) },
      { k: "recl_vcpu_hours", label: "recl vCPU-h", num: true, fmt: big }, { k: "recl_mem_gb_hours", label: "recl GB-h", num: true, fmt: big },
      { k: "cpu_events", label: "CPU ev", num: true }, { k: "cpu_exhausted_minutes", label: "CPU min", num: true },
      { k: "mem_events", label: "mem ev", num: true }, { k: "mem_exhausted_minutes", label: "mem min", num: true },
      { k: "any_violation", label: "outcome", fmt: (v) => (v === "True" ? `<span class="badge bad">violated</span>` : `<span class="badge good">safe</span>`) },
    ], { limit: 400 });
  }
  $("#outSearch").oninput = drawOut; $("#outViol").onchange = drawOut; drawOut();

  /* ---------------- objectives ---------------- */
  const pk = BL("all_vms", "peak");
  const allocV = pk.recl_vcpu_hours / (pk.recl_vcpu_pct / 100), allocM = pk.recl_mem_gb_hours / (pk.recl_mem_pct / 100);
  $("#o1Tiles").innerHTML = tile(big(allocV), "vCPU-hours allocated", "792 VMs over the 9-day evaluation window") +
    tile(big(allocM), "GB-hours allocated", "same window") +
    tile(f(pk.recl_vcpu_pct, 1) + "% / " + f(pk.recl_mem_pct, 1) + "%", "never needed (CPU / mem)", "peak rule, snapped to catalog") +
    tile(pct(med(D.dataset.map((r) => r.mem_p95_util)), 0), "median memory P95 utilisation", "across all 1,250 trace files");
  const flavMethods = outMethods.filter((m) => m !== "as_provisioned");
  let flavMethod = flavMethods.includes("p95") ? "p95" : flavMethods[0], flavRes = "mem";
  seg("#flavMethod", flavMethods.map((m) => [m, MNAME[m] || m]), flavMethod, (v) => { flavMethod = v; renders.objectives(); });
  seg("#flavRes", [["mem", "Memory flavour"], ["cpu", "vCPU flavour"]], flavRes, (v) => { flavRes = v; renders.objectives(); });
  renders.objectives = () => {
    const cat = flavRes === "cpu" ? [1, 2, 4, 8, 16] : [2, 4, 8, 16, 32, 64];
    const ko = flavRes === "cpu" ? "orig_cores" : "orig_mem_gb", ka = flavRes === "cpu" ? "alloc_cores" : "alloc_mem_gb";
    const rows = D.perVm.filter((r) => r.method === flavMethod);
    const snapUp = (x) => cat.find((c) => c >= x - 1e-9) ?? `>${cat[cat.length - 1]}`;
    const cO = cat.map((c) => rows.filter((r) => snapUp(r[ko]) === c).length), cA = cat.map((c) => rows.filter((r) => +r[ka] === c).length);
    $("#flavTitle").textContent = `VMs per ${flavRes === "cpu" ? "vCPU" : "memory"} flavour: original (snapped) vs ${MNAME[flavMethod] || flavMethod}`;
    const items = [];
    cat.forEach((c, i) => {
      const u = flavRes === "cpu" ? " vCPU" : " GB";
      items.push({ x0: i + 0.08, x1: i + 0.5, y: cO[i], color: css("--s1"), tip: `<b>${c}${u}</b><br>original: ${cO[i]} VMs` });
      items.push({ x0: i + 0.5, x1: i + 0.92, y: cA[i], color: css("--s2"), tip: `<b>${c}${u}</b><br>${MNAME[flavMethod] || flavMethod}: ${cA[i]} VMs` });
    });
    const el = $("#flavChart");
    plot(el, { height: 280, x: [0, cat.length], y: [0, Math.max(...cO, ...cA) * 1.12], yLabel: "VMs",
      xTicks: cat.map((_, i) => i + 0.5), xFmt: (v) => cat[Math.round(v - 0.5)] + (flavRes === "cpu" ? "" : " GB"), layers: [{ type: "rects", items }] });
    el.insertAdjacentHTML("afterbegin", `<div class="legend"><span><i class="sq" style="background:${css("--s1")}"></i>Original allocation</span><span><i class="sq" style="background:${css("--s2")}"></i>${esc(MNAME[flavMethod] || flavMethod)} recommendation</span></div>`);
  };
  const srcLines = D.sources.reduce((a, s) => a + s.lines, 0);
  $("#codeTiles").innerHTML = tile(D.git.length, "commits in Git history", D.git.length ? `${D.git[D.git.length - 1].date} → ${D.git[0].date}` : "") +
    tile(D.sources.length, "Python source + test files", big(srcLines) + " lines") + tile("125 / 125", "tests passing", "8 test files, 0 skipped");
  renderTable("#testTable", [["test_baselines.py", 35, "violation rules, catalog snapping, scoring"], ["test_evt.py", 41, "GPD fit, extremal index, ε → allocation"],
    ["test_bootstrap.py", 16, "paired resampling, undefined-share handling"], ["test_hypotheses.py", 13, "H1–H3 statistics and verdict logic"],
    ["test_multisplit.py", 9, "split planning and drift"], ["test_demo_guard.py", 4, "timestamp-unit guard"], ["test_demo_vm.py", 4, "per-VM walkthrough"],
    ["test_replay_agent.py", 3, "in-container replay agent"]].map(([file, n, what]) => ({ file, n, what })), [
    { k: "file", label: "File", fmt: (v) => `<code>${v}</code>` }, { k: "n", label: "Result", num: true, fmt: (v) => `<span class="badge good">${v}/${v} passed</span>` }, { k: "what", label: "Covers" }]);
  renderTable("#srcTable", D.sources, [{ k: "path", label: "File", fmt: (v) => `<code>${esc(v)}</code>` }, { k: "lines", label: "Lines", num: true }]);
  renderTable("#gitTable", D.git, [{ k: "hash", fmt: (v) => `<code>${v}</code>` }, { k: "date" }, { k: "subject" }]);

  /* ---------------- comparative ---------------- */
  let frRes = "cpu", frScope = "all_vms";
  seg("#frRes", [["cpu", "CPU (vCPU-h)"], ["mem", "Memory (GB-h)"]], frRes, (v) => { frRes = v; renders.comparative(); });
  seg("#frScope", [["all_vms", "All VMs"], ["non_idle", "Non-idle"]], frScope, (v) => { frScope = v; renders.comparative(); });
  const pointTip = (r) => `<b>${r.method === "evt" ? "M(ε = " + r.eps + ")" : MNAME[r.method]}</b><br>reclaimed ${f(r.recl_pct, 2)}% [${f(r.recl_lo, 1)}, ${f(r.recl_hi, 1)}]<br>VMs violating ${r.vms_with_violation} of ${r.vms} (${pct(r.viol_frac, 2)})<br>risk CI [${pct(r.viol_lo, 2)}, ${pct(r.viol_hi, 2)}]`;
  renders.comparative = () => {
    const rows = D.frontierCI.filter((r) => r.scope === frScope && r.resource === frRes);
    const evt = rows.filter((r) => r.method === "evt").sort((a, b) => a.eps - b.eps);
    const P = (r, extra) => Object.assign({ x: 100 * r.viol_frac, y: r.recl_pct, xlo: 100 * r.viol_lo, xhi: 100 * r.viol_hi, ylo: r.recl_lo, yhi: r.recl_hi, tip: pointTip(r) }, extra);
    const base = ["as_provisioned", "peak", "peak_capped", "p95"].map((m) => rows.find((r) => r.method === m)).filter(Boolean);
    const xmax = Math.max(...rows.map((r) => 100 * r.viol_hi)) * 1.05, ymax = Math.min(100, Math.max(...rows.map((r) => r.recl_hi)) * 1.08);
    legend("#frLegend", [{ label: "M(ε), proposed (ε sweep)", color: css("--s1") }, { label: "P95 (B2)", color: css("--s2"), kind: "sq" },
      { label: "peak_capped", color: css("--s3") }, { label: "Peak (B1)", color: css("--s3"), hollow: true }, { label: "As-provisioned (B0)", color: css("--muted") }]);
    plot("#frChart", {
      height: 420, x: [0, xmax], y: [0, ymax], xLabel: "Measured risk: % of VMs with ≥1 violation in held-out 9 days  (lower is better)",
      yLabel: "Reclaimed capacity, %", xFmt: (v) => v + "%", yFmt: (v) => v + "%",
      layers: [
        { type: "line", pts: evt.map((r) => ({ x: 100 * r.viol_frac, y: r.recl_pct })), color: css("--s1") },
        { type: "points", pts: evt.map((r, i) => P(r, { color: css("--s1"), label: [0, 3, evt.length - 1].includes(i) ? "ε=" + r.eps : "", ly: 16, lx: 6 })) },
        { type: "points", pts: base.map((r) => P(r, {
          color: css(MCOL[r.method]), hollow: r.method === "peak", shape: r.method === "p95" ? "sq" : "", r: r.method === "peak" ? 7 : 5,
          label: { p95: "P95", peak_capped: "peak_capped", peak: "", as_provisioned: "B0" }[r.method], lx: r.method === "peak_capped" ? -84 : 8 })) },
      ],
    });
  };
  renderTable("#gapCiTable", D.gaps, [
    { k: "scope" }, { k: "resource" }, { k: "gap_pp", label: "Gap (pp)", num: true, fmt: (v) => `<b>${f(v, 2)}</b>` },
    { k: "gap_lo", label: "95% CI", num: true, fmt: (v, r) => (r.gap_lo !== "" ? `[${f(r.gap_lo, 2)}, ${f(r.gap_hi, 2)}]` : `conditional [${f(r.cond_lo, 2)}, ${f(r.cond_hi, 2)}]`) },
    { k: "undefined_share", label: "Undefined share", num: true },
    { k: "excludes_zero", label: "Excludes 0?", fmt: (v, r) => ((v === "True" || r.cond_excludes_zero === "True") ? "yes" : "no") },
  ]);

  /* ---------------- ablation ---------------- */
  const ARMS = { theta_off: "θ off (no clustering correction)", theta_runs: "θ by runs declustering", snap_off: "No catalog snapping †", cap_off: "No cap at original size", u_q90: "Threshold u = q0.90", u_q98: "Threshold u = q0.98" };
  let abCell = "all_vms|mem";
  seg("#abCell", [["all_vms|cpu", "CPU · all"], ["non_idle|cpu", "CPU · non-idle"], ["all_vms|mem", "Memory · all"], ["non_idle|mem", "Memory · non-idle"]], abCell, (v) => { abCell = v; renders.ablation(); });
  renders.ablation = () => {
    const [sc, res] = abCell.split("|");
    const items = D.ablationShifts.filter((r) => r.scope === sc && r.resource === res).map((r) => {
      const strict = r.shift_lo !== "", lo = strict ? r.shift_lo : r.cond_lo, hi = strict ? r.shift_hi : r.cond_hi;
      const excl = strict ? r.excludes_zero === "True" : r.cond_excludes_zero === "True";
      return { label: ARMS[r.arm] || r.arm, value: r.shift_vs_reference_pp === "" ? null : r.shift_vs_reference_pp, lo, hi, bold: excl,
        color: css("--s1"), text: (r.shift_vs_reference_pp === "" ? "n/a" : f(r.shift_vs_reference_pp, 2)) + (strict ? "" : " c"),
        tip: `<b>${esc(ARMS[r.arm])}</b><br>shift ${f(r.shift_vs_reference_pp, 2)} pp<br>${strict ? "95% CI" : "conditional CI"} [${f(lo, 2)}, ${f(hi, 2)}]${strict ? "" : `<br>undefined in ${pct(r.undefined_share, 1)} of resamples`}<br>${excl ? "<b>excludes zero</b>" : "contains zero"}` };
    });
    hbars("#abChart", items, { fmt: (v) => v + "", labW: 220, rowH: 34 });
  };

  /* ---------------- stats ---------------- */
  renderTable("#splitPlan", D.splitPlan, [{ k: "split" }, { k: "obs_days", label: "obs days", num: true }, { k: "eval_days", label: "eval days", num: true },
    { k: "obs_start", label: "obs start" }, { k: "obs_end", label: "obs end" }, { k: "eval_start", label: "eval start" }, { k: "eval_end", label: "eval end" }]);
  const cellName = (r) => `${r.resource === "cpu" ? "CPU" : "Memory"} · ${r.scope === "all_vms" ? "all" : "non-idle"}`;
  renders.stats = () => {
    const rows = D.splitGaps.filter((r) => r.config === "reference");
    legend("#splitLegend", [{ label: "14-day observation splits A0–A3", color: css("--s1") }, { label: "Splits B7 / B14", color: css("--s2"), kind: "sq" }, { label: "Headline 21/9 split", color: css("--ink"), kind: "sq" }]);
    const el = $("#splitChart");
    const W = Math.max(el.clientWidth, 280), rowH = 44, labW = 130, H = rows.length * rowH + 36;
    const vals = rows.flatMap((r) => ["A0", "A1", "A2", "A3", "B7", "B14", "B21"].map((k) => r[k]).filter((v) => v !== ""));
    const lo = Math.min(...vals, 0) - 0.5, hi = 0.5;
    const sx = scale([lo, hi], [labW, W - 20]);
    el.innerHTML = "";
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` }, el);
    const g = svgEl("g", { class: "grid" }, svg);
    niceTicks(lo, hi, 6).forEach((v) => { svgEl("line", { x1: sx(v), x2: sx(v), y1: 4, y2: H - 28 }, g); svgEl("text", { x: sx(v), y: H - 12, "text-anchor": "middle" }, svg).textContent = v + " pp"; });
    svgEl("line", { x1: sx(0), x2: sx(0), y1: 4, y2: H - 28, stroke: css("--axis"), "stroke-width": 1.5 }, svg);
    rows.forEach((r, i) => {
      const y = 22 + i * rowH;
      svgEl("text", { x: labW - 10, y: y + 4, "text-anchor": "end", class: "lbl" }, svg).textContent = cellName(r);
      [["A0", "--s1"], ["A1", "--s1"], ["A2", "--s1"], ["A3", "--s1"], ["B7", "--s2"], ["B14", "--s2"], ["headline_21_9", "--ink"]].forEach(([k, c], j) => {
        if (r[k] === "") return;
        const sq = k[0] !== "A";
        const n = sq ? svgEl("rect", { x: sx(r[k]) - 5, y: y - 5 + (k === "headline_21_9" ? 0 : (j % 2 ? 5 : -5)), width: 10, height: 10, rx: 1.5 }, svg)
          : svgEl("circle", { cx: sx(r[k]), cy: y + (j % 2 ? 5 : -5), r: 5 }, svg);
        n.setAttribute("fill", css(c)); n.setAttribute("stroke", css("--surface")); n.setAttribute("stroke-width", 2);
        n.addEventListener("mousemove", (e) => showTip(e, `<b>${cellName(r)}</b><br>split ${k === "headline_21_9" ? "21/9 (headline)" : k}: ${f(r[k], 2)} pp`));
        n.addEventListener("mouseleave", hideTip);
      });
    });
    const rr = D.rndFrontier.filter((r) => r.scope === "all_vms" && r.resource === rndRes);
    const line = (key) => rr.filter((r) => r.method === "evt").sort((a, b) => a.eps - b.eps).map((r) => ({ x: 100 * r["viol_frac_" + key], y: r["recl_pct_" + key], eps: r.eps }));
    const fs = line("fastStorage"), rn = line("rnd2013-8");
    const bp = (key, color) => ["peak_capped", "p95"].map((m) => rr.find((r) => r.method === m)).filter(Boolean).map((r) => ({
      x: 100 * r["viol_frac_" + key], y: r["recl_pct_" + key], color, shape: r.method === "p95" ? "sq" : "", label: r.method === "p95" ? "P95" : "peak_c",
      tip: `<b>${MNAME[r.method]}, ${key}</b><br>risk ${f(100 * r["viol_frac_" + key], 2)}% · reclaimed ${f(r["recl_pct_" + key], 2)}%` }));
    legend("#rndLegend", [{ label: "fastStorage (792 VMs)", color: css("--s1") }, { label: "Rnd Aug 2013 (316 VMs)", color: css("--s2") }]);
    const pts = fs.concat(rn, bp("fastStorage", ""), bp("rnd2013-8", ""));
    plot("#rndChart", { height: 340, x: [0, Math.max(...pts.map((p) => p.x)) * 1.08], y: [0, Math.max(...pts.map((p) => p.y)) * 1.1],
      xLabel: "Measured risk, % of VMs violating (all VMs)", yLabel: "Reclaimed, %", xFmt: (v) => v + "%", yFmt: (v) => v + "%",
      layers: [
        { type: "line", pts: fs, color: css("--s1") }, { type: "line", pts: rn, color: css("--s2") },
        { type: "points", pts: fs.map((p) => Object.assign({}, p, { color: css("--s1"), tip: `<b>M(ε=${p.eps}), fastStorage</b><br>risk ${f(p.x, 2)}% · reclaimed ${f(p.y, 2)}%` })) },
        { type: "points", pts: rn.map((p) => Object.assign({}, p, { color: css("--s2"), tip: `<b>M(ε=${p.eps}), Rnd</b><br>risk ${f(p.x, 2)}% · reclaimed ${f(p.y, 2)}%` })) },
        { type: "points", pts: bp("fastStorage", css("--s1")).concat(bp("rnd2013-8", css("--s2"))).map((p) => Object.assign(p, { hollow: true, r: 6 })) },
      ] });
  };
  let rndRes = "cpu";
  seg("#rndRes", [["cpu", "CPU"], ["mem", "Memory"]], rndRes, (v) => { rndRes = v; renders.stats(); });
  renderTable("#splitTable", D.splitGaps, [{ k: "config" }, { k: "scope" }, { k: "resource" },
    ...["A0", "A1", "A2", "A3"].map((k) => ({ k, num: true, fmt: (v) => f(v, 2) })), { k: "A_mean", label: "A mean", num: true, fmt: (v) => f(v, 2) }, { k: "A_sd", label: "A SD", num: true, fmt: (v) => f(v, 2) },
    ...["B7", "B14", "B21"].map((k) => ({ k, num: true, fmt: (v) => f(v, 2) })), { k: "headline_21_9", label: "21/9", num: true, fmt: (v) => `<b>${f(v, 2)}</b>` }]);

  renderTable("#rndH", [
    ["Usable VMs", "792", "316"],
    ["Gap vs peak_capped, CPU all (pp)", "−5.56", "−4.35 (c [−8.06, −2.13])"], ["Gap vs peak_capped, memory all (pp)", "−6.30", "−2.85"],
  ].map(([a, b, c]) => ({ a, b, c })), [{ k: "a", label: "Result" }, { k: "b", label: "fastStorage" }, { k: "c", label: "Rnd (Aug 2013)" }]);

  /* ---------------- kernel ---------------- */
  $("#kTiles").innerHTML = tile("50", "VMs replayed", "5 waves, S = 1/16") + tile("26", "OOMKilled by kernel", "all predicted offline") +
    tile("92%", "naive accuracy", "26 TP · 4 FN · 0 FP · 20 TN") + tile("100%", "like-for-like agreement", "mismatch rate < 7.1% (Wilson)");
  renderTable("#confTable", D.confusion.filter((r) => r.scope === "pooled" && r.axis === "memory" && !/artifact/.test(r.population)), [
    { k: "population", label: "Compared", fmt: (v, r) => (r.tn === 24 ? "demand vs enforced limit (like-for-like)" : esc(v.replace(/_/g, " "))) },
    { k: "n", num: true }, { k: "tp", label: "TP", num: true }, { k: "fn", label: "FN", num: true }, { k: "fp", label: "FP", num: true },
    { k: "tn", label: "TN", num: true }, { k: "accuracy_pct", label: "Accuracy %", num: true }]);
  renders.kernel = () => {
    const W = D.waves;
    legend("#kLegend", [{ label: "OOMKilled", color: css("--s1") }, { label: "Survived", color: css("--s1"), hollow: true }, { label: "Margin-band VM", color: css("--s2"), kind: "sq" }, { label: "demand = limit", color: css("--muted"), kind: "ln" }]);
    const pts = W.map((r) => ({
      x: r.limit_mib, y: Math.max(10, r.peak_demand_mib), hollow: r.obs_oomkilled !== "True", color: r.margin_band === "True" ? css("--s2") : css("--s1"),
      shape: r.margin_band === "True" ? "sq" : "",
      tip: `<b>VM ${r.vm_id}</b> (wave ${r.wave})<br>peak demand ${f(r.peak_demand_mib, 1)} MiB<br>budget ${r.budget_mib} · enforced limit ${r.limit_mib} MiB<br>predicted violation: ${r.pred_mem_violation}<br>kernel: ${r.obs_oomkilled === "True" ? "OOMKilled" : "survived"}`,
    }));
    plot("#kChart", { height: 360, x: [150, 1300], xLog: true, y: [10, 4000], yLog: true, xTicks: [200, 300, 600, 1100], yTicks: [10, 100, 1000],
      xLabel: "Enforced memory limit, MiB (log)", yLabel: "Peak scaled demand, MiB (log)",
      layers: [{ type: "line", pts: [{ x: 150, y: 150 }, { x: 1300, y: 1300 }], color: css("--muted"), dash: "5 4", width: 1.5 }, { type: "points", pts }] });
  };
  renderTable("#waveTable", D.waves, [{ k: "wave", num: true }, { k: "vm_id", label: "VM", num: true }, { k: "budget_mib", label: "budget MiB", num: true },
    { k: "limit_mib", label: "limit MiB", num: true }, { k: "peak_demand_mib", label: "peak demand MiB", num: true, fmt: (v) => f(v, 1) },
    { k: "peak_cgroup_mib", label: "peak cgroup MiB", num: true, fmt: (v) => f(v, 1) },
    { k: "pred_mem_violation", label: "predicted", fmt: (v) => (v === "True" ? "violation" : "safe") },
    { k: "obs_oomkilled", label: "kernel", fmt: (v) => (v === "True" ? `<span class="badge bad">OOMKilled</span>` : `<span class="badge good">survived</span>`) },
    { k: "margin_band", label: "margin band", fmt: (v) => (v === "True" ? "yes" : "") }, { k: "samples_logged", label: "samples", num: true, fmt: (v, r) => `${v} / ${r.samples_total}` }],
    { hl: (r) => r.margin_band === "True" });

  /* ---------------- gallery ---------------- */
  const FIGS = [["frontier_theta_intervals", "Risk frontier, M(ε) vs baselines, with CIs"], ["frontier_theta_intervals_unsnapped", "Frontier, unsnapped"],
    ["frontier_snapped", "Frontier, θ = 1, snapped"], ["frontier_unsnapped", "Frontier, θ = 1, unsnapped"], ["ablation_cpu", "Ablation arms, CPU"], ["ablation_mem", "Ablation arms, memory"],
    ["fig1_headroom_ci", "Fig 1: utilisation headroom (CI)"], ["fig1_headroom", "Fig 1: utilisation headroom"], ["fig2_reclamation_vs_safety_ci", "Fig 2: reclamation vs safety (CI)"],
    ["fig2_reclamation_vs_safety", "Fig 2: reclamation vs safety"], ["fig3_safety_split_ci", "Fig 3: safety split by resource (CI)"], ["fig3_safety_split", "Fig 3: safety split"],
    ["fig4_exclusion_cascade", "Fig 4: exclusion cascade"], ["fig5_t8_validation_ci", "Fig 5: kernel validation (CI)"], ["fig5_t8_validation", "Fig 5: kernel validation"],
    ["fig6_idle_contamination_ci", "Fig 6: idle contamination (CI)"], ["fig6_idle_contamination", "Fig 6: idle contamination"], ["slide13", "Testbed: kubectl describe"], ["slide13_2", "Testbed: pod status"]];
  $("#gallery").innerHTML = FIGS.map(([n, c]) => `<figure><a href="figures/${n}.png" target="_blank"><img src="figures/${n}.png" alt="${esc(c)}" loading="lazy"></a><figcaption><b>${esc(c)}</b> · <code>${n}.png</code></figcaption></figure>`).join("");

  route();
})();
