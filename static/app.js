/*
 * app.js — SPA controller for the VideoAnnotations studio.
 *
 * Five tabs, all sharing the dark aesthetic of the original timeline_viewer.html:
 *   - Annotate: pull frames from /api/annotator/next, label with 1/2/3, undo with Ctrl+Z
 *   - Timeline: load from /api/timeline/data or paste/upload, render color-coded bars
 *               (parsing + rendering ported verbatim from the original viewer)
 *   - Crop:     POST params to /api/crop/start, stream the SSE log
 *   - Concat:   pick a folder, POST to /api/concat/start, stream SSE log
 *   - Consolidate: multipart upload, render summary, link to download
 */

(() => {
  // ====================================================================
  // Utilities
  // ====================================================================
  const $  = (id) => document.getElementById(id);
  const VALID_LABELS = new Set(['no', 'yes', 'perfect']);
  const LABEL_RANK = { no: 1, yes: 2, perfect: 3 };
  const TS_RE = /^(\d{2}):(\d{2}):(\d{2})\.(\d{3})$/;

  function tsToSec(ts) {
    ts = (ts || '').trim();
    const m = TS_RE.exec(ts);
    if (m) return (+m[1])*3600 + (+m[2])*60 + (+m[3]) + (+m[4])/1000;
    const parts = ts.split(':');
    try {
      if (parts.length === 3) return +parts[0]*3600 + +parts[1]*60 + parseFloat(parts[2]);
      if (parts.length === 2) return +parts[0]*60 + parseFloat(parts[1]);
      return parseFloat(parts[0]) || 0;
    } catch { return 0; }
  }

  function fmtTime(sec) {
    sec = Math.max(0, sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${s.toFixed(2).padStart(5,'0')}`;
    return `${m}:${s.toFixed(2).padStart(5,'0')}`;
  }

  function clampInt(v, lo, hi, fallback) {
    const n = parseInt(v, 10);
    if (Number.isNaN(n)) return fallback;
    return Math.max(lo, Math.min(hi, n));
  }

  // ====================================================================
  // Tab switching
  // ====================================================================
  let activeTab = 'annotate';
  document.querySelectorAll('nav.tabs button').forEach(btn => {
    btn.addEventListener('click', () => {
      const tab = btn.dataset.tab;
      document.querySelectorAll('nav.tabs button').forEach(b =>
        b.classList.toggle('active', b === btn));
      document.querySelectorAll('.view').forEach(v =>
        v.classList.toggle('active', v.id === `view-${tab}`));
      activeTab = tab;

      // Lazy initializers per tab
      if (tab === 'concat' && !concatFoldersLoaded) {
        loadConcatFolders();
        concatFoldersLoaded = true;
      }
      if (tab === 'timeline' && !tlInitialized) {
        loadLiveTimelineData();
        tlInitialized = true;
      }
    });
  });

  // ====================================================================
  // Server status
  // ====================================================================
  async function refreshStatus() {
    try {
      const r = await fetch('/api/status');
      const s = await r.json();
      $('dotEngine').className  = 'dot ' + (s.engine_loaded ? 'ok' : (s.engine_error ? 'bad' : ''));
      $('dotFfmpeg').className  = 'dot ' + (s.ffmpeg_present && s.ffprobe_present ? 'ok' : 'bad');
      $('dotData').className    = 'dot ' + (s.annotation_file_exists ? 'ok' : 'bad');
      $('pathVideoDir').textContent = s.video_dir;
      $('pathAnnFile').textContent  = s.annotation_file;
    } catch (e) { /* server down */ }
  }
  refreshStatus();

  // ====================================================================
  // ANNOTATE
  // ====================================================================
  let annLoading = false;

  function setStats(stats) {
    if (!stats) return;
    const c = stats.counts || {yes:0,no:0,perfect:0};
    $('cntPerfect').textContent = c.perfect;
    $('cntYes').textContent     = c.yes;
    $('cntNo').textContent      = c.no;
    $('cntTotal').textContent   = stats.total || 0;
    $('cntUndo').textContent    = stats.undo_depth || 0;
    $('btnUndo').disabled       = !(stats.undo_depth > 0);

    const cov = stats.videos_total ? (stats.videos_annotated / stats.videos_total) : 0;
    $('covLabel').textContent = `${stats.videos_annotated}/${stats.videos_total} videos annotated`;
    $('covBar').style.width   = (cov * 100).toFixed(1) + '%';

    const q = stats.queue_max ? (stats.queue_size / stats.queue_max) : 0;
    $('qLabel').textContent   = `${stats.queue_size}/${stats.queue_max} frames buffered`;
    $('qBar').style.width     = (q * 100).toFixed(1) + '%';
  }

  async function fetchNextFrame() {
    if (annLoading) return;
    annLoading = true;
    try {
      const r = await fetch('/api/annotator/next', { cache: 'no-store' });
      if (r.status === 503) {
        $('annImgWrap').innerHTML = '<div class="placeholder" style="color:var(--danger)">Engine not loaded — check server console.</div>';
        return;
      }
      if (r.status === 204) {
        $('annImgWrap').innerHTML = '<div class="placeholder">No frame available yet — preloader is filling…</div>';
        setTimeout(fetchNextFrame, 1000);
        return;
      }
      const vidName = r.headers.get('X-Video-Name') || '—';
      const ts      = r.headers.get('X-Timestamp')  || '—';
      const blob = await r.blob();
      const url  = URL.createObjectURL(blob);
      const wrap = $('annImgWrap');
      // Revoke the previous URL when image swaps to keep memory bounded
      const old = wrap.querySelector('img');
      if (old && old.src.startsWith('blob:')) URL.revokeObjectURL(old.src);
      wrap.innerHTML = '';
      const img = document.createElement('img');
      img.src = url;
      img.alt = vidName;
      wrap.appendChild(img);
      $('annVideoName').textContent = vidName;
      $('annTimestamp').textContent = ts;

      // Refresh stats after each new frame
      refreshAnnStats();
    } catch (e) {
      $('annImgWrap').innerHTML = `<div class="placeholder" style="color:var(--danger)">${e.message}</div>`;
    } finally {
      annLoading = false;
    }
  }

  async function refreshAnnStats() {
    try {
      const r = await fetch('/api/annotator/stats');
      const j = await r.json();
      if (j.ok) setStats(j.stats);
    } catch (e) { /* swallow */ }
  }

  async function recordLabel(label) {
    if (annLoading) return;
    try {
      const r = await fetch('/api/annotator/record', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label }),
      });
      const j = await r.json();
      if (j.stats) setStats(j.stats);
    } catch (e) { /* swallow */ }
    fetchNextFrame();
  }

  async function undoLast() {
    try {
      const r = await fetch('/api/annotator/undo', { method: 'POST' });
      const j = await r.json();
      if (j.stats) setStats(j.stats);
      if (j.ok && j.reshown) {
        // Backend already re-showed the undone frame; fetch /current to display it
        const cr = await fetch('/api/annotator/current', { cache: 'no-store' });
        if (cr.status === 200) {
          const vidName = cr.headers.get('X-Video-Name') || '—';
          const ts      = cr.headers.get('X-Timestamp')  || '—';
          const blob = await cr.blob();
          const url  = URL.createObjectURL(blob);
          const wrap = $('annImgWrap');
          const old = wrap.querySelector('img');
          if (old && old.src.startsWith('blob:')) URL.revokeObjectURL(old.src);
          wrap.innerHTML = '';
          const img = document.createElement('img');
          img.src = url;
          wrap.appendChild(img);
          $('annVideoName').textContent = vidName + '  (undone)';
          $('annTimestamp').textContent = ts;
        }
      } else {
        fetchNextFrame();
      }
    } catch (e) { /* swallow */ }
  }

  // Buttons
  $('btnYes').addEventListener('click',     () => recordLabel('yes'));
  $('btnNo').addEventListener('click',      () => recordLabel('no'));
  $('btnPerfect').addEventListener('click', () => recordLabel('perfect'));
  $('btnUndo').addEventListener('click',    undoLast);

  // Keyboard
  document.addEventListener('keydown', (e) => {
    // Only act in the annotate view, and avoid triggering inside inputs
    if (activeTab !== 'annotate') return;
    if (e.target.matches('input, textarea, select')) return;
    if (e.ctrlKey && (e.key === 'z' || e.key === 'Z')) { e.preventDefault(); undoLast(); return; }
    if (e.key === '1') { e.preventDefault(); recordLabel('yes'); }
    else if (e.key === '2') { e.preventDefault(); recordLabel('no'); }
    else if (e.key === '3') { e.preventDefault(); recordLabel('perfect'); }
  });

  // Kick off the annotator
  (async () => {
    try {
      const r = await fetch('/api/annotator/init');
      const j = await r.json();
      if (j.ok) {
        setStats(j.stats);
        fetchNextFrame();
      } else {
        $('annImgWrap').innerHTML = `<div class="placeholder" style="color:var(--danger)">Engine init failed: ${j.error}</div>`;
      }
    } catch (e) {
      $('annImgWrap').innerHTML = `<div class="placeholder" style="color:var(--danger)">${e.message}</div>`;
    }
  })();

  // Periodic stats refresh (queue depth, etc.)
  setInterval(() => {
    if (activeTab === 'annotate') refreshAnnStats();
  }, 2000);

  // ====================================================================
  // TIMELINE (parsing + rendering ported verbatim from timeline_viewer.html)
  // ====================================================================
  let tlInitialized = false;

  function parseLine(rawLine) {
    const idx = rawLine.indexOf('|');
    if (idx < 0) return null;
    const name = rawLine.slice(0, idx).trim();
    const rest = rawLine.slice(idx + 1).trim();
    if (!name || !rest) return null;

    if (rest.startsWith('[') && rest.endsWith(']')) {
      const inside = rest.slice(1, -1);
      const labels = [];
      for (const ch of inside) {
        if (ch === 'O' || ch === '+') labels.push('perfect');
        else if (ch === 'o' || ch === '=') labels.push('yes');
        else if (ch === ' ') labels.push('no');
        else labels.push('empty');
      }
      return { name, kind: 'prebuilt', labels };
    }

    const tsMap = {};
    for (let chunk of rest.split(',')) {
      chunk = chunk.trim();
      if (!chunk || !chunk.includes('=')) continue;
      const eq = chunk.indexOf('=');
      let ts = chunk.slice(0, eq).trim();
      // Defensive: strip junk like '#conflict2' that we found in the wild
      if (ts.includes('#')) ts = ts.split('#')[0].trim();
      const lab = chunk.slice(eq + 1).trim().toLowerCase();
      if (ts && VALID_LABELS.has(lab)) tsMap[ts] = lab;
    }
    if (Object.keys(tsMap).length === 0) return null;
    return { name, kind: 'annotations', tsMap };
  }

  function buildSlots(entry, width, unlabeledAsNo) {
    if (entry.kind === 'prebuilt') {
      const src = entry.labels;
      if (src.length === 0) {
        return { slots: new Array(width).fill('empty'), duration: null,
                 counts: {no:0,yes:0,perfect:0}, annCount: 0 };
      }
      const slots = new Array(width);
      for (let i = 0; i < width; i++) {
        const j = Math.round((i / (width - 1 || 1)) * (src.length - 1));
        slots[i] = src[j];
      }
      if (unlabeledAsNo) {
        for (let i = 0; i < width; i++) if (slots[i] === 'empty') slots[i] = 'no';
      }
      const counts = { no: 0, yes: 0, perfect: 0 };
      for (const s of src) if (counts[s] !== undefined) counts[s]++;
      return { slots, duration: null, counts, annCount: src.length };
    }

    const tsMap = entry.tsMap;
    const entries = Object.entries(tsMap).map(([ts, lab]) => [tsToSec(ts), lab]);
    const maxTs = entries.reduce((a, [t]) => Math.max(a, t), 0);
    const duration = Math.max(0.001, maxTs);

    const slots = new Array(width).fill(unlabeledAsNo ? 'no' : 'empty');
    const ranks = new Array(width).fill(unlabeledAsNo ? LABEL_RANK.no : 0);

    const counts = { no: 0, yes: 0, perfect: 0 };
    for (const [t, lab] of entries) {
      counts[lab]++;
      const tc = Math.max(0, Math.min(duration, t));
      let i = Math.round((tc / duration) * (width - 1));
      i = Math.max(0, Math.min(width - 1, i));
      if (LABEL_RANK[lab] >= ranks[i]) {
        slots[i] = lab;
        ranks[i] = LABEL_RANK[lab];
      }
    }
    return { slots, duration, counts, annCount: entries.length };
  }

  function renderRuns(slots) {
    const frag = document.createDocumentFragment();
    if (slots.length === 0) return frag;
    let curLab = slots[0], curLen = 1, startIdx = 0;
    const total = slots.length;
    const flush = (lab, len, s) => {
      const seg = document.createElement('div');
      seg.className = 'seg ' + lab;
      seg.style.flexGrow = String(len);
      seg.dataset.lab = lab;
      seg.dataset.start = String(s);
      seg.dataset.end = String(s + len - 1);
      seg.dataset.total = String(total);
      frag.appendChild(seg);
    };
    for (let i = 1; i < slots.length; i++) {
      if (slots[i] === curLab) { curLen++; continue; }
      flush(curLab, curLen, startIdx);
      startIdx = i; curLab = slots[i]; curLen = 1;
    }
    flush(curLab, curLen, startIdx);
    return frag;
  }

  function buildAxis(duration, width) {
    const axis = document.createElement('div');
    axis.className = 'axis';
    if (!duration || duration <= 0) {
      ['start', 'end'].forEach((label, i) => {
        const t = document.createElement('div');
        t.className = 'tick';
        t.style.left = i === 0 ? '0%' : '100%';
        t.textContent = label;
        axis.appendChild(t);
      });
      return axis;
    }
    for (const p of [0, 0.25, 0.5, 0.75, 1]) {
      const t = document.createElement('div');
      t.className = 'tick';
      t.style.left = (p * 100) + '%';
      t.textContent = fmtTime(duration * p);
      axis.appendChild(t);
    }
    return axis;
  }

  function renderAll(entries) {
    const out = $('tlOutput');
    out.innerHTML = '';

    if (entries.length === 0) {
      out.innerHTML = '<div class="empty-state"><div class="big">No valid lines parsed</div><div>Each line should look like: <code>name | 00:00:01.000=yes, …</code></div></div>';
      $('tlStats').style.display = 'none';
      return;
    }

    const width = clampInt($('tlWidth').value, 40, 2000, 300);
    const showAxis = $('tlShowAxis').checked;
    const unlabeledAsNo = $('tlUnlabeledNo').checked;
    const sortMode = $('tlSort').value;

    const computed = entries.map(e => {
      const built = buildSlots(e, width, unlabeledAsNo);
      const perfectPct = built.annCount > 0 ? (built.counts.perfect / built.annCount) * 100 : 0;
      const yesPct = built.annCount > 0 ? (built.counts.yes / built.annCount) * 100 : 0;
      return { entry: e, built, perfectPct, yesPct };
    });

    if (sortMode === 'name')       computed.sort((a,b) => a.entry.name.localeCompare(b.entry.name));
    else if (sortMode === 'duration') computed.sort((a,b) => (b.built.duration||0) - (a.built.duration||0));
    else if (sortMode === 'perfectPct') computed.sort((a,b) => b.perfectPct - a.perfectPct);
    else if (sortMode === 'yesPct')   computed.sort((a,b) => b.yesPct - a.yesPct);

    let totalAnns = 0, totalPerfect = 0, totalYes = 0, totalDur = 0;

    for (const { entry, built } of computed) {
      const row = document.createElement('div');
      row.className = 'row';
      const head = document.createElement('div');
      head.className = 'row-head';
      const name = document.createElement('div');
      name.className = 'name';
      name.textContent = entry.name;
      const meta = document.createElement('div');
      meta.className = 'meta';
      const durTxt = built.duration != null ? fmtTime(built.duration) : '—';
      meta.innerHTML = `
        <span class="pill">⏱ ${durTxt}</span>
        <span class="pill"><span class="mini perfect"></span>${built.counts.perfect}</span>
        <span class="pill"><span class="mini yes"></span>${built.counts.yes}</span>
        <span class="pill"><span class="mini no"></span>${built.counts.no}</span>
      `;
      head.appendChild(name); head.appendChild(meta);

      const frame = document.createElement('div');
      frame.className = 'bar-frame';
      frame.appendChild(renderRuns(built.slots));

      row.appendChild(head);
      row.appendChild(frame);
      if (showAxis) row.appendChild(buildAxis(built.duration, width));

      out.appendChild(row);

      totalAnns += built.annCount;
      totalPerfect += built.counts.perfect;
      totalYes += built.counts.yes;
      if (built.duration) totalDur += built.duration;

      frame.addEventListener('mousemove', onSegHover);
      frame.addEventListener('mouseleave', hideTooltip);
    }

    $('tlStats').style.display = '';
    $('tlSVideos').textContent = String(computed.length);
    $('tlSAnns').textContent   = String(totalAnns);
    const pctP = totalAnns ? (totalPerfect / totalAnns * 100) : 0;
    const pctY = totalAnns ? (totalYes / totalAnns * 100) : 0;
    $('tlSPerfect').innerHTML = `${pctP.toFixed(1)}<span class="unit">%</span>`;
    $('tlSYes').innerHTML     = `${pctY.toFixed(1)}<span class="unit">%</span>`;
    $('tlSDur').innerHTML     = `${fmtTime(totalDur)}<span class="unit"></span>`;
  }

  // Tooltip
  const tooltipEl = $('tooltip');
  function onSegHover(e) {
    const target = e.target.closest('.seg');
    if (!target) { hideTooltip(); return; }
    const lab = target.dataset.lab;
    const start = +target.dataset.start;
    const end = +target.dataset.end;
    const total = +target.dataset.total;
    const row = target.closest('.row');
    const center = ((start + end) / 2) / Math.max(1, total - 1);
    const durTxt = row?.querySelector('.meta .pill')?.textContent?.replace('⏱', '').trim() || '';
    let timeStr = '';
    const durSec = (() => {
      const parts = durTxt.split(':');
      try {
        if (parts.length === 3) return +parts[0]*3600 + +parts[1]*60 + parseFloat(parts[2]);
        if (parts.length === 2) return +parts[0]*60 + parseFloat(parts[1]);
        return parseFloat(parts[0]);
      } catch { return NaN; }
    })();
    if (!Number.isNaN(durSec) && durSec > 0) {
      timeStr = ` · ${fmtTime(durSec * center)}`;
    }
    const segPct = ((end - start + 1) / total * 100).toFixed(1);
    tooltipEl.innerHTML = `<span class="lab ${lab}">${lab}</span>${timeStr} · ${segPct}% wide`;
    tooltipEl.classList.add('show');
    positionTooltip(e);
  }
  function positionTooltip(e) {
    const pad = 14;
    let x = e.clientX + pad;
    let y = e.clientY + pad;
    const tw = tooltipEl.offsetWidth, th = tooltipEl.offsetHeight;
    if (x + tw + pad > window.innerWidth)  x = e.clientX - tw - pad;
    if (y + th + pad > window.innerHeight) y = e.clientY - th - pad;
    tooltipEl.style.left = x + 'px';
    tooltipEl.style.top  = y + 'px';
  }
  function hideTooltip() { tooltipEl.classList.remove('show'); }
  document.addEventListener('mousemove', e => {
    if (tooltipEl.classList.contains('show')) positionTooltip(e);
  });

  // Wiring
  function parseInputText(text) {
    const out = [];
    for (const raw of text.split(/\r?\n/)) {
      const line = raw.trim();
      if (!line) continue;
      const p = parseLine(line);
      if (p) out.push(p);
    }
    return out;
  }

  $('tlRender').addEventListener('click', () => {
    const text = $('tlInput').value;
    $('tlErr').textContent = '';
    const entries = parseInputText(text);
    if (entries.length === 0 && text.trim().length > 0) {
      $('tlErr').textContent = 'Could not parse any lines. Format: name | 00:00:01.000=yes, …';
    }
    renderAll(entries);
  });

  $('tlClear').addEventListener('click', () => {
    $('tlInput').value = '';
    $('tlErr').textContent = '';
    $('tlOutput').innerHTML = '<div class="empty-state"><div class="big">No timelines yet</div><div>Hit <strong style="color:var(--accent)">Load live data</strong> to pull from the current annotation file.</div></div>';
    $('tlStats').style.display = 'none';
  });

  $('tlFile').addEventListener('change', async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    $('tlInput').value = await f.text();
    $('tlRender').click();
  });

  async function loadLiveTimelineData() {
    try {
      const r = await fetch('/api/timeline/data');
      const text = await r.text();
      $('tlInput').value = text;
      $('tlRender').click();
    } catch (e) {
      $('tlErr').textContent = e.message;
    }
  }
  $('tlLoadLive').addEventListener('click', loadLiveTimelineData);

  // Re-render on option change
  for (const id of ['tlWidth', 'tlSort', 'tlShowAxis', 'tlUnlabeledNo']) {
    $(id).addEventListener('change', () => {
      if ($('tlInput').value.trim()) $('tlRender').click();
    });
  }

  // ====================================================================
  // SHARED: SSE log streaming for cropper + concat
  // ====================================================================
  function classifyLine(line) {
    if (line.includes('[FATAL]') || line.includes('[ERROR]') || line.includes('FAILED')) return 'err';
    if (line.includes('[WARN]')) return 'warn';
    if (line.includes('[DONE]') || line.includes('CREATED')) return 'ok';
    return 'info';
  }

  function streamJob(jobId, streamUrl, logEl, onDone) {
    logEl.textContent = '';
    const es = new EventSource(`${streamUrl}/${jobId}`);
    es.onmessage = (ev) => {
      try {
        const j = JSON.parse(ev.data);
        if (j.log) {
          const span = document.createElement('span');
          span.className = classifyLine(j.log);
          span.textContent = j.log + '\n';
          logEl.appendChild(span);
          logEl.scrollTop = logEl.scrollHeight;
        }
      } catch (e) { /* keepalive or malformed */ }
    };
    es.addEventListener('done', (ev) => {
      es.close();
      try {
        const result = JSON.parse(ev.data);
        onDone(result);
      } catch (e) { onDone({ ok: false, error: e.message }); }
    });
    es.onerror = () => {
      es.close();
      const span = document.createElement('span');
      span.className = 'err';
      span.textContent = '\n[stream closed]\n';
      logEl.appendChild(span);
    };
  }

  function renderSummary(container, result, fields) {
    container.innerHTML = '';
    const card = document.createElement('div');
    card.className = 'summary-card';
    if (!result.ok) {
      const row = document.createElement('div');
      row.className = 'row-kv';
      row.innerHTML = `<span class="k">Status</span><span class="v err">${result.error || 'failed'}</span>`;
      card.appendChild(row);
    } else {
      for (const [k, label] of fields) {
        if (!(k in result)) continue;
        const row = document.createElement('div');
        row.className = 'row-kv';
        const v = result[k];
        row.innerHTML = `<span class="k">${label}</span><span class="v">${v}</span>`;
        card.appendChild(row);
      }
    }
    container.appendChild(card);
  }

  // ====================================================================
  // CROPPER tab
  // ====================================================================
  $('cpRun').addEventListener('click', async () => {
    const params = {
      min_perfects:   +$('cpMinPerf').value,
      max_gap:        +$('cpMaxGap').value,
      pre_roll:       +$('cpPre').value,
      post_roll:      +$('cpPost').value,
      merge_gap:      +$('cpMerge').value,
      max_segments:   +$('cpMaxSeg').value,
      select_top_by:  $('cpSelect').value,
      run_prefix:     $('cpPrefix').value,
      source_offset:  +$('cpOffset').value,
      source_limit:   +$('cpLimit').value,
      reencode:       $('cpReencode').checked,
    };
    $('cpRun').disabled = true;
    $('cpSummary').innerHTML = '';
    try {
      const r = await fetch('/api/crop/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      });
      const j = await r.json();
      if (!j.ok) {
        $('cpLog').textContent = 'Failed to start job.';
        $('cpRun').disabled = false;
        return;
      }
      streamJob(j.job_id, '/api/crop/stream', $('cpLog'), (result) => {
        $('cpRun').disabled = false;
        renderSummary($('cpSummary'), result, [
          ['run_dir', 'Run folder'],
          ['summary_csv', 'Summary CSV'],
          ['created', 'Created'],
          ['failed',  'Failed'],
          ['skipped_missing', 'Skipped (missing source)'],
          ['skipped_no_segments', 'Skipped (no qualifying segments)'],
        ]);
      });
    } catch (e) {
      $('cpLog').textContent = e.message;
      $('cpRun').disabled = false;
    }
  });

  // ====================================================================
  // CONCAT tab
  // ====================================================================
  let concatFoldersLoaded = false;

  async function loadConcatFolders() {
    try {
      const r = await fetch('/api/concat/list_run_dirs');
      const j = await r.json();
      const sel = $('ccFolder');
      sel.innerHTML = '';
      if (!j.folders.length) {
        sel.innerHTML = '<option value="">(no folders found)</option>';
        return;
      }
      // Group: cropper_run first, then video_subdir
      const groups = { cropper_run: [], video_subdir: [] };
      for (const f of j.folders) (groups[f.kind] || (groups[f.kind] = [])).push(f);
      const addOptions = (label, list) => {
        if (!list.length) return;
        const og = document.createElement('optgroup');
        og.label = label;
        for (const f of list) {
          const o = document.createElement('option');
          o.value = f.path;
          o.textContent = f.name;
          og.appendChild(o);
        }
        sel.appendChild(og);
      };
      addOptions('Cropper runs', groups.cropper_run);
      addOptions('Video folders', groups.video_subdir);
    } catch (e) {
      $('ccFolder').innerHTML = `<option value="">${e.message}</option>`;
    }
  }
  $('ccRefreshFolders').addEventListener('click', loadConcatFolders);

  $('ccRun').addEventListener('click', async () => {
    const folder = $('ccFolder').value;
    if (!folder) { alert('Pick a folder first.'); return; }
    const params = {
      folder,
      out:       $('ccOut').value,
      exts:      $('ccExts').value,
      size:      $('ccSize').value.trim() || null,
      fps:       $('ccFps').value.trim()  || null,
      recursive: $('ccRecursive').checked,
    };
    $('ccRun').disabled = true;
    $('ccSummary').innerHTML = '';
    try {
      const r = await fetch('/api/concat/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      });
      const j = await r.json();
      if (!j.ok) {
        $('ccLog').textContent = 'Failed to start job.';
        $('ccRun').disabled = false;
        return;
      }
      streamJob(j.job_id, '/api/concat/stream', $('ccLog'), (result) => {
        $('ccRun').disabled = false;
        renderSummary($('ccSummary'), result, [
          ['output', 'Output file'],
        ]);
      });
    } catch (e) {
      $('ccLog').textContent = e.message;
      $('ccRun').disabled = false;
    }
  });

  // ====================================================================
  // CONSOLIDATE tab
  // ====================================================================
  $('conFiles').addEventListener('change', (e) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) {
      $('conFileList').textContent = 'No files chosen.';
      return;
    }
    $('conFileList').innerHTML = `Chosen: ${files.length} file(s) — ` +
      files.map(f => `<code>${f.name}</code>`).join(', ');
  });

  $('conRun').addEventListener('click', async () => {
    const files = $('conFiles').files;
    if (!files || !files.length) {
      $('conErr').textContent = 'Choose files first.';
      return;
    }
    $('conErr').textContent = '';
    $('conSummary').textContent = 'Consolidating…';

    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    fd.append('output_name',    $('conOutName').value);
    fd.append('conflict_mode',  $('conMode').value);
    fd.append('infer_folders',  $('conInferFolders').checked ? '1' : '0');

    try {
      const r = await fetch('/api/consolidate', { method: 'POST', body: fd });
      const j = await r.json();
      if (!j.ok) {
        $('conSummary').innerHTML = '';
        $('conErr').textContent = j.error || 'Consolidation failed.';
        return;
      }
      const s = j.summary;
      const container = $('conSummary');
      container.innerHTML = '';
      const card = document.createElement('div');
      card.className = 'summary-card';
      const kv = [
        ['Videos',                 s.videos],
        ['Unique records',         s.records],
        ['Duplicates skipped',     s.duplicates_skipped],
        ['Conflicts resolved',     s.conflicts_resolved],
        ['Folder-inferred names',  s.folder_inferred],
      ];
      for (const [k, v] of kv) {
        const row = document.createElement('div');
        row.className = 'row-kv';
        row.innerHTML = `<span class="k">${k}</span><span class="v">${v}</span>`;
        card.appendChild(row);
      }
      if (s.ambiguous && s.ambiguous.length) {
        const row = document.createElement('div');
        row.className = 'row-kv';
        row.innerHTML = `<span class="k">Ambiguous basenames</span><span class="v err">${s.ambiguous.join(', ')}</span>`;
        card.appendChild(row);
      }
      container.appendChild(card);

      // Download link
      const a = document.createElement('a');
      a.href = `/api/consolidate/download/${s.download_token}`;
      a.textContent = `↓ Download ${$('conOutName').value}`;
      a.style.display = 'inline-block';
      a.style.marginTop = '12px';
      container.appendChild(a);
    } catch (e) {
      $('conErr').textContent = e.message;
    }
  });

})();
