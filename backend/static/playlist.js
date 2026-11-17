// Revisión de playlist de Spotify vs biblioteca de Navidrome
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const LABEL = {
    missing: 'Falta', auto: 'Ya está', maybe: '¿Es esta?', have: 'La tengo', queued: 'En cola',
    downloading: 'Descargando', downloaded: 'Revisar descarga', approved: 'Descargada ✔', error: 'Error', skip: 'Omitida',
};
const FILTERS = [
    { key: 'all', label: 'Todas', st: null },
    { key: 'missing', label: 'Faltan', st: ['missing', 'error'] },
    { key: 'review', label: 'Por revisar', st: ['maybe', 'downloaded'] },
    { key: 'active', label: 'Descargando', st: ['queued', 'downloading'] },
    { key: 'done', label: 'Ya están', st: ['auto', 'have', 'approved'] },
    { key: 'skip', label: 'Omitidas', st: ['skip'] },
    { key: 'new', label: '✦ Nuevas', fn: (t) => !!t.is_new },
    { key: 'upgrade', label: '⬆ Mejorables', fn: (t) => isUpgradable(t) },
];
const DONE = ['auto', 'have', 'approved', 'skip'];
const FOUND = ['auto', 'have', 'approved', 'maybe'];
const isUpgradable = (t) => FOUND.includes(t.status) && !!t.upgrade_reason && !t.upgrade_ignored;
const fmatch = (f, t) => (f.fn ? f.fn(t) : (!f.st || f.st.includes(t.status)));

let state = { pid: null, data: null, filter: 'missing', text: '' };
let rowEls = new Map();   // track_id -> {el, sig}
let pollTimer = null;

function toast(msg, err = false, ms = null) {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast' + (err ? ' err' : '');
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.add('hidden'), ms || (err ? 6000 : 3000));
}

async function api(path, opts = {}) {
    const o = { ...opts };
    if (o.body && typeof o.body !== 'string') {
        o.body = JSON.stringify(o.body);
        o.headers = { 'Content-Type': 'application/json' };
    }
    const r = await fetch(path, o);
    let data = null;
    try { data = await r.json(); } catch (_) { /* sin cuerpo */ }
    if (!r.ok) throw new Error((data && data.detail) || `Error ${r.status}`);
    return data;
}

const isSpotifyPl = (pid) => !!pid && !pid.includes(':');
const isUnresolved = (t) => t.track_id.startsWith('q:');
const trackUrl = (t) => {
    const id = t.track_id;
    if (id.startsWith('dz:')) return `https://www.deezer.com/track/${id.slice(3)}`;
    if (id.startsWith('q:')) return t.video_id ? `https://www.youtube.com/watch?v=${t.video_id}`
        : `https://music.youtube.com/search?q=${encodeURIComponent(`${t.artist} ${t.name}`)}`;
    return `https://open.spotify.com/track/${id}`;
};
const fmtDur = (ms) => { const s = Math.round((ms || 0) / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };

// ------------------------------------------------------------------ cuenta Spotify
async function loadAccount() {
    const s = await api('api/spotify/status').catch(() => ({ connected: false }));
    const box = $('#spAccount');
    if (s.connected) {
        box.innerHTML = `Spotify: <b>${esc(s.user)}</b> <button class="btn sm" id="spOut">Desconectar</button>`;
        $('#spOut').onclick = async () => { await api('api/spotify/logout', { method: 'POST' }); loadAccount(); };
    } else {
        box.innerHTML = `<button class="btn sm" id="spIn">Conectar Spotify</button>`;
        $('#spIn').onclick = () => showConnect(true);
    }
    state.connected = s.connected;
    state.redirect = s.redirect_uri;
    return s;
}

async function showConnect(force = false) {
    const box = $('#connectBox');
    const pl = state.data && state.data.playlist;
    const partial = pl && pl.loaded < pl.total && isSpotifyPl(pl.id);
    if (state.connected || (!partial && !force)) { box.classList.add('hidden'); return; }
    const { url, redirect_uri } = await api('api/spotify/auth-url');
    box.innerHTML = `
        ${partial ? `<b>Solo se cargaron ${pl.loaded} de ${pl.total} canciones.</b> Sin iniciar sesión, Spotify solo deja ver las primeras 100.` : '<b>Conectar tu cuenta de Spotify</b>'}
        Para ver la playlist completa, conecta tu cuenta una sola vez:
        <ol>
            <li>En <a href="https://developer.spotify.com/dashboard" target="_blank" rel="noopener">developer.spotify.com/dashboard</a> abre tu app → <i>Settings</i> → <i>Edit</i> y en <i>Redirect URIs</i> agrega exactamente: <code>${esc(redirect_uri)}</code> y guarda.</li>
            <li><a href="${esc(url)}" target="_blank" rel="noopener"><b>Haz clic aquí para autorizar en Spotify</b></a>.</li>
            <li>Después de aceptar, el navegador te mandará a una página que <b>no va a cargar</b>; es normal. Copia el link completo de la barra de direcciones y pégalo aquí:</li>
        </ol>
        <div class="row">
            <input type="text" id="codeUrl" placeholder="http://127.0.0.1:8000/callback?code=…">
            <button class="btn primary" id="codeBtn">Conectar</button>
        </div>`;
    box.classList.remove('hidden');
    $('#codeBtn').onclick = async () => {
        try {
            const s = await api('api/spotify/auth-code', { method: 'POST', body: { url: $('#codeUrl').value } });
            if (!s.connected) throw new Error(s.error || 'No se pudo conectar');
            toast(`Conectado como ${s.user}`);
            await loadAccount();
            box.classList.add('hidden');
            if (state.pid) importPlaylist(state.pid);
        } catch (e) { toast(e.message, true); }
    };
}

// ------------------------------------------------------------------ playlists guardadas
async function loadSaved() {
    const list = await api('api/playlists').catch(() => []);
    const box = $('#savedPlaylists');
    box.innerHTML = list.length ? '<span class="muted">Guardadas:</span>' : '';
    list.forEach((p) => {
        const b = document.createElement('button');
        b.className = 'btn';
        b.textContent = `${p.name} (${p.loaded}${p.loaded < p.total ? '/' + p.total : ''})`;
        b.onclick = () => openPlaylist(p.id);
        box.appendChild(b);
    });
    return list;
}

// muestra el avance mientras el servidor lee la playlist y busca las canciones en el catálogo
function watchProgress(token) {
    const box = $('#importStatus');
    box.textContent = 'Leyendo la lista…';
    box.classList.remove('hidden');
    const started = Date.now();
    let stop = false;
    (async function tick() {
        if (stop) return;
        try {
            const p = await api(`api/import/progress/${token}`);
            const secs = Math.round((Date.now() - started) / 1000);
            if (p.stage === 'resolving' && p.total) {
                box.textContent = `Buscando las canciones en el catálogo: ${p.done} de ${p.total} (${secs} s)…`;
            } else if (p.stage === 'saving') {
                box.textContent = 'Guardando y revisando tu biblioteca…';
            } else if (secs > 2) {
                box.textContent = `Leyendo la lista… (${secs} s)`;
            }
        } catch (_) { /* sin progreso: se sigue esperando */ }
        if (!stop) setTimeout(tick, 1000);
    })();
    return () => { stop = true; box.classList.add('hidden'); };
}

async function importPlaylist(url, extra = null) {
    const btns = [$('#importBtn'), $('#txtImport')];
    const btn = extra ? $('#txtImport') : $('#importBtn');
    const label = btn.textContent;
    btns.forEach((b) => { b.disabled = true; });
    btn.textContent = 'Cargando…';
    const token = Math.random().toString(36).slice(2) + Date.now().toString(36);
    const done = watchProgress(token);
    try {
        const r = await api('api/playlists/import', { method: 'POST', body: { url: url || '', token, ...(extra || {}) } });
        let msg = `${r.name}: ${r.loaded} canciones cargadas${r.loaded < r.total ? ` de ${r.total}` : ''}`;
        if (r.unresolved) msg += ` · ${r.unresolved} no encontradas en el catálogo (búscalas a mano)`;
        if (r.ignored && r.ignored.length) msg += ` · ${r.ignored.length} líneas ignoradas`;
        toast(msg, false, r.unresolved || (r.ignored && r.ignored.length) ? 9000 : 3000);
        if (r.user_error) toast('No se pudo leer con tu cuenta: ' + r.user_error, true);
        if (r.ignored && r.ignored.length) console.info('Líneas ignoradas:', r.ignored);
        $('#plUrl').value = '';
        if (extra) { $('#txtList').value = ''; $('#txtName').value = ''; $('#txtFileName').textContent = ''; $('#textBox').classList.add('hidden'); }
        await loadSaved();
        await openPlaylist(r.id);
    } catch (e) { toast(e.message, true); }
    finally { done(); btns.forEach((b) => { b.disabled = false; }); btn.textContent = label; }
}

async function openPlaylist(pid) {
    if (state.pid !== pid) { rowEls = new Map(); $('#list').innerHTML = ''; }
    state.pid = pid;
    try { localStorage.setItem('pl_last', pid); } catch (_) { /* sin storage */ }
    await refresh();
    $('#plView').classList.remove('hidden');
    $('#syncCfg').classList.add('hidden');
    showConnect();
    loadSync();
}

// ------------------------------------------------------------------ sincronización
const ago = (ts) => {
    if (!ts) return '';
    const s = Math.round(Date.now() / 1000 - ts);
    if (s < 0) { const m = Math.round(-s / 60); return m < 90 ? `en ${m} min` : `en ${Math.round(m / 60)} h`; }
    if (s < 90) return 'hace un momento';
    if (s < 5400) return `hace ${Math.round(s / 60)} min`;
    if (s < 172800) return `hace ${Math.round(s / 3600)} h`;
    return `hace ${Math.round(s / 86400)} días`;
};
const fmtDate = (ts) => ts ? new Date(ts * 1000).toLocaleString('es-MX', { dateStyle: 'short', timeStyle: 'short' }) : '';

async function loadSync() {
    if (!state.pid) return;
    try { state.sync = await api(`api/playlists/${state.pid}/sync`); } catch (_) { state.sync = null; }
    renderSyncBar();
}

function syncResultText(r) {
    if (!r) return '';
    const parts = [`${r.added} nueva${r.added === 1 ? '' : 's'}`];
    if (r.removed) parts.push(`${r.removed} quitada${r.removed === 1 ? '' : 's'}`);
    if (r.downloads) parts.push(`${r.downloads} descargada${r.downloads === 1 ? '' : 's'} automáticamente`);
    if (r.downloads_note) parts.push(r.downloads_note);
    return parts.join(', ');
}

function renderSyncBar() {
    const s = state.sync;
    const bar = $('#syncBar');
    if (!s) { bar.innerHTML = ''; return; }
    const res = s.last_sync_result;
    let html = '';
    if (s.last_sync_at) {
        html += `<span>Última sincronización: <b title="${esc(fmtDate(s.last_sync_at))}">${esc(ago(s.last_sync_at))}</b>` +
            (res && res.trigger === 'auto' ? ' (automática)' : '') + '</span>';
        if (s.last_sync_ok === 0) html += `<span class="serr">✖ ${esc(s.last_sync_error || 'Falló')}</span>`;
        else if (res) html += `<span>${esc(syncResultText(res))}</span>`;
    } else html += '<span>Todavía no se ha sincronizado.</span>';
    if (s.auto_sync) {
        html += `<span class="stag ${s.global.enabled ? 'on' : 'off'}">Automática cada ${s.global.test_minutes ? s.global.test_minutes + ' min (prueba)' : s.interval_hours + ' h'}` +
            (s.global.enabled ? (s.next_sync_at ? ` · próxima ${esc(ago(s.next_sync_at))}` : '') : ' · apagada en el servidor') + '</span>';
        if (s.fail_count) html += `<span class="serr">${s.fail_count} fallo${s.fail_count === 1 ? '' : 's'} seguido${s.fail_count === 1 ? '' : 's'}: se espera más entre intentos</span>`;
    }
    if (s.partial) {
        const pl = state.data && state.data.playlist;
        html += `<div class="swarn">⚠ Sin iniciar sesión en Spotify solo se leen las primeras 100${pl ? ` de ${pl.total}` : ''} canciones. ` +
            'Al sincronizar no se borra ninguna canción que ya no aparezca, y las nuevas que estén después de la 100 no se detectan.</div>';
    }
    bar.innerHTML = html;
}

function renderSyncCfg() {
    const s = state.sync;
    const box = $('#syncCfg');
    if (!s) { box.innerHTML = '<div class="muted">No se pudo leer la configuración.</div>'; return; }
    const g = s.global;
    box.innerHTML = `
        <div class="cfg-title">Sincronización automática</div>
        ${!g.enabled ? '<div class="swarn">La sincronización automática está apagada en el servidor (<code>AUTO_SYNC_ENABLED=false</code>). Puedes dejarla configurada, pero no se ejecutará hasta que se active.</div>' : ''}
        ${!s.syncable ? '<div class="swarn">Esta playlist no se puede volver a leer automáticamente.</div>' : ''}
        <label class="chk"><input type="checkbox" id="cfgAuto" ${s.auto_sync ? 'checked' : ''} ${s.syncable ? '' : 'disabled'}> Sincronizar esta playlist automáticamente</label>
        <label class="chk">Cada <input type="number" id="cfgHours" min="${g.min_hours}" max="720" step="1" value="${s.interval_hours || g.default_hours}"> horas <span class="muted">(mínimo ${g.min_hours})</span></label>
        <label class="chk"><input type="checkbox" id="cfgDl" ${s.auto_download ? 'checked' : ''}> Descargar automáticamente las nuevas <span class="muted">(máximo ${g.max_downloads} por sincronización; si está apagado, solo quedan marcadas como faltantes)</span></label>
        <div class="muted">Para cuidar tu cuenta se sincroniza una playlist a la vez, con pausas, y nunca al arrancar el servidor. Si falla, espera cada vez más antes de reintentar.</div>
        <div class="acts" style="justify-content:flex-start"><button class="btn primary" id="cfgSave">Guardar</button><button class="btn" id="cfgClose">Cerrar</button></div>`;
    $('#cfgClose').onclick = () => box.classList.add('hidden');
    $('#cfgSave').onclick = async () => {
        let h = parseInt($('#cfgHours').value, 10) || g.default_hours;
        if (h < g.min_hours) { h = g.min_hours; toast(`El mínimo es cada ${g.min_hours} horas`); }
        try {
            state.sync = await api(`api/playlists/${state.pid}/sync`, { method: 'PUT', body: { auto_sync: $('#cfgAuto').checked, interval_hours: h, auto_download: $('#cfgDl').checked } });
            toast(state.sync.auto_sync ? `Sincronización automática activada (cada ${state.sync.interval_hours} h)` : 'Sincronización automática desactivada');
            renderSyncBar(); renderSyncCfg();
        } catch (e) { toast(e.message, true); }
    };
}

async function syncNow() {
    const btn = $('#reloadBtn');
    btn.disabled = true; btn.textContent = 'Sincronizando…';
    try {
        const r = await api(`api/playlists/${state.pid}/sync`, { method: 'POST' });
        toast(`Sincronizada: ${syncResultText(r)}${r.partial ? ` (solo se leyeron ${r.loaded} de ${r.total})` : ''}`);
        if (r.user_error) toast('No se pudo leer con tu cuenta: ' + r.user_error, true);
        if (r.added) state.filter = 'new';
        await loadSaved();
        await refresh();
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; btn.textContent = '↻ Sincronizar ahora'; await loadSync(); }
}

// ------------------------------------------------------------------ render
async function refresh() {
    if (!state.pid) return;
    const d = await api(`api/playlists/${state.pid}`);
    state.data = d;
    renderSummary();
    renderList();
    schedulePoll();
}

function schedulePoll() {
    clearTimeout(pollTimer);
    const active = state.data.bulk.running || state.data.tracks.some((t) => ['queued', 'downloading'].includes(t.status));
    if (active) pollTimer = setTimeout(() => refresh().catch(() => schedulePoll()), 3000);
}

function renderSummary() {
    const { playlist: pl, tracks, bulk } = state.data;
    const n = (sts) => tracks.filter((t) => !sts || sts.includes(t.status)).length;
    const nf = (f) => tracks.filter((t) => fmatch(f, t)).length;
    const done = n(DONE);
    const unres = tracks.filter(isUnresolved).length;
    $('#plName').textContent = pl.name;
    $('#plMeta').innerHTML = `${esc(pl.owner || '')} · ${pl.loaded}${pl.loaded < pl.total ? ` de ${pl.total}` : ''} canciones · ` +
        `<b style="color:var(--text)">${done}</b> listas, <b style="color:#ff8a8d">${n(['missing', 'error'])}</b> faltan` +
        (pl.loaded < pl.total && isSpotifyPl(pl.id) ? ' · <a href="#" id="needLogin">cargar las demás</a>' : '') +
        (unres ? ` · <span style="color:#ffc35c">${unres} sin catálogo</span>` : '');
    $('#reloadBtn').classList.toggle('hidden', pl.id.startsWith('txt:'));
    const nl = $('#needLogin'); if (nl) nl.onclick = (e) => { e.preventDefault(); showConnect(true); window.scrollTo(0, 0); };
    $('#progFill').style.width = tracks.length ? `${(done / tracks.length) * 100}%` : '0';

    $('#filters').innerHTML = FILTERS.map((f) =>
        `<button class="chip ${state.filter === f.key ? 'active' : ''} ${f.fn && nf(f) ? 'hl' : ''}" data-f="${f.key}">${f.label}<b>${nf(f)}</b></button>`).join('') +
        (state.filter === 'new' && nf(FILTERS.find((f) => f.key === 'new')) ? '<button class="btn sm" id="newSeen">Marcar como vistas</button>' : '');
    const ns = $('#newSeen');
    if (ns) ns.onclick = async () => { await api(`api/playlists/${state.pid}/new/seen`, { method: 'POST' }); state.filter = 'all'; await refresh(); };
    $('#filters').querySelectorAll('.chip').forEach((c) => c.onclick = () => { state.filter = c.dataset.f; renderSummary(); applyFilter(); });

    const bar = $('#bulkBar');
    const bb = $('#bulkBtn');
    if (bulk.running && bulk.playlist_id === state.pid) {
        bar.classList.remove('hidden');
        bar.innerHTML = `⏳ Descarga automática: ${bulk.done} de ${bulk.total} <button class="btn sm bad" id="bulkStop">Detener</button>`;
        $('#bulkStop').onclick = async () => { await api('api/playlists/bulk/stop', { method: 'POST' }); toast('Se detendrá al terminar la canción actual'); };
        bb.disabled = true;
    } else {
        bar.classList.add('hidden');
        bb.disabled = bulk.running || !bulkable().length;
    }
}

// las que no están en el catálogo y no traen video de YouTube se descargan eligiendo el video a mano
const bulkable = () => state.data.tracks.filter((t) => ['missing', 'error'].includes(t.status) && !(isUnresolved(t) && !t.video_id));

function sig(t) {
    return [t.status, t.progress, t.file_rel, t.match_rel, t.note, t.album_art, t.is_new, t.upgrade_reason,
        t.upgrade_ignored, t.old_rel, t.old_exists, t.lib_bitrate, t.lib_length, t.video_id].join('|');
}

function renderList() {
    const list = $('#list');
    state.data.tracks.forEach((t, i) => {
        let r = rowEls.get(t.track_id);
        if (!r) {
            const el = document.createElement('div');
            el.className = 'track';
            el.innerHTML = '<div class="main"></div><div class="panel"></div>';
            r = { el, sig: null };
            rowEls.set(t.track_id, r);
        }
        if (list.children[i] !== r.el) list.insertBefore(r.el, list.children[i] || null);
        const s = sig(t);
        if (s !== r.sig) {
            const prev = r.sig && r.sig.split('|')[0];
            r.sig = s;
            r.el.dataset.status = t.status;
            r.el.querySelector('.main').innerHTML = rowHtml(t);
            bindRow(r.el, t);
            // al terminar una descarga, abre el reproductor para revisarla
            if (t.status === 'downloaded' && prev && prev !== 'downloaded') openReview(r.el, t);
            if (t.status !== 'downloaded' && r.el._panel === 'review') closePanel(r.el);
        }
    });
    applyFilter();
}

function applyFilter() {
    const f = FILTERS.find((x) => x.key === state.filter);
    const q = state.text.toLowerCase();
    state.data.tracks.forEach((t) => {
        const r = rowEls.get(t.track_id);
        const okSt = fmatch(f, t) || r.el._panel; // no esconder la que estás revisando
        const okTx = !q || `${t.name} ${t.artist} ${t.album}`.toLowerCase().includes(q);
        r.el.classList.toggle('hidden', !(okSt && okTx));
    });
}

function rowHtml(t) {
    const art = t.album_art ? `<img class="art" src="${esc(t.album_art)}" loading="lazy" alt="">` : '<div class="art"></div>';
    let extra = '';
    const finfo = [t.lib_bitrate ? `${t.lib_bitrate} kbps` : '', t.lib_length ? fmtDur(t.lib_length * 1000) : ''].filter(Boolean).join(' · ');
    if (['auto', 'maybe', 'have'].includes(t.status) && t.match_rel) extra = `<div class="path">En tu biblioteca: <b>${esc(t.match_rel)}</b>${finfo ? ` <span class="finfo">${finfo}</span>` : ''}</div>`;
    if (['downloaded', 'approved'].includes(t.status) && t.file_rel) extra = `<div class="path">Descargada en: <b>${esc(t.file_rel)}</b></div>`;
    if (['queued', 'downloading'].includes(t.status)) extra = `<div class="path">${esc(t.job_message || '')} ${t.progress ? `(${t.progress}%)` : ''}</div>`;
    if (t.note && ['missing', 'error'].includes(t.status)) extra += `<div class="note">${esc(t.note)}</div>`;
    if (t.note && t.status === 'have') extra += `<div class="muted">${esc(t.note)}</div>`;
    if (isUpgradable(t)) extra += `<div class="upg">⬆ Mejorable: ${esc(t.upgrade_reason)}</div>`;
    if (t.status === 'approved' && t.old_rel && t.old_exists) extra += `<div class="upg old">Versión anterior (se conserva): <b>${esc(t.old_rel)}</b></div>`;
    if (['queued', 'downloading', 'downloaded'].includes(t.status) && t.old_rel) extra += `<div class="muted">Buscando mejor versión; la anterior se conserva: ${esc(t.old_rel)}</div>`;

    const B = (act, label, cls = '') => `<button class="btn sm ${cls}" data-a="${act}">${label}</button>`;
    let acts = '';
    switch (t.status) {
        case 'missing': case 'error':
            acts = B('search', '🔍 Buscar', 'primary') +
                (t.video_id ? B('auto', 'Descargar video de la playlist') : isUnresolved(t) ? '' : B('auto', 'Descargar automática')) +
                B('have', 'Ya la tengo') + B('skip', 'Omitir'); break;
        case 'maybe':
            acts = B('play-match', '▶') + B('have', 'Sí es', 'ok') + B('missing', 'No es', 'bad'); break;
        case 'auto':
            acts = B('play-match', '▶') + B('missing', 'No es esta'); break;
        case 'downloaded':
            acts = B('review', '▶ Escuchar') + B('approve', '✔ Está bien', 'ok') + B('reject', '✖ No, buscar otra', 'bad'); break;
        case 'have': case 'approved':
            acts = (t.match_rel || t.file_rel ? B(t.status === 'approved' ? 'review' : 'play-match', '▶') : '') + B('missing', 'Deshacer'); break;
        case 'skip':
            acts = B('missing', 'Deshacer'); break;
        default: acts = '';
    }
    if (isUpgradable(t)) acts += B('upgrade', '⬆ Buscar mejor versión', 'primary') + B('ignore', 'Ignorar');
    else if (FOUND.includes(t.status) && t.upgrade_reason && t.upgrade_ignored) acts += B('unignore', 'Volver a sugerir mejora');
    if (t.status === 'approved' && t.old_rel && t.old_exists) acts += B('delete-old', 'Borrar la versión vieja', 'bad') + B('keep-both', 'Conservar ambas');
    return `
        <div class="pos">${t.position + 1}</div>
        ${art}
        <div class="info">
            <div class="title">${t.is_new ? '<span class="newtag">Nueva</span>' : ''}<a href="${esc(trackUrl(t))}" target="_blank" rel="noopener">${esc(t.name)}</a></div>
            <div class="sub">${esc(t.artist)}${t.album ? ' · ' + esc(t.album) : ''} · ${fmtDur(t.duration_ms)}</div>
            ${extra}
        </div>
        <div class="side">
            <div>${isUnresolved(t) ? '<span class="badge b-unres" title="No se encontró en Deezer/Spotify: búscala a mano; se descargará con metadatos básicos">Sin catálogo</span>' : ''}<span class="badge b-${t.status}">${LABEL[t.status] || t.status}</span></div>
            <div class="acts">${acts}</div>
        </div>`;
}

function bindRow(el, t) {
    el.querySelectorAll('[data-a]').forEach((b) => b.onclick = () => act(el, t, b.dataset.a, b));
}

async function act(el, t, a, btn) {
    const base = `api/playlists/${state.pid}/tracks/${t.track_id}`;
    try {
        if (['have', 'missing', 'skip', 'approve'].includes(a)) {
            await api(`${base}/mark`, { method: 'POST', body: { status: a === 'approve' ? 'approved' : a } });
            closePanel(el);
        } else if (a === 'search') {
            return el._panel === 'search' ? closePanel(el) : openSearch(el, t);
        } else if (a === 'auto') {
            await api(`${base}/download`, { method: 'POST', body: {} });
            closePanel(el);
        } else if (a === 'review') {
            return el._panel === 'review' ? closePanel(el) : openReview(el, t);
        } else if (a === 'play-match') {
            return el._panel === 'play' ? closePanel(el) : openPlayer(el, t, 'match');
        } else if (a === 'reject') {
            btn.disabled = true;
            const rr = await api(`${base}/reject`, { method: 'POST' });
            if (rr && rr.restored) { toast('Descarga borrada. Se conserva la versión anterior.'); closePanel(el); await refresh(); return; }
            toast('Descarga borrada. Elige otra opción.');
            await refresh();
            return openSearch(el, state.data.tracks.find((x) => x.track_id === t.track_id) || t);
        } else if (a === 'upgrade') {
            return el._panel === 'search' ? closePanel(el) : openSearch(el, t, '', true);
        } else if (a === 'ignore' || a === 'unignore') {
            await api(`${base}/upgrade-ignore`, { method: 'POST', body: { ignore: a === 'ignore' } });
        } else if (a === 'keep-both') {
            await api(`${base}/keep-both`, { method: 'POST' });
            toast('Se conservan las dos versiones');
        } else if (a === 'delete-old') {
            if (!confirm(`¿Borrar definitivamente la versión vieja?\n\n${t.old_rel}\n\nSe conserva la nueva:\n${t.file_rel || ''}\n\nEsta acción no se puede deshacer.`)) return;
            btn.disabled = true;
            await api(`${base}/delete-old`, { method: 'POST' });
            toast('Versión vieja borrada');
        }
        await refresh();
    } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ paneles
function closePanel(el) { el.querySelector('.panel').innerHTML = ''; el._panel = null; el._upgrade = false; }

function openPlayer(el, t, which) {
    const p = el.querySelector('.panel');
    el._panel = 'play';
    p.innerHTML = `<audio controls autoplay src="api/playlists/${state.pid}/tracks/${t.track_id}/audio?which=${which}&t=${Date.now()}"></audio>`;
}

function openReview(el, t) {
    const p = el.querySelector('.panel');
    el._panel = 'review';
    p.innerHTML = `
        <div class="muted">Escucha la descarga. ¿Es la canción correcta?</div>
        <audio controls src="api/playlists/${state.pid}/tracks/${t.track_id}/audio?which=file&t=${Date.now()}"></audio>
        <div class="acts" style="justify-content:flex-start">
            <button class="btn ok" data-r="approve">✔ Está bien</button>
            <button class="btn bad" data-r="reject">✖ No, borrar y buscar otra</button>
        </div>`;
    p.querySelectorAll('[data-r]').forEach((b) => b.onclick = () => act(el, t, b.dataset.r, b));
    applyFilter();
}

async function openSearch(el, t, q = '', upgrade = false) {
    const p = el.querySelector('.panel');
    el._panel = 'search';
    if (upgrade || el._upgrade) el._upgrade = true;
    p.innerHTML = `
        ${el._upgrade && FOUND.includes(t.status) ? `<div class="muted">Buscando una mejor versión (${esc(t.upgrade_reason || '')}). La que tienes se conserva; después de escuchar la nueva decides si borrar la vieja.</div>` : ''}
        <form class="row sform">
            <input type="text" name="q" value="${esc(q)}" placeholder="Búsqueda personalizada (opcional): ${esc(t.artist)} ${esc(t.name)}">
            <button class="btn" type="submit">Buscar</button>
        </form>
        <div class="cands"><div class="muted">Buscando en YouTube Music…</div></div>`;
    p.querySelector('.sform').onsubmit = (e) => { e.preventDefault(); openSearch(el, t, e.target.q.value); };
    p.querySelector('.sform').insertAdjacentHTML('beforeend', '<button class="btn" type="button" data-close>Cerrar</button>');
    p.querySelector('[data-close]').onclick = () => closePanel(el);
    applyFilter();
    try {
        const r = await api(`api/playlists/${state.pid}/tracks/${t.track_id}/candidates${q ? '?q=' + encodeURIComponent(q) : ''}`);
        const box = p.querySelector('.cands');
        if (!box) return;
        if (!r.success || !r.candidates.length) { box.innerHTML = `<div class="note">${esc(r.error || 'Sin resultados')}</div>`; return; }
        box.innerHTML = r.candidates.map((c) => {
            const sc = c.score >= 0.85 ? 'hi' : c.score >= 0.6 ? 'mid' : 'lo';
            return `<div class="cand">
                <img src="${esc(c.thumbnail)}" alt="" loading="lazy">
                <div class="info">
                    <div class="title">${esc(c.title)}</div>
                    <div class="sub">${esc(c.channel)} · ${fmtDur((c.duration || 0) * 1000)} <span class="score ${sc}">· coincide ${Math.round(c.score * 100)}%</span></div>
                </div>
                <div class="acts">
                    <a class="btn sm" href="${esc(c.url)}" target="_blank" rel="noopener">Ver ↗</a>
                    <button class="btn sm primary" data-v="${esc(c.video_id)}">Descargar esta</button>
                </div>
            </div>`;
        }).join('') + (r.warning ? `<div class="muted">${esc(r.warning)}</div>` : '');
        box.querySelectorAll('[data-v]').forEach((b) => b.onclick = async () => {
            b.disabled = true;
            try {
                await api(`api/playlists/${state.pid}/tracks/${t.track_id}/download`, { method: 'POST', body: { video_id: b.dataset.v } });
                closePanel(el);
                toast('Descargando… cuando termine te aparece para escucharla');
                await refresh();
            } catch (e) { b.disabled = false; toast(e.message, true); }
        });
    } catch (e) {
        const box = p.querySelector('.cands');
        if (box) box.innerHTML = `<div class="note">${esc(e.message)}</div>`;
    }
}

// ------------------------------------------------------------------ init
$('#importForm').onsubmit = (e) => { e.preventDefault(); const v = $('#plUrl').value.trim(); if (v) importPlaylist(v); };
$('#reloadBtn').onclick = () => syncNow();
$('#syncCfgBtn').onclick = () => { const b = $('#syncCfg'); b.classList.toggle('hidden'); if (!b.classList.contains('hidden')) renderSyncCfg(); };
$('#rescanBtn').onclick = async () => {
    try { const r = await api(`api/playlists/${state.pid}/rescan`, { method: 'POST' }); toast(`Biblioteca revisada: ${r.files} archivos`); await refresh(); }
    catch (e) { toast(e.message, true); }
};
$('#bulkBtn').onclick = async () => {
    const n = bulkable().length;
    const manual = state.data.tracks.filter((t) => ['missing', 'error'].includes(t.status)).length - n;
    if (!confirm(`Se van a descargar ${n} canciones automáticamente (elige la mejor coincidencia de YouTube, o el video de la playlist si vino de YouTube). Después puedes escucharlas y aprobarlas o rechazarlas.` +
        (manual ? ` ${manual} canciones sin catálogo quedan fuera: descárgalas con «Buscar».` : '') + ' ¿Continuar?')) return;
    try { await api(`api/playlists/${state.pid}/download-missing`, { method: 'POST' }); state.filter = 'active'; await refresh(); }
    catch (e) { toast(e.message, true); }
};
$('#textToggle').onclick = () => { $('#textBox').classList.toggle('hidden'); if (!$('#textBox').classList.contains('hidden')) $('#txtList').focus(); };
$('#txtFile').onchange = (e) => {
    const f = e.target.files[0];
    if (!f) return;
    if (f.size > 2 * 1024 * 1024) { toast('El archivo es demasiado grande (máximo 2 MB)', true); e.target.value = ''; return; }
    const rd = new FileReader();
    rd.onload = () => {
        $('#txtList').value = rd.result;
        $('#txtFileName').textContent = f.name;
        if (!$('#txtName').value.trim()) $('#txtName').value = f.name.replace(/\.(csv|tsv|txt)$/i, '');
    };
    rd.onerror = () => toast('No se pudo leer el archivo', true);
    rd.readAsText(f);
    e.target.value = '';
};
$('#txtImport').onclick = () => {
    const text = $('#txtList').value;
    if (!text.trim()) { toast('Pega una lista o elige un archivo', true); return; }
    importPlaylist('', { text, name: $('#txtName').value.trim() || null });
};
$('#filterText').oninput = (e) => { state.text = e.target.value; applyFilter(); };

(async () => {
    await loadAccount();
    const saved = await loadSaved();
    let last = null;
    try { last = localStorage.getItem('pl_last'); } catch (_) { /* sin storage */ }
    const pick = saved.find((p) => p.id === last) || saved[0];
    if (pick) openPlaylist(pick.id);
})();
