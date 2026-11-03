"""Registro de pessoas detectadas: eventos num SQLite e miniaturas em disco.

Um evento começa na primeira detecção de pessoa e continua enquanto houver detecções a
cada REGISTRO_GAP_S segundos ou menos. Quando a pessoa some por mais que isso, o próximo
avistamento vira um evento novo.
"""

import os
import queue
import sqlite3
import threading
import time
from contextlib import closing

import cv2
import numpy as np

from gravacao import GRAVACAO_DIR, intervalos_video

GAP_S = float(os.environ.get("REGISTRO_GAP_S", "30"))
CONFIANCA = float(os.environ.get("REGISTRO_CONFIANCA", "0.45"))
# De quanto em quanto tempo varrer eventos sem gravação correspondente.
LIMPEZA_ORFAOS_S = float(os.environ.get("LIMPEZA_ORFAOS_S", "600"))
DB = os.path.join(GRAVACAO_DIR, "registros.db")

_db_lock = threading.Lock()
_fila = queue.Queue(maxsize=16)
_ativo = {}  # cam_id -> {"id", "ultimo", "n", "conf"}
_sem_detector_avisado = False


def _conexao():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def iniciar_banco():
    os.makedirs(GRAVACAO_DIR, exist_ok=True)
    with _db_lock, closing(_conexao()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS eventos (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       cam TEXT NOT NULL,
                       inicio REAL NOT NULL,
                       fim REAL NOT NULL,
                       deteccoes INTEGER NOT NULL DEFAULT 1,
                       confianca REAL,
                       miniatura TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_eventos_inicio ON eventos(inicio)")
        c.commit()


def _salvar_miniatura(cam_id, frame, boxes, ts):
    rel = os.path.join("registros", cam_id, f"{int(ts * 1000)}.jpg")
    caminho = os.path.join(GRAVACAO_DIR, rel)
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    desenho = frame.copy()
    for conf, (x1, y1, x2, y2) in boxes:
        cv2.rectangle(desenho, (x1, y1), (x2, y2), (0, 120, 255), 2)
        cv2.putText(desenho, f"pessoa {conf:.0%}", (x1, max(14, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 120, 255), 2)
    cv2.imwrite(caminho, desenho, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return rel


def _registrar(cam_id, frame, boxes, ts):
    ativo = _ativo.get(cam_id)
    if not boxes:
        if ativo and ts - ativo["ultimo"] > GAP_S:
            _ativo.pop(cam_id, None)
        return

    conf = max(c for c, _ in boxes)
    if ativo is None or ts - ativo["ultimo"] > GAP_S:
        miniatura = _salvar_miniatura(cam_id, frame, boxes, ts)
        with _db_lock, closing(_conexao()) as c:
            cur = c.execute("INSERT INTO eventos (cam, inicio, fim, deteccoes, confianca, miniatura)"
                            " VALUES (?, ?, ?, 1, ?, ?)", (cam_id, ts, ts, conf, miniatura))
            c.commit()
            eid = cur.lastrowid
        _ativo[cam_id] = {"id": eid, "ultimo": ts, "n": 1, "conf": conf}
        print(f"[registro] {cam_id}: pessoa detectada (evento {eid})", flush=True)
        return

    ativo["ultimo"] = ts
    ativo["n"] += 1
    ativo["conf"] = max(ativo["conf"], conf)
    with _db_lock, closing(_conexao()) as c:
        c.execute("UPDATE eventos SET fim = ?, deteccoes = ?, confianca = ? WHERE id = ?",
                  (ts, ativo["n"], ativo["conf"], ativo["id"]))
        c.commit()


def enfileirar(cam_id, jpg, ts):
    """Chamado pelo gravador a cada quadro de 1 fps. Descarta o mais antigo se a fila encher."""
    item = (cam_id, jpg, ts)
    try:
        _fila.put_nowait(item)
    except queue.Full:
        try:
            _fila.get_nowait()
        except queue.Empty:
            pass
        try:
            _fila.put_nowait(item)
        except queue.Full:
            pass


def _worker(get_detector):
    global _sem_detector_avisado
    while True:
        cam_id, jpg, ts = _fila.get()
        try:
            detector = get_detector()
            if detector is None:
                if not _sem_detector_avisado:
                    print("[registro] detector indisponível; registro de pessoas desligado", flush=True)
                    _sem_detector_avisado = True
                continue
            frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            boxes = detector.pessoas(frame, conf=CONFIANCA)
            _registrar(cam_id, frame, boxes, ts)
        except Exception as e:
            print(f"[registro] erro ao processar {cam_id}: {e}", flush=True)


def _remover_miniatura(mini):
    """Apaga o arquivo da miniatura, conferindo que fica dentro de GRAVACAO_DIR."""
    if not mini:
        return
    base = os.path.realpath(GRAVACAO_DIR)
    caminho = os.path.realpath(os.path.join(GRAVACAO_DIR, mini))
    if caminho.startswith(base + os.sep) and os.path.isfile(caminho):
        try:
            os.remove(caminho)
        except OSError:
            pass


def limpar_orfaos():
    """Apaga eventos (e suas miniaturas) que não têm mais nenhuma gravação cobrindo o
    instante do avistamento — tipicamente porque o vídeo já saiu pela retenção.
    Retorna quantos eventos foram removidos."""
    with _db_lock, closing(_conexao()) as c:
        linhas = [dict(r) for r in c.execute(
            "SELECT id, cam, inicio, miniatura FROM eventos").fetchall()]

    cobertura = {}
    orfaos = []
    for r in linhas:
        cam = r["cam"]
        if cam not in cobertura:
            cobertura[cam] = intervalos_video(cam)
        ts = r["inicio"]
        if not any(a <= ts < b for a, b in cobertura[cam]):
            orfaos.append(r)

    if not orfaos:
        return 0

    with _db_lock, closing(_conexao()) as c:
        for r in orfaos:
            _remover_miniatura(r["miniatura"])
            c.execute("DELETE FROM eventos WHERE id = ?", (r["id"],))
        c.commit()
    print(f"[registro] limpeza: {len(orfaos)} evento(s) órfão(s) removido(s)", flush=True)
    return len(orfaos)


def _loop_orfaos():
    while True:
        time.sleep(LIMPEZA_ORFAOS_S)
        try:
            limpar_orfaos()
        except Exception as e:
            print(f"[registro] limpeza órfãos erro: {e}", flush=True)


def iniciar(get_detector):
    iniciar_banco()
    threading.Thread(target=_worker, args=(get_detector,), daemon=True,
                     name="registro-detector").start()
    threading.Thread(target=_loop_orfaos, daemon=True, name="registro-orfaos").start()
    print(f"[registro] ativo (lacuna {GAP_S:.0f} s, confiança {CONFIANCA:.2f})", flush=True)


def listar(cam_id=None, limite=200):
    sql = "SELECT id, cam, inicio, fim, deteccoes, confianca FROM eventos"
    args = []
    if cam_id:
        sql += " WHERE cam = ?"
        args.append(cam_id)
    sql += " ORDER BY inicio DESC LIMIT ?"
    args.append(int(limite))
    with _db_lock, closing(_conexao()) as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def miniatura_caminho(eid):
    """Caminho absoluto da miniatura do evento, ou None (confere se fica dentro da pasta)."""
    with _db_lock, closing(_conexao()) as c:
        row = c.execute("SELECT miniatura FROM eventos WHERE id = ?", (eid,)).fetchone()
    if not row or not row["miniatura"]:
        return None
    base = os.path.realpath(GRAVACAO_DIR)
    caminho = os.path.realpath(os.path.join(GRAVACAO_DIR, row["miniatura"]))
    if not caminho.startswith(base + os.sep) or not os.path.isfile(caminho):
        return None
    return caminho
