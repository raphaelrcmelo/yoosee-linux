"""Mede a latência de ponta a ponta do vídeo ao vivo de uma câmera da rede local.

Método: manda um passo de PTZ (ONVIF) e cronometra até o primeiro quadro com movimento chegar
(a) ao ChannelView REAL do app (o que o usuário vê) e (b) a um ffmpeg de baixa latência lendo o
mesmo stream (o piso físico: motor + codificação + rede, ~0,6 s). A diferença é o atraso que
o player acrescenta. Mesma métrica nos dois: diferença média de quadros 64x36 em cinza.

    .venv/bin/python tools/medir_latencia.py <final-do-devId> [main|sub] [tentativas]
    NOFF=1 ...   só o app (a câmera não aceita 2 leitores no stream "main": usa o piso fixo de 0,6 s)

Cuidados (aprendidos na prática, 2026-11-12):
- Feche o app antes: a câmera tem poucas sessões RTSP e o app já usa uma por canal.
- Cada execução move a câmera ~2 passos para cada lado (volta ao lugar no fim).
- Interromper com Ctrl+C/kill deixa sessões RTSP penduradas e já travou uma câmera por um tempo;
  deixe terminar (o encerramento limpo é feito no fim). Espere ~20 s entre execuções.
"""
import os
import subprocess
import sys
import threading
import time
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import app as yoosee_app  # noqa: E402
import ptz  # noqa: E402
import store  # noqa: E402
from settings import STREAMS  # noqa: E402
from PySide6.QtCore import QCoreApplication, QTimer, Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

suffix = sys.argv[1]
quality = sys.argv[2] if len(sys.argv) > 2 else "sub"
trials = int(sys.argv[3]) if len(sys.argv) > 3 else 6
NOFF = bool(os.environ.get("NOFF"))
FLOOR_S = 0.6  # piso medido (ffmpeg -fflags nobuffer) quando NOFF=1
W, H = 64, 36

cam = next(c for d, c in store.load_cameras().items() if d.endswith(suffix))
url = f"rtsp://{quote(cam.user, safe='')}:{quote(cam.password, safe='')}@{cam.ip}:554/{STREAMS[quality]}"
events = {"app": [], "ff": []}
first = {"app": None, "ff": None}


# ---- leitor ffmpeg (piso) ----
ffp = None
if not NOFF:
    ffp = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay",
         "-probesize", "32768", "-analyzeduration", "0", "-rtsp_transport", "udp", "-i", url, "-an",
         "-vf", f"scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def ff_reader():
        prev = None
        while True:
            buf = ffp.stdout.read(W * H)
            if len(buf) < W * H:
                return
            t = time.monotonic()
            first["ff"] = first["ff"] or t
            if prev is not None:
                events["ff"].append((t, sum(abs(a - b) for a, b in zip(prev, buf)) / (W * H * 255.0)))
            prev = buf
    threading.Thread(target=ff_reader, daemon=True).start()
else:
    first["ff"] = time.monotonic()

# ---- leitor do app: ChannelView real ----
qapp = QApplication(sys.argv)
ch = yoosee_app.ChannelView(0)
ch.resize(640, 360)
ch.show()
prev_app = [None]


def on_frame(frame):
    t = time.monotonic()
    first["app"] = first["app"] or t
    img = frame.toImage()
    if img.isNull():
        return
    g = img.scaled(W, H, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).convertToFormat(QImage.Format_Grayscale8)
    cur = bytes(g.constBits())
    if prev_app[0] is not None and len(prev_app[0]) == len(cur):
        events["app"].append((t, sum(abs(a - b) for a, b in zip(prev_app[0], cur)) / (len(cur) * 255.0)))
    prev_app[0] = cur


ch.video.videoSink().videoFrameChanged.connect(on_frame)
done = [False]


def poll_done():  # thread da GUI: encerra limpo (TEARDOWN do RTSP)
    if done[0]:
        ch.stop()
        if ffp:
            ffp.terminate()
        QCoreApplication.quit()


poll = QTimer()
poll.setInterval(200)
poll.timeout.connect(poll_done)
poll.start()


def onset(which, t0):
    quiet = [s for t, s in events[which] if t0 - 2.5 <= t < t0]
    thr = max(max(quiet, default=0) * 3.0, 0.006)
    hit = next((t for t, s in events[which] if t > t0 and s > thr), None)
    return None if hit is None else hit - t0


def run():
    t_start = time.monotonic()
    for _ in range(500):
        if first["app"] and first["ff"]:
            break
        time.sleep(0.1)
    if not (first["app"] and first["ff"]):
        print("sem vídeo em um dos leitores", flush=True)
        done[0] = True
        return
    print(f"1º quadro: app={first['app'] - t_start:.2f}s" + ("" if NOFF else f"  ffmpeg={first['ff'] - t_start:.2f}s"), flush=True)
    time.sleep(12)
    rows = []
    for i in range(trials):
        direction = "right" if i % 2 == 0 else "left"
        time.sleep(4.0)
        t0 = time.monotonic()
        ptz.continuous_move(cam.ip, cam.user, cam.password, direction, 1)
        time.sleep(5.0)
        a = onset("app", t0)
        f = FLOOR_S if NOFF else onset("ff", t0)
        rows.append((a, f))
        fmt = lambda v: "  --  " if v is None else f"{v:5.2f}s"  # noqa: E731
        print(f"  {i + 1} ({direction:5}): app={fmt(a)} piso={fmt(f)}", flush=True)
    med = lambda l: sorted(l)[len(l) // 2] if l else float("nan")  # noqa: E731
    apps = [a for a, _ in rows[2:] if a is not None]
    extra = [a - f for a, f in rows[2:] if a is not None and f is not None]
    print(f"RESUMO (tentativas 3..{trials}): app={med(apps):.2f}s  atraso extra sobre o piso={med(extra):+.2f}s", flush=True)
    done[0] = True


ch.device = {"remarkName": "medição", "devId": "x"}
ch.quality = quality
ch.body.setCurrentWidget(ch.video)
ch._open_lan(cam)
threading.Thread(target=run, daemon=True).start()
qapp.exec()
