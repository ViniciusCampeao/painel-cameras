"""Captura e reconhecimento de rostos.

Quando o detector vê pessoas, procura um rosto dentro do recorte de cada pessoa
(dlib HOG), salva uma miniatura do rosto e o encoding de 128 dimensões, e deixa
o rosto PENDENTE para ser rotulado no painel como morador, estranho ou com um
nome. Rostos que recebem um nome viram referência: os próximos rostos são
comparados com eles e marcados com o nome reconhecido automaticamente.

Usa dlib diretamente (não o pacote face_recognition, que depende de pkg_resources
e quebra no Python 3.12). Os modelos .dat ficam em ~/.face_recognition_lib.
Se dlib ou os modelos não estiverem disponíveis, o módulo se desliga sozinho e o
resto do sistema segue funcionando.
"""

import os
import sqlite3
import threading
from contextlib import closing

import cv2
import numpy as np

from gravacao import GRAVACAO_DIR

LIB = os.environ.get("FACE_LIB_DIR",
                     os.path.join(os.path.expanduser("~"), ".face_recognition_lib"))
DB = os.path.join(GRAVACAO_DIR, "registros.db")
# distância máxima (encoding) para considerar que é a mesma pessoa conhecida
TOLERANCIA = float(os.environ.get("FACE_TOLERANCIA", "0.6"))
# distância abaixo da qual dois rostos do MESMO evento são o mesmo (dedup)
DEDUP = float(os.environ.get("FACE_DEDUP", "0.5"))
# no máximo quantos rostos distintos salvar por evento
MAX_POR_EVENTO = int(os.environ.get("FACE_MAX_POR_EVENTO", "6"))
# lado mínimo do recorte da pessoa antes de procurar rosto (amplia se menor)
ALVO_LADO = 160

_db_lock = threading.Lock()
_models = None
_models_lock = threading.Lock()
FACE_OK = None  # None = ainda não tentou; True/False depois


def _conexao():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _achar_modelo(nome):
    """Procura um .dat em ~/.face_recognition_lib e, se não achar, dentro do
    pacote face_recognition_models (que já traz os modelos embutidos)."""
    candidatos = [os.path.join(LIB, nome)]
    try:
        import face_recognition_models as frm  # noqa: só para localizar a pasta
        candidatos.append(os.path.join(os.path.dirname(frm.__file__), "models", nome))
    except Exception:
        import importlib.util
        spec = importlib.util.find_spec("face_recognition_models")
        if spec and spec.submodule_search_locations:
            candidatos.append(os.path.join(
                list(spec.submodule_search_locations)[0], "models", nome))
    for p in candidatos:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(f"modelo {nome} não encontrado em {candidatos}")


def disponivel():
    """Carrega os modelos dlib uma vez. Retorna a tupla (det, sp, enc) ou None."""
    global _models, FACE_OK
    with _models_lock:
        if _models is not None:
            return _models
        if FACE_OK is False:
            return None
        try:
            import dlib
            det = dlib.get_frontal_face_detector()
            sp = dlib.shape_predictor(
                _achar_modelo("shape_predictor_68_face_landmarks.dat"))
            enc = dlib.face_recognition_model_v1(
                _achar_modelo("dlib_face_recognition_resnet_model_v1.dat"))
            _models = (det, sp, enc)
            FACE_OK = True
            print("[rostos] modelos dlib carregados", flush=True)
        except Exception as e:
            FACE_OK = False
            print(f"[rostos] reconhecimento de rostos indisponível: {e}", flush=True)
        return _models


def iniciar_banco():
    os.makedirs(GRAVACAO_DIR, exist_ok=True)
    with _db_lock, closing(_conexao()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS rostos (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       cam TEXT NOT NULL,
                       ts REAL NOT NULL,
                       evento_id INTEGER,
                       thumb TEXT NOT NULL,
                       encoding BLOB,
                       rotulo TEXT,        -- 'morador' | 'estranho' | 'conhecido' | NULL
                       nome TEXT,          -- nome livre (quando informado)
                       auto_nome TEXT,     -- nome reconhecido automaticamente
                       auto_dist REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_rostos_ts ON rostos(ts)")
        c.commit()


# --------------------------------------------------------------- encodings

def _enc_para_bytes(e):
    return np.asarray(e, dtype=np.float32).tobytes()


def _bytes_para_enc(b):
    if not b:
        return None
    v = np.frombuffer(b, dtype=np.float32)
    return v if v.shape[0] == 128 else None


def _conhecidos():
    """(nomes, matriz NxD) dos rostos que já têm nome, para reconhecimento."""
    with _db_lock, closing(_conexao()) as c:
        rows = c.execute("SELECT nome, encoding FROM rostos "
                         "WHERE nome IS NOT NULL AND nome <> '' "
                         "AND encoding IS NOT NULL").fetchall()
    nomes, encs = [], []
    for r in rows:
        v = _bytes_para_enc(r["encoding"])
        if v is not None:
            nomes.append(r["nome"])
            encs.append(v)
    return nomes, (np.array(encs) if encs else None)


def _reconhecer(e, conhecidos):
    nomes, M = conhecidos
    if M is None or not nomes:
        return None, None
    d = np.linalg.norm(M - e, axis=1)
    i = int(np.argmin(d))
    if d[i] <= TOLERANCIA:
        return nomes[i], round(float(d[i]), 3)
    return None, round(float(d[i]), 3)


# --------------------------------------------------------------- detecção

def _rosto_na_pessoa(frame_bgr, box, models):
    """Procura o maior rosto dentro do recorte da pessoa. Retorna (encoding,
    recorte_bgr_do_rosto) ou None."""
    det, sp, enc = models
    x1, y1, x2, y2 = box
    h, w = frame_bgr.shape[:2]
    mx, my = int((x2 - x1) * 0.1), int((y2 - y1) * 0.1)
    cx1, cy1 = max(0, x1 - mx), max(0, y1 - my)
    cx2, cy2 = min(w, x2 + mx), min(h, y2 + my)
    crop = frame_bgr[cy1:cy2, cx1:cx2]
    if crop.size == 0:
        return None
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    ch, cw = rgb.shape[:2]
    escala = max(1.0, ALVO_LADO / max(1, min(ch, cw)))
    if escala > 1.0:
        rgb = cv2.resize(rgb, (int(cw * escala), int(ch * escala)))
    faces = det(rgb, 1)
    if not faces:
        return None
    f = max(faces, key=lambda d: (d.right() - d.left()) * (d.bottom() - d.top()))
    shape = sp(rgb, f)
    e = np.array(enc.compute_face_descriptor(rgb, shape), dtype=np.float32)
    fx1, fy1 = max(0, f.left()), max(0, f.top())
    fx2, fy2 = min(rgb.shape[1], f.right()), min(rgb.shape[0], f.bottom())
    rosto = rgb[fy1:fy2, fx1:fx2]
    if rosto.size == 0:
        return None
    return e, cv2.cvtColor(rosto, cv2.COLOR_RGB2BGR)


def _salvar_thumb(cam, rosto_bgr, ts, idx):
    rel = os.path.join("rostos", cam, f"{int(ts * 1000)}_{idx}.jpg")
    caminho = os.path.join(GRAVACAO_DIR, rel)
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    cv2.imwrite(caminho, cv2.resize(rosto_bgr, (160, 160)),
                [cv2.IMWRITE_JPEG_QUALITY, 85])
    return rel


def capturar(cam, frame_bgr, boxes, ts, evento_id, vistos):
    """Procura rostos nas pessoas do frame e salva os novos como pendentes.
    `vistos` é a lista de encodings já capturados neste evento (dedup); é
    modificada no lugar. Retorna quantos rostos novos foram salvos."""
    models = disponivel()
    if not models or not boxes or len(vistos) >= MAX_POR_EVENTO:
        return 0
    conhecidos = _conhecidos()
    salvos = 0
    for box in boxes:
        if len(vistos) >= MAX_POR_EVENTO:
            break
        res = _rosto_na_pessoa(frame_bgr, box, models)
        if not res:
            continue
        e, rosto_bgr = res
        if any(np.linalg.norm(v - e) < DEDUP for v in vistos):
            continue
        vistos.append(e)
        auto_nome, auto_dist = _reconhecer(e, conhecidos)
        rel = _salvar_thumb(cam, rosto_bgr, ts, len(vistos))
        with _db_lock, closing(_conexao()) as c:
            c.execute("INSERT INTO rostos (cam, ts, evento_id, thumb, encoding, "
                      "auto_nome, auto_dist) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      (cam, ts, evento_id, rel, _enc_para_bytes(e), auto_nome, auto_dist))
            c.commit()
        salvos += 1
    return salvos


# --------------------------------------------------------------- API painel

def listar(status=None, limite=300):
    sql = ("SELECT id, cam, ts, evento_id, rotulo, nome, auto_nome, auto_dist "
           "FROM rostos")
    cond, args = [], []
    if status == "pendente":
        cond.append("rotulo IS NULL")
    elif status == "rotulado":
        cond.append("rotulo IS NOT NULL")
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(int(limite))
    with _db_lock, closing(_conexao()) as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def rotular(rid, rotulo, nome=None):
    """Define rótulo (morador/estranho/conhecido) e/ou nome de um rosto.
    Qualquer rosto com nome vira referência para o reconhecimento automático."""
    nome = (nome or "").strip() or None
    if rotulo not in ("morador", "estranho", "conhecido"):
        return False
    with _db_lock, closing(_conexao()) as c:
        cur = c.execute("UPDATE rostos SET rotulo = ?, nome = ? WHERE id = ?",
                        (rotulo, nome, rid))
        c.commit()
    return cur.rowcount > 0


def thumb_caminho(rid):
    with _db_lock, closing(_conexao()) as c:
        row = c.execute("SELECT thumb FROM rostos WHERE id = ?", (rid,)).fetchone()
    if not row or not row["thumb"]:
        return None
    base = os.path.realpath(GRAVACAO_DIR)
    caminho = os.path.realpath(os.path.join(GRAVACAO_DIR, row["thumb"]))
    if not caminho.startswith(base + os.sep) or not os.path.isfile(caminho):
        return None
    return caminho


def remover_pendentes_orfaos(eventos_vivos):
    """Apaga rostos PENDENTES (sem rótulo) cujo evento já não existe mais.
    Rostos já rotulados são preservados — são a base do reconhecimento."""
    with _db_lock, closing(_conexao()) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, thumb, evento_id FROM rostos WHERE rotulo IS NULL").fetchall()]
    orfaos = [r for r in rows if r["evento_id"] not in eventos_vivos]
    if not orfaos:
        return 0
    base = os.path.realpath(GRAVACAO_DIR)
    with _db_lock, closing(_conexao()) as c:
        for r in orfaos:
            if r["thumb"]:
                p = os.path.realpath(os.path.join(GRAVACAO_DIR, r["thumb"]))
                if p.startswith(base + os.sep) and os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            c.execute("DELETE FROM rostos WHERE id = ?", (r["id"],))
        c.commit()
    return len(orfaos)
