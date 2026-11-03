"""Página de Rostos: galeria de PESSOAS (rostos agrupados por semelhança) para
rotular como morador, estranho ou com um nome. Rotular um grupo vale para todas
as aparições daquela pessoa; quem recebe nome passa a ser reconhecido sozinho."""

from paginas_gravacao import _BASE_CSS

ROSTOS_HTML = """<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Rostos</title>
<style>""" + _BASE_CSS + """
.filtros { display:flex; gap:6px; flex-wrap:wrap; }
.filtros button.on { background:var(--card-2); color:var(--txt); border-color:var(--acc); }
.grade { display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr)); gap:12px; }
.rosto { background:var(--card); border:1px solid var(--line); border-radius:14px; overflow:hidden; }
.thumb { position:relative; }
.rosto img { width:100%; aspect-ratio:1/1; object-fit:cover; background:#000; display:block; }
.badge { position:absolute; top:6px; right:6px; background:rgba(10,12,15,.8); color:#fff;
         font-size:11px; font-weight:600; padding:3px 7px; border-radius:999px; }
.rosto .corpo { padding:8px 10px 10px; display:flex; flex-direction:column; gap:6px; }
.rosto .cam { font-size:11px; color:var(--acc); font-weight:600; text-transform:uppercase; letter-spacing:.4px; }
.rosto .hora { font-size:12px; color:var(--dim); }
.rosto .auto { font-size:12px; color:#7fd0ff; }
.tag { display:inline-block; font-size:12px; font-weight:600; padding:2px 8px; border-radius:999px; }
.tag.morador { background:#15311f; color:#5fe39a; }
.tag.estranho { background:#3a1d1f; color:#ff8a8a; }
.tag.conhecido { background:#1b2b3a; color:#7fd0ff; }
.acoes { display:flex; gap:5px; flex-wrap:wrap; }
.acoes button { padding:6px 8px; font-size:12px; flex:1; min-width:56px; }
.acoes .mor { border-color:#2c5a3b; }
.acoes .est { border-color:#5a2a2e; }
.nomerow { display:flex; gap:5px; }
.nomerow input { flex:1; min-width:0; padding:6px 8px; font-size:12px; }
.nomerow button { padding:6px 8px; font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>Rostos</h1>
  <nav>
    <a href="/">Ao vivo</a>
    <a href="/registros">Registros</a>
    <a class="on" href="/rostos">Rostos</a>
    <a href="/gravacoes">Gravações</a>
  </nav>
</header>
<div class="main">
  <div class="linha filtros">
    <button data-f="pendente" class="on" onclick="setFiltro('pendente')">Pendentes</button>
    <button data-f="rotulado" onclick="setFiltro('rotulado')">Rotulados</button>
    <button data-f="" onclick="setFiltro('')">Todos</button>
    <span class="aviso" id="status"></span>
  </div>
  <div class="grade" id="grade"></div>
</div>
<div class="toast" id="toast" style="position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(80px);background:#232a33;border:1px solid var(--line);color:var(--txt);padding:10px 16px;border-radius:11px;font-size:13px;opacity:0;transition:all .22s ease;pointer-events:none;z-index:200;"></div>
<script>
let filtro = 'pendente';

function aviso(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.opacity = '1'; t.style.transform = 'translateX(-50%) translateY(0)';
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.style.opacity = '0'; t.style.transform = 'translateX(-50%) translateY(80px)'; }, 2400);
}
function fmtData(ts) {
  return new Date(ts * 1000).toLocaleString('pt-BR', {day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit'});
}
function setFiltro(f) {
  filtro = f;
  document.querySelectorAll('.filtros button').forEach(b => b.classList.toggle('on', b.dataset.f === f));
  carregar();
}
function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

async function rotular(it, rotulo, nome) {
  const url = (it.grupo != null)
    ? '/api/grupo/' + it.grupo + '/rotular'
    : '/api/rosto/' + it.rep_id + '/rotular';
  try {
    const r = await fetch(url, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rotulo: rotulo, nome: nome || null})
    });
    const j = await r.json();
    if (j.ok) { aviso(j.afetados ? ('Salvo (' + j.afetados + ' fotos)') : 'Salvo'); carregar(); }
    else aviso('Falha: ' + (j.erro || 'erro'));
  } catch (e) { aviso('Sem resposta'); }
}

async function carregar() {
  const st = document.getElementById('status');
  const grade = document.getElementById('grade');
  st.className = 'aviso'; st.textContent = 'carregando...';
  try {
    const r = await fetch('/api/rostos' + (filtro ? '?status=' + filtro : ''), {cache:'no-store'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const itens = await r.json();
    st.textContent = itens.length + ' pessoa(s)';
    grade.innerHTML = '';
    if (!itens.length) {
      grade.innerHTML = '<p class="aviso">Nenhuma pessoa ' +
        (filtro === 'pendente' ? 'pendente' : filtro === 'rotulado' ? 'rotulada' : '') +
        ' ainda. Rostos aparecem aqui quando alguém é filmado de frente.</p>';
      return;
    }
    for (const it of itens) {
      const card = document.createElement('div');
      card.className = 'rosto';
      const badge = it.total > 1 ? '<span class="badge">' + it.total + ' fotos</span>' : '';
      let corpo = '<div class="corpo">';
      corpo += '<span class="cam">' + esc((it.cams || []).join(', ')) + '</span>';
      corpo += '<span class="hora">última: ' + fmtData(it.ts) + '</span>';
      if (it.auto_nome && !it.rotulo) corpo += '<span class="auto">parece: ' + esc(it.auto_nome) + '</span>';
      if (it.rotulo) {
        const txt = it.nome ? esc(it.nome) : (it.rotulo === 'morador' ? 'Morador' :
                    it.rotulo === 'estranho' ? 'Estranho' : 'Conhecido');
        corpo += '<span class="tag ' + it.rotulo + '">' + txt + '</span>';
      }
      corpo += '<div class="acoes">' +
        '<button class="mor" data-a="morador">Morador</button>' +
        '<button class="est" data-a="estranho">Estranho</button></div>';
      corpo += '<div class="nomerow"><input placeholder="nome..." value="' + esc(it.nome || '') +
        '"><button data-a="nome">Salvar</button></div>';
      corpo += '</div>';
      card.innerHTML = '<div class="thumb"><img loading="lazy" alt="rosto" src="/rosto/' +
        it.rep_id + '/imagem">' + badge + '</div>' + corpo;
      card.querySelector('[data-a="morador"]').onclick = () =>
        rotular(it, 'morador', card.querySelector('.nomerow input').value.trim());
      card.querySelector('[data-a="estranho"]').onclick = () => rotular(it, 'estranho', null);
      card.querySelector('[data-a="nome"]').onclick = () => {
        const nome = card.querySelector('.nomerow input').value.trim();
        if (!nome) { aviso('Digite um nome'); return; }
        rotular(it, 'conhecido', nome);
      };
      grade.appendChild(card);
    }
  } catch (e) {
    st.className = 'erro'; st.textContent = 'Falha ao carregar: ' + e.message;
  }
}
carregar();
</script>
</body>
</html>
"""
