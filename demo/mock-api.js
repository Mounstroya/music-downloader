/*
 * Music Downloader · API simulado para la demo de GitHub Pages.
 *
 * Se carga antes que los scripts de la app (lo inyecta demo/build.py justo después
 * de <base>) y reemplaza window.fetch: toda petición a "api/..." (o "/api/...") la
 * responde una tabla de rutas en memoria con la misma forma que el backend real
 * (backend/app.py, backend/playlist_feature.py, backend/youtube_cookies.py).
 * Nada sale del navegador: no hay descargas, ni llaves, ni datos privados.
 *
 * Agregar un endpoint:
 *     MDDemo.route('GET', 'sync/status', ({ params, query, body }) => ({ ok: true }));
 *     MDDemo.route('POST', 'algo/:id/hacer', ({ params }) => { ...; return {...}; });
 * Un handler devuelve el JSON de respuesta (status 200), lanza MDDemo.fail(status, detalle)
 * para un error con {detail}, o devuelve un objeto Response propio.
 * El estado vive en MDDemo.state() y se guarda en sessionStorage (si se puede); MDDemo.reset() lo reinicia.
 */
(function () {
    'use strict';

    // =================================================================== utilidades
    const STORE_KEY = 'md-demo-state-v1';
    const now = () => Date.now();
    const sec = () => Math.floor(Date.now() / 1000);
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

    function hash(str) {
        let h = 2166136261;
        for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
        return h >>> 0;
    }
    const B62 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz';
    const B64 = B62 + '-_';
    function fakeId(seed, len, alphabet = B62) {
        let out = '', h = hash(seed);
        for (let i = 0; i < len; i++) { h = Math.imul(h ^ (h >>> 15), 2246822507) + i >>> 0; out += alphabet[h % alphabet.length]; }
        return out;
    }
    const norm = (s) => String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
    const safeName = (s) => String(s).replace(/[\\/:*?"<>|]/g, '_');
    const pad2 = (n) => String(n).padStart(2, '0');

    // =================================================================== imágenes generadas (sin derechos de autor)
    const PALETTES = [
        ['#1db954', '#0b3d20'], ['#ff5f6d', '#ffc371'], ['#4568dc', '#b06ab3'], ['#f7971e', '#ffd200'],
        ['#00c6ff', '#0072ff'], ['#ee0979', '#ff6a00'], ['#8e2de2', '#4a00e0'], ['#11998e', '#38ef7d'],
        ['#fc466b', '#3f5efb'], ['#c94b4b', '#4b134f'], ['#283c86', '#45a247'], ['#e96443', '#904e95'],
    ];
    const xmlEsc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' }[c]));
    const svgUri = (svg) => 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
    const initials = (s) => String(s).split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join('').toUpperCase();

    function cover(title, artist) {
        const h = hash(title + '|' + artist);
        const [a, b] = PALETTES[h % PALETTES.length];
        const ang = h % 360, r = 18 + (h >> 4) % 40, cx = 30 + (h >> 8) % 140, cy = 30 + (h >> 12) % 140;
        return svgUri(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
<defs><linearGradient id="g" gradientTransform="rotate(${ang} .5 .5)"><stop offset="0" stop-color="${a}"/><stop offset="1" stop-color="${b}"/></linearGradient></defs>
<rect width="200" height="200" fill="url(#g)"/>
<circle cx="${cx}" cy="${cy}" r="${r + 40}" fill="#fff" opacity=".10"/><circle cx="${200 - cx}" cy="${200 - cy}" r="${r}" fill="#000" opacity=".12"/>
<text x="16" y="168" font-family="system-ui,sans-serif" font-size="46" font-weight="800" fill="#fff" opacity=".92">${xmlEsc(initials(title))}</text>
<text x="17" y="188" font-family="system-ui,sans-serif" font-size="11" fill="#fff" opacity=".8">${xmlEsc(String(artist).slice(0, 30))}</text>
</svg>`);
    }

    function thumb(title, seed) {
        const h = hash(seed);
        const [a, b] = PALETTES[h % PALETTES.length];
        return svgUri(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 90">
<defs><linearGradient id="g" x2="1" y2="1"><stop offset="0" stop-color="${a}"/><stop offset="1" stop-color="${b}"/></linearGradient></defs>
<rect width="160" height="90" fill="url(#g)"/><rect width="160" height="90" fill="#000" opacity=".25"/>
<path d="M68 31v28l24-14z" fill="#fff" opacity=".9"/>
<text x="8" y="82" font-family="system-ui,sans-serif" font-size="8" fill="#fff" opacity=".85">${xmlEsc(String(title).slice(0, 36))}</text>
</svg>`);
    }

    // =================================================================== audio generado (tono WAV hecho aquí mismo)
    const audioCache = new Map();
    function audioUrl(seed) {
        if (audioCache.has(seed)) return audioCache.get(seed);
        const rate = 22050, secs = 4, n = rate * secs;
        const buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
        const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
        w(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); w(8, 'WAVE'); w(12, 'fmt ');
        v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
        v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
        w(36, 'data'); v.setUint32(40, n * 2, true);
        // melodía pentatónica de 8 notas que depende de la canción
        const scale = [0, 2, 4, 7, 9, 12, 14, 16];
        const h = hash(seed), root = 196 * Math.pow(2, (h % 7) / 12);
        const notes = Array.from({ length: 8 }, (_, i) => root * Math.pow(2, scale[(h >> (i * 3)) % scale.length] / 12));
        const len = n / notes.length;
        for (let i = 0; i < n; i++) {
            const k = Math.floor(i / len), t = (i % len) / rate, f = notes[k];
            const env = Math.min(1, t * 60) * Math.exp(-t * 3.2) * Math.min(1, (n - i) / (rate * 0.05));
            const s = Math.sin(2 * Math.PI * f * t) * 0.6 + Math.sin(4 * Math.PI * f * t) * 0.18 + Math.sin(3 * Math.PI * f * t) * 0.1;
            v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s * env * 0.45)) * 32767, true);
        }
        const url = URL.createObjectURL(new Blob([buf], { type: 'audio/wav' }));
        audioCache.set(seed, url);
        return url;
    }

    // =================================================================== catálogo de ejemplo (solo metadatos)
    // [álbum, artista(s), fecha, [[canción, "m:ss", artistas extra?], ...]]
    const ALBUMS_RAW = [
        ['Random Access Memories', ['Daft Punk'], '2013-05-17', [
            ['Give Life Back to Music', '4:34'], ['The Game of Love', '5:21'], ['Giorgio by Moroder', '9:04'],
            ['Within', '3:48'], ['Instant Crush', '5:37', ['Julian Casablancas']], ['Lose Yourself to Dance', '5:53', ['Pharrell Williams']],
            ['Touch', '8:18', ['Paul Williams']], ['Get Lucky', '6:09', ['Pharrell Williams', 'Nile Rodgers']], ['Beyond', '4:50'],
            ['Motherboard', '5:41'], ['Fragments of Time', '4:39', ['Todd Edwards']], ["Doin' it Right", '4:11', ['Panda Bear']], ['Contact', '6:21']]],
        ['Canción Animal', ['Soda Stereo'], '1990-08-07', [
            ['(En) El Séptimo Día', '4:57'], ['Un Millón de Años Luz', '4:54'], ['Té para Tres', '2:23'], ['Sueles Dejarme Solo', '4:39'],
            ['Cae el Sol', '3:49'], ['Hombre al Agua', '4:07'], ['Canción Animal', '3:31'], ['1990', '3:34'],
            ['Entre Caníbales', '4:12'], ['De Música Ligera', '3:33']]],
        ['A Night at the Opera', ['Queen'], '1975-11-21', [
            ['Death on Two Legs', '3:43'], ['Lazing on a Sunday Afternoon', '1:08'], ["I'm in Love with My Car", '3:05'],
            ["You're My Best Friend", '2:50'], ["'39", '3:30'], ['Sweet Lady', '4:01'], ['Seaside Rendezvous', '2:13'],
            ["The Prophet's Song", '8:21'], ['Love of My Life', '3:38'], ['Good Company', '3:26'], ['Bohemian Rhapsody', '5:55'],
            ['God Save the Queen', '1:11']]],
        ['Rumours', ['Fleetwood Mac'], '1977-02-04', [
            ['Second Hand News', '2:43'], ['Dreams', '4:17'], ['Never Going Back Again', '2:14'], ["Don't Stop", '3:11'],
            ['Go Your Own Way', '3:38'], ['Songbird', '3:20'], ['The Chain', '4:30'], ['You Make Loving Fun', '3:31'],
            ["I Don't Want to Know", '3:15'], ['Oh Daddy', '3:56'], ['Gold Dust Woman', '4:56']]],
        ['Bocanada', ['Gustavo Cerati'], '1999-06-01', [
            ['Tabú', '5:37'], ['Engaña', '4:42'], ['Puente', '4:50'], ['Río Babel', '4:21'], ['Paseo Inmoral', '5:36'], ['Bocanada', '4:24']]],
        ['Re', ['Café Tacvba'], '1994-07-01', [
            ['El Aparato', '3:44'], ['Esa Noche', '2:34'], ['La Ingrata', '3:31'], ['El Ciclón', '3:06'], ['Las Flores', '3:17'], ['Ojalá Que Llueva Café', '3:24']]],
        ['Hasta la Raíz', ['Natalia Lafourcade'], '2015-03-17', [
            ['Hasta la Raíz', '3:46'], ['Lo Que Construimos', '3:54'], ['Tú Sí Sabes Quererme', '3:45'], ['Mi Lugar Favorito', '3:31'], ['Para Qué Sufrir', '3:31']]],
        ['Un Verano Sin Ti', ['Bad Bunny'], '2022-05-06', [
            ['Moscow Mule', '4:05'], ['Tití Me Preguntó', '4:03'], ['Me Porto Bonito', '2:58', ['Chencho Corleone']],
            ['Ojitos Lindos', '4:18', ['Bomba Estéreo']], ['Efecto', '3:33'], ['Party', '3:47', ['Rauw Alejandro']]]],
    ];
    const toMs = (s) => { const [m, x] = s.split(':').map(Number); return (m * 60 + x) * 1000; };

    const ALBUMS = [], TRACKS = new Map();
    ALBUMS_RAW.forEach(([name, artists, date, list]) => {
        const id = fakeId('album:' + name, 22);
        const art = cover(name, artists.join(', '));
        const album = {
            id, name, artist: artists.join(', '), artists, release_date: date, total_tracks: list.length,
            album_art: art, external_url: `https://open.spotify.com/album/${id}`, tracks: [],
        };
        list.forEach(([tname, dur, extra], i) => {
            const all = artists.concat(extra || []);
            const tid = fakeId('track:' + name + ':' + tname, 22);
            const t = {
                id: tid, name: tname, artist: all.join(', '), artists: all, album: name, album_id: id,
                duration_ms: toMs(dur), track_number: i + 1, external_url: `https://open.spotify.com/track/${tid}`,
                preview_url: null, album_art: art, release_date: date, source: 'spotify',
            };
            album.tracks.push(t);
            TRACKS.set(tid, t);
        });
        ALBUMS.push(album);
    });
    const ALL_TRACKS = [...TRACKS.values()];
    const findTrack = (name) => ALL_TRACKS.find((t) => t.name === name);
    // canciones "difíciles": YouTube no da una coincidencia clara y la app pide elegir
    const LOW_CONFIDENCE = new Set(['Bohemian Rhapsody', 'Té para Tres', 'Tabú', 'Dreams'].map((n) => findTrack(n).id));
    // canciones que el visitante "ya tiene" al abrir la demo
    const SEED_LIBRARY = ['Go Your Own Way', 'De Música Ligera', 'Hasta la Raíz'].map((n) => findTrack(n).id);
    // por defecto el buscador muestra algo aunque la búsqueda no coincida
    const FEATURED = ['Get Lucky', 'De Música Ligera', 'Bohemian Rhapsody', 'Dreams', 'Tití Me Preguntó', 'La Ingrata',
        'Puente', 'Hasta la Raíz', 'Instant Crush', 'Go Your Own Way'].map(findTrack);

    function searchIn(items, text, query) {
        const words = norm(query).split(/[^a-z0-9']+/).filter((w) => w.length > 1);
        if (!words.length) return [];
        return items.map((it, i) => {
            const hay = norm(text(it));
            const hits = words.filter((w) => hay.includes(w)).length;
            return { it, i, hits };
        }).filter((x) => x.hits > 0 && x.hits >= Math.ceil(words.length / 2))
            .sort((a, b) => b.hits - a.hits || a.i - b.i).map((x) => x.it);
    }
    const trackText = (t) => `${t.name} ${t.artist} ${t.album}`;
    const withSource = (t, source) => ({ ...t, source: source === 'deezer' ? 'deezer' : 'spotify' });
    const publicTrack = (t, source) => {
        const { album_id, track_number, ...rest } = withSource(t, source); // TrackResponse no incluye estos
        return rest;
    };

    // candidatos de YouTube (misma forma que YouTubeService.search_candidates)
    const VIDEO_QUERIES = new Map(); // video_id -> búsqueda real, para que "Ver ↗" abra algo útil
    function makeCandidates(name, artist, durMs, opts = {}) {
        const low = !!opts.low, n = opts.num || 5;
        const dur = Math.round((durMs || 210000) / 1000);
        const main = String(artist).split(',')[0].trim();
        const variants = [
            [`${name}`, `${main}`, dur, low ? 0.61 : 0.97, 'ytmusic'],
            [`${main} - ${name} (Official Video)`, `${main}`, dur + 14, low ? 0.58 : 0.86, 'ytmusic'],
            [`${name} (Live)`, `${main}`, dur + 41, 0.52, 'ytmusic'],
            [`${main} - ${name} (Lyrics / Letra)`, 'Lyrics Hub', dur + 2, low ? 0.55 : 0.71, 'yt-dlp'],
            [`${name} (Remix)`, 'DJ Ejemplo', dur + 75, 0.36, 'yt-dlp'],
            [`${name} - Cover acústico`, 'Canal de covers', dur - 20, 0.28, 'yt-dlp'],
            [`${main} - ${name} (Audio)`, `${main} - Topic`, dur, low ? 0.6 : 0.9, 'ytmusic'],
            [`${name} (Slowed + Reverb)`, 'Edits', dur + 60, 0.22, 'yt-dlp'],
        ].slice(0, Math.max(n, 5));
        const cands = variants.map(([title, channel, d, score, source]) => {
            const video_id = fakeId('yt:' + title + channel, 11, B64);
            VIDEO_QUERIES.set(video_id, `${title} ${channel}`);
            const host = source === 'ytmusic' ? 'https://music.youtube.com' : 'https://www.youtube.com';
            return { video_id, title, channel, duration: d, thumbnail: thumb(title, video_id), score, url: `${host}/watch?v=${video_id}`, source };
        }).sort((a, b) => b.score - a.score);
        const best = cands[0].score;
        return { success: true, candidates: cands.slice(0, 5), best_score: best, needs_confirmation: best < 0.65, threshold: 0.65 };
    }

    // =================================================================== estado (sessionStorage, con respaldo en memoria)
    const DEFAULT_STATE = () => ({
        jobs: {},                 // id -> {t0, kind, location, track:{name,artist,album,track_number}, pid?}
        library: SEED_LIBRARY.slice(),
        playlists: {},            // pid -> {meta, tracks:[...]}
        bulk: { running: false, playlist_id: null, done: 0, total: 0, tids: [], stop: false },
        cookies: { configured: false },
        spotifyUser: null,
    });
    let state;
    try { state = JSON.parse(sessionStorage.getItem(STORE_KEY)) || DEFAULT_STATE(); } catch (_) { state = DEFAULT_STATE(); }
    function save() { try { sessionStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (_) { /* sin storage: solo memoria */ } }

    // =================================================================== descargas simuladas
    // cada etapa: [segundos desde que inicia, status, stage, progreso, mensaje]
    const STEPS = [
        [0, 'processing', 'fetching', 10, 'Fetching track info...'],
        [0.8, 'processing', 'preparing', 15, 'Preparing download location...'],
        [1.6, 'processing', 'downloading', 30, 'Searching YouTube and downloading...'],
        [4.6, 'processing', 'tagging', 85, 'Applying metadata...'],
        [5.6, 'processing', 'copying', 90, 'Copying to Navidrome library...'],
    ];
    const JOB_SECONDS = 6.5;

    function startJob(id, info) {
        state.jobs[id] = { t0: now() + (info.delay || 0) * 1000, ...info };
        delete state.jobs[id].delay;
        return state.jobs[id];
    }
    function libPath(t, ext = 'mp3') {
        const art = safeName(String(t.artist || 'Desconocido').split(',')[0].trim());
        return `${art}/${safeName(t.album || 'Singles')}/${pad2(t.track_number || 1)} - ${safeName(t.name)}.${ext}`;
    }
    function jobView(id) {
        const j = state.jobs[id];
        if (!j) return null;
        const el = (now() - j.t0) / 1000;
        const base = { file_path: null, download_url: null, error: null };
        const where = j.location === 'local' ? 'local downloads folder' : 'Navidrome server';
        if (j.cancelled) return { ...base, status: 'error', stage: 'error', progress: 0, message: 'Cancelada' };
        if (el < 0) return { ...base, status: 'queued', stage: 'queued', progress: 0, message: j.queuedMsg || `Download queued for ${where}` };
        if (el >= JOB_SECONDS) {
            if (!j.done) { j.done = true; if (TRACKS.has(id) && !state.library.includes(id)) state.library.push(id); }
            const fp = (j.location === 'local' ? '/app/downloads/temp/' : '/music/') + libPath(j.track || {}, j.format || 'mp3');
            return {
                ...base, status: 'completed', stage: 'completed', progress: 100, file_path: fp,
                message: j.location === 'local'
                    ? 'Track ready for download (demo: en tu instalación el navegador lo guardaría en Descargas)'
                    : 'Track successfully added to Navidrome library',
            };
        }
        let cur = STEPS[0];
        for (const s of STEPS) if (el >= s[0]) cur = s;
        let progress = cur[3];
        if (cur[2] === 'downloading') progress = Math.round(30 + 50 * Math.min(1, (el - 1.6) / 3));
        const msg = cur[2] === 'downloading' && progress > 30 ? `Downloading from YouTube... ${progress}%` : cur[4];
        return { ...base, status: cur[1], stage: cur[2], progress, message: msg };
    }

    // =================================================================== playlists
    const SAMPLE_PLAYLIST_ID = '5DemoPlaylistMusicDl01';
    const SAMPLE_URL = `https://open.spotify.com/playlist/${SAMPLE_PLAYLIST_ID}`;
    // [canción, coincidencia en la "biblioteca" (o null), tipo de coincidencia]
    const SAMPLE_TRACKS = [
        ['De Música Ligera'], ['Get Lucky'], ['Bohemian Rhapsody', 'Queen/Greatest Hits/11 - Bohemian Rhapsody (Live at Wembley).mp3', 'maybe'],
        ['Dreams'], ['La Ingrata'], ['Tabú'], ['Tití Me Preguntó'], ['Instant Crush'],
        ['Puente', 'Gustavo Cerati/Bocanada/03 - Puente.flac', 'auto'], ['Hasta la Raíz'], ['Un Millón de Años Luz'],
        ['The Chain'], ['Me Porto Bonito'], ['Love of My Life', 'Queen/Live Killers/Love of My Life (Live).mp3', 'maybe'],
        ['Ojalá Que Llueva Café'], ['Mi Lugar Favorito'],
    ];
    const PRESET_MATCH = new Map(SAMPLE_TRACKS.filter((x) => x[1]).map(([n, p, k]) => [findTrack(n).id, [k, p]]));

    function parsePlaylistId(url) {
        url = String(url || '').trim();
        let m = url.match(/deezer\.com\/(?:[a-z]{2}(?:-[a-z]{2})?\/)?playlist\/(\d+)/);
        if (m) return 'dz:' + m[1];
        if (/^dz:\d+$/.test(url)) return url;
        if (/(^|\.)(deezer\.page\.link|link\.deezer\.com)\//.test(url)) return 'dz:' + (hash(url) % 1e10);
        m = url.match(/playlist[/:]([A-Za-z0-9]{22})/);
        if (m) return m[1];
        if (/^[A-Za-z0-9]{22}$/.test(url)) return url;
        throw fail(400, 'No reconozco ese link. Pega el link de una playlist de Spotify o Deezer.');
    }

    function rematch(pid) {
        const pl = state.playlists[pid];
        pl.tracks.forEach((t) => {
            if (!['missing', 'auto', 'maybe'].includes(t.status)) return;
            const cat = TRACKS.get(t.track_id);
            if (state.library.includes(t.track_id)) {
                t.status = 'auto'; t.match_path = '/music/' + libPath(cat);
            } else if (PRESET_MATCH.has(t.track_id) && !t.rejectedMatch) {
                const [k, p] = PRESET_MATCH.get(t.track_id);
                t.status = k; t.match_path = '/music/' + p;
            } else { t.status = 'missing'; t.match_path = null; }
        });
    }

    function syncJobs(pid) {
        const pl = state.playlists[pid];
        pl.tracks.forEach((t) => {
            if (!['queued', 'downloading'].includes(t.status)) return;
            const j = jobView(t.track_id);
            if (!j) { t.status = 'missing'; return; }
            if (j.status === 'completed') {
                t.status = 'downloaded'; t.file_path = '/music/' + libPath(TRACKS.get(t.track_id) || t); t.note = null;
                t.updated_at = sec();
            } else if (j.status === 'error') { t.status = 'missing'; t.note = null; }
            else if (j.status === 'processing') t.status = 'downloading';
        });
        // descarga masiva: cuenta las terminadas y se apaga sola
        const b = state.bulk;
        if (b.running && b.playlist_id === pid) {
            b.done = b.tids.filter((tid) => { const j = jobView(tid); return j && j.status === 'completed'; }).length;
            const pending = pl.tracks.some((t) => b.tids.includes(t.track_id) && ['queued', 'downloading'].includes(t.status));
            if (!pending) Object.assign(b, { running: false, stop: false });
        }
    }

    function playlistRow(pid, t) {
        const x = { ...t, playlist_id: pid };
        delete x.rejectedMatch;
        for (const k of ['match_path', 'file_path']) if (x[k]) x[k.replace('_path', '_rel')] = x[k].replace(/^\/music\//, '');
        if (['queued', 'downloading'].includes(x.status)) {
            const j = jobView(t.track_id) || {};
            x.progress = j.progress; x.job_message = j.message;
        }
        return x;
    }

    function getRow(pid, tid) {
        const pl = state.playlists[pid];
        const t = pl && pl.tracks.find((x) => x.track_id === tid);
        if (!t) throw fail(404, 'Canción no encontrada en la playlist');
        return t;
    }

    function queuePlaylistDownload(pid, t, delay = 0) {
        const cat = TRACKS.get(t.track_id) || t;
        startJob(t.track_id, { kind: 'playlist', pid, location: 'navidrome', delay, queuedMsg: 'En cola (playlist)', track: { name: cat.name, artist: cat.artist, album: cat.album, track_number: cat.track_number } });
        t.status = 'queued'; t.note = null; t.updated_at = sec();
    }

    // =================================================================== router
    class HttpError extends Error { constructor(status, detail) { super(detail); this.status = status; this.detail = detail; } }
    const fail = (status, detail) => new HttpError(status, detail);
    const routes = [];
    function route(method, pattern, handler) {
        const keys = [];
        const rx = new RegExp('^' + pattern.replace(/^\/+|\/+$/g, '').split('/').map((seg) => {
            if (seg.startsWith(':')) { keys.push(seg.slice(1)); return '([^/]+)'; }
            return seg.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        }).join('/') + '/?$');
        routes.unshift({ method: method.toUpperCase(), rx, keys, handler }); // las últimas registradas tienen prioridad
    }
    function dispatch(method, path, query, body) {
        let allowed = false;
        for (const r of routes) {
            const m = path.match(r.rx);
            if (!m) continue;
            if (r.method !== method) { allowed = true; continue; }
            const params = {};
            r.keys.forEach((k, i) => { params[k] = decodeURIComponent(m[i + 1]); });
            return r.handler({ params, query, body, method, path });
        }
        if (allowed) throw fail(405, 'Method Not Allowed');
        throw fail(501, `Esta función no está disponible en la demo (${method} api/${path}).`);
    }

    // ---- generales
    route('GET', 'health', () => ({ status: 'healthy', spotify_configured: true, metadata_sources: ['spotify', 'deezer'], metadata_source: 'auto', navidrome_path: '/music', demo: true }));
    route('GET', 'formats', () => ({
        formats: [
            { value: 'mp3', label: 'MP3', description: 'Compatible with most devices' },
            { value: 'm4a', label: 'M4A/AAC', description: 'Better quality, smaller files' },
            { value: 'opus', label: 'Opus', description: 'High quality, efficient compression' },
            { value: 'ogg', label: 'OGG Vorbis', description: 'Open source format' },
            { value: 'flac', label: 'FLAC', description: 'Lossless, larger files' },
        ],
        qualities: [
            { value: '96', label: '96 kbps', description: 'Low quality, small files' },
            { value: '128', label: '128 kbps', description: 'Standard quality (default)' },
            { value: '192', label: '192 kbps', description: 'Good quality' },
            { value: '256', label: '256 kbps', description: 'High quality' },
            { value: '320', label: '320 kbps', description: 'Maximum quality' },
            { value: 'lossless', label: 'Lossless', description: 'FLAC only - no quality loss' },
        ],
        default_format: 'mp3', default_quality: '128',
    }));

    // ---- búsqueda
    route('POST', 'search', ({ body }) => {
        const { query = '', limit = 10, offset = 0, source } = body || {};
        let found = searchIn(ALL_TRACKS, trackText, query);
        if (!found.length) found = FEATURED; // en la demo siempre hay algo que probar
        return found.slice(offset || 0, (offset || 0) + (limit || 10)).map((t) => publicTrack(t, source));
    });
    route('POST', 'search/tracks/top', ({ body }) => {
        const { query = '', limit = 5, source } = body || {};
        return searchIn(ALL_TRACKS, trackText, query).slice(0, Math.max(1, Math.min(limit || 5, 10))).map((t) => publicTrack(t, source));
    });
    route('POST', 'search/albums', ({ body }) => {
        const { query = '', limit = 10, offset = 0, source } = body || {};
        let found = searchIn(ALBUMS, (a) => `${a.name} ${a.artist} ${a.tracks.map((t) => t.name).join(' ')}`, query);
        if (!found.length) found = ALBUMS;
        return found.slice(offset || 0, (offset || 0) + (limit || 10)).map(({ tracks, ...a }) => ({ ...a, source: source === 'deezer' ? 'deezer' : 'spotify' }));
    });
    route('GET', 'album/:id', ({ params }) => {
        const a = ALBUMS.find((x) => x.id === params.id);
        if (!a) throw fail(404, 'Album not found');
        return a;
    });
    route('GET', 'track/:id', ({ params }) => {
        const t = TRACKS.get(params.id);
        if (!t) throw fail(404, 'Track not found');
        return publicTrack(t);
    });
    route('GET', 'track/:id/exists', ({ params }) => {
        const t = TRACKS.get(params.id);
        const exists = !!t && state.library.includes(params.id);
        return { exists, file_path: exists ? '/app/downloads/' + libPath(t) : null };
    });

    // ---- YouTube / descargas
    route('GET', 'youtube/candidates/:id', ({ params }) => {
        const t = TRACKS.get(params.id);
        if (!t) throw fail(404, 'Track not found');
        return { track: { id: t.id, name: t.name, artist: t.artist, album: t.album }, ...makeCandidates(t.name, t.artist, t.duration_ms, { low: LOW_CONFIDENCE.has(t.id) }) };
    });
    route('POST', 'download', ({ body }) => {
        const b = body || {};
        const t = TRACKS.get(b.track_id);
        if (!t) throw fail(404, 'Track not found');
        const location = b.location === 'navidrome' ? 'navidrome' : 'local';
        startJob(t.id, { kind: 'track', location, format: b.format || 'mp3', track: { name: t.name, artist: t.artist, album: t.album, track_number: t.track_number } });
        const where = location === 'local' ? 'local downloads folder' : 'Navidrome server';
        return { status: 'queued', message: `Download started to ${where}`, track_id: t.id };
    });
    route('GET', 'download/status/:id', ({ params }) => {
        const j = jobView(params.id);
        if (!j) throw fail(404, 'Download not found');
        return j;
    });
    route('POST', 'download/album', ({ body }) => {
        const b = body || {};
        const a = ALBUMS.find((x) => x.id === b.album_id);
        if (!a) throw fail(404, 'Album not found');
        const location = b.location === 'navidrome' ? 'navidrome' : 'local';
        // una tras otra, como en el servidor real (pero mucho más rápido)
        a.tracks.forEach((t, i) => startJob(t.id, {
            kind: 'track', location, album_id: a.id, format: b.format || 'mp3', delay: i * 2.5,
            queuedMsg: `Queued (Album: ${a.name})`, track: { name: t.name, artist: t.artist, album: t.album, track_number: t.track_number },
        }));
        state.jobs['album:' + a.id] = { t0: now(), kind: 'album', album_id: a.id };
        const where = location === 'local' ? 'local downloads folder' : 'Navidrome server';
        return { status: 'queued', message: `Album '${a.name}' queued for download to ${where}`, album_id: a.id, total_tracks: a.tracks.length };
    });
    route('GET', 'download/album/status/:id', ({ params }) => {
        const a = ALBUMS.find((x) => x.id === params.id);
        if (!a || !state.jobs['album:' + a.id]) throw fail(404, 'Album download not found');
        const views = a.tracks.map((t) => [t, jobView(t.id) || { status: 'queued' }]);
        const completed = views.filter(([, j]) => j.status === 'completed').length;
        const failed = views.filter(([, j]) => j.status === 'error').length;
        const cur = views.find(([, j]) => j.status === 'processing');
        return {
            status: completed + failed === a.tracks.length ? 'completed' : cur ? 'processing' : 'queued',
            album_name: a.name, artist: a.artist, total_tracks: a.tracks.length, completed_tracks: completed,
            failed_tracks: failed, current_track: cur ? cur[0].name : null, track_ids: a.tracks.map((t) => t.id),
        };
    });
    route('GET', 'download/file/:id', () => { throw fail(404, 'En la demo no hay archivos que descargar.'); });

    // ---- YouTube -> metadatos (pegar un link de YouTube en el buscador)
    route('POST', 'reverse/youtube', ({ body }) => {
        const url = String((body || {}).url || '');
        let vid = '';
        try { const u = new URL(url); vid = u.searchParams.get('v') || (u.hostname === 'youtu.be' ? u.pathname.slice(1) : ''); } catch (_) { /* link raro */ }
        vid = (vid || fakeId(url, 11, B64)).slice(0, 11);
        const t = ALL_TRACKS[hash(vid) % ALL_TRACKS.length];
        const title = `${t.artist.split(',')[0]} - ${t.name} (Official Audio)`;
        return {
            youtube: { success: true, video_id: vid, title, uploader: t.artist.split(',')[0], duration: Math.round(t.duration_ms / 1000), thumbnail: thumb(title, vid), webpage_url: `https://www.youtube.com/watch?v=${vid}` },
            query: title,
            spotify_candidates: searchIn(ALL_TRACKS, trackText, `${t.name} ${t.artist}`).slice(0, 5).map((x) => publicTrack(x)),
        };
    });
    route('POST', 'reverse/download', ({ body }) => {
        const b = body || {};
        const location = b.location === 'navidrome' ? 'navidrome' : 'local';
        const id = 'yt-' + (hash(`${b.youtube_url}|${b.spotify_track_id || ''}|${location}`) % 10000000);
        const t = TRACKS.get(b.spotify_track_id) || {};
        const md = b.metadata || {};
        startJob(id, { kind: 'reverse', location, track: { name: md.name || t.name || 'YouTube', artist: md.artist || t.artist || 'YouTube', album: md.album || t.album || 'YouTube', track_number: md.track_number || t.track_number || 1 } });
        const where = location === 'local' ? 'local downloads folder' : 'Navidrome server';
        return { status: 'queued', message: `Reverse download started to ${where}`, job_id: id };
    });

    // ---- cuenta de Spotify (no se puede conectar en la demo)
    route('GET', 'spotify/status', () => (state.spotifyUser ? { connected: true, user: state.spotifyUser } : { connected: false, redirect_uri: 'http://127.0.0.1:8000/callback' }));
    route('GET', 'spotify/auth-url', () => ({ url: '#md-demo-spotify', redirect_uri: 'http://127.0.0.1:8000/callback' }));
    route('POST', 'spotify/auth-code', () => { throw fail(400, 'En la demo no se puede conectar una cuenta de Spotify. En tu instalación, este paso guarda el permiso para leer playlists completas.'); });
    route('POST', 'spotify/logout', () => { state.spotifyUser = null; return { connected: false }; });

    // ---- playlists
    route('GET', 'playlists', () => Object.values(state.playlists).map((p) => p.meta).sort((a, b) => b.imported_at - a.imported_at));
    route('POST', 'playlists/import', ({ body }) => {
        const pid = parsePlaylistId((body || {}).url);
        const isDz = pid.startsWith('dz:');
        const prev = state.playlists[pid];
        const name = pid === SAMPLE_PLAYLIST_ID ? 'Clásicos para la carretera (ejemplo)' : isDz ? 'Playlist de Deezer (ejemplo)' : 'Playlist importada (ejemplo)';
        const meta = { id: pid, name, owner: isDz ? 'Usuario de Deezer' : 'Usuario de la demo', total: SAMPLE_TRACKS.length, loaded: SAMPLE_TRACKS.length, source: isDz ? 'deezer' : 'embed', imported_at: sec() };
        const old = new Map(((prev && prev.tracks) || []).map((t) => [t.track_id, t]));
        const tracks = SAMPLE_TRACKS.map(([n], pos) => {
            const c = findTrack(n);
            const o = old.get(c.id);
            return o ? { ...o, position: pos } : {
                track_id: c.id, position: pos, name: c.name, artist: c.artist, album: c.album, duration_ms: c.duration_ms,
                album_art: c.album_art, status: 'missing', match_path: null, file_path: null, note: null, updated_at: sec(),
            };
        });
        state.playlists[pid] = { meta, tracks };
        rematch(pid);
        return { id: pid, name, total: meta.total, loaded: meta.loaded, source: meta.source, skipped: 0, user_error: null };
    });
    route('GET', 'playlists/:pid', ({ params }) => {
        const pl = state.playlists[params.pid];
        if (!pl) throw fail(404, 'Playlist no importada');
        syncJobs(params.pid);
        const { tids, ...bulk } = state.bulk;
        return { playlist: { ...pl.meta }, tracks: pl.tracks.map((t) => playlistRow(params.pid, t)), library_scanned_at: sec() - 60, bulk: { running: bulk.running, playlist_id: bulk.playlist_id, done: bulk.done, total: bulk.total } };
    });
    route('DELETE', 'playlists/:pid', ({ params }) => { delete state.playlists[params.pid]; return { ok: true }; });
    route('POST', 'playlists/:pid/rescan', ({ params }) => {
        if (!state.playlists[params.pid]) throw fail(404, 'Playlist no importada');
        rematch(params.pid);
        return { ok: true, files: 1287 + state.library.length };
    });
    // ---- sincronización (en la demo la automática está apagada, igual que por defecto en la app real)
    const syncCfg = (pid) => {
        const pl = state.playlists[pid];
        if (!pl) throw fail(404, 'Playlist no importada');
        pl.sync = pl.sync || { auto_sync: false, interval_hours: 24, auto_download: false, last_sync_at: null, last_sync_ok: null,
            last_sync_result: null, last_sync_error: null, fail_count: 0, next_sync_at: null };
        return pl;
    };
    const syncPublic = (pid) => {
        const pl = syncCfg(pid);
        return { playlist_id: pid, ...pl.sync, syncable: true, source: pl.meta.source, partial: false,
            new_count: pl.tracks.filter((t) => t.is_new).length, running: false,
            global: { enabled: false, min_hours: 6, default_hours: 24, max_downloads: 20, test_minutes: null } };
    };
    route('GET', 'playlists/:pid/sync', ({ params }) => syncPublic(params.pid));
    route('PUT', 'playlists/:pid/sync', ({ params, body }) => {
        const pl = syncCfg(params.pid); const b = body || {};
        Object.assign(pl.sync, { auto_sync: !!b.auto_sync, auto_download: !!b.auto_download,
            interval_hours: Math.max(6, parseInt(b.interval_hours, 10) || 24) });
        return syncPublic(params.pid);
    });
    route('POST', 'playlists/:pid/sync', ({ params }) => {
        const pl = syncCfg(params.pid);
        Object.assign(pl.sync, { last_sync_at: sec(), last_sync_ok: 1, last_sync_error: null,
            last_sync_result: JSON.stringify({ added: 0, removed: 0 }) });
        return { trigger: 'manual', added: 0, removed: 0, loaded: pl.meta.loaded, total: pl.meta.total,
            source: pl.meta.source, partial: false, user_error: null, downloads: 0 };
    });
    route('POST', 'playlists/:pid/new/seen', ({ params }) => { syncCfg(params.pid).tracks.forEach((t) => { t.is_new = 0; }); return { ok: true }; });
    // ---- mejora de versiones
    route('POST', 'playlists/:pid/tracks/:tid/upgrade-ignore', ({ params, body }) => {
        const t = getRow(params.pid, params.tid); t.upgrade_ignored = (body || {}).ignore === false ? 0 : 1; return { ok: true };
    });
    route('POST', 'playlists/:pid/tracks/:tid/keep-both', ({ params }) => { const t = getRow(params.pid, params.tid); t.old_path = null; return { ok: true }; });
    route('POST', 'playlists/:pid/tracks/:tid/delete-old', ({ params }) => { const t = getRow(params.pid, params.tid); t.old_path = null; return { ok: true }; });
    // ---- avance de importación (la demo importa al instante)
    route('GET', 'import/progress/:token', () => ({ stage: 'done', done: 0, total: 0 }));

    route('POST', 'playlists/:pid/tracks/:tid/mark', ({ params, body }) => {
        const st = (body || {}).status;
        if (!['have', 'missing', 'skip', 'approved'].includes(st)) throw fail(400, 'Estado no válido');
        const t = getRow(params.pid, params.tid);
        if (st === 'missing') { t.match_path = null; if (PRESET_MATCH.has(t.track_id)) t.rejectedMatch = true; }
        if (st === 'have' && !t.match_path && t.file_path) t.match_path = t.file_path;
        if (st === 'approved' && !state.library.includes(t.track_id)) state.library.push(t.track_id);
        t.status = st; t.updated_at = sec();
        return { ok: true };
    });
    route('GET', 'playlists/:pid/tracks/:tid/candidates', ({ params, query }) => {
        const t = getRow(params.pid, params.tid);
        const q = (query.get('q') || '').trim();
        if (q) return makeCandidates(q, '', t.duration_ms, { num: 8 });
        return makeCandidates(t.name, t.artist, t.duration_ms, { low: LOW_CONFIDENCE.has(t.track_id), num: 8 });
    });
    route('POST', 'playlists/:pid/tracks/:tid/download', ({ params }) => {
        queuePlaylistDownload(params.pid, getRow(params.pid, params.tid));
        return { ok: true };
    });
    route('POST', 'playlists/:pid/tracks/:tid/reject', ({ params }) => {
        const t = getRow(params.pid, params.tid);
        if (t.status !== 'downloaded') throw fail(400, 'Solo se puede rechazar una descarga pendiente de revisión');
        Object.assign(t, { status: 'missing', file_path: null, match_path: null, note: 'Descarga rechazada', updated_at: sec() });
        state.library = state.library.filter((x) => x !== t.track_id);
        delete state.jobs[t.track_id];
        return { ok: true };
    });
    route('GET', 'playlists/:pid/tracks/:tid/audio', ({ params, query }) => {
        const t = getRow(params.pid, params.tid);
        const p = query.get('which') === 'match' ? t.match_path : t.file_path;
        if (!p) throw fail(404, 'No hay archivo para reproducir');
        return { __audio: audioUrl(t.track_id + (query.get('which') || 'file')) };
    });
    route('POST', 'playlists/:pid/download-missing', ({ params }) => {
        if (state.bulk.running) throw fail(409, 'Ya hay una descarga masiva corriendo');
        const pl = state.playlists[params.pid];
        if (!pl) throw fail(404, 'Playlist no importada');
        const todo = pl.tracks.filter((t) => ['missing', 'error'].includes(t.status));
        todo.forEach((t, i) => queuePlaylistDownload(params.pid, t, i * (JOB_SECONDS + 1)));
        state.bulk = { running: todo.length > 0, playlist_id: params.pid, done: 0, total: todo.length, tids: todo.map((t) => t.track_id), stop: false };
        return { ok: true, queued: todo.length };
    });
    route('POST', 'playlists/bulk/stop', () => {
        const b = state.bulk;
        b.stop = true;
        // las que no han empezado regresan a "faltan"; la actual termina
        const pl = state.playlists[b.playlist_id];
        if (pl) pl.tracks.forEach((t) => {
            const j = state.jobs[t.track_id];
            if (b.tids.includes(t.track_id) && t.status === 'queued' && j && j.t0 > now()) { t.status = 'missing'; delete state.jobs[t.track_id]; }
        });
        return { ok: true };
    });

    // ---- cookies de YouTube
    const cookieStatus = () => {
        const c = state.cookies;
        return { configured: !!c.configured, ...Object.fromEntries(['uploaded_at', 'source', 'count', 'expires_at', 'tested_at', 'test'].map((k) => [k, c.configured ? (c[k] ?? null) : null])) };
    };
    function saveCookies(text, source) {
        text = String(text || '').trim();
        if (!text) throw fail(400, 'El archivo de cookies está vacío');
        const rows = text.split(/\r?\n/).map((l) => l.replace(/^#HttpOnly_/, '')).filter((l) => l && !l.startsWith('#') && l.split('\t').length >= 7);
        if (!rows.length) throw fail(400, 'No encontré cookies. Debe ser formato Netscape (cookies.txt) o JSON.');
        const yt = rows.filter((l) => /(youtube|google)\.com/.test(l.split('\t')[0]));
        if (!yt.some((l) => /^(SID|__Secure-3PSID|LOGIN_INFO|SAPISID)$/.test(l.split('\t')[5]))) {
            throw fail(400, 'Las cookies no tienen una sesión de YouTube. Entra a youtube.com con tu cuenta y vuelve a exportarlas.');
        }
        state.cookies = {
            configured: true, uploaded_at: sec(), source, count: yt.length, expires_at: sec() + 180 * 86400, tested_at: sec(),
            test: { ok: true, logged_in: true, message: 'Sesión de YouTube válida (demo: no se envió nada a YouTube)' },
        };
        return cookieStatus();
    }
    route('GET', 'youtube-cookies/status', () => cookieStatus());
    route('POST', 'youtube-cookies', ({ body }) => saveCookies((body || {}).text, 'manual'));
    route('POST', 'youtube-cookies/test', () => {
        if (!state.cookies.configured) throw fail(404, 'No hay cookies guardadas');
        state.cookies.tested_at = sec();
        return cookieStatus();
    });
    route('DELETE', 'youtube-cookies', () => { state.cookies = { configured: false }; return cookieStatus(); });
    route('GET', 'youtube-cookies/helper/:os', ({ params }) => {
        if (!['windows', 'mac', 'linux'].includes(params.os)) throw fail(400, 'Sistema no válido');
        throw fail(404, 'En la demo no se descarga el programa.');
    });

    // =================================================================== fetch simulado
    const realFetch = window.fetch ? window.fetch.bind(window) : null;
    function apiPath(url) {
        if (url.origin !== location.origin) return null;
        const appRoot = new URL(document.baseURI).pathname.replace(/[^/]*$/, '');
        if (url.pathname.startsWith(appRoot + 'api/')) return url.pathname.slice(appRoot.length + 4);
        if (url.pathname.startsWith('/api/')) return url.pathname.slice(5); // p. ej. fetch('/api/formats')
        return null;
    }
    const json = (status, data) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });

    async function handle(method, url, rawBody) {
        let body = rawBody;
        if (typeof body === 'string') { try { body = JSON.parse(body); } catch (_) { /* texto plano */ } }
        const path = apiPath(url);
        Object.keys(state.jobs).forEach(jobView); // las descargas avanzan aunque nadie las consulte
        await sleep(path.startsWith('search') || path.endsWith('/candidates') || path.includes('reverse/youtube') ? 350 + Math.random() * 300 : 60 + Math.random() * 140);
        try {
            const out = dispatch(method, path.replace(/\/+$/, ''), url.searchParams, body);
            save();
            if (out instanceof Response) return out;
            if (out && out.__audio) return fetchAudio(out.__audio);
            return json(200, out === undefined ? { ok: true } : out);
        } catch (e) {
            save();
            if (e instanceof HttpError) {
                if (e.status === 501) console.info('[demo]', e.detail);
                return json(e.status, { detail: e.detail });
            }
            console.warn('[demo] error en el API simulado:', e);
            return json(500, { detail: 'Error en la demo: ' + (e && e.message) });
        }
    }
    async function fetchAudio(blobUrl) { return realFetch ? realFetch(blobUrl) : json(404, { detail: 'Sin audio' }); }

    window.fetch = function (input, init) {
        init = init || {};
        const isReq = typeof Request !== 'undefined' && input instanceof Request;
        let url;
        try { url = new URL(isReq ? input.url : String(input), document.baseURI); } catch (_) { return realFetch(input, init); }
        if (apiPath(url) === null) return realFetch(input, init);
        const method = String(init.method || (isReq ? input.method : 'GET')).toUpperCase();
        if (isReq && init.body === undefined && method !== 'GET') return input.clone().text().then((t) => handle(method, url, t));
        return handle(method, url, init.body);
    };

    // =================================================================== <audio src="api/..."> y enlaces a api/...
    // Los <audio> no pasan por fetch: se cambia su src por el WAV generado.
    function fixMedia(el) {
        const src = el.getAttribute('src');
        if (!src || src.startsWith('blob:') || src.startsWith('data:')) return;
        let url;
        try { url = new URL(src, document.baseURI); } catch (_) { return; }
        const path = apiPath(url);
        if (path === null) return;
        el.setAttribute('data-demo-src', src);
        let seed = path;
        const m = path.match(/^playlists\/[^/]+\/tracks\/([^/]+)\/audio$/);
        if (m) seed = decodeURIComponent(m[1]) + (url.searchParams.get('which') || 'file');
        el.setAttribute('src', audioUrl(seed));
    }
    // la app usa via.placeholder.com cuando no hay portada (p. ej. descargas desde un link de YouTube)
    function fixPlaceholder(img) {
        const src = img.getAttribute('src') || '';
        if (src.startsWith('https://via.placeholder.com')) img.setAttribute('src', cover(img.getAttribute('alt') || 'YouTube', 'YouTube'));
    }
    function scan(root) {
        if (!root || root.nodeType !== 1) return;
        if (/^(AUDIO|VIDEO|SOURCE)$/.test(root.tagName)) fixMedia(root);
        if (root.tagName === 'IMG') fixPlaceholder(root);
        root.querySelectorAll && root.querySelectorAll('audio[src],video[src],source[src]').forEach(fixMedia);
        root.querySelectorAll && root.querySelectorAll('img[src^="https://via.placeholder.com"]').forEach(fixPlaceholder);
    }
    new MutationObserver((muts) => {
        for (const m of muts) {
            if (m.type === 'attributes') scan(m.target);
            else m.addedNodes.forEach(scan);
        }
    }).observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['src'] });

    // ---- avisos de la demo
    let toastEl = null;
    function toast(msg, ms = 4500) {
        if (!toastEl) {
            toastEl = document.createElement('div');
            toastEl.className = 'md-demo-toast';
            toastEl.setAttribute('role', 'status');
            document.body.appendChild(toastEl);
        }
        toastEl.textContent = msg;
        toastEl.hidden = false;
        clearTimeout(toastEl._h);
        toastEl._h = setTimeout(() => { toastEl.hidden = true; }, ms);
    }

    // clics en enlaces que en la app real irían al servidor o a IDs inventados
    const LINK_HANDLERS = [
        [/^youtube-cookies\/helper\/(windows|mac|linux)$/, (m) => {
            toast('En la demo no se descarga el programa. Simulando que tu navegador subió las cookies…');
            setTimeout(() => {
                const name = { windows: 'edge', mac: 'safari', linux: 'firefox' }[m[1]];
                const cookie = '.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tdemo';
                try { saveCookies(cookie, `auto (${name})`); save(); } catch (_) { /* nada */ }
            }, 3500);
        }],
    ];
    document.addEventListener('click', (e) => {
        const a = e.target.closest && e.target.closest('a[href]');
        if (!a) return;
        const href = a.getAttribute('href');
        if (href === '#md-demo-spotify') {
            e.preventDefault();
            toast('En la demo no se conecta ninguna cuenta de Spotify.');
            return;
        }
        let url;
        try { url = new URL(href, document.baseURI); } catch (_) { return; }
        const path = apiPath(url);
        if (path !== null) {
            e.preventDefault();
            for (const [rx, fn] of LINK_HANDLERS) { const m = path.match(rx); if (m) return fn(m); }
            toast('Esto no está disponible en la demo.');
            return;
        }
        // IDs de ejemplo: abre una búsqueda real en vez de una página inexistente
        let ext = null;
        const sp = url.hostname === 'open.spotify.com' && url.pathname.match(/^\/track\/([A-Za-z0-9]{22})$/);
        if (sp && TRACKS.has(sp[1])) { const t = TRACKS.get(sp[1]); ext = `https://open.spotify.com/search/${encodeURIComponent(t.name + ' ' + t.artist)}`; }
        const v = /(^|\.)youtube\.com$/.test(url.hostname) && url.searchParams.get('v');
        if (v && VIDEO_QUERIES.has(v)) ext = `https://www.youtube.com/results?search_query=${encodeURIComponent(VIDEO_QUERIES.get(v))}`;
        if (ext) { e.preventDefault(); window.open(ext, '_blank', 'noopener'); }
    }, true);

    // ---- atajos por página para probar rápido
    function hint(label, items) {
        const box = document.createElement('div');
        box.className = 'md-demo-hint';
        box.innerHTML = `<span>${label}</span>`;
        items.forEach(([text, fn]) => {
            const b = document.createElement('button');
            b.type = 'button'; b.textContent = text; b.onclick = fn;
            box.appendChild(b);
        });
        return box;
    }
    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('audio[src],source[src]').forEach(fixMedia);
        const search = document.getElementById('searchInput');
        if (search) {
            const go = (q, albums) => () => {
                const btn = document.getElementById(albums ? 'searchAlbums' : 'searchTracks');
                search.value = q;
                if (btn && !btn.classList.contains('active')) btn.click(); else document.getElementById('searchBtn').click();
            };
            document.querySelector('.search-options').after(hint('Prueba:', [
                ['Daft Punk', go('Daft Punk')], ['Soda Stereo', go('Soda Stereo')], ['Bohemian Rhapsody', go('Bohemian Rhapsody')],
                ['Álbum: Rumours', go('Rumours', true)], ['Link de YouTube', go('https://www.youtube.com/watch?v=dQw4w9WgXcQ')],
            ]));
        }
        const plUrl = document.getElementById('plUrl');
        if (plUrl) {
            document.getElementById('importForm').after(hint('Demo:', [['Cargar la playlist de ejemplo', () => {
                plUrl.value = SAMPLE_URL;
                document.getElementById('importForm').requestSubmit();
            }]]));
        }
        const cookieText = document.getElementById('text');
        if (cookieText && document.getElementById('save')) {
            cookieText.after(hint('Demo:', [['Pegar un cookies.txt de ejemplo', () => {
                cookieText.value = '# Netscape HTTP Cookie File\n# EJEMPLO para la demo: no son cookies reales\n' +
                    '.youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\tEJEMPLO-no-es-real\n' +
                    '.youtube.com\tTRUE\t/\tTRUE\t1893456000\tLOGIN_INFO\tEJEMPLO-no-es-real\n' +
                    '.youtube.com\tTRUE\t/\tTRUE\t1893456000\tPREF\tf6=40000000&hl=es\n';
            }]]));
        }
    });

    window.MDDemo = { route, fail, HttpError, state: () => state, save, toast, audioUrl, cover, thumb, tracks: TRACKS, albums: ALBUMS, reset() { state = DEFAULT_STATE(); save(); } };
})();
