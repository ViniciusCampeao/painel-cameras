"""Páginas de Registros (pessoas detectadas) e Gravações (playback com barra de tempo).

Os templates recebem só o nome das câmeras (opcoes), nunca o dicionário CAMERAS,
que contém host e senha.
"""

_BASE_CSS = """
:root { --bg:#0b0d10; --card:#14181d; --card-2:#1b2027; --line:#232a33;
        --txt:#e6e9ed; --dim:#8b95a1; --ok:#35d07f; --acc:#ff8a3d; }
* { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
body { background:var(--bg); color:var(--txt);
       font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
       min-height:100dvh; padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom); }
header { display:flex; align-items:center; justify-content:space-between; gap:12px;
         padding:16px 18px 12px; flex-wrap:wrap; }
header h1 { font-size:19px; font-weight:650; }
nav { display:flex; gap:6px; flex-wrap:wrap; }
nav a { color:var(--dim); text-decoration:none; font-size:13px; padding:7px 12px;
        border-radius:10px; border:1px solid var(--line); background:var(--card); }
nav a.on { color:var(--txt); background:var(--card-2); }
.main { padding:4px 14px 32px; display:flex; flex-direction:column; gap:14px; max-width:1100px; margin:0 auto; }
.linha { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
select, input, button { font:inherit; color:var(--txt); background:var(--card);
        border:1px solid var(--line); border-radius:10px; padding:8px 10px; }
button { cursor:pointer; }
button:active { background:var(--card-2); }
.aviso { color:var(--dim); font-size:13px; }
.erro { color:#f2545b; font-size:13px; }
"""

REGISTROS_HTML = """<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Registros</title>
<style>""" + _BASE_CSS + """
.grade { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:12px; }
.item { background:var(--card); border:1px solid var(--line); border-radius:14px; overflow:hidden;
        text-decoration:none; color:inherit; display:block; }
.item img { width:100%; aspect-ratio:16/9; object-fit:cover; background:#000; display:block; }
.item .corpo { padding:10px 12px 12px; }
.item .cam { font-size:12px; color:var(--acc); font-weight:600; text-transform:uppercase; letter-spacing:.4px; }
.item .hora { font-size:15px; font-weight:600; margin-top:2px; }
.item .meta { font-size:12px; color:var(--dim); margin-top:4px; }
</style>
</head>
<body>
<header>
  <h1>Registros</h1>
  <nav>
    <a href="/">Ao vivo</a>
    <a class="on" href="/registros">Registros</a>
    <a href="/gravacoes">Gravações</a>
  </nav>
</header>
<div class="main">
  <div class="linha">
    <select id="cam" onchange="carregar()">
      <option value="">Todas as câmeras</option>
      {% for cid, nome in opcoes.items() %}<option value="{{ cid }}">{{ nome }}</option>{% endfor %}
    </select>
    <button onclick="carregar()">Atualizar</button>
    <span class="aviso" id="status"></span>
  </div>
  <div class="grade" id="grade"></div>
</div>
<script>
function fmtData(ts) {
  return new Date(ts * 1000).toLocaleString('pt-BR', {day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit'});
}
function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), x = s%60;
  return (h ? h + 'h ' : '') + (m || h ? m + 'min ' : '') + (h ? '' : x + 's');
}
async function carregar() {
  const cam = document.getElementById('cam').value;
  const st = document.getElementById('status');
  const grade = document.getElementById('grade');
  st.textContent = 'carregando...';
  try {
    const r = await fetch('/api/registros' + (cam ? '?cam=' + encodeURIComponent(cam) : ''), {cache:'no-store'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const itens = await r.json();
    st.textContent = itens.length + ' registro(s)';
    grade.innerHTML = '';
    if (!itens.length) {
      grade.innerHTML = '<p class="aviso">Nenhuma pessoa registrada ainda.</p>';
      return;
    }
    for (const it of itens) {
      const a = document.createElement('a');
      a.className = 'item';
      a.href = '/gravacoes?cam=' + encodeURIComponent(it.cam) + '&t=' + Math.max(0, it.inicio - 15);
      a.innerHTML =
        '<img loading="lazy" alt="">' +
        '<div class="corpo"><div class="cam"></div><div class="hora"></div>' +
        '<div class="meta"></div></div>';
      if (it.miniatura_url) a.querySelector('img').src = it.miniatura_url;
      a.querySelector('.cam').textContent = it.cam_nome;
      a.querySelector('.hora').textContent = fmtData(it.inicio);
      a.querySelector('.meta').textContent =
        'duração ' + fmtDur(it.fim - it.inicio) + ' · ' + it.deteccoes + ' detecções · ' +
        Math.round((it.confianca || 0) * 100) + '%';
      grade.appendChild(a);
    }
  } catch (e) {
    st.textContent = '';
    st.className = 'erro';
    st.textContent = 'Falha ao carregar: ' + e.message;
  }
}
carregar();
</script>
</body>
</html>
"""

GRAVACOES_HTML = """<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Gravações</title>
<style>""" + _BASE_CSS + """
video { width:100%; max-height:70vh; background:#000; border-radius:14px; border:1px solid var(--line); }
.hora-atual { font-size:22px; font-weight:650; font-variant-numeric:tabular-nums; }
.pulos button { padding:8px 12px; }
.sem { display:none; }
</style>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.7/dist/hls.min.js"></script>
</head>
<body>
<header>
  <h1>Gravações</h1>
  <nav>
    <a href="/">Ao vivo</a>
    <a href="/registros">Registros</a>
    <a class="on" href="/gravacoes">Gravações</a>
  </nav>
</header>
<div class="main">
  <div class="linha">
    <select id="cam" onchange="trocarCamera()">
      {% for cid, nome in opcoes.items() %}<option value="{{ cid }}">{{ nome }}</option>{% endfor %}
    </select>
    <span class="aviso" id="status">carregando...</span>
  </div>

  <video id="v" controls playsinline preload="metadata"></video>
  <p class="erro sem" id="erro-video">Este navegador não toca esta gravação (H.265). Abra em Safari/iPhone.</p>

  <div class="hora-atual" id="hora">--</div>

  <div class="linha">
    <input type="datetime-local" id="ir" step="1">
    <button onclick="irDataHora()">Ir para o horário</button>
  </div>
  <div class="linha pulos">
    <button onclick="pular(-600)">−10 min</button>
    <button onclick="pular(-60)">−1 min</button>
    <button onclick="pular(60)">+1 min</button>
    <button onclick="pular(600)">+10 min</button>
  </div>
  <p class="aviso" id="info"></p>
</div>
<script>
const CAM_INICIAL = {{ cam_inicial|tojson }};
const T_INICIAL = {{ t_inicial|tojson }};
const v = document.getElementById('v');
let segs = [], cum = [], total = 0;

function setStatus(msg) { document.getElementById('status').textContent = msg; }
function fmtHora(e) { return new Date(e * 1000).toLocaleString('pt-BR'); }
function valorLocal(e) {
  const d = new Date(e * 1000), p = n => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p(d.getMonth()+1) + '-' + p(d.getDate()) + 'T' +
         p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

// posição no player (segundos desde o primeiro segmento) -> horário real (epoch)
function horarioDe(pos) {
  if (!segs.length) return null;
  let lo = 0, hi = cum.length - 1;
  if (pos <= 0) return segs[0].i;
  while (lo < hi) {
    const m = Math.ceil((lo + hi) / 2);
    if (cum[m] <= pos) lo = m; else hi = m - 1;
  }
  return segs[lo].i + Math.min(pos - cum[lo], segs[lo].d);
}

// horário real (epoch) -> posição no player. Dentro de um buraco, vai para o próximo segmento.
function posicaoDe(epoch) {
  if (!segs.length) return null;
  if (epoch <= segs[0].i) return 0;
  let lo = 0, hi = segs.length - 1;
  while (lo < hi) {
    const m = Math.ceil((lo + hi) / 2);
    if (segs[m].i <= epoch) lo = m; else hi = m - 1;
  }
  const s = segs[lo];
  if (epoch - s.i >= s.d) return cum[lo] + s.d;
  return cum[lo] + (epoch - s.i);
}

function camAtual() { return document.getElementById('cam').value; }

async function carregarCamera(cam, tAlvo) {
  setStatus('carregando...');
  document.getElementById('erro-video').style.display = 'none';
  const r = await fetch('/gravacao/' + encodeURIComponent(cam) + '/segmentos', {cache:'no-store'});
  if (!r.ok) { setStatus('Falha ao carregar: HTTP ' + r.status); return; }
  const j = await r.json();
  segs = j.segmentos; cum = []; total = 0;
  for (const s of segs) { cum.push(total); total += s.d; }
  if (!segs.length) {
    setStatus('Sem gravação disponível para esta câmera.');
    v.removeAttribute('src'); v.load();
    return;
  }
  const inicio = segs[0].i, fim = segs[segs.length-1].i + segs[segs.length-1].d;
  document.getElementById('info').textContent =
    'Gravação disponível de ' + fmtHora(inicio) + ' até ' + fmtHora(fim);
  setStatus('');

  const src = '/gravacao/' + encodeURIComponent(cam) + '/playlist.m3u8';
  const alvo = tAlvo ? posicaoDe(tAlvo) : null;
  const aoPronto = () => { if (alvo !== null) v.currentTime = alvo; };
  v.onloadedmetadata = aoPronto;
  if (v.canPlayType('application/vnd.apple.mpegurl')) {
    v.src = src;
  } else if (window.Hls && Hls.isSupported()) {
    if (window._hls) window._hls.destroy();
    window._hls = new Hls();
    window._hls.loadSource(src);
    window._hls.attachMedia(v);
    window._hls.on(Hls.Events.ERROR, (_, d) => {
      if (d.fatal) document.getElementById('erro-video').style.display = 'block';
    });
  } else {
    document.getElementById('erro-video').style.display = 'block';
  }
  v.ontimeupdate = () => {
    const h = horarioDe(v.currentTime);
    if (h) document.getElementById('hora').textContent = fmtHora(h);
  };
  v.onerror = () => { document.getElementById('erro-video').style.display = 'block'; };
  if (tAlvo) document.getElementById('ir').value = valorLocal(tAlvo);
}

function trocarCamera() {
  const u = new URL(location.href);
  u.searchParams.set('cam', camAtual());
  u.searchParams.delete('t');
  history.replaceState(null, '', u);
  carregarCamera(camAtual(), null);
}

function irDataHora() {
  const val = document.getElementById('ir').value;
  if (!val) return;
  const epoch = new Date(val).getTime() / 1000;
  if (!segs.length) return;
  const fora = epoch < segs[0].i || epoch > segs[segs.length-1].i + segs[segs.length-1].d;
  if (fora) {
    document.getElementById('info').textContent = 'Esse horário está fora da gravação disponível.';
    return;
  }
  v.currentTime = posicaoDe(epoch);
  v.play().catch(() => {});
}

function pular(seg) {
  if (!segs.length) return;
  v.currentTime = Math.max(0, Math.min(total, v.currentTime + seg));
}

const sel = document.getElementById('cam');
if (CAM_INICIAL && [...sel.options].some(o => o.value === CAM_INICIAL)) sel.value = CAM_INICIAL;
carregarCamera(camAtual(), T_INICIAL ? Number(T_INICIAL) : null);
</script>
</body>
</html>
"""
