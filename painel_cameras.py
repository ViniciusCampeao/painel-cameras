from flask import Flask, Response, render_template_string, jsonify, request, send_file
from flask_httpauth import HTTPBasicAuth
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import subprocess
import time
import threading
import socket
import os
from urllib.parse import quote
import cv2
import numpy as np
from io import BytesIO

load_dotenv()

try:
    from onvif import ONVIFCamera
    ONVIF_OK = True
except Exception:
    ONVIF_OK = False

try:
    from detector_lite import Detector
    DETECTOR_OK = True
except Exception as e:
    print(f"[init] Detector não disponível: {e}")
    DETECTOR_OK = False

import gravacao
import registro
import rostos
from paginas_gravacao import REGISTROS_HTML, GRAVACOES_HTML
from paginas_rostos import ROSTOS_HTML

app = Flask(__name__)
auth = HTTPBasicAuth()

USERS = {
    os.environ["PAINEL_USER"]: generate_password_hash(os.environ["PAINEL_SENHA"])
}

@auth.verify_password
def verify(username, password):
    if username in USERS and check_password_hash(USERS[username], password):
        return username


CAMERAS = {
    "cozinha": {
        "nome": "Cozinha",
        "host": os.environ["CAM_COZINHA_HOST"],
        "user": os.environ["CAM_COZINHA_USER"],
        "senha": os.environ["CAM_COZINHA_SENHA"],
        "path": "/onvif2",
        "transporte": "tcp",
        "ptz": True,
        "audio": False,
        "onvif_porta": 8899,
        "inverter_pan": True,
        "inverter_tilt": False,
        # recodificado para 640x360 H.264: 1080p HEVC em cópia gastava ~10 GB/dia
        "gravar_h264": True
    },
    "garagem": {
        "nome": "Garagem",
        "host": os.environ["CAM_GARAGEM_HOST"],
        "user": os.environ["CAM_GARAGEM_USER"],
        "senha": os.environ["CAM_GARAGEM_SENHA"],
        "path": "/onvif2",
        "audio_path": "/onvif1",
        "transporte": "udp",
        "ptz": False,
        "audio": True,
        "onvif_porta": 50000,
        "inverter_pan": False,
        "inverter_tilt": False,
        # UDP sem timestamps válidos não grava HEVC em cópia; 640x360 recodificado é barato
        "gravar_h264": True
    }
}

PORTAS_ONVIF = [80, 8899, 8000, 2020, 8080, 5000, 8899, 8081, 10080]


def rtsp_url(cam):
    user = quote(cam['user'], safe='')
    senha = quote(cam['senha'], safe='')
    return f"rtsp://{user}:{senha}@{cam['host']}:554{cam['path']}"


# ---------------------------------------------------------------- PTZ

_ptz_cache = {}
_ptz_falhas = {}
_ptz_lock = threading.Lock()


def porta_aberta(host, porta, tempo=2.0):
    s = socket.socket()
    s.settimeout(tempo)
    try:
        s.connect((host, int(porta)))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def get_ptz(cam_id):
    cam = CAMERAS[cam_id]

    if not cam.get("ptz", True):
        return None, "PTZ desabilitado para esta camera"

    if not ONVIF_OK:
        return None, "onvif-zeep nao instalado"

    with _ptz_lock:
        if cam_id in _ptz_cache:
            return _ptz_cache[cam_id], None
        falha = _ptz_falhas.get(cam_id)
        if falha and time.time() - falha[0] < 60:
            return None, falha[1]

    portas = [cam["onvif_porta"]] if cam.get("onvif_porta") else PORTAS_ONVIF
    erro = None

    for porta in portas:
        if not porta_aberta(cam["host"], porta):
            erro = f"porta {porta} fechada"
            continue
        try:
            dev = ONVIFCamera(cam["host"], porta, cam["user"], cam["senha"])
            media = dev.create_media_service()
            ptz = dev.create_ptz_service()
            perfil = media.GetProfiles()[0]
            handle = {"ptz": ptz, "token": perfil.token, "porta": porta}
            with _ptz_lock:
                _ptz_cache[cam_id] = handle
                _ptz_falhas.pop(cam_id, None)
            print(f"[{cam_id}] ONVIF conectado na porta {porta}")
            return handle, None
        except Exception as e:
            erro = f"porta {porta}: {type(e).__name__}"
            continue

    erro = erro or "nenhuma porta ONVIF respondeu"
    with _ptz_lock:
        _ptz_falhas[cam_id] = (time.time(), erro)
    return None, erro


VELOCIDADES = {
    "up":        {"pan": 0.0,  "tilt": 0.6,  "zoom": 0.0},
    "down":      {"pan": 0.0,  "tilt": -0.6, "zoom": 0.0},
    "left":      {"pan": -0.6, "tilt": 0.0,  "zoom": 0.0},
    "right":     {"pan": 0.6,  "tilt": 0.0,  "zoom": 0.0},
    "zoom_in":   {"pan": 0.0,  "tilt": 0.0,  "zoom": 0.5},
    "zoom_out":  {"pan": 0.0,  "tilt": 0.0,  "zoom": -0.5},
}


def mover(cam_id, direcao, duracao=0.4):
    handle, erro = get_ptz(cam_id)
    if not handle:
        return False, erro

    v = VELOCIDADES.get(direcao)
    if not v:
        return False, "direcao invalida"

    cam = CAMERAS[cam_id]
    pan = -v["pan"] if cam.get("inverter_pan") else v["pan"]
    tilt = -v["tilt"] if cam.get("inverter_tilt") else v["tilt"]

    ptz = handle["ptz"]
    token = handle["token"]

    try:
        req = ptz.create_type("ContinuousMove")
        req.ProfileToken = token
        req.Velocity = {
            "PanTilt": {"x": pan, "y": tilt},
            "Zoom": {"x": v["zoom"]}
        }
        ptz.ContinuousMove(req)
        time.sleep(min(float(duracao), 2.0))
        ptz.Stop({"ProfileToken": token})
        return True, None
    except Exception as e:
        with _ptz_lock:
            _ptz_cache.pop(cam_id, None)
        return False, str(e)


# ------------------------------------------------------------ STREAMS

class StreamManager:
    def __init__(self):
        self.locks = {c: threading.Lock() for c in CAMERAS}
        self.viewers = {c: 0 for c in CAMERAS}
        self.last_frame = {c: None for c in CAMERAS}
        self.running = {c: False for c in CAMERAS}
        self.last_poll = {c: 0.0 for c in CAMERAS}
        threading.Thread(target=self._poll_watchdog, daemon=True).start()

    def touch(self, cam_id):
        self.last_poll[cam_id] = time.time()
        with self.locks[cam_id]:
            if not self.running[cam_id]:
                self.running[cam_id] = True
                self.viewers[cam_id] = 1
                threading.Thread(target=self._loop, args=(cam_id,), daemon=True).start()

    def _poll_watchdog(self):
        while True:
            time.sleep(5)
            for cam_id in CAMERAS:
                if self.running[cam_id] and self.last_poll[cam_id] and time.time() - self.last_poll[cam_id] > 15:
                    with self.locks[cam_id]:
                        self.running[cam_id] = False
                        self.viewers[cam_id] = 0

    def _cmd(self, cam):
        cmd = ["ffmpeg"]
        t = cam.get("transporte", "tcp")
        if t in ("tcp", "udp"):
            cmd += ["-rtsp_transport", t]
        cmd += [
            "-i", rtsp_url(cam),
            "-an",
            "-threads", "2",
            "-vf", "scale=640:-1",
            "-r", "8",
            "-q:v", "8",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-"
        ]
        return cmd

    def _loop(self, cam_id):
        cam = CAMERAS[cam_id]
        while self.running[cam_id]:
            print(f"[{cam_id}] FFmpeg iniciando (viewers: {self.viewers[cam_id]})")
            proc = None
            try:
                proc = subprocess.Popen(self._cmd(cam), stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
                buf = b""
                while self.running[cam_id] and self.viewers[cam_id] > 0:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        i = buf.find(b"\xff\xd8")
                        f = buf.find(b"\xff\xd9")
                        if i != -1 and f != -1 and f > i:
                            self.last_frame[cam_id] = buf[i:f + 2]
                            buf = buf[f + 2:]
                        else:
                            break
            except Exception as e:
                print(f"[{cam_id}] Erro: {e}")
            finally:
                try:
                    if proc:
                        proc.kill()
                        proc.wait()
                except Exception:
                    pass

            if self.running[cam_id] and self.viewers[cam_id] > 0:
                time.sleep(3)
            else:
                break

        self.running[cam_id] = False
        self.last_frame[cam_id] = None
        print(f"[{cam_id}] FFmpeg parado")

    def add(self, cam_id):
        with self.locks[cam_id]:
            self.viewers[cam_id] += 1
            if not self.running[cam_id]:
                self.running[cam_id] = True
                threading.Thread(target=self._loop, args=(cam_id,), daemon=True).start()

    def remove(self, cam_id):
        with self.locks[cam_id]:
            self.viewers[cam_id] = max(0, self.viewers[cam_id] - 1)
            if self.viewers[cam_id] == 0:
                self.running[cam_id] = False

    def generate(self, cam_id):
        self.add(cam_id)
        try:
            ultimo = None
            while self.running[cam_id] or self.viewers[cam_id] > 0:
                frame = self.last_frame[cam_id]
                if frame and frame is not ultimo:
                    ultimo = frame
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                           b"Cache-Control: no-cache\r\n\r\n" + frame + b"\r\n")
                else:
                    time.sleep(0.05)
        except GeneratorExit:
            pass
        finally:
            self.remove(cam_id)


streams = StreamManager()

detector_instance = None
detector_lock = threading.Lock()

def get_detector():
    global detector_instance
    if not DETECTOR_OK:
        return None
    with detector_lock:
        if detector_instance is None:
            print("[detector] inicializando Detector...")
            detector_instance = Detector()
        return detector_instance


def gerar_audio(cam_id):
    cam = CAMERAS[cam_id]
    cmd = ["ffmpeg", "-fflags", "nobuffer"]
    t = cam.get("transporte", "tcp")
    if t in ("tcp", "udp"):
        cmd += ["-rtsp_transport", t]
    audio_url = rtsp_url(cam)
    if cam.get("audio_path"):
        audio_url = audio_url.replace(cam["path"], cam["audio_path"])
    cmd += [
        "-i", audio_url,
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", "32k",
        "-ac", "1",
        "-ar", "16000",
        "-flush_packets", "1",
        "-f", "mp3",
        "-"
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, bufsize=0)
    print(f"[{cam_id}] audio iniciado")
    try:
        while True:
            chunk = proc.stdout.read(2048)
            if not chunk:
                break
            yield chunk
    except GeneratorExit:
        pass
    finally:
        try:
            proc.kill()
            proc.wait()
        except Exception:
            pass
        print(f"[{cam_id}] audio parado")


HTML = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover, user-scalable=no">
<title>Cameras</title>
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#0b0d10">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Cameras">
<link rel="apple-touch-icon" href="/icon-192.svg">
<style>
:root {
  --bg: #0b0d10;
  --card: #14181d;
  --card-2: #1b2027;
  --line: #232a33;
  --txt: #e6e9ed;
  --dim: #8b95a1;
  --ok: #35d07f;
  --off: #f2545b;
}
* { margin:0; padding:0; box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
body {
  background: var(--bg); color: var(--txt);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  min-height: 100dvh;
  padding: env(safe-area-inset-top) 0 env(safe-area-inset-bottom);
}
header {
  display:flex; align-items:center; justify-content:space-between;
  padding: 16px 18px 12px;
}
header h1 { font-size:19px; font-weight:650; letter-spacing:-0.2px; }
header .sub { font-size:12px; color:var(--dim); margin-top:2px; }
.restart-btn {
  width:38px; height:38px; border-radius:11px; border:1px solid var(--line);
  background:var(--card); color:var(--txt); font-size:18px; cursor:pointer;
  display:flex; align-items:center; justify-content:center; line-height:1;
}
.restart-btn:active { background:var(--card-2); transform:scale(0.94); }
.restart-btn.loading { animation: girar 0.8s linear infinite; pointer-events:none; opacity:0.6; }
@keyframes girar { from { transform:rotate(0deg); } to { transform:rotate(360deg); } }

.wrap { display:flex; flex-direction:column; gap:18px; padding: 4px 14px 28px; }
@media (min-width:900px) {
  .wrap { display:grid; grid-template-columns:1fr 1fr; gap:20px; padding:4px 20px 32px; }
}

.card {
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 16px;
  overflow: hidden;
}
.card-top {
  display:flex; align-items:center; justify-content:space-between;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
}
.card-top .nome { font-size:14px; font-weight:600; letter-spacing:0.2px; }
.dot { width:8px; height:8px; border-radius:50%; background:var(--ok); box-shadow:0 0 8px var(--ok); }
.dot.off { background:var(--off); box-shadow:0 0 8px var(--off); }

.feed { position:relative; background:#000; line-height:0; }
.feed img { width:100%; display:block; min-height:190px; object-fit:contain; background:#000; }
.expand {
  position:absolute; bottom:10px; right:10px;
  width:34px; height:34px; border-radius:10px; border:none;
  background:rgba(10,12,15,0.75); backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
  color:#fff; font-size:15px; cursor:pointer; line-height:1;
}

.som {
  position:absolute; bottom:10px; right:52px;
  width:34px; height:34px; border-radius:10px; border:none;
  background:rgba(10,12,15,0.75); backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
  color:#fff; font-size:15px; cursor:pointer; line-height:1;
}
.som.on { background:rgba(53,208,127,0.85); }

.detect {
  position:absolute; bottom:10px; right:94px;
  width:34px; height:34px; border-radius:10px; border:none;
  background:rgba(10,12,15,0.75); backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
  color:#fff; font-size:15px; cursor:pointer; line-height:1;
}
.detect.on { background:rgba(255,100,0,0.85); }

.ptz { padding: 14px; display:flex; align-items:center; justify-content:center; gap:22px; }
.dpad { display:grid; grid-template-columns:repeat(3,42px); grid-template-rows:repeat(3,42px); gap:6px; }
.dpad button, .zbtn {
  background: var(--card-2); border:1px solid var(--line); color:var(--txt);
  border-radius:11px; font-size:16px; cursor:pointer; display:flex;
  align-items:center; justify-content:center; font-family:inherit; user-select:none;
}
.dpad button:active, .zbtn:active { background:#2b3440; transform:scale(0.94); }
.dpad .center { background:transparent; border:none; cursor:default; }
.zoom { display:flex; flex-direction:column; gap:6px; }
.zbtn { width:42px; height:42px; font-size:19px; font-weight:500; }

.tabs { display:flex; gap:6px; padding:0 18px 10px; }
.tabs a { color:var(--dim); text-decoration:none; font-size:13px; padding:7px 12px;
          border-radius:10px; border:1px solid var(--line); background:var(--card); }
.tabs a.on { color:var(--txt); background:var(--card-2); }
.painel-travado {
  position:fixed; inset:0; z-index:300; background:#000; display:none; flex-direction:column;
  padding: env(safe-area-inset-top) 12px env(safe-area-inset-bottom);
}
.painel-travado.on { display:flex; }
.tr-topo { display:flex; justify-content:space-between; align-items:center; padding:14px 6px; color:#ccc; }
.tr-titulo { font-size:14px; letter-spacing:.3px; }
.tr-relogio { font-size:26px; font-weight:650; font-variant-numeric:tabular-nums; color:#fff; }
.tr-grade { flex:1; min-height:0; display:grid; gap:8px; grid-template-columns:1fr; align-content:center; }
@media (min-width:900px) { .tr-grade { grid-template-columns:1fr 1fr; } }
.tr-cel { position:relative; background:#111; border-radius:12px; overflow:hidden; }
.tr-cel canvas { width:100%; display:block; }
.tr-cel .nome { position:absolute; left:8px; top:6px; font-size:12px; color:#fff;
                background:rgba(0,0,0,.55); padding:2px 7px; border-radius:6px; }
.tr-rodape { padding:12px 6px; display:flex; flex-direction:column; gap:10px; color:#bbb; font-size:13px; }
.tr-botoes { display:flex; gap:8px; }
.tr-botoes button { flex:1; padding:13px; border-radius:12px; border:1px solid var(--line);
                    background:var(--card-2); color:var(--txt); font-size:15px; cursor:pointer; }
.tr-botoes button.perigo { background:#3a1d1f; border-color:#5a2a2e; }
.toast {
  position:fixed; left:50%; bottom:24px; transform:translateX(-50%) translateY(80px);
  background:#232a33; border:1px solid var(--line); color:var(--txt);
  padding:10px 16px; border-radius:11px; font-size:13px;
  opacity:0; transition:all .22s ease; pointer-events:none; z-index:200; max-width:88vw;
}
.toast.show { opacity:1; transform:translateX(-50%) translateY(0); }

.fs { position:fixed; inset:0; z-index:150; background:#000; display:none; }
.fs.on { display:flex; align-items:center; justify-content:center; }
.fs img { width:100%; height:100%; object-fit:contain; }
.fs-close {
  position:fixed; top:calc(16px + env(safe-area-inset-top)); right:16px; z-index:151;
  width:38px; height:38px; border-radius:50%; border:none;
  background:rgba(10,12,15,0.8); backdrop-filter:blur(10px); color:#fff; font-size:17px; cursor:pointer;
}
.fs-ptz {
  position:fixed; bottom:calc(22px + env(safe-area-inset-bottom)); left:50%; transform:translateX(-50%);
  z-index:151; display:flex; gap:20px; align-items:center;
  background:rgba(10,12,15,0.7); backdrop-filter:blur(12px);
  padding:12px 18px; border-radius:18px; border:1px solid rgba(255,255,255,0.08);
}
</style>
</head>
<body>

<header>
  <div>
    <h1>Cameras</h1>
    <div class="sub" id="sub">{{ cameras|length }} dispositivos</div>
  </div>
  <button class="restart-btn" id="restartBtn" onclick="confirmarRestart()" title="Reiniciar servico">&#8635;</button>
</header>

<nav class="tabs">
  <a class="on" href="/">Ao vivo</a>
  <a href="/registros">Registros</a>
  <a href="/rostos">Rostos</a>
  <a href="/gravacoes">Gravações</a>
</nav>

<div class="wrap">
  {% for cid, cam in cameras.items() %}
  <section class="card" data-cam="{{ cid }}">
    <div class="card-top">
      <span class="nome">{{ cam.nome }}</span>
      <span class="dot"></span>
    </div>

    <div class="feed">
      <img src="/frame/{{ cid }}" alt="{{ cam.nome }}" data-cam-feed="{{ cid }}" data-normal-url="/frame/{{ cid }}" data-detect-url="/frame/{{ cid }}/detect">
      {% if cam.audio %}
      <button class="som" id="som-{{ cid }}" onclick="toggleSom('{{ cid }}')">&#128263;</button>
      {% endif %}
      <button class="detect" id="detect-{{ cid }}" onclick="toggleDetect('{{ cid }}')" title="YOLO + Face Detection">🔍</button>
      <button class="expand" onclick="abrirFs('{{ cid }}', {{ 'true' if cam.ptz else 'false' }})">&#9974;</button>
    </div>

    {% if cam.ptz %}
    <div class="ptz">
      <div class="dpad">
        <span></span>
        <button onclick="ptz('{{ cid }}','up')">&#9650;</button>
        <span></span>
        <button onclick="ptz('{{ cid }}','left')">&#9664;</button>
        <span class="center"></span>
        <button onclick="ptz('{{ cid }}','right')">&#9654;</button>
        <span></span>
        <button onclick="ptz('{{ cid }}','down')">&#9660;</button>
        <span></span>
      </div>
      <div class="zoom">
        <button class="zbtn" onclick="ptz('{{ cid }}','zoom_in')">+</button>
        <button class="zbtn" onclick="ptz('{{ cid }}','zoom_out')">&#8722;</button>
      </div>
    </div>
    {% endif %}
  </section>
  {% endfor %}
</div>

<div class="fs" id="fs"><img id="fsImg" alt=""></div>
<button class="fs-close" id="fsClose" onclick="fecharFs()" style="display:none">&#10005;</button>
<div class="fs-ptz" id="fsPtz" style="display:none">
  <div class="dpad">
    <span></span><button data-d="up">&#9650;</button><span></span>
    <button data-d="left">&#9664;</button><span class="center"></span><button data-d="right">&#9654;</button>
    <span></span><button data-d="down">&#9660;</button><span></span>
  </div>
  <div class="zoom">
    <button class="zbtn" data-d="zoom_in">+</button>
    <button class="zbtn" data-d="zoom_out">&#8722;</button>
  </div>
</div>

<div class="painel-travado" id="travado" aria-hidden="true">
  <div class="tr-topo">
    <span class="tr-titulo">Imagem travada</span>
    <span class="tr-relogio" id="tr-relogio"></span>
  </div>
  <div class="tr-grade" id="tr-grade"></div>
  <div class="tr-rodape">
    <div id="tr-estado"></div>
    <div class="tr-botoes">
      <button id="tr-priv" class="perigo" onclick="alternarPrivacidade()">Ativar privacidade</button>
      <button onclick="fecharTravado()">Fechar</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let camAtual = null;
const CAMERAS_NOMES = {{ nomes|tojson }};

function marcar(cid, ok) {
  const card = document.querySelector('[data-cam="' + cid + '"]');
  if (!card) return;
  card.querySelector('.dot').classList.toggle('off', !ok);
}

function aviso(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('show'), 2600);
}

async function ptz(cid, dir) {
  try {
    const r = await fetch('/ptz/' + cid, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({dir: dir})
    });
    const j = await r.json();
    if (!j.ok) aviso('PTZ: ' + (j.erro || 'falhou'));
  } catch (e) {
    aviso('PTZ: sem resposta');
  }
}

const sons = {};

function toggleSom(cid) {
  const btn = document.getElementById('som-' + cid);
  if (sons[cid]) {
    sons[cid].pause();
    sons[cid].src = '';
    delete sons[cid];
    btn.innerHTML = '&#128263;';
    btn.classList.remove('on');
    return;
  }
  const a = new Audio('/audio/' + cid + '?t=' + Date.now());
  a.onerror = () => { aviso('Audio indisponivel'); delete sons[cid]; btn.innerHTML = '&#128263;'; btn.classList.remove('on'); };
  a.play().then(() => {
    sons[cid] = a;
    btn.innerHTML = '&#128266;';
    btn.classList.add('on');
  }).catch(() => aviso('Toque de novo para liberar o audio'));
}

const detectStatus = {};

function toggleDetect(cid) {
  const btn = document.getElementById('detect-' + cid);
  const img = document.querySelector('[data-cam-feed="' + cid + '"]');
  if (!img) return;

  if (detectStatus[cid]) {
    img.dataset.useDetect = 'false';
    detectStatus[cid] = false;
    btn.classList.remove('on');
    aviso('Detecção desativada');
  } else {
    img.dataset.useDetect = 'true';
    detectStatus[cid] = true;
    btn.classList.add('on');
    aviso('Detecção ativada (YOLO + Face)');
  }
}

function abrirFs(cid, temPtz) {
  camAtual = cid;
  document.getElementById('fs').classList.add('on');
  document.getElementById('fsClose').style.display = 'block';
  document.getElementById('fsPtz').style.display = temPtz ? 'flex' : 'none';
}

function fecharFs() {
  document.getElementById('fsImg').src = '';
  document.getElementById('fs').classList.remove('on');
  document.getElementById('fsClose').style.display = 'none';
  document.getElementById('fsPtz').style.display = 'none';
  camAtual = null;
}

document.querySelectorAll('#fsPtz [data-d]').forEach(b => {
  b.onclick = () => { if (camAtual) ptz(camAtual, b.dataset.d); };
});

document.addEventListener('keydown', e => { if (e.key === 'Escape') fecharFs(); });

document.querySelectorAll('[data-cam-feed]').forEach(img => {
  const cid = img.dataset.camFeed;
  let active = true;
  img.dataset.useDetect = 'false';
  function poll() {
    if (window.congelado) { setTimeout(poll, 400); return; }
    if (!active) return;
    const next = new Image();
    next.onload = () => {
      img.src = next.src;
      const fsImg = document.getElementById('fsImg');
      if (camAtual === cid) fsImg.src = next.src;
      marcar(cid, true);
      setTimeout(poll, 150);
    };
    next.onerror = () => {
      marcar(cid, false);
      setTimeout(poll, 2000);
    };
    const useDetect = detectStatus[cid] ? 'detect' : '';
    const baseUrl = useDetect ? img.dataset.detectUrl : img.dataset.normalUrl;
    next.src = baseUrl + '?t=' + Date.now();
  }
  poll();
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { active = false; }
    else { active = true; poll(); }
  });
});

window.addEventListener('pagehide', () => {
  Object.values(sons).forEach(a => { a.pause(); a.src = ''; });
});

function confirmarRestart() {
  if (!confirm('Reiniciar o servico das cameras? As transmissoes ficam fora do ar por alguns segundos.')) return;
  reiniciar();
}

async function reiniciar() {
  const btn = document.getElementById('restartBtn');
  btn.classList.add('loading');
  try {
    const r = await fetch('/restart', { method: 'POST' });
    const j = await r.json();
    if (j.ok) {
      aviso('Reiniciando... a pagina vai recarregar sozinha');
      aguardarVoltar();
    } else {
      aviso('Falha ao reiniciar: ' + (j.erro || 'erro desconhecido'));
      btn.classList.remove('loading');
    }
  } catch (e) {
    aviso('Falha ao reiniciar: sem resposta');
    btn.classList.remove('loading');
  }
}

function aguardarVoltar() {
  let tentativas = 0;
  const iv = setInterval(async () => {
    tentativas++;
    try {
      const r = await fetch('/status', { cache: 'no-store' });
      if (r.ok) {
        clearInterval(iv);
        location.reload();
        return;
      }
    } catch (e) {}
    if (tentativas > 40) clearInterval(iv);
  }, 1500);
}

// --- painel travado: segurar o título "Cameras" por ~2 s (escondido de propósito)
let estadoPriv = {ativo: false, desde: null};
let relogioTimer = null;

function atualizarEstadoPriv() {
  const est = document.getElementById('tr-estado');
  const btn = document.getElementById('tr-priv');
  if (estadoPriv.ativo) {
    const h = new Date(estadoPriv.desde * 1000).toLocaleTimeString('pt-BR', {hour:'2-digit', minute:'2-digit'});
    est.textContent = 'Privacidade ATIVA desde ' + h + ': gravação e vídeo pausados.';
    btn.textContent = 'Desativar privacidade';
  } else {
    est.textContent = 'Gravação e vídeo ativos.';
    btn.textContent = 'Ativar privacidade';
  }
}

async function carregarEstadoPriv() {
  try {
    const r = await fetch('/privacidade', {cache: 'no-store'});
    if (r.ok) estadoPriv = await r.json();
  } catch (e) {}
  atualizarEstadoPriv();
}

function desenharRelogio() {
  document.getElementById('tr-relogio').textContent =
    new Date().toLocaleTimeString('pt-BR', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}

function abrirTravado() {
  window.congelado = true;
  const grade = document.getElementById('tr-grade');
  grade.innerHTML = '';
  document.querySelectorAll('[data-cam-feed]').forEach(img => {
    const cid = img.dataset.camFeed;
    const cel = document.createElement('div');
    cel.className = 'tr-cel';
    const nome = document.createElement('span');
    nome.className = 'nome';
    nome.textContent = CAMERAS_NOMES[cid] || cid;
    const cv = document.createElement('canvas');
    if (img.complete && img.naturalWidth > 0) {
      cv.width = img.naturalWidth; cv.height = img.naturalHeight;
      cv.getContext('2d').drawImage(img, 0, 0);
    }
    cel.appendChild(cv); cel.appendChild(nome);
    grade.appendChild(cel);
  });
  desenharRelogio();
  relogioTimer = setInterval(desenharRelogio, 500);
  document.getElementById('travado').classList.add('on');
  carregarEstadoPriv();
}

function fecharTravado() {
  document.getElementById('travado').classList.remove('on');
  clearInterval(relogioTimer);
  window.congelado = false;
}

async function alternarPrivacidade() {
  try {
    const r = await fetch('/privacidade', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ativo: !estadoPriv.ativo})
    });
    const j = await r.json();
    if (j.ok) { estadoPriv = j; atualizarEstadoPriv(); }
    else aviso('Falha: ' + (j.erro || 'erro'));
  } catch (e) {
    aviso('Sem resposta do servidor');
  }
}

(function () {
  const alvo = document.querySelector('header h1');
  let timer = null;
  const cancelar = () => { clearTimeout(timer); timer = null; };
  alvo.addEventListener('pointerdown', () => { timer = setTimeout(abrirTravado, 1800); });
  alvo.addEventListener('pointerup', cancelar);
  alvo.addEventListener('pointerleave', cancelar);
  alvo.addEventListener('pointercancel', cancelar);
  alvo.addEventListener('contextmenu', e => e.preventDefault());
  alvo.style.userSelect = 'none';
  alvo.style.webkitUserSelect = 'none';
  alvo.style.webkitTouchCallout = 'none';
})();
document.addEventListener('keydown', e => { if (e.key === 'Escape') fecharTravado(); });

if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
</script>
</body>
</html>
"""

MANIFEST = {
    "name": "Cameras", "short_name": "Cameras", "start_url": "/",
    "display": "standalone", "background_color": "#0b0d10",
    "theme_color": "#0b0d10", "orientation": "any",
    "icons": [
        {"src": "/icon-192.svg", "sizes": "192x192", "type": "image/svg+xml"},
        {"src": "/icon-512.svg", "sizes": "512x512", "type": "image/svg+xml"}
    ]
}

SW_JS = "self.addEventListener('fetch', e => e.respondWith(fetch(e.request)));"

ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">'
            '<rect width="192" height="192" rx="42" fill="#0b0d10"/>'
            '<circle cx="96" cy="86" r="34" fill="none" stroke="#35d07f" stroke-width="7"/>'
            '<circle cx="96" cy="86" r="11" fill="#35d07f"/>'
            '<rect x="58" y="134" width="76" height="8" rx="4" fill="#2b3440"/></svg>')


@app.route("/")
@auth.login_required
def index():
    return render_template_string(HTML, cameras=CAMERAS, nomes=_opcoes_cameras())


@app.route("/manifest.json")
def manifest():
    return jsonify(MANIFEST)


@app.route("/sw.js")
def sw():
    return Response(SW_JS, mimetype="application/javascript")


@app.route("/icon-192.svg")
@app.route("/icon-512.svg")
def icone():
    return Response(ICON_SVG, mimetype="image/svg+xml")


@app.route("/video/<cam_id>")
@auth.login_required
def video(cam_id):
    if cam_id not in CAMERAS:
        return "Camera nao encontrada", 404
    if gravacao.privacidade_ativa():
        return "Privacidade ativa", 403
    return Response(streams.generate(cam_id),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


_PLACEHOLDER = None


def _placeholder_jpeg():
    """Imagem neutra servida no lugar do vídeo enquanto a privacidade está ativa."""
    global _PLACEHOLDER
    if _PLACEHOLDER is None:
        img = np.zeros((360, 640, 3), dtype=np.uint8)
        cv2.putText(img, "Privacidade ativa", (150, 185), cv2.FONT_HERSHEY_SIMPLEX,
                    1.0, (120, 120, 120), 2)
        _, buf = cv2.imencode(".jpg", img)
        _PLACEHOLDER = buf.tobytes()
    return _PLACEHOLDER


@app.route("/frame/<cam_id>")
@auth.login_required
def frame(cam_id):
    if cam_id not in CAMERAS:
        return "Camera nao encontrada", 404
    if gravacao.privacidade_ativa():
        return Response(_placeholder_jpeg(), mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    streams.touch(cam_id)
    f = streams.last_frame[cam_id]
    if f is None:
        return "", 204
    return Response(f, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.route("/frame/<cam_id>/detect")
@auth.login_required
def frame_detect(cam_id):
    if cam_id not in CAMERAS:
        return "Camera nao encontrada", 404
    if gravacao.privacidade_ativa():
        return Response(_placeholder_jpeg(), mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    detector = get_detector()
    if not detector:
        return "Detector nao disponivel", 501

    streams.touch(cam_id)
    jpeg_data = streams.last_frame[cam_id]
    if jpeg_data is None:
        return "", 204

    try:
        frame_bgr = cv2.imdecode(np.frombuffer(jpeg_data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame_bgr is None:
            return "", 204

        annotated = detector.process(frame_bgr)

        _, buffer = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
        jpeg_out = buffer.tobytes()

        return Response(jpeg_out, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        print(f"[frame_detect] erro: {e}")
        return "", 500


@app.route("/audio/<cam_id>")
@auth.login_required
def audio(cam_id):
    if cam_id not in CAMERAS:
        return "Camera nao encontrada", 404
    if not CAMERAS[cam_id].get("audio"):
        return "Camera sem audio", 404
    if gravacao.privacidade_ativa():
        return "Privacidade ativa", 403
    return Response(gerar_audio(cam_id), mimetype="audio/mpeg")


@app.route("/ptz/<cam_id>", methods=["POST"])
@auth.login_required
def ptz_route(cam_id):
    if cam_id not in CAMERAS:
        return jsonify({"ok": False, "erro": "camera invalida"}), 404
    dados = request.get_json(silent=True) or {}
    direcao = dados.get("dir", "")
    dur = dados.get("dur", 0.4)
    ok, erro = mover(cam_id, direcao, dur)
    return jsonify({"ok": ok, "erro": erro})


@app.route("/ptz-debug/<cam_id>")
@auth.login_required
def ptz_debug(cam_id):
    if cam_id not in CAMERAS:
        return jsonify({"erro": "camera invalida"}), 404
    handle, erro = get_ptz(cam_id)
    if not handle:
        return jsonify({"onvif_lib": ONVIF_OK, "conectado": False, "erro": erro})
    return jsonify({"onvif_lib": ONVIF_OK, "conectado": True,
                    "porta": handle["porta"], "token": str(handle["token"])})


@app.route("/snapshot/<cam_id>")
@auth.login_required
def snapshot(cam_id):
    if cam_id not in CAMERAS:
        return "Camera nao encontrada", 404
    if gravacao.privacidade_ativa():
        return "Privacidade ativa", 403
    cam = CAMERAS[cam_id]
    cmd = ["ffmpeg"]
    t = cam.get("transporte", "tcp")
    if t in ("tcp", "udp"):
        cmd += ["-rtsp_transport", t]
    cmd += ["-i", rtsp_url(cam), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    try:
        img = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=12)
        return Response(img, mimetype="image/jpeg")
    except Exception:
        return "Erro ao capturar snapshot", 500


# ------------------------------------------------- GRAVAÇÃO E REGISTRO

def comando_gravacao(cam_id, pasta_run):
    """ffmpeg com duas saídas: segmentos .ts da gravação e 1 quadro/s para o detector."""
    cam = CAMERAS[cam_id]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-fflags", "+genpts+discardcorrupt+igndts"]
    t = cam.get("transporte", "tcp")
    if t in ("tcp", "udp"):
        cmd += ["-rtsp_transport", t]
    cmd += ["-i", rtsp_url(cam), "-map", "0:v:0", "-an"]
    if cam.get("gravar_h264"):
        cmd += ["-vf", "scale=640:-2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-g", "48", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-f", "segment", "-segment_time", str(gravacao.SEGMENTO_S),
            "-segment_format", "mpegts", "-strftime", "1",
            os.path.join(pasta_run, "%Y%m%d_%H%M%S.ts")]
    cmd += ["-map", "0:v:0", "-an", "-vf", "fps=1/2,scale=640:-2",
            "-c:v", "mjpeg", "-q:v", "6", "-f", "image2pipe", "pipe:1"]
    return cmd


def _opcoes_cameras():
    # só o nome: CAMERAS tem host e senha e não pode ir para o template
    return {cid: c["nome"] for cid, c in CAMERAS.items()}


@app.route("/registros")
@auth.login_required
def registros_page():
    return render_template_string(REGISTROS_HTML, opcoes=_opcoes_cameras())


@app.route("/gravacoes")
@auth.login_required
def gravacoes_page():
    cam = request.args.get("cam", "")
    if cam not in CAMERAS:
        cam = next(iter(CAMERAS))
    try:
        t = float(request.args["t"]) if request.args.get("t") else None
    except ValueError:
        t = None
    return render_template_string(GRAVACOES_HTML, opcoes=_opcoes_cameras(),
                                  cam_inicial=cam, t_inicial=t)


@app.route("/api/registros")
@auth.login_required
def api_registros():
    cam = request.args.get("cam") or None
    if cam and cam not in CAMERAS:
        return jsonify([])
    itens = registro.listar(cam)
    for it in itens:
        it["cam_nome"] = CAMERAS[it["cam"]]["nome"]
        it["miniatura_url"] = f"/registro/{it['id']}/imagem"
    return jsonify(itens)


@app.route("/registro/<int:eid>/imagem")
@auth.login_required
def registro_imagem(eid):
    caminho = registro.miniatura_caminho(eid)
    if not caminho:
        return "", 404
    return send_file(caminho, mimetype="image/jpeg")


@app.route("/rostos")
@auth.login_required
def rostos_page():
    return render_template_string(ROSTOS_HTML)


@app.route("/api/rostos")
@auth.login_required
def api_rostos():
    status = request.args.get("status") or None
    if status not in (None, "pendente", "rotulado"):
        status = None
    return jsonify(rostos.listar_grupos(status))


@app.route("/rosto/<int:rid>/imagem")
@auth.login_required
def rosto_imagem(rid):
    caminho = rostos.thumb_caminho(rid)
    if not caminho:
        return "", 404
    return send_file(caminho, mimetype="image/jpeg")


@app.route("/api/rosto/<int:rid>/rotular", methods=["POST"])
@auth.login_required
def api_rosto_rotular(rid):
    dados = request.get_json(silent=True) or {}
    rotulo = dados.get("rotulo")
    nome = dados.get("nome")
    if rotulo not in ("morador", "estranho", "conhecido"):
        return jsonify({"ok": False, "erro": "rótulo inválido"}), 400
    ok = rostos.rotular(rid, rotulo, nome)
    return jsonify({"ok": ok, "erro": None if ok else "rosto não encontrado"})


@app.route("/api/grupo/<int:grupo>/rotular", methods=["POST"])
@auth.login_required
def api_grupo_rotular(grupo):
    dados = request.get_json(silent=True) or {}
    rotulo = dados.get("rotulo")
    nome = dados.get("nome")
    if rotulo not in ("morador", "estranho", "conhecido"):
        return jsonify({"ok": False, "erro": "rótulo inválido"}), 400
    n = rostos.rotular_grupo(grupo, rotulo, nome)
    return jsonify({"ok": n > 0, "afetados": n,
                    "erro": None if n > 0 else "grupo não encontrado"})


@app.route("/gravacao/<cam_id>/playlist.m3u8")
@auth.login_required
def gravacao_playlist(cam_id):
    if cam_id not in CAMERAS:
        return "Camera nao encontrada", 404
    texto = gravacao.playlist(cam_id)
    if texto is None:
        return "Sem gravacao disponivel", 404
    return Response(texto, mimetype="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store"})


@app.route("/gravacao/<cam_id>/segmentos")
@auth.login_required
def gravacao_segmentos(cam_id):
    if cam_id not in CAMERAS:
        return jsonify({"erro": "camera invalida"}), 404
    segs = gravacao.segmentos(cam_id)
    return jsonify({"segmentos": [{"i": s["i"], "d": s["d"], "quebra": s["quebra"]}
                                  for s in segs]})


@app.route("/gravacao/<cam_id>/<run>/<nome>")
@auth.login_required
def gravacao_segmento(cam_id, run, nome):
    if (cam_id not in CAMERAS or not gravacao.RE_RUN.match(run)
            or not gravacao.RE_SEG.match(nome)):
        return "Nao encontrado", 404
    caminho = os.path.join(gravacao.pasta_cam(cam_id), run, nome)
    if not os.path.isfile(caminho):
        return "Nao encontrado", 404
    return send_file(caminho, mimetype="video/mp2t")


@app.route("/privacidade", methods=["GET", "POST"])
@auth.login_required
def privacidade():
    if request.method == "GET":
        return jsonify(gravacao.privacidade_estado())
    dados = request.get_json(silent=True) or {}
    if not isinstance(dados.get("ativo"), bool):
        return jsonify({"ok": False, "erro": "informe ativo: true ou false"}), 400
    estado = gravacao.definir_privacidade(dados["ativo"])
    if estado["ativo"]:
        # derruba também os streams ao vivo que estiverem abertos
        for c in CAMERAS:
            with streams.locks[c]:
                streams.running[c] = False
                streams.viewers[c] = 0
    return jsonify({"ok": True, **estado})


@app.route("/restart", methods=["POST"])
@auth.login_required
def restart():
    try:
        subprocess.Popen(
            ["bash", "-c", "sleep 1 && sudo -n systemctl restart --no-block painel-cameras.service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.route("/status")
@auth.login_required
def status():
    return jsonify({c: {"viewers": streams.viewers[c], "running": streams.running[c]}
                    for c in CAMERAS})


if __name__ == "__main__":
    gravacao.iniciar(list(CAMERAS), comando_gravacao, registro.enfileirar)
    rostos.iniciar_banco()
    registro.iniciar(get_detector)
    app.run(host="0.0.0.0", port=5055, debug=False, threaded=True)
