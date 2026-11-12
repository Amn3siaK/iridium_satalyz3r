// iridium_satalyz3r front-end: subscribes to /events (SSE) and renders the dashboard.
(() => {
  const $ = (id) => document.getElementById(id);
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

  const TYPE_COLORS = {
    IRA: "#38bdf8", IBC: "#a78bfa", ITL: "#34d399", IMS: "#fbbf24", IME: "#f59e0b", MSG: "#fde047",
    IRI: "#cbd5e1", IDA: "#f472b6", IIP: "#fb7185", VOC: "#94a3b8", VOD: "#94a3b8", VDA: "#94a3b8",
    IU3: "#8b9bb0", RAW: "#8b9bb0",
  };
  const SAT_PALETTE = ["#38bdf8", "#f472b6", "#34d399", "#fbbf24", "#a78bfa", "#fb923c", "#22d3ee", "#e879f9", "#a3e635", "#f87171"];
  const satColor = (id) => SAT_PALETTE[id % SAT_PALETTE.length];
  const tcolor = (t) => TYPE_COLORS[t] || "#8b9bb0";

  const fmtTime = (t) => t ? new Date(t * 1000).toLocaleTimeString([], { hour12: false }) : "–";
  const ago = (t, now) => {
    if (!t) return "–";
    const s = Math.max(0, Math.round(now - t));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m ago";
  };
  const isLive = (s) => s.mode === "live" || s.mode === "demo";
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ---------- map ----------
  const map = L.map("map", { worldCopyJump: true, zoomControl: true }).setView([30, 20], 3);
  const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/";
  L.tileLayer(ESRI + "World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
    attribution: "Tiles &copy; Esri", maxZoom: 16,
  }).addTo(map);
  L.tileLayer(ESRI + "World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}", { maxZoom: 16, opacity: .8 }).addTo(map);
  const layers = {}; // satId -> {track, head, spots, spotKey}
  let fittedOnce = false;
  // the user panning or zooming means "leave the view alone": switch Auto-fit off
  const stopFollow = () => { $("follow").checked = false; };
  map.on("dragstart", stopFollow);
  map.getContainer().addEventListener("wheel", stopFollow, { passive: true });
  map.zoomControl.getContainer().addEventListener("click", stopFollow);

  function updateMap(sats) {
    const bounds = [];
    for (const s of sats) {
      if (!s.confirmed) continue;         // a single frame may be a bit error in the satellite ID
      const color = satColor(s.id);
      let L0 = layers[s.id];
      if (!L0) {
        L0 = layers[s.id] = {
          track: L.polyline([], { color, weight: 2.5, opacity: .9 }).addTo(map),
          head: L.circleMarker([0, 0], { radius: 7, color, fillColor: color, fillOpacity: .9, weight: 2 })
            .bindTooltip("SAT " + s.id, { permanent: true, direction: "right", className: "satlbl", offset: [8, 0] }),
          spots: L.layerGroup().addTo(map),
        };
      }
      const pts = s.track.map((p) => [p[0], p[1]]);
      L0.track.setLatLngs(passes(s.track).map((seg) => seg.map((p) => [p[0], p[1]])));
      if (pts.length) {
        L0.head.setLatLng(pts[pts.length - 1]);
        if (!map.hasLayer(L0.head)) L0.head.addTo(map);
        bounds.push(...pts);
      }
      const seen = new Map(); // latest spot per beam
      for (const p of s.spots) seen.set(p[2], p);
      // rebuilding every update would close any tooltip the user is hovering
      const spotKey = [...seen.values()].map((p) => p.join(",")).join(";");
      if (spotKey !== L0.spotKey) {
        L0.spotKey = spotKey;
        L0.spots.clearLayers();
        for (const [beam, p] of seen) {
          L.circle([p[0], p[1]], { radius: 200000, color, weight: 1.2, fillColor: color, fillOpacity: .12 })
            .bindTooltip(`SAT ${s.id} · beam ${beam}<br>${p[0].toFixed(2)}, ${p[1].toFixed(2)}<br>${fmtTime(p[3])}`)
            .addTo(L0.spots);
        }
      }
      for (const p of seen.values()) bounds.push([p[0], p[1]]);
    }
    if (bounds.length && ($("follow").checked || !fittedOnce)) {
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 5 });
      fittedOnce = true;
    }
    lastBounds = bounds;
  }
  let lastBounds = [];
  // split a track into passes: a gap of 10 minutes means the satellite went around the Earth in between
  function passes(track, gap = 600) {
    const out = [];
    for (const p of track) {
      const seg = out[out.length - 1];
      if (seg && p[2] - seg[seg.length - 1][2] <= gap) seg.push(p); else out.push([p]);
    }
    return out;
  }
  $("fit").onclick = () => lastBounds.length && map.fitBounds(lastBounds, { padding: [40, 40], maxZoom: 5 });
  function resetMap() {
    for (const id in layers) { const L0 = layers[id]; map.removeLayer(L0.track); map.removeLayer(L0.head); map.removeLayer(L0.spots); delete layers[id]; }
    fittedOnce = false;
  }

  // ---------- satellites table ----------
  const few = (list, n = 5) => list.slice(0, n).join(", ") + (list.length > n ? ` +${list.length - n}` : "");
  function updateSats(sats, now) {
    const tb = $("sats").querySelector("tbody");
    const rx = rxLocation();
    tb.innerHTML = sats.map((s) => {
      const p = s.track.length ? s.track[s.track.length - 1] : null;
      const sky = rx && p ? lookAngles(rx, p[0], p[1]) : null;
      return `
      <tr data-sat="${s.id}" class="${s.confirmed ? "" : "unconfirmed"}" title="${s.confirmed ? "Click for details" : "Heard only once so far: possibly a decoding error"}">
        <td><span class="satdot" style="background:${satColor(s.id)}"></span><b>${s.id}</b>${s.confirmed ? "" : " ?"}</td>
        <td>${ago(s.last, now)}</td>
        <td>${s.frames}</td>
        <td>${s.snr != null ? s.snr.toFixed(1) : "–"}</td>
        <td class="nowrap">${sky && sky.el > 0 ? `${sky.el.toFixed(0)}° ${compass(sky.az)}` : "–"}</td>
        <td class="muted">${s.beams.length ? "beams " + few(s.beams) : ""}${s.beams.length && s.cells.length ? " · " : ""}${s.cells.length ? "cells " + few(s.cells) : ""}</td>
      </tr>`; }).join("");
    $("sats-empty").classList.toggle("hidden", sats.length > 0);
    $("sats-hint").classList.toggle("hidden", sats.length === 0);
    const sure = sats.filter((s) => s.confirmed);
    $("sats-note").textContent = sats.length ? `${sure.filter((s) => now - s.last < 600).length} in last 10 min` : "";
  }
  $("sats").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-sat]"); if (tr) openSat(+tr.dataset.sat); });

  // ---------- frame types ----------
  function updateTypes(types) {
    const max = Math.max(1, ...types.map((t) => t.count));
    $("types").innerHTML = types.map((t) => `
      <div class="trow" title="${esc(t.desc)}">
        <span class="tag" style="color:${tcolor(t.type)}">${t.type}</span>
        <div class="tmid"><span class="tdesc">${esc(t.desc || "Iridium frame type " + t.type)}</span>
          <div class="tbar"><i style="width:${(100 * t.count / max).toFixed(1)}%;background:${tcolor(t.type)}"></i></div></div>
        <span class="tcount">${t.count}</span>
      </div>`).join("") || '<div class="empty">Waiting for frames…</div>';
  }

  // ---------- canvases ----------
  function setupCanvas(c) {
    const dpr = window.devicePixelRatio || 1;
    const w = c.clientWidth, h = c.getAttribute("height") * 1;
    if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); c.style.height = h + "px"; }
    const g = c.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    return [g, w, h];
  }

  const CHANNELS = [
    [1626.104, "pager"], [1626.271, "ring alert"], [1626.438, "pager"],
  ];
  function drawFreq(f) {
    const [g, w, h] = setupCanvas($("freq"));
    const n = f.counts.length, max = Math.max(1, ...f.counts);
    const padB = 22, padT = 18, bw = w / n;
    const fmin = f.min / 1e6, fspan = (n * f.bin) / 1e6;
    const xOf = (mhz) => ((mhz - fmin) / fspan) * w;
    // simplex band shading
    g.fillStyle = "rgba(56,189,248,.06)";
    g.fillRect(xOf(1626.0), padT, xOf(1626.5) - xOf(1626.0), h - padT - padB);
    g.fillStyle = css("--muted"); g.font = "10px system-ui";
    g.fillText("duplex (two-way)", 4, 11);
    g.fillText("simplex", xOf(1626.0) + 4, 11);
    for (let i = 0; i < n; i++) {
      const v = f.counts[i]; if (!v) continue;
      const bh = (v / max) * (h - padT - padB);
      g.fillStyle = css("--accent");
      g.fillRect(i * bw + .5, h - padB - bh, Math.max(1, bw - 1), bh);
    }
    g.strokeStyle = css("--line"); g.beginPath(); g.moveTo(0, h - padB + .5); g.lineTo(w, h - padB + .5); g.stroke();
    g.fillStyle = css("--muted");
    const tick = fspan > 4 ? 1 : 0.5;
    for (let m = Math.ceil(fmin / tick) * tick; m <= fmin + fspan + 1e-6; m += tick) {
      const x = xOf(m); g.fillRect(x, h - padB, 1, 4);
      const lbl = m.toFixed(1), lw = g.measureText(lbl).width;
      g.fillText(lbl, Math.min(w - lw - 2, Math.max(0, x - lw / 2)), h - 6);
    }
    g.setLineDash([3, 3]); g.strokeStyle = "rgba(251,191,36,.6)";
    for (const [mhz, name] of CHANNELS) {
      const x = xOf(mhz); g.beginPath(); g.moveTo(x, padT); g.lineTo(x, h - padB); g.stroke();
    }
    g.setLineDash([]);
  }

  // waterfall: one dot per frame, x = frequency, y = time (newest at the top), colour = frame type
  const WF_SPAN = 30 * 60;          // seconds shown
  const points = [];                // [seq, t, freq, type, snr]
  let freqMode = "hist";
  try { freqMode = localStorage.getItem("iri.freqMode") || "hist"; } catch (e) {}
  if (location.hash === "#waterfall") freqMode = "wf";
  function setFreqMode(m) {
    freqMode = m;
    document.querySelectorAll("#freqmode .seg-b").forEach((b) => b.classList.toggle("on", b.dataset.m === m));
    $("freq").classList.toggle("hidden", m !== "hist");
    $("wf").classList.toggle("hidden", m !== "wf");
    $("freq-note").textContent = m === "hist" ? "frames per 20 kHz" : "last 30 min · newest at top";
    try { localStorage.setItem("iri.freqMode", m); } catch (e) {}
    if (last) m === "hist" ? drawFreq(last.freq) : drawWaterfall(last);
  }
  $("freqmode").onclick = (e) => { const m = e.target.dataset && e.target.dataset.m; if (m) setFreqMode(m); };

  function drawWaterfall(s) {
    const c = $("wf"); if (c.classList.contains("hidden")) return;
    const [g, w, h] = setupCanvas(c);
    const padB = 22, padT = 18, padL = 34;
    const fmin = s.freq.min / 1e6, fspan = (s.freq.counts.length * s.freq.bin) / 1e6;
    const xOf = (mhz) => padL + ((mhz - fmin) / fspan) * (w - padL);
    g.font = "10px system-ui"; g.fillStyle = css("--muted");
    if (!points.length) { g.fillText("Waiting for frames…", padL + 4, h / 2); return; }
    const tTop = isLive(s) ? Math.max(Date.now() / 1000, s.last_frame || 0) : points[points.length - 1][1];
    const yOf = (t) => padT + ((tTop - t) / WF_SPAN) * (h - padT - padB);
    g.fillStyle = "rgba(56,189,248,.06)";
    g.fillRect(xOf(1626.0), padT, xOf(1626.5) - xOf(1626.0), h - padT - padB);
    g.setLineDash([3, 3]); g.strokeStyle = "rgba(251,191,36,.35)";
    for (const [mhz] of CHANNELS) { const x = xOf(mhz); g.beginPath(); g.moveTo(x, padT); g.lineTo(x, h - padB); g.stroke(); }
    g.setLineDash([]);
    for (const p of points) {
      const y = yOf(p[1]); if (y < padT || y > h - padB) continue;
      g.fillStyle = tcolor(p[3]);
      g.globalAlpha = Math.min(1, Math.max(.35, (p[4] - 8) / 16));   // weak frames fainter
      g.fillRect(xOf(p[2] / 1e6) - 1, y - 1, 2.5, 2.5);
    }
    g.globalAlpha = 1;
    g.fillStyle = css("--muted");
    for (let m = 0; m <= 30; m += 10) {             // time axis: minutes ago
      const y = padT + (m * 60 / WF_SPAN) * (h - padT - padB);
      g.fillRect(padL - 4, y, 4, 1);
      g.fillText(m ? `-${m}m` : "now", 2, y + 3);
    }
    g.strokeStyle = css("--line"); g.beginPath(); g.moveTo(padL, h - padB + .5); g.lineTo(w, h - padB + .5); g.stroke();
    const tick = fspan > 4 ? 1 : 0.5;
    for (let m = Math.ceil(fmin / tick) * tick; m <= fmin + fspan + 1e-6; m += tick) {
      const x = xOf(m); g.fillRect(x, h - padB, 1, 4);
      const lbl = m.toFixed(1), lw = g.measureText(lbl).width;
      g.fillText(lbl, Math.min(w - lw - 2, Math.max(padL, x - lw / 2)), h - 6);
    }
    g.fillText("duplex (two-way)", padL + 4, 11);
    g.fillText("simplex", xOf(1626.0) + 4, 11);
  }

  let sigMode = "snr";
  $("sigmode").onclick = (e) => {
    const m = e.target.dataset && e.target.dataset.m; if (!m) return;
    sigMode = m;
    document.querySelectorAll("#sigmode .seg-b").forEach((b) => b.classList.toggle("on", b.dataset.m === m));
    $("sig-note").textContent = m === "snr" ? "avg SNR per minute" : "avg noise level per minute (dB)";
    if (last) drawSnr(last.snr_series);
  };
  function drawSnr(series) {
    const [g, w, h] = setupCanvas($("snr"));
    const padL = 34, padB = 20, padT = 10;
    g.fillStyle = css("--muted"); g.font = "10px system-ui";
    if (!series.length) { g.fillText("Waiting for frames…", padL, h / 2); return; }
    const noise = sigMode === "noise";
    series = series.map((s) => [s[0], noise ? s[3] : s[1], s[2]]).filter((s) => s[1] != null);
    if (!series.length) return;
    const vals = series.map((s) => s[1]);
    const lo = noise ? Math.floor((Math.min(...vals) - 2) / 5) * 5 : Math.floor(Math.min(15, ...vals) / 5) * 5;
    const hi = noise ? Math.ceil((Math.max(...vals) + 2) / 5) * 5 : Math.ceil(Math.max(30, ...vals) / 5) * 5;
    const t0 = series[0][0], t1 = Math.max(series[series.length - 1][0], t0 + 60);
    const x = (t) => padL + ((t - t0) / (t1 - t0)) * (w - padL - 8);
    const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB);
    g.strokeStyle = css("--line");
    for (let v = lo; v <= hi; v += 5) {
      g.beginPath(); g.moveTo(padL, y(v) + .5); g.lineTo(w, y(v) + .5); g.stroke();
      g.fillText(String(v), 4, y(v) + 3);
    }
    const maxN = Math.max(...series.map((s) => s[2]));
    for (const s of series) { // frame count bars
      const bh = (s[2] / maxN) * 24;
      g.fillStyle = "rgba(125,141,161,.25)";
      g.fillRect(x(s[0]) - 2, h - padB - bh, 4, bh);
    }
    const lineColor = css(noise ? "--warn" : "--good");
    g.strokeStyle = lineColor; g.lineWidth = 2; g.beginPath();
    series.forEach((s, i) => (i ? g.lineTo(x(s[0]), y(s[1])) : g.moveTo(x(s[0]), y(s[1]))));
    g.stroke(); g.lineWidth = 1;
    g.fillStyle = lineColor;
    for (const s of series) { g.beginPath(); g.arc(x(s[0]), y(s[1]), 2.5, 0, 7); g.fill(); }
    g.fillStyle = css("--muted");
    g.fillText(fmtTime(t0).slice(0, 5), padL, h - 5);
    const r = fmtTime(t1).slice(0, 5); g.fillText(r, w - g.measureText(r).width - 4, h - 5);
  }

  // ---------- live feed ----------
  const feed = []; let paused = false; const activeTypes = new Set(); const knownTypes = new Set();
  $("pause").onclick = () => { paused = !paused; $("pause").textContent = paused ? "Resume" : "Pause"; $("pause").classList.toggle("on", paused); if (!paused) renderFeed(); };

  function details(f) {
    const i = f.info || {};
    if (f.type === "IRA" && i.sat != null) {
      const what = i.alt > 700 ? `satellite at ${i.lat.toFixed(2)}, ${i.lon.toFixed(2)} · ${i.alt} km` : `beam centre ${i.lat.toFixed(2)}, ${i.lon.toFixed(2)}`;
      return `SAT ${i.sat} beam ${i.beam} · ${what}${i.pages ? ` · ${i.pages} page(s)` : ""}`;
    }
    if (f.type === "IBC" && i.sat != null) return `SAT ${i.sat} cell ${i.cell} · ` + f.text.replace(/^.*?cell:\d+\s*/, "");
    return f.text;
  }
  let openSeq = null;
  $("feed").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-seq]"); if (!tr) return;
    openSeq = openSeq === +tr.dataset.seq ? null : +tr.dataset.seq;
    renderFeed();
  });
  function renderFeed(newSeqs) {
    if (paused || (openSeq !== null && newSeqs)) return;    // don't move the table under a row being read
    const rows = feed.filter((f) => !activeTypes.size || activeTypes.has(f.type)).slice(-250).reverse();
    $("feed").querySelector("tbody").innerHTML = rows.map((f) => `
      <tr data-seq="${f.seq}" class="${newSeqs && newSeqs.has(f.seq) ? "new" : ""}${f.seq === openSeq ? " open" : ""}">
        <td>${fmtTime(f.t)}</td>
        <td><span class="tag" style="color:${tcolor(f.type)}">${f.type}</span></td>
        <td>${(f.freq / 1e6).toFixed(4)}</td>
        <td>${f.snr.toFixed(1)}</td>
        <td>${f.conf}%</td>
        <td class="details">${f.seq === openSeq
          ? `<div class="fmeta">${f.dir === "UL" ? "uplink" : "downlink"} · ${f.len} symbols · level ${f.level} dB · noise ${f.noise} dB · ${f.freq.toLocaleString()} Hz</div>${esc(f.text)}`
          : esc(details(f))}</td>
      </tr>`).join("");
  }
  function updateChips() {
    $("filters").innerHTML = ["ALL", ...[...knownTypes].sort()].map((t) =>
      `<button class="chip ${t === "ALL" ? (activeTypes.size ? "" : "on") : (activeTypes.has(t) ? "on" : "")}" data-t="${t}">${t}</button>`).join("");
  }
  $("filters").onclick = (e) => {
    const t = e.target.dataset && e.target.dataset.t; if (!t) return;
    if (t === "ALL") activeTypes.clear(); else activeTypes.has(t) ? activeTypes.delete(t) : activeTypes.add(t);
    updateChips(); renderFeed();
  };

  // ---------- pages & messages ----------
  document.querySelectorAll(".tab").forEach((b) => b.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === b));
    $("tab-pages").classList.toggle("hidden", b.dataset.tab !== "pages");
    $("tab-msgs").classList.toggle("hidden", b.dataset.tab !== "msgs");
  });
  function updatePages(pages) {
    $("pages").querySelector("tbody").innerHTML = pages.map((p) =>
      `<tr><td>${fmtTime(p.t)}</td><td>${p.sat}</td><td>${p.beam}</td><td>${p.tmsi}</td><td>${p.msc}</td></tr>`).join("");
    $("pages-empty").classList.toggle("hidden", pages.length > 0);
  }
  const messages = [];
  let showContent = false;
  try { showContent = localStorage.getItem("iri.showContent") === "1"; } catch (e) {}
  $("showContent").checked = showContent;
  $("showContent").onchange = (e) => { showContent = e.target.checked; try { localStorage.setItem("iri.showContent", showContent ? "1" : "0"); } catch (x) {} renderMsgs(); };
  function renderMsgs() {
    $("msgs").innerHTML = messages.slice(0, 150).map((m) => {
      const i = m.info;
      const meta = i ? [`RIC ${esc(i.ric)}`, i.kind, `${i.parts} part${i.parts === 1 ? "" : "s"}`, i.ok ? "checksum OK" : "checksum failed"].join(" · ") : "";
      const body = showContent
        ? `<pre>${esc(m.json ? JSON.stringify(m.json, null, 1) : m.text)}</pre>`
        : `<div class="hiddenc">${esc(m.mode)} message · ${(m.text || "").length} chars · content hidden</div>`;
      const kindTag = i && i.kind === "device data" ? '<span class="tag dev" title="base64 machine data with a valid CRC-16, not human text">DEVICE DATA</span>' : "";
      return `<div class="msg${i && i.kind === "device data" ? " devmsg" : ""}"><div class="mh"><span><span class="tag">${esc(m.mode.toUpperCase())}</span> ${kindTag}</span><span>${fmtTime(m.t)}</span></div>`
        + (meta ? `<div class="mmeta">${meta}</div>` : "") + body + `</div>`;
    }).join("");
    $("msgs-empty").classList.toggle("hidden", messages.length > 0);
  }

  // ---------- voice traffic (call metadata only) ----------
  const fmtDur = (s) => s == null ? "–" : s < 60 ? Math.round(s) + "s" : Math.floor(s / 60) + "m " + String(Math.round(s % 60)).padStart(2, "0") + "s";
  function bars(canvas, items, labelFmt, color) {
    const [g, w, h] = setupCanvas(canvas);
    g.font = "10px system-ui"; g.fillStyle = css("--muted");
    if (!items.length) { g.fillText("No calls yet", 4, h / 2); return; }
    const padB = 16, padT = 12, max = Math.max(1, ...items.map((i) => i[1]));
    const n = items.length, bw = Math.min(28, (w - 4) / n);
    items.forEach(([k, v], i) => {
      const bh = (v / max) * (h - padT - padB), x = 2 + i * bw;
      g.fillStyle = color; g.fillRect(x + 1, h - padB - bh, Math.max(2, bw - 3), bh);
      g.fillStyle = css("--text"); if (bw > 14) g.fillText(String(v), x + 2, h - padB - bh - 3);
    });
    g.fillStyle = css("--muted");
    let nextFree = 0; // skip labels that would overlap the previous one
    items.forEach(([k], i) => {
      const lbl = labelFmt(k), lw = g.measureText(lbl).width, x = 2 + i * bw;
      if (x >= nextFree && x + lw <= w) { g.fillText(lbl, x, h - 3); nextFree = x + lw + 6; }
    });
  }
  function updateVoice(c) {
    if (!c) return;
    $("v-active").innerHTML = c.active.length ? `<span class="live-dot"></span>${c.active.length}` : "0";
    $("v-total").textContent = c.total;
    $("v-avg").textContent = fmtDur(c.avg_duration);
    $("v-long").textContent = fmtDur(c.longest);
    $("v-conc").textContent = c.max_concurrent;
    $("v-short").textContent = c.short_bursts;
    bars($("vtimeline"), c.timeline, (k) => fmtTime(k).slice(0, 5), css("--warn"));
    bars($("vchannels"), c.channels, (k) => "ch " + k, css("--accent"));
    const rows = [...c.active, ...c.recent];
    $("calls").querySelector("tbody").innerHTML = rows.map((r) => `
      <tr>
        <td>${r.active ? '<span class="live-dot"></span>active' : '<span class="muted">ended</span>'}</td>
        <td>${fmtTime(r.start)}</td>
        <td>${fmtDur(r.duration)}</td>
        <td>${r.channel}</td>
        <td>${(r.freq / 1e6).toFixed(4)}</td>
        <td>${r.drift >= 0 ? "+" : ""}${(r.drift / 1000).toFixed(1)} kHz</td>
        <td>${r.frames}</td>
        <td>${r.snr.toFixed(1)}</td>
      </tr>`).join("");
    $("calls-empty").classList.toggle("hidden", rows.length > 0);
  }

  // ---------- main update ----------
  let last = null, serverStart = null, maxSeq = 0, maxMsgSeq = 0;
  function resetClient() {
    feed.length = 0; points.length = 0; messages.length = 0; knownTypes.clear(); activeTypes.clear();
    maxSeq = 0; maxMsgSeq = 0; resetMap();
    updateChips(); renderFeed(); renderMsgs();
  }
  function apply(s) {
    if (s.closed) { if (view === "rec") switchView("live"); return; }   // the recording was closed
    mainMode = s.main_mode || s.mode;     // live / replay / idle (browse mode: no live view)
    // a new server (restart / new run) starts its sequence numbers again: start over
    if (serverStart !== null && s.started !== serverStart) resetClient();
    serverStart = s.started;
    // after an EventSource reconnect the server resends from 0: keep only what is new
    s.frames = s.frames.filter((f) => f.seq > maxSeq);
    s.points = (s.points || []).filter((p) => p[0] > maxSeq);
    s.messages = s.messages.filter((m) => m.seq > maxMsgSeq);
    maxSeq = Math.max(maxSeq, s.seq); maxMsgSeq = Math.max(maxMsgSeq, s.msg_seq);
    last = s;
    const now = Date.now() / 1000;
    const pill = $("pill");
    const fresh = s.last_frame && now - s.last_frame < 120;
    const past = s.mode === "replay" || s.mode === "decoding";      // times are relative to the recording, not now
    const prog = s.session && s.session.progress != null ? ` ${Math.floor(s.session.progress * 100)}%` : "";
    pill.className = "pill " + ({ replay: "replay", decoding: "replay", live: "live", idle: "idle", demo: "demo" }[s.mode] || "down");
    pill.textContent = { replay: view === "rec" ? "RECORDING" : "REPLAY", decoding: "DECODING" + prog, live: "LIVE", idle: "IDLE", demo: "DEMO · simulated" }[s.mode] || "STOPPED";
    $("lastframe").textContent = s.mode === "idle" ? "start the receiver or open a recording"
      : s.last_frame ? (past ? `last frame ${fmtDateTime(s.last_frame)}` : `last frame ${ago(s.last_frame, now)}` + (isLive(s) && !fresh ? " · waiting for signal" : ""))
      : s.mode === "decoding" ? "decoding…" : "waiting for first frame…";
    updateRecbar(s);
    updateRxButton(s.receiver);

    if (s.sdr && s.sdr.fmin) $("sdrline").textContent =
      `${s.sdr.name} · ${(s.sdr.fmin / 1e6).toFixed(1)}–${(s.sdr.fmax / 1e6).toFixed(1)} MHz · iridium-toolkit`;
    const typeCount = Object.fromEntries(s.types.map((t) => [t.type, t.count]));
    $("k-total").textContent = s.total.toLocaleString();
    $("k-rate").textContent = s.per_min;
    $("k-sats").textContent = s.sats.filter((x) => x.confirmed).length;
    $("k-ira").textContent = typeCount.IRA || 0;
    $("k-pages").textContent = s.page_total;
    $("k-snr").textContent = s.avg_snr != null ? s.avg_snr.toFixed(1) : "–";
    $("k-ok").textContent = s.extractor && s.extractor.ok_avg != null ? s.extractor.ok_avg + "%" : "–";

    const ref = past && s.last_frame ? s.last_frame : now;        // "now" for this view
    updateMap(s.sats);
    updateRxMarker();
    updateSats(s.sats, ref);
    drawSky(s, ref);
    updateHealth(s, now);
    updateTypes(s.types);
    for (const p of s.points) points.push(p);
    if (points.length > 8000) points.splice(0, points.length - 8000);
    if (freqMode === "hist") drawFreq(s.freq); else drawWaterfall(s);
    drawSnr(s.snr_series);
    updatePages(s.pages);
    updateVoice(s.calls);

    const newSeqs = new Set();
    for (const f of s.frames) { feed.push(f); newSeqs.add(f.seq); knownTypes.add(f.type); }
    if (feed.length > 1000) feed.splice(0, feed.length - 1000);
    if (s.frames.length) { updateChips(); renderFeed(newSeqs); }

    if (s.messages.length) { messages.unshift(...s.messages.slice().sort((a, b) => b.seq - a.seq)); messages.length = Math.min(messages.length, 300); renderMsgs(); }
    $("k-msgs").textContent = s.msg_seq.toLocaleString();
    const ra = s.reassemblers || {}, down = Object.keys(ra).filter((k) => ra[k] === "unavailable" || ra[k] === "failed");
    $("reass-note").textContent = down.length ? `Message reassembly not running for: ${down.join(", ")} (is iridium-toolkit installed? run ./install.sh --check)` : "";
  }

  // ---------- views: "live" (the receiver, or the --replay file) and "rec" (a recording opened here) ----------
  let view = location.hash === "#rec" ? "rec" : "live", mainMode = null, es = null, pollGen = 0, autoOpened = false;
  function connect() {
    const gen = ++pollGen, v = view;
    if (location.search.includes("poll") || !window.EventSource) {
      // fallback: periodic polling instead of a live event stream
      const tick = () => {
        if (gen !== pollGen) return;              // view switched: this loop ends
        fetch(`/api/state?view=${v}&since=${maxSeq}&since_msg=${maxMsgSeq}`, { cache: "no-store" })
          .then((r) => r.json()).then((s) => gen === pollGen && apply(s))
          .catch(() => { $("pill").className = "pill down"; $("pill").textContent = "DISCONNECTED"; })
          .finally(() => setTimeout(tick, 2000));
      };
      return tick();
    }
    es = new EventSource(`/events?view=${v}`);
    es.onmessage = (e) => apply(JSON.parse(e.data));
    es.onerror = () => { $("pill").className = "pill down"; $("pill").textContent = "DISCONNECTED"; };
  }
  function switchView(v) {
    view = v;
    try { history.replaceState(null, "", location.pathname + location.search + (v === "rec" ? "#rec" : "")); } catch (e) {}
    if (es) { es.close(); es = null; }
    serverStart = null; last = null; resetClient();
    $("follow").checked = true;
    if (v === "live") $("recbar").classList.add("hidden");
    connect();
  }

  // ---------- recordings ----------
  const fmtDateTime = (t) => t ? new Date(t * 1000).toLocaleString([], { hour12: false, year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–";
  const fmtDate = (t) => t ? new Date(t * 1000).toLocaleString([], { hour12: false, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "–";
  const fmtSpan = (s) => {
    if (s == null) return "–";
    s = Math.round(s);
    if (s < 60) return s + " s";
    if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
    return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  };
  const fmtSize = (b) => b >= 1e9 ? (b / 1e9).toFixed(2) + " GB" : b >= 1e6 ? (b / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1e3)) + " kB";
  const mhz = (hz, d = 3) => hz ? (hz / 1e6).toFixed(d) + " MHz" : "–";

  function recMeta(i) {
    if (i.kind === "iq") return [i.sdr || "unknown radio", `centre ${mhz(i.centre)}`, `${(i.rate / 1e6).toFixed(2)} MSps`,
      fmtSpan(i.duration), fmtSize(i.size), i.format === "cu8" ? "8-bit" : i.format, `recorded ${fmtDateTime(i.start)}`];
    return [i.sdr || "unknown radio", i.demod || "", `${(i.frames || 0).toLocaleString()} frames`, fmtSpan(i.duration),
      fmtSize(i.size), i.first ? `recorded ${fmtDateTime(i.first)}` : ""].filter(Boolean);
  }
  function updateRecbar(s) {
    const bar = $("recbar");
    if (view !== "rec" || !s.session || !s.session.name) { bar.classList.add("hidden"); return; }
    const i = s.session;
    bar.classList.remove("hidden");
    $("rb-kind").textContent = i.kind === "iq" ? "IQ" : "SESSION";
    $("rb-name").textContent = i.name;
    $("rb-meta").textContent = recMeta(i).join(" · ");
    const st = $("rb-status"), prog = $("rb-prog");
    st.classList.toggle("err", i.status === "error");
    prog.classList.toggle("hidden", i.status !== "decoding");
    if (i.status === "decoding") {
      const p = i.progress || 0, el = Date.now() / 1000 - i.decode_started;
      const eta = p > 0.02 ? ` · about ${fmtSpan(el * (1 - p) / p)} left` : "";
      st.textContent = `Decoding ${(p * 100).toFixed(0)}%${eta}`;
      prog.firstElementChild.style.width = (p * 100).toFixed(1) + "%";
    } else if (i.status === "error") st.textContent = "Decoding failed: " + (i.error || "unknown error");
    else if (i.status === "loading") st.textContent = "Loading…";
    else if (i.status === "cancelled") st.textContent = "Stopped";
    else st.textContent = i.kind === "iq" ? (i.cached ? "Decoded earlier (saved result)" : `Decoded in ${fmtSpan(i.decode_seconds)}`) : "Loaded";
    $("rb-live").classList.toggle("hidden", mainMode === "idle");
  }

  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  }
  function showDlgError(msg) { const e = $("rd-error"); e.textContent = msg || ""; e.classList.toggle("hidden", !msg); }

  const recSort = { key: "start", dir: -1 };
  $("rd-sessions").querySelector("thead").addEventListener("click", (e) => {
    const th = e.target.closest("th[data-sort]"); if (!th) return;
    recSort.dir = recSort.key === th.dataset.sort ? -recSort.dir : -1;
    recSort.key = th.dataset.sort;
    loadRecordings();
  });
  $("recdlg").addEventListener("change", async (e) => {
    const i = e.target.closest("input.note"); if (!i) return;
    try { const r = await post("/api/note", { kind: i.dataset.kind, name: i.dataset.name, note: i.value }); i.value = r.note; i.classList.add("saved"); setTimeout(() => i.classList.remove("saved"), 1200); }
    catch (err) { showDlgError(err.message); }
  });
  async function loadRecordings() {
    let ls;
    try { ls = await (await fetch("/api/recordings", { cache: "no-store" })).json(); }
    catch (e) { showDlgError("Could not load the list of recordings."); return; }
    if (ls.error) { showDlgError(ls.error); return; }
    mainMode = ls.main_mode;
    const cur = ls.current;
    const isCur = (kind, name) => cur && cur.kind === kind && cur.name === name;
    const btn = (kind, name, label, extra = "") => `<button class="btn" data-kind="${kind}" data-name="${esc(name)}"${extra}>${label}</button>`;
    const note = (kind, r) => `<input class="note" data-kind="${kind}" data-name="${esc(r.name)}" value="${esc(r.note || "")}" placeholder="add a note" maxlength="200" title="Your note, e.g. where the antenna was">`;
    const del = (kind, r) => r.live ? "" : `<button class="btn del" data-del="1" data-kind="${kind}" data-name="${esc(r.name)}" title="Delete this recording and its files">Delete</button>`;
    const liveTag = (r) => r.live ? ' <span class="badge ok">recording now</span>' : "";
    const k = recSort.key, sessions = ls.sessions.slice().sort((a, b) => ((a[k] ?? -1) - (b[k] ?? -1)) * recSort.dir);
    const maxRate = Math.max(1, ...sessions.map((r) => r.per_min || 0));
    document.querySelectorAll("#rd-sessions th[data-sort]").forEach((th) => th.classList.toggle("sorted", th.dataset.sort === k));
    $("rd-sessions").querySelector("tbody").innerHTML = sessions.map((r) => `
      <tr class="${isCur("session", r.name) ? "cur" : ""}" title="${esc(r.name)}">
        <td>${fmtDate(r.start)}${liveTag(r)}</td><td>${fmtSpan(r.duration)}</td><td>${(r.frames || 0).toLocaleString()}</td>
        <td><div class="cellbar"><i style="width:${(100 * (r.per_min || 0) / maxRate).toFixed(0)}%"></i><span>${r.per_min != null ? r.per_min : "–"}</span></div></td>
        <td>${r.avg_snr != null ? r.avg_snr.toFixed(1) : "–"}</td><td>${r.sats != null ? r.sats : "–"}</td><td>${r.ok_avg != null ? r.ok_avg + "%" : "–"}</td>
        <td>${esc([r.sdr, r.demod].filter(Boolean).join(" · ") || "–")}</td>
        <td>${note("session", r)}</td>
        <td class="act">${btn("session", r.name, isCur("session", r.name) ? "View" : "Open")}${del("session", r)}</td>
      </tr>`).join("");
    $("rd-sessions-empty").classList.toggle("hidden", ls.sessions.length > 0);
    $("rd-iq").querySelector("tbody").innerHTML = ls.iq.map((r) => r.error ? `
      <tr title="${esc(r.name)}"><td colspan="5" class="mono">${esc(r.name)}</td><td>${fmtSize(r.size)}</td><td colspan="2" class="bad">${esc(r.error)}</td><td class="act">${del("iq", r)}</td></tr>` : `
      <tr class="${isCur("iq", r.name) ? "cur" : ""}" title="${esc(r.name)}">
        <td>${fmtDate(r.start)}${liveTag(r)}</td><td>${esc(r.sdr || "–")}</td><td>${mhz(r.centre, 2)}</td><td>${(r.rate / 1e6).toFixed(2)} MSps</td>
        <td>${fmtSpan(r.duration)}</td><td>${fmtSize(r.size)}</td>
        <td>${r.decoded != null ? `✓ ${r.decoded.toLocaleString()} frames · ${r.per_min}/min` : '<span class="muted">not yet</span>'}</td>
        <td>${note("iq", r)}</td>
        <td class="act">${r.live ? "" : isCur("iq", r.name) ? btn("iq", r.name, "View") : btn("iq", r.name, r.decoded != null ? "Open" : "Decode")}${r.decoded != null && !isCur("iq", r.name) ? btn("iq", r.name, "Re-decode", ' data-redecode="1" title="Decode the IQ again, e.g. after updating iridium-sniffer"') : ""}${del("iq", r)}</td>
      </tr>`).join("");
    if (ls.disk) $("rd-disk").textContent = `recordings use ${fmtSize(ls.disk.used)}${ls.disk.free != null ? ` · ${fmtSize(ls.disk.free)} free` : ""}`;
    $("rd-iq-empty").classList.toggle("hidden", ls.iq.length > 0);
    const c = $("rd-current");
    c.classList.toggle("hidden", !cur);
    if (cur) c.innerHTML = `<span>Open now: <b class="mono">${esc(cur.name)}</b> · ${esc(cur.status || "")}${cur.status === "decoding" ? ` ${Math.floor((cur.progress || 0) * 100)}%` : ""}</span>
      <span class="actions"><button class="btn" data-view="rec">View</button><button class="btn" data-close="1">Close it</button></span>`;
    const warn = [];
    if (!ls.sniffer) warn.push("iridium-sniffer is not installed, so IQ recordings can't be decoded (run ./install.sh).");
    if (!ls.toolkit) warn.push("iridium-toolkit is missing (run ./install.sh).");
    showDlgError(warn.join(" "));
  }
  function openDialog() {
    const d = $("recdlg");
    if (!d.open) (d.showModal ? d.showModal() : d.setAttribute("open", ""));
    loadRecordings();
  }
  function closeDialog() { const d = $("recdlg"); d.close ? d.close() : d.removeAttribute("open"); }
  $("recbtn").onclick = openDialog;
  $("rd-x").onclick = closeDialog;
  $("rd-refresh").onclick = loadRecordings;
  $("recdlg").addEventListener("click", async (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.view) { closeDialog(); if (view !== "rec") switchView("rec"); return; }
    if (b.dataset.close) { await post("/api/close").catch(() => {}); if (view === "rec") switchView("live"); loadRecordings(); return; }
    if (!b.dataset.kind) return;
    if (b.dataset.del) {
      if (!confirm(`Delete ${b.dataset.name} and the files that belong to it? This cannot be undone.`)) return;
      try { await post("/api/delete", { kind: b.dataset.kind, name: b.dataset.name }); if (view === "rec" && last && last.session && last.session.name === b.dataset.name) switchView("live"); }
      catch (err) { showDlgError(err.message); return; }
      loadRecordings(); return;
    }
    if (b.textContent === "View") { closeDialog(); if (view !== "rec") switchView("rec"); return; }
    b.disabled = true;
    try {
      await post("/api/open", { kind: b.dataset.kind, name: b.dataset.name, redecode: !!b.dataset.redecode });
      closeDialog(); switchView("rec");
    } catch (err) { showDlgError(err.message); }
    finally { b.disabled = false; }
  });
  $("rb-live").onclick = () => switchView("live");
  $("rb-close").onclick = async () => { await post("/api/close").catch(() => {}); switchView("live"); };
  // ---------- geometry: where in the sky a satellite is, seen from the receiver ----------
  const R_E = 6371, SAT_ALT = 780;
  const rad = (d) => d * Math.PI / 180, deg = (r) => r * 180 / Math.PI;
  const ecef = (lat, lon, r) => [r * Math.cos(rad(lat)) * Math.cos(rad(lon)), r * Math.cos(rad(lat)) * Math.sin(rad(lon)), r * Math.sin(rad(lat))];
  function lookAngles(rx, lat, lon, alt = SAT_ALT) {
    const o = ecef(rx.lat, rx.lon, R_E), p = ecef(lat, lon, R_E + alt);
    const d = [p[0] - o[0], p[1] - o[1], p[2] - o[2]];
    const a = rad(rx.lat), l = rad(rx.lon);
    const e = -Math.sin(l) * d[0] + Math.cos(l) * d[1];
    const n = -Math.sin(a) * Math.cos(l) * d[0] - Math.sin(a) * Math.sin(l) * d[1] + Math.cos(a) * d[2];
    const u = Math.cos(a) * Math.cos(l) * d[0] + Math.cos(a) * Math.sin(l) * d[1] + Math.sin(a) * d[2];
    const range = Math.hypot(d[0], d[1], d[2]);
    return { az: (deg(Math.atan2(e, n)) + 360) % 360, el: deg(Math.asin(u / range)), range };
  }
  const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  const compass = (az) => COMPASS[Math.round(az / 45) % 8];

  // ---------- receiver location (kept in this browser only) ----------
  let userLoc = null;
  try { userLoc = JSON.parse(localStorage.getItem("iri.loc")); } catch (e) {}
  if (!userLoc || typeof userLoc.lat !== "number" || typeof userLoc.lon !== "number") userLoc = null;
  function rxLocation() {
    if (last && last.demo_rx) return { lat: last.demo_rx.lat, lon: last.demo_rx.lon, source: "demo" };
    if (userLoc) return { lat: userLoc.lat, lon: userLoc.lon, source: "set" };
    if (last && last.rx_est) return { lat: last.rx_est.lat, lon: last.rx_est.lon, source: "estimate" };
    return null;
  }
  function setUserLoc(loc) {
    userLoc = loc;
    try { loc ? localStorage.setItem("iri.loc", JSON.stringify(loc)) : localStorage.removeItem("iri.loc"); } catch (e) {}
    if (last) { updateRxMarker(); updateSats(last.sats, lastRef); drawSky(last, lastRef); }
  }
  let rxMarker = null, rxKey = "";
  function updateRxMarker() {
    const rx = rxLocation(), key = rx ? `${rx.lat},${rx.lon},${rx.source}` : "";
    if (key === rxKey) return;
    rxKey = key;
    if (rxMarker) { map.removeLayer(rxMarker); rxMarker = null; }
    if (!rx) return;
    const what = { set: "Receiver (your location)", demo: "Simulated receiver", estimate: "Receiver, estimated from the spot beams heard (±200 km)" }[rx.source];
    rxMarker = L.circleMarker([rx.lat, rx.lon], { radius: 6, color: "#ffffff", weight: 2, fillColor: "#0b1017", fillOpacity: 1,
      dashArray: rx.source === "estimate" ? "3 3" : null }).bindTooltip(`${what}<br>${rx.lat.toFixed(2)}, ${rx.lon.toFixed(2)}`).addTo(map);
  }
  const dlgOpen = (id) => { const d = $(id); if (!d.open) (d.showModal ? d.showModal() : d.setAttribute("open", "")); };
  const dlgClose = (id) => { const d = $(id); d.close ? d.close() : d.removeAttribute("open"); };
  document.querySelectorAll("button[data-close]").forEach((b) => { if ($(b.dataset.close)) b.onclick = () => dlgClose(b.dataset.close); });
  $("loc-btn").onclick = () => {
    const rx = rxLocation();
    $("loc-lat").value = userLoc ? userLoc.lat : ""; $("loc-lon").value = userLoc ? userLoc.lon : "";
    const est = last && last.rx_est;
    $("loc-est").disabled = !est;
    $("loc-note").textContent = (rx && rx.source === "demo" ? "Demo mode uses the simulated receiver's location. " : "")
      + (est ? `Estimate from the ${est.n} spot beams heard: ${est.lat.toFixed(2)}, ${est.lon.toFixed(2)} (good to about 200 km, which can shift the sky view by up to 15°).`
             : "No estimate yet: it needs a few ring alerts that carry a spot-beam position.");
    dlgOpen("locdlg");
  };
  $("loc-save").onclick = () => {
    const lat = parseFloat($("loc-lat").value), lon = parseFloat($("loc-lon").value);
    if (!(lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180)) { $("loc-note").textContent = "Enter a latitude between -90 and 90 and a longitude between -180 and 180."; return; }
    setUserLoc({ lat, lon }); dlgClose("locdlg");
  };
  $("loc-est").onclick = () => { setUserLoc(null); dlgClose("locdlg"); };
  $("loc-pick").onclick = () => {
    dlgClose("locdlg");
    map.getContainer().classList.add("picking");
    map.once("click", (e) => {
      map.getContainer().classList.remove("picking");
      setUserLoc({ lat: +e.latlng.lat.toFixed(4), lon: +(((e.latlng.lng + 540) % 360) - 180).toFixed(4) });
    });
    map.getContainer().scrollIntoView({ behavior: "smooth", block: "center" });
  };

  // ---------- sky view: where the decoded satellite positions were, seen from the receiver ----------
  let lastRef = 0;
  function drawSky(s, ref) {
    lastRef = ref;
    const [g, w, h] = setupCanvas($("sky"));
    const rx = rxLocation();
    const cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2 - 16;
    const xy = (az, el) => { const r = ((90 - Math.max(0, el)) / 90) * R; return [cx + r * Math.sin(rad(az)), cy - r * Math.cos(rad(az))]; };
    g.font = "10px system-ui";
    const pts = [];
    if (rx) for (const sat of s.sats) {
      if (!sat.confirmed) continue;
      for (const p of sat.track) {
        const a = lookAngles(rx, p[0], p[1]);
        if (a.el > 0) pts.push({ az: a.az, el: a.el, t: p[2], snr: p[3], id: sat.id });
      }
    }
    // coverage: how many positions fell into each patch of sky (12 directions x 3 heights)
    const bins = new Array(36).fill(0);
    for (const p of pts) bins[Math.min(2, Math.floor(p.el / 30)) * 12 + Math.floor(p.az / 30) % 12]++;
    const bmax = Math.max(1, ...bins);
    for (let i = 0; i < 36; i++) {
      if (!bins[i]) continue;
      const ring = Math.floor(i / 12), sec = i % 12;
      const r0 = ((90 - (ring + 1) * 30) / 90) * R, r1 = ((90 - ring * 30) / 90) * R;
      const a0 = rad(sec * 30) - Math.PI / 2, a1 = rad(sec * 30 + 30) - Math.PI / 2;
      g.beginPath(); g.arc(cx, cy, r1, a0, a1); g.arc(cx, cy, r0, a1, a0, true); g.closePath();
      g.fillStyle = `rgba(56,189,248,${(0.05 + 0.22 * bins[i] / bmax).toFixed(3)})`; g.fill();
    }
    g.strokeStyle = css("--line"); g.fillStyle = css("--muted");
    for (const el of [0, 30, 60]) { g.beginPath(); g.arc(cx, cy, ((90 - el) / 90) * R, 0, 7); g.stroke(); }
    for (let az = 0; az < 360; az += 30) { const [x, y] = xy(az, 0); g.beginPath(); g.moveTo(cx, cy); g.lineTo(x, y); g.stroke(); }
    for (const [lbl, az] of [["N", 0], ["E", 90], ["S", 180], ["W", 270]]) {
      const [x, y] = xy(az, -9); g.fillText(lbl, x - g.measureText(lbl).width / 2, y + 4);
    }
    g.fillText("60°", cx + 3, cy - R / 3 - 2); g.fillText("30°", cx + 3, cy - (2 * R) / 3 - 2); g.fillText("horizon", cx + 3, cy - R + 11);
    $("sky-loc").textContent = !rx ? "" : rx.source === "estimate" ? `≈ ${rx.lat.toFixed(1)}, ${rx.lon.toFixed(1)} (estimated)` : `${rx.lat.toFixed(2)}, ${rx.lon.toFixed(2)}${rx.source === "demo" ? " (demo)" : ""}`;
    if (!rx) {
      g.fillStyle = css("--text");
      const m = "Waiting for a spot beam to estimate where you are…"; g.fillText(m, cx - g.measureText(m).width / 2, cy + 4);
      $("sky-note").textContent = "The sky view needs your position: it is estimated from the first few ring alerts, or set it with Location.";
      return;
    }
    // passes as lines, positions as dots, a label at each satellite heard in the last 2 minutes
    for (const sat of s.sats) {
      if (!sat.confirmed) continue;
      const color = satColor(sat.id);
      for (const seg of passes(sat.track, 120)) {
        const v = seg.map((p) => ({ a: lookAngles(rx, p[0], p[1]), p })).filter((q) => q.a.el > 0);
        if (!v.length) continue;
        g.strokeStyle = color; g.globalAlpha = .55; g.lineWidth = 1.5; g.beginPath();
        v.forEach((q, i) => { const [x, y] = xy(q.a.az, q.a.el); i ? g.lineTo(x, y) : g.moveTo(x, y); });
        g.stroke(); g.lineWidth = 1;
        for (const q of v) {
          const [x, y] = xy(q.a.az, q.a.el);
          g.globalAlpha = Math.min(1, Math.max(.35, ((q.p[3] || 15) - 8) / 14)); g.fillStyle = color;
          g.beginPath(); g.arc(x, y, 2.6, 0, 7); g.fill();
        }
        g.globalAlpha = 1;
        const lastQ = v[v.length - 1];
        if (ref - lastQ.p[2] < 120) {
          const [x, y] = xy(lastQ.a.az, lastQ.a.el);
          g.strokeStyle = "#fff"; g.beginPath(); g.arc(x, y, 4.5, 0, 7); g.stroke();
          g.fillStyle = css("--text"); g.fillText(String(sat.id), x + 7, y + 3);
        }
      }
    }
    g.globalAlpha = 1;
    g.fillStyle = "#fff"; g.beginPath(); g.arc(cx, cy, 2, 0, 7); g.fill();
    if (!pts.length) { $("sky-note").textContent = "No satellite positions in the last hour yet. Each ring alert that carries a satellite position adds a dot here."; return; }
    const els = pts.map((p) => p.el), lo = Math.min(...els), hi = Math.max(...els);
    let sx = 0, sy = 0, sw = 0;
    for (const p of pts) { const wgt = Math.cos(rad(p.el)); sx += wgt * Math.sin(rad(p.az)); sy += wgt * Math.cos(rad(p.az)); sw += wgt; }
    const strength = Math.hypot(sx, sy) / Math.max(1e-9, sw);
    const dir = strength < 0.25 ? "from all directions" : `mostly towards ${compass((deg(Math.atan2(sx, sy)) + 360) % 360)}`;
    const nsat = new Set(pts.map((p) => p.id)).size;
    $("sky-note").textContent = `${pts.length} positions from ${nsat} satellite${nsat === 1 ? "" : "s"} in the last hour · heard between ${lo.toFixed(0)}° and ${hi.toFixed(0)}° above the horizon, ${dir}.`
      + (strength >= 0.25 ? " The empty parts of the sky are blocked for this antenna." : "")
      + (rx.source === "estimate" ? " Location is estimated: set yours for exact angles." : "");
  }

  // ---------- satellite details ----------
  let satOpen = null;
  function openSat(id) { satOpen = id; $("sat-title").textContent = "Satellite " + id; dlgOpen("satdlg"); loadSat(); }
  $("satdlg").addEventListener("close", () => { satOpen = null; });
  async function loadSat() {
    if (satOpen == null || !$("satdlg").open) return;
    try {
      const r = await fetch(`/api/sat?view=${view}&id=${satOpen}`, { cache: "no-store" });
      if (r.ok) drawSat(await r.json());
    } catch (e) {}
  }
  setInterval(loadSat, 3000);
  function timeAxis(g, x, t0, t1, y, w) {
    g.fillStyle = css("--muted");
    const a = fmtTime(t0), b = fmtTime(t1);
    g.fillText(a, x(t0), y); g.fillText(b, Math.min(w - g.measureText(b).width - 2, x(t1) - g.measureText(b).width / 2), y);
  }
  function drawSat(d) {
    const rx = rxLocation(), color = satColor(d.id);
    const dopPasses = passes(d.doppler.map((p) => [p[1], p[2], p[0], p[3]]), 600);   // passes() keys on index 2 = time
    const pass = dopPasses.length ? dopPasses[dopPasses.length - 1].map((p) => ({ t: p[2], hz: p[0], snr: p[1], ch: p[3] })) : [];
    const chips = [`${d.frames} frames`, `first heard ${fmtTime(d.first)}`, `last heard ${fmtTime(d.last)}`, `${d.track.length} positions`,
      d.beams.length ? `beams ${few(d.beams, 10)}` : "", d.cells.length ? `cells ${few(d.cells, 10)}` : "",
      Object.entries(d.types).map(([k, v]) => `${k} ${v}`).join(" · ")].filter(Boolean);
    let peak = null;
    if (rx) for (const p of d.track) { const a = lookAngles(rx, p[0], p[1]); if (!peak || a.el > peak.el) peak = { el: a.el, az: a.az, t: p[2] }; }
    if (peak && peak.el > 0) chips.push(`highest at ${peak.el.toFixed(0)}° ${compass(peak.az)} (${fmtTime(peak.t)})`);
    if (!d.confirmed) chips.unshift("heard only once: possibly a decoding error");
    $("sat-chips").innerHTML = chips.map((c) => `<span class="chip on">${esc(c)}</span>`).join("");

    // Doppler curve of the latest pass
    let [g, w, h] = setupCanvas($("sat-dop"));
    const padL = 40, padB = 18, padT = 10;
    g.font = "10px system-ui"; g.fillStyle = css("--muted");
    $("sat-dop-note").textContent = "";
    if (pass.length < 2) {
      g.fillText("Needs at least two ring alerts from this satellite.", padL, h / 2);
    } else {
      // closest approach: where the shift goes from + (approaching) to - (moving away)
      let tca = null, slope = null, edge = false;
      for (let i = 1; i < pass.length; i++) {
        const a = pass[i - 1], b = pass[i];
        if (a.hz > 0 && b.hz <= 0 && b.t - a.t < 300) { tca = a.t + (a.hz / (a.hz - b.hz)) * (b.t - a.t); slope = (b.hz - a.hz) / (b.t - a.t); }
      }
      // heard from just after (or until just before) the closest approach: extend the first (last) steps
      const ends = [[pass[0], pass[Math.min(2, pass.length - 1)], -1], [pass[pass.length - 1], pass[Math.max(0, pass.length - 3)], 1]];
      if (!tca) for (const [p, q, side] of ends) {
        const sl = (p.hz - q.hz) / (p.t - q.t);
        if (Math.abs(p.hz) < 4000 && sl < -20 && Math.sign(p.hz) === side && p.t !== q.t) { tca = p.t - p.hz / sl; slope = sl; edge = true; }
      }
      // the time axis also covers an extrapolated closest approach
      const t0 = Math.min(pass[0].t, tca || Infinity), t1 = Math.max(pass[pass.length - 1].t, tca || 0, t0 + 30);
      const span = Math.max(45, Math.ceil(Math.max(...pass.map((p) => Math.abs(p.hz))) / 5000) * 5);   // kHz
      const x = (t) => padL + ((t - t0) / (t1 - t0)) * (w - padL - 10), y = (khz) => padT + (1 - (khz + span) / (2 * span)) * (h - padT - padB);
      g.strokeStyle = css("--line");
      for (let k = -span; k <= span; k += span > 60 ? 30 : 15) {
        g.beginPath(); g.moveTo(padL, y(k) + .5); g.lineTo(w, y(k) + .5); g.stroke();
        g.fillText((k > 0 ? "+" : "") + k + " kHz", 2, y(k) + 3);
      }
      g.strokeStyle = "rgba(255,255,255,.35)"; g.beginPath(); g.moveTo(padL, y(0) + .5); g.lineTo(w, y(0) + .5); g.stroke();
      g.strokeStyle = color; g.globalAlpha = .5; g.beginPath();
      pass.forEach((p, i) => (i ? g.lineTo(x(p.t), y(p.hz / 1000)) : g.moveTo(x(p.t), y(p.hz / 1000)))); g.stroke(); g.globalAlpha = 1;
      g.fillStyle = color;
      for (const p of pass) { g.beginPath(); g.arc(x(p.t), y(p.hz / 1000), 2.8, 0, 7); g.fill(); }
      const first = pass[0].hz, lastHz = pass[pass.length - 1].hz;
      let note = `latest pass, ${pass.length} ring alerts on ${(pass[pass.length - 1].ch / 1e6).toFixed(4)} MHz`;
      if (tca) {
        g.setLineDash([4, 3]); g.strokeStyle = css("--warn"); g.beginPath(); g.moveTo(x(tca), padT); g.lineTo(x(tca), h - padB); g.stroke(); g.setLineDash([]);
        g.fillStyle = css("--warn"); g.fillText("closest", Math.min(w - 44, x(tca) + 4), padT + 9);
        note += ` · closest approach ${edge ? "(extrapolated) " : ""}about ${fmtTime(tca)} · ${Math.abs(slope).toFixed(0)} Hz/s at the crossing (${Math.abs(slope) > 280 ? "a high pass" : Math.abs(slope) > 140 ? "a medium pass" : "a low pass, far to the side"})`;
      } else note += first > 0 && lastHz > 0 ? " · still approaching" : first < 0 && lastHz < 0 ? " · heard only while moving away" : "";
      if (dopPasses.length > 1) note += ` · ${dopPasses.length - 1} earlier pass${dopPasses.length > 2 ? "es" : ""} not shown`;
      $("sat-dop-note").textContent = note;
      timeAxis(g, x, t0, t1, h - 4, w);

      // signal (and elevation, when the location is known) over the same time span
      [g, w, h] = setupCanvas($("sat-snr"));
      g.font = "10px system-ui";
      const ys = (v) => padT + (1 - (v - 5) / 30) * (h - padT - padB);
      g.strokeStyle = css("--line"); g.fillStyle = css("--muted");
      for (const v of [10, 20, 30]) { g.beginPath(); g.moveTo(padL, ys(v) + .5); g.lineTo(w, ys(v) + .5); g.stroke(); g.fillText(v + " dB", 2, ys(v) + 3); }
      const elPts = rx ? d.track.filter((p) => p[2] >= t0 - 5 && p[2] <= t1 + 5).map((p) => ({ t: p[2], el: lookAngles(rx, p[0], p[1]).el })).filter((p) => p.el > 0) : [];
      if (elPts.length > 1) {
        const ye = (el) => padT + (1 - el / 90) * (h - padT - padB);
        g.strokeStyle = "rgba(255,255,255,.55)"; g.setLineDash([5, 3]); g.beginPath();
        elPts.forEach((p, i) => (i ? g.lineTo(x(p.t), ye(p.el)) : g.moveTo(x(p.t), ye(p.el)))); g.stroke(); g.setLineDash([]);
        g.fillStyle = "rgba(255,255,255,.7)"; g.fillText("elevation (0–90°)", w - 96, padT + 9);
      }
      $("sat-snr-note").textContent = "SNR of each ring alert" + (elPts.length > 1 ? " · dashed: elevation above the horizon" : "");
      g.fillStyle = color;
      for (const p of pass) { g.beginPath(); g.arc(x(p.t), ys(Math.min(35, p.snr)), 2.8, 0, 7); g.fill(); }
      timeAxis(g, x, t0, t1, h - 4, w);
      return;
    }
    [g, w, h] = setupCanvas($("sat-snr"));
  }

  // ---------- receiver health ----------
  function updateHealth(s, now) {
    const e = s.extractor || {};
    const pct = (v) => (v != null ? v + "%" : "–");
    $("h-bursts").textContent = e.bursts_per_s != null ? e.bursts_per_s : e.bursts_total != null ? e.bursts_total.toLocaleString() + " total" : "–";
    $("h-ok").textContent = pct(e.ok_now);
    $("h-okavg").textContent = pct(e.ok_avg);
    $("h-queue").textContent = e.queue != null ? e.queue : "–";
    $("h-drop").textContent = e.dropped != null ? e.dropped.toLocaleString() : "–";
    $("h-rej").textContent = (s.rejected || 0).toLocaleString();
    $("h-drop").classList.toggle("badv", e.dropped > 0);
    const tips = [];
    const live = s.mode === "live", up = now - s.started;
    if (s.mode === "demo") tips.push(["info", "Simulated data: there is no receiver. Start a real one with the Receiver button."]);
    if (s.mode === "idle") tips.push(["info", "No receiver is running. Start one with the Receiver button, or open a recording."]);
    if (live && !s.total && up > 90) tips.push(["bad", `No frame decoded in ${Math.round(up / 60)} min. Check that the antenna is powered (bias tee) and can see some sky; indoors, a window helps a lot.`]);
    if (live && s.total && s.last_frame && now - s.last_frame > 300) tips.push(["warn", `No frame for ${Math.round((now - s.last_frame) / 60)} minutes. Indoors that happens between passes; outdoors it suggests a loose antenna or lost antenna power.`]);
    if (e.dropped > 0) tips.push(["bad", "The decoder is dropping bursts because the computer can't keep up. Close other programs, or use iridium-sniffer (the Receiver button)."]);
    if (e.srr != null && e.srr < 97) tips.push(["bad", `Only ${e.srr}% of the samples arrive from the radio: USB or CPU is too slow (in a VM, give it more cores and USB 3).`]);
    if (e.bursts_per_s > 150 && e.ok_avg != null && e.ok_avg < 10) tips.push(["warn", "Many detections but almost none decode: that is interference, not Iridium. On a HackRF, check that the manual IF in configs/hackrf.conf is in use."]);
    if (s.rejected) tips.push(["info", `${s.rejected} satellite position${s.rejected === 1 ? " was" : "s were"} left out as impossible jumps (bit errors in weak frames).`]);
    if (!tips.length && e.ok_avg != null) tips.push([e.ok_avg >= 50 ? "good" : "info", e.ok_avg >= 50 ? `Healthy: ${e.ok_avg}% of the detected bursts decode.` : `${e.ok_avg}% of the detected bursts decode. A weak signal lowers this; 50% and more is good.`]);
    $("health-tips").innerHTML = tips.map(([k, t]) => `<li class="${k}">${esc(t)}</li>`).join("");
    $("health-note").textContent = e.t && isLive(s) ? `statistics ${ago(e.t, now)}` : "";
  }

  // ---------- receiver control ----------
  let rxStatus = null, rxChoice = null, rxDismissed = 0;
  try { rxDismissed = +sessionStorage.getItem("iri.rxDismissed") || 0; } catch (e) {}
  function updateRxButton(state) {
    if (!state) { $("rxbtn").classList.add("hidden"); return; }
    $("rxdot").className = "dot " + state;
    $("rxlabel").textContent = { idle: "Receiver", running: "Receiving", recording: "Recording IQ", stopping: "Stopping…" }[state] || "Receiver";
  }
  async function loadRx() {
    try {
      const r = await fetch("/api/receiver", { cache: "no-store" });
      if (!r.ok) return;
      rxStatus = await r.json();
    } catch (e) { return; }
    updateRxButton(rxStatus.state);
    renderRx();
    const l = rxStatus.last;
    const show = rxStatus.state === "idle" && l && l.failed && l.ended > rxDismissed;
    $("rxalert").classList.toggle("hidden", !show);
    if (show) {
      $("rxalert-title").textContent = l.kind === "record" ? "The IQ recording failed." : "The receiver stopped.";
      $("rxalert-text").textContent = l.error || "";
    }
    if (rxStatus.main_mode === "idle" && !autoOpened && view === "live" && !$("recdlg").open) { autoOpened = true; dlgOpen("rxdlg"); }
  }
  function renderRx() {
    const st = rxStatus; if (!st) return;
    const j = st.job, l = st.last;
    let text;
    if (j && j.kind === "live") text = `${st.state === "stopping" ? "Stopping" : "Receiving"} with the ${j.radio}${j.demod ? " · " + j.demod : ""} · ${fmtSpan(j.elapsed)} · ${(j.frames || 0).toLocaleString()} frames${j.seconds ? ` · stops after ${fmtSpan(j.seconds)}` : ""}`;
    else if (j) text = `${st.state === "stopping" ? "Finishing" : "Recording"} raw IQ from the ${j.radio} · ${fmtSpan(j.elapsed)}${j.seconds ? " of " + fmtSpan(j.seconds) : ""} · ${fmtSize(j.size || 0)}`;
    else if (l && !l.failed) text = l.kind === "record" ? `Idle. Last recording: ${l.file} (${fmtSize(l.size)}, ${fmtSpan(l.duration)}). Open it under Recordings.`
      : `Idle. Last run: ${(l.frames || 0).toLocaleString()} frames in ${fmtSpan(l.duration)} with the ${l.radio}.`;
    else text = "Idle. Choose a radio and start.";
    $("rx-status").textContent = text;
    const err = $("rx-error");
    const failed = !j && l && l.failed;
    if (failed) err.textContent = l.error;
    if (!rxErrorSticky) err.classList.toggle("hidden", !failed);
    $("rx-log").textContent = l && l.log ? l.log.join("\n") : "No log yet.";
    if (rxChoice == null || !st.configs.some((c) => c.name === rxChoice)) {
      const c = st.configs.find((c) => c.connected) || st.configs[0];
      rxChoice = c ? c.name : null;
    }
    const key = JSON.stringify(st.configs) + rxChoice;
    if (key !== renderRx.key) {
      renderRx.key = key;
      $("rx-radios").innerHTML = st.configs.map((c) => `
        <label class="rx-radio${c.name === rxChoice ? " on" : ""}">
          <input type="radio" name="rxsdr" value="${esc(c.name)}"${c.name === rxChoice ? " checked" : ""}>
          <span><b>${esc(c.radio)}</b> <span class="muted">${esc(c.name)}.conf · ${c.centre ? `${((c.centre - c.rate / 2) / 1e6).toFixed(1)}–${((c.centre + c.rate / 2) / 1e6).toFixed(1)} MHz` : ""}${c.antenna_power ? " · antenna power on" : ""}${c.manual_if ? " · manual IF" : ""}</span></span>
          <span class="badge ${c.connected ? "ok" : c.connected === false ? "no" : ""}">${c.connected ? "connected" : c.connected === false ? "not found" : "unknown"}</span>
        </label>`).join("") || '<div class="empty">No SDR configs in configs/.</div>';
    }
    const idle = st.state === "idle";
    $("rx-start").disabled = $("rx-record").disabled = !idle || !rxChoice;
    $("rx-stop").disabled = !(st.state === "running" || st.state === "recording");
    const d = st.demods || {};
    $("rx-demod").querySelector('[value="sniffer"]').disabled = !d.sniffer;
    $("rx-demod").querySelector('[value="gr"]').disabled = !d.gr;
  }
  let rxErrorSticky = false;
  $("rx-radios").addEventListener("change", (e) => { if (e.target.name === "rxsdr") { rxChoice = e.target.value; renderRx(); } });
  async function rxAction(path, body) {
    rxErrorSticky = false;
    try {
      await post(path, body);
      $("rx-error").classList.add("hidden");
      rxDismissed = Date.now() / 1000;           // an earlier failure is no longer news
      return true;
    } catch (err) {
      $("rx-error").textContent = err.message; $("rx-error").classList.remove("hidden"); rxErrorSticky = true;
      return false;
    } finally { loadRx(); }
  }
  const rxSeconds = () => { const m = parseFloat($("rx-min").value); return m > 0 ? Math.round(m * 60) : null; };
  $("rx-start").onclick = async () => {
    if (await rxAction("/api/receiver/start", { sdr: rxChoice, seconds: rxSeconds(), demod: $("rx-demod").value })) {
      dlgClose("rxdlg"); if (view !== "live") switchView("live");
    }
  };
  $("rx-record").onclick = () => rxAction("/api/receiver/record", { sdr: rxChoice, seconds: rxSeconds() });
  $("rx-stop").onclick = () => rxAction("/api/receiver/stop", {});
  $("rxbtn").onclick = () => { dlgOpen("rxdlg"); loadRx(); };
  $("rxalert-open").onclick = () => { dlgOpen("rxdlg"); loadRx(); };
  $("rxalert-x").onclick = () => {
    rxDismissed = Date.now() / 1000; try { sessionStorage.setItem("iri.rxDismissed", rxDismissed); } catch (e) {}
    $("rxalert").classList.add("hidden");
  };
  setInterval(() => loadRx(), 3000);
  loadRx();

  // ---------- export ----------
  $("exportmenu").addEventListener("click", (e) => {
    const a = e.target.closest("a[data-what]"); if (!a) return;
    a.href = `/api/export?view=${view}&what=${a.dataset.what}`;
    a.setAttribute("download", "");
    setTimeout(() => { $("exportmenu").open = false; }, 0);
  });
  document.addEventListener("click", (e) => { if (!$("exportmenu").contains(e.target)) $("exportmenu").open = false; });

  window.addEventListener("resize", () => last && (freqMode === "hist" ? drawFreq(last.freq) : drawWaterfall(last), drawSnr(last.snr_series), updateVoice(last.calls), drawSky(last, lastRef)));
  // the waterfall scrolls with time even when no new frames arrive
  setInterval(() => last && freqMode === "wf" && isLive(last) && drawWaterfall(last), 5000);
  setFreqMode(freqMode); updateChips(); renderMsgs(); connect();
  // links straight to a dialog: #receiver, #recordings, #sat=59
  const hash = location.hash;
  if (hash === "#receiver") { autoOpened = true; dlgOpen("rxdlg"); }
  else if (hash === "#recordings") { autoOpened = true; openDialog(); }
  else if (/^#sat=\d+$/.test(hash)) { autoOpened = true; openSat(+hash.slice(5)); }
})();
