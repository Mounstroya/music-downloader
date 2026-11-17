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
];
const DONE = ['auto', 'have', 'approved', 'skip'];

let state = { pid: null, data: null, filter: 'missing', text: '' };
let rowEls = new Map();   // track_id -> {el, sig}
let pollTimer = null;

function toast(msg, err = false) {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast' + (err ? ' err' : '');
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.add('hidden'), err ? 6000 : 3000);
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
    const partial = pl && pl.loaded < pl.total;
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

async function importPlaylist(url) {
    const btn = $('#importBtn');
    btn.disabled = true; btn.textContent = 'Cargando…';
    try {
        const r = await api('api/playlists/import', { method: 'POST', body: { url } });
        toast(`${r.name}: ${r.loaded} canciones cargadas${r.loaded < r.total ? ` de ${r.total}` : ''}`);
        if (r.user_error) toast('No se pudo leer con tu cuenta: ' + r.user_error, true);
        $('#plUrl').value = '';
        await loadSaved();
        await openPlaylist(r.id);
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; btn.textContent = 'Cargar'; }
}

async function openPlaylist(pid) {
    if (state.pid !== pid) { rowEls = new Map(); $('#list').innerHTML = ''; }
    state.pid = pid;
    try { localStorage.setItem('pl_last', pid); } catch (_) { /* sin storage */ }
    await refresh();
    $('#plView').classList.remove('hidden');
    showConnect();
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
    const done = n(DONE);
    $('#plName').textContent = pl.name;
    $('#plMeta').innerHTML = `${esc(pl.owner || '')} · ${pl.loaded}${pl.loaded < pl.total ? ` de ${pl.total}` : ''} canciones · ` +
        `<b style="color:var(--text)">${done}</b> listas, <b style="color:#ff8a8d">${n(['missing', 'error'])}</b> faltan` +
        (pl.loaded < pl.total ? ' · <a href="#" id="needLogin">cargar las demás</a>' : '');
    const nl = $('#needLogin'); if (nl) nl.onclick = (e) => { e.preventDefault(); showConnect(true); window.scrollTo(0, 0); };
    $('#progFill').style.width = tracks.length ? `${(done / tracks.length) * 100}%` : '0';

    $('#filters').innerHTML = FILTERS.map((f) =>
        `<button class="chip ${state.filter === f.key ? 'active' : ''}" data-f="${f.key}">${f.label}<b>${n(f.st)}</b></button>`).join('');
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
        bb.disabled = bulk.running || !n(['missing', 'error']);
    }
}

function sig(t) { return [t.status, t.progress, t.file_rel, t.match_rel, t.note, t.album_art].join('|'); }

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
        const okSt = !f.st || f.st.includes(t.status) || r.el._panel; // no esconder la que estás revisando
        const okTx = !q || `${t.name} ${t.artist} ${t.album}`.toLowerCase().includes(q);
        r.el.classList.toggle('hidden', !(okSt && okTx));
    });
}

function rowHtml(t) {
    const art = t.album_art ? `<img class="art" src="${esc(t.album_art)}" loading="lazy" alt="">` : '<div class="art"></div>';
    let extra = '';
    if (['auto', 'maybe', 'have'].includes(t.status) && t.match_rel) extra = `<div class="path">En tu biblioteca: <b>${esc(t.match_rel)}</b></div>`;
    if (['downloaded', 'approved'].includes(t.status) && t.file_rel) extra = `<div class="path">Descargada en: <b>${esc(t.file_rel)}</b></div>`;
    if (['queued', 'downloading'].includes(t.status)) extra = `<div class="path">${esc(t.job_message || '')} ${t.progress ? `(${t.progress}%)` : ''}</div>`;
    if (t.note && ['missing', 'error'].includes(t.status)) extra += `<div class="note">${esc(t.note)}</div>`;

    const B = (act, label, cls = '') => `<button class="btn sm ${cls}" data-a="${act}">${label}</button>`;
    let acts = '';
    switch (t.status) {
        case 'missing': case 'error':
            acts = B('search', '🔍 Buscar', 'primary') + B('auto', 'Descargar automática') + B('have', 'Ya la tengo') + B('skip', 'Omitir'); break;
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
    return `
        <div class="pos">${t.position + 1}</div>
        ${art}
        <div class="info">
            <div class="title"><a href="https://open.spotify.com/track/${esc(t.track_id)}" target="_blank" rel="noopener">${esc(t.name)}</a></div>
            <div class="sub">${esc(t.artist)}${t.album ? ' · ' + esc(t.album) : ''} · ${fmtDur(t.duration_ms)}</div>
            ${extra}
        </div>
        <div class="side">
            <span class="badge b-${t.status}">${LABEL[t.status] || t.status}</span>
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
            await api(`${base}/reject`, { method: 'POST' });
            toast('Descarga borrada. Elige otra opción.');
            await refresh();
            return openSearch(el, state.data.tracks.find((x) => x.track_id === t.track_id) || t);
        }
        await refresh();
    } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ paneles
function closePanel(el) { el.querySelector('.panel').innerHTML = ''; el._panel = null; }

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

async function openSearch(el, t, q = '') {
    const p = el.querySelector('.panel');
    el._panel = 'search';
    p.innerHTML = `
        <form class="row sform">
            <input type="text" name="q" value="${esc(q)}" placeholder="Búsqueda personalizada (opcional): ${esc(t.artist)} ${esc(t.name)}">
            <button class="btn" type="submit">Buscar</button>
        </form>
        <div class="cands"><div class="muted">Buscando en YouTube Music…</div></div>`;
    p.querySelector('.sform').onsubmit = (e) => { e.preventDefault(); openSearch(el, t, e.target.q.value); };
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
$('#reloadBtn').onclick = () => importPlaylist(state.pid);
$('#rescanBtn').onclick = async () => {
    try { const r = await api(`api/playlists/${state.pid}/rescan`, { method: 'POST' }); toast(`Biblioteca revisada: ${r.files} archivos`); await refresh(); }
    catch (e) { toast(e.message, true); }
};
$('#bulkBtn').onclick = async () => {
    const n = state.data.tracks.filter((t) => ['missing', 'error'].includes(t.status)).length;
    if (!confirm(`Se van a descargar ${n} canciones automáticamente (elige la mejor coincidencia de YouTube). Después puedes escucharlas y aprobarlas o rechazarlas. ¿Continuar?`)) return;
    try { await api(`api/playlists/${state.pid}/download-missing`, { method: 'POST' }); state.filter = 'active'; await refresh(); }
    catch (e) { toast(e.message, true); }
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
