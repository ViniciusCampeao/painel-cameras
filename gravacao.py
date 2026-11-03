"""Gravação contínua das câmeras em segmentos, com retenção de 24 h.

Cada câmera roda um ffmpeg que faz duas coisas ao mesmo tempo:
  - grava segmentos .ts de SEGMENTO_S segundos (cópia ou recodificação, ver painel_cameras);
  - envia 1 quadro por segundo (JPEG pequeno) pela saída padrão, usado pelo detector de pessoas.

Os segmentos ficam em GRAVACAO_DIR/<cam>/<AAAAMMDD_HHMMSS>/<AAAAMMDD_HHMMSS>.ts, onde a
primeira pasta marca o início de cada execução do ffmpeg. Quando o ffmpeg reinicia, a pasta
nova marca a quebra na linha do tempo.
"""

import json
import math
import os
import re
import threading
import time
import subprocess
from datetime import datetime

GRAVACAO_DIR = os.environ.get("GRAVACAO_DIR", os.path.join(os.path.expanduser("~"), "gravacoes"))
RETENCAO_S = float(os.environ.get("RETENCAO_HORAS", "24")) * 3600
SEGMENTO_S = int(os.environ.get("SEGMENTO_S", "300"))

FORMATO_TEMPO = "%Y%m%d_%H%M%S"
RE_SEG = re.compile(r"^(\d{8}_\d{6})\.ts$")
RE_RUN = re.compile(r"^\d{8}_\d{6}$")
RE_RTSP = re.compile(r"rtsp://\S+")


def pasta_cam(cam_id):
    return os.path.join(GRAVACAO_DIR, cam_id)


def _epoch(texto):
    # hora local, o mesmo fuso que o strftime do ffmpeg usa para nomear os arquivos
    return datetime.strptime(texto, FORMATO_TEMPO).timestamp()


def _log(cam_id, msg):
    print(f"[{cam_id}] {msg}", flush=True)


# ------------------------------------------------------------ PRIVACIDADE

_priv_lock = threading.Lock()
_priv = {"ativo": None, "desde": None}  # None = ainda não lido do disco
_gravadores = []


def _arquivo_privacidade():
    return os.path.join(GRAVACAO_DIR, "privacidade.json")


def privacidade_estado():
    """{'ativo': bool, 'desde': epoch ou None}. Lido do disco na primeira vez, para
    sobreviver a reinício do serviço."""
    with _priv_lock:
        if _priv["ativo"] is None:
            try:
                with open(_arquivo_privacidade()) as f:
                    dados = json.load(f)
                _priv["ativo"] = bool(dados.get("ativo"))
                _priv["desde"] = dados.get("desde")
            except (OSError, ValueError):
                _priv["ativo"] = False
                _priv["desde"] = None
        return {"ativo": _priv["ativo"], "desde": _priv["desde"]}


def privacidade_ativa():
    return privacidade_estado()["ativo"]


def definir_privacidade(ativo):
    """Liga/desliga o modo privacidade. Ligado: para a gravação e o detector na hora."""
    desde = time.time() if ativo else None
    os.makedirs(GRAVACAO_DIR, exist_ok=True)
    tmp = _arquivo_privacidade() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"ativo": bool(ativo), "desde": desde}, f)
    os.replace(tmp, _arquivo_privacidade())
    with _priv_lock:
        _priv["ativo"] = bool(ativo)
        _priv["desde"] = desde
    if ativo:
        for g in _gravadores:
            g.pausar()
    print(f"[privacidade] {'ATIVADA: gravação e detecção paradas' if ativo else 'desativada'}",
          flush=True)
    return privacidade_estado()


class Gravador(threading.Thread):
    """Mantém um ffmpeg de gravação rodando para uma câmera, reiniciando se ele cair."""

    def __init__(self, cam_id, build_cmd, on_frame):
        super().__init__(daemon=True, name=f"gravador-{cam_id}")
        self.cam_id = cam_id
        self.build_cmd = build_cmd
        self.on_frame = on_frame
        self.proc = None

    def pausar(self):
        """Chamado quando a privacidade liga: mata o ffmpeg. O loop não reinicia enquanto
        a privacidade estiver ativa."""
        self._matar()

    def run(self):
        while True:
            while privacidade_ativa():
                time.sleep(2)
            run_dir = os.path.join(pasta_cam(self.cam_id), time.strftime(FORMATO_TEMPO))
            os.makedirs(run_dir, exist_ok=True)
            cmd = self.build_cmd(self.cam_id, run_dir)
            _log(self.cam_id, f"gravação iniciando ({os.path.basename(run_dir)})")
            try:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, bufsize=0)
                threading.Thread(target=self._ler_erros, args=(self.proc,), daemon=True).start()
                self._ler_quadros(self.proc)
            except Exception as e:
                _log(self.cam_id, f"gravação erro: {e}")
            finally:
                self._matar()
            _log(self.cam_id, "gravação parou; reiniciando em 5 s")
            time.sleep(5)

    def _ler_quadros(self, proc):
        buf = b""
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk or privacidade_ativa():
                break
            buf += chunk
            while True:
                i = buf.find(b"\xff\xd8")
                f = buf.find(b"\xff\xd9", i + 2) if i != -1 else -1
                if i == -1 or f == -1:
                    buf = buf[i:] if i > 0 else buf
                    if len(buf) > 2_000_000:
                        buf = b""
                    break
                quadro = buf[i:f + 2]
                buf = buf[f + 2:]
                try:
                    self.on_frame(self.cam_id, quadro, time.time())
                except Exception as e:
                    _log(self.cam_id, f"quadro descartado: {e}")

    def _ler_erros(self, proc):
        for linha in proc.stderr:
            texto = RE_RTSP.sub("rtsp://<oculto>", linha.decode(errors="replace").strip())
            if texto:
                _log(self.cam_id, f"ffmpeg: {texto}")

    def _matar(self):
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass


def _limpar(cam_ids):
    """Apaga segmentos mais antigos que a retenção e pastas de execução vazias."""
    limite = time.time() - RETENCAO_S
    for cam_id in cam_ids:
        base = pasta_cam(cam_id)
        if not os.path.isdir(base):
            continue
        for run in os.listdir(base):
            rd = os.path.join(base, run)
            if not RE_RUN.match(run) or not os.path.isdir(rd):
                continue
            for nome in os.listdir(rd):
                if not RE_SEG.match(nome):
                    continue
                try:
                    if _epoch(nome[:15]) < limite:
                        os.remove(os.path.join(rd, nome))
                except (ValueError, OSError):
                    pass
            try:
                # pasta vazia e com mais de 1 h: não apaga a execução recém-criada
                if not os.listdir(rd) and _epoch(run) < time.time() - 3600:
                    os.rmdir(rd)
            except (ValueError, OSError):
                pass


def intervalos_video(cam_id):
    """(inicio, fim) de cada segmento .ts existente da câmera, para saber se ainda há
    gravação cobrindo um instante (inclui o segmento que está sendo escrito agora)."""
    base = pasta_cam(cam_id)
    ivs = []
    if os.path.isdir(base):
        for run in os.listdir(base):
            rd = os.path.join(base, run)
            if not RE_RUN.match(run) or not os.path.isdir(rd):
                continue
            for nome in os.listdir(rd):
                if RE_SEG.match(nome):
                    try:
                        ini = _epoch(nome[:15])
                        ivs.append((ini, ini + SEGMENTO_S))
                    except ValueError:
                        pass
    ivs.sort()
    return ivs


def _loop_limpeza(cam_ids):
    while True:
        try:
            _limpar(cam_ids)
        except Exception as e:
            print(f"[gravacao] limpeza erro: {e}", flush=True)
        time.sleep(600)


def iniciar(cam_ids, build_cmd, on_frame):
    os.makedirs(GRAVACAO_DIR, exist_ok=True)
    for cam_id in cam_ids:
        os.makedirs(pasta_cam(cam_id), exist_ok=True)
        g = Gravador(cam_id, build_cmd, on_frame)
        _gravadores.append(g)
        g.start()
    threading.Thread(target=_loop_limpeza, args=(list(cam_ids),), daemon=True,
                     name="gravacao-limpeza").start()
    print(f"[gravacao] {len(cam_ids)} câmera(s), retenção {RETENCAO_S / 3600:.0f} h, "
          f"segmento {SEGMENTO_S} s, pasta {GRAVACAO_DIR}", flush=True)


def segmentos(cam_id, agora=None):
    """Segmentos prontos dentro da retenção, em ordem.

    Cada item tem: i (início, epoch), d (duração em s), run, nome e quebra (True quando
    começa uma execução nova do ffmpeg, ou seja, há descontinuidade no arquivo).
    O segmento mais recente ainda está sendo escrito, então fica fora da lista.
    """
    agora = agora or time.time()
    limite = agora - RETENCAO_S
    base = pasta_cam(cam_id)
    itens = []
    if os.path.isdir(base):
        for run in os.listdir(base):
            rd = os.path.join(base, run)
            if not RE_RUN.match(run) or not os.path.isdir(rd):
                continue
            for nome in os.listdir(rd):
                if RE_SEG.match(nome):
                    try:
                        itens.append((_epoch(nome[:15]), run, nome))
                    except ValueError:
                        pass
    itens.sort()
    completos = itens[:-1]  # o último está em gravação

    saida = []
    for idx, (ini, run, nome) in enumerate(completos):
        if ini < limite:
            continue
        proximo = completos[idx + 1][0] if idx + 1 < len(completos) else itens[-1][0]
        duracao = min(proximo - ini, SEGMENTO_S + 5)
        quebra = idx > 0 and completos[idx - 1][1] != run
        saida.append({"i": ini, "d": round(duracao, 3), "run": run, "nome": nome,
                      "quebra": quebra})
    return saida


def playlist(cam_id):
    """Playlist HLS VOD cobrindo toda a retenção. None se não há gravação."""
    segs = segmentos(cam_id)
    if not segs:
        return None
    alvo = math.ceil(max(s["d"] for s in segs))
    linhas = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-PLAYLIST-TYPE:VOD",
              f"#EXT-X-TARGETDURATION:{alvo}", "#EXT-X-MEDIA-SEQUENCE:0"]
    for s in segs:
        if s["quebra"]:
            linhas.append("#EXT-X-DISCONTINUITY")
        linhas.append(f"#EXTINF:{s['d']:.3f},")
        linhas.append(f"{s['run']}/{s['nome']}")
    linhas.append("#EXT-X-ENDLIST")
    return "\n".join(linhas) + "\n"
