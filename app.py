#!/usr/bin/env python3
"""Câmeras Yoosee — cliente Linux (PySide6), layout inspirado no app oficial.

Vídeo: RTSP direto da câmera via QMediaPlayer (backend FFmpeg do Qt).
PTZ: ONVIF (ptz.py). Contas/lista de dispositivos: nuvem Yoosee (yoosee_api.py).
"""
import hashlib
import os
import sys
import threading
import time
from urllib.parse import quote

# o backend FFmpeg do Qt despeja a URL RTSP (com senha) no stderr ao abrir o stream
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.ffmpeg*=false")

import urllib3
from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QBrush, QColor, QConicalGradient, QIcon, QPainter, QPen
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QSizePolicy, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QStackedWidget, QStyle, QToolButton, QVBoxLayout, QWidget,
)

import lan
import p2p
import ptz
import store
from ptz import PtzController
from settings import DEFAULT_RTSP_USER, SCREENSHOT_DIR, STREAMS
from yoosee_api import YooseeClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Paleta quente (mesma identidade do ícone do app: laranja -> rosa/vermelho),
# em duas variações: escura e clara. Os tiles de vídeo continuam escuros nas
# duas — vídeo de câmera fica ilegível emoldurado em branco.
THEMES = {
    "dark": dict(
        ACCENT="#ff8a3d", ACCENT_2="#e8455f", ON_ACCENT="#241310",
        BG="#201d1c", BG_PANEL="#2a2522", BG_RAISED="#352e2a", BG_RAISED_HOVER="#443a35",
        BORDER="#453c37", TEXT="#efe6e0", TEXT_MUTED="#a89a92", SEL_END="#ff6b55",
        TILE="#0e0b0a", TILE_BORDER="#2b2522", TILE_HEADER="#262120",
        PLACE_HI="#191412", PLACE_TEXT="#6d5f58",
        CHIP_BG="rgba(255,138,61,0.14)", CHIP_TEXT="#ffb37d", ACCENT_SOFT="rgba(255,138,61,0.16)",
        TOAST_BG="rgba(255,138,61,0.10)", TOAST_BORDER="rgba(255,138,61,0.30)", TOAST_TEXT="#ffc9a1",
        CAPTION="#7a6c64", DPAD_CENTER="#5c524c",
        DISABLED_TEXT="#4a413c", DISABLED_BORDER="#2e2724",
    ),
    "light": dict(
        ACCENT="#e8601c", ACCENT_2="#d63a55", ON_ACCENT="#fff6ef",
        BG="#f3eae2", BG_PANEL="#faf4ee", BG_RAISED="#eadfd4", BG_RAISED_HOVER="#dfcfc0",
        BORDER="#d9c6b6", TEXT="#3a322c", TEXT_MUTED="#8d7c71", SEL_END="#e04a34",
        TILE="#17120f", TILE_BORDER="#d4c1b1", TILE_HEADER="#2b2420",
        PLACE_HI="#211b17", PLACE_TEXT="#8d7d73",
        CHIP_BG="rgba(232,96,28,0.18)", CHIP_TEXT="#ffb98f", ACCENT_SOFT="rgba(232,96,28,0.14)",
        TOAST_BG="rgba(232,96,28,0.09)", TOAST_BORDER="rgba(232,96,28,0.35)", TOAST_TEXT="#a54a12",
        CAPTION="#9a8a7e", DPAD_CENTER="#a4948a",
        DISABLED_TEXT="#c4b4a8", DISABLED_BORDER="#e3d5c8",
    ),
}

STYLE_TEMPLATE = """
QWidget {{ background: {BG}; color: {TEXT}; font-size: 13px; }}
QLabel {{ background: transparent; }}
QToolTip {{ background: {BG_RAISED}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 9px; }}

#topbar {{ background: {BG_PANEL}; border-bottom: 1px solid {BORDER}; }}
#logoBadge {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT}, stop:1 {ACCENT_2});
              color: {ON_ACCENT}; font-size: 17px; font-weight: 900; border-radius: 9px; }}
#appTitle {{ font-size: 17px; font-weight: 700; letter-spacing: 0.5px; }}
#accountBtn {{ background: transparent; border: 1px solid {BORDER}; border-radius: 16px;
               padding: 6px 14px; color: {TEXT_MUTED}; }}
#accountBtn:hover {{ border-color: {ACCENT}; color: {TEXT}; background: {BG_RAISED}; }}
#ghostBtn {{ background: transparent; border: none; border-radius: 14px; padding: 4px 9px;
             color: {TEXT_MUTED}; font-size: 15px; }}
#ghostBtn:hover {{ background: {BG_RAISED}; color: {ACCENT}; }}

#sidebar {{ background: {BG_PANEL}; border-right: 1px solid {BORDER}; }}
#sidebarHeader {{ background: transparent; padding: 12px 6px 4px 8px; }}
#sidebarHeader QLabel {{ font-weight: 700; color: {TEXT_MUTED}; letter-spacing: 2px; font-size: 10px; }}
QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ padding: 10px 12px; margin: 3px 8px; border-radius: 10px; }}
QListWidget::item:hover {{ background: {BG_RAISED}; }}
QListWidget::item:selected {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {SEL_END});
                              color: {ON_ACCENT}; }}

#channel {{ background: {TILE}; border: 2px solid {TILE_BORDER}; border-radius: 12px; }}
#channel[selected="true"] {{ border: 2px solid {ACCENT}; }}
#channelHeader {{ background: {TILE_HEADER}; border-top-left-radius: 10px; border-top-right-radius: 10px; }}
#chTitle {{ font-size: 12px; font-weight: 700; color: #efe6e0; }}
#stateChip {{ background: {CHIP_BG}; color: {CHIP_TEXT}; border-radius: 8px;
              padding: 2px 9px; font-size: 11px; }}
#placeholder {{ background: qradialgradient(cx:0.5, cy:0.45, radius:0.9, fx:0.5, fy:0.45,
                                            stop:0 {PLACE_HI}, stop:1 {TILE});
                color: {PLACE_TEXT}; font-size: 14px;
                border-bottom-left-radius: 10px; border-bottom-right-radius: 10px; }}

#loader {{ background: qradialgradient(cx:0.5, cy:0.45, radius:0.9, fx:0.5, fy:0.45,
                                       stop:0 {PLACE_HI}, stop:1 {TILE});
           border-bottom-left-radius: 10px; border-bottom-right-radius: 10px; }}
#loader QWidget {{ background: transparent; }}
#loaderCaption {{ color: {PLACE_TEXT}; font-size: 12px; letter-spacing: 1px; }}

QToolButton, QPushButton {{ background: {BG_RAISED}; border: none; border-radius: 10px;
                            padding: 8px 14px; color: {TEXT}; }}
QToolButton:hover, QPushButton:hover {{ background: {BG_RAISED_HOVER}; color: {ACCENT}; }}
QToolButton:pressed, QPushButton:pressed {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT}, stop:1 {ACCENT_2});
    color: {ON_ACCENT}; }}

#bottombar {{ background: {BG_PANEL}; border-radius: 14px; border: 1px solid {BORDER}; }}
#bottombar QWidget {{ background: transparent; }}
QFrame#vsep {{ background: {BORDER}; min-width: 1px; }}
#bottomBtn {{ background: transparent; font-size: 16px; font-weight: 600; border-radius: 12px; }}
#bottomBtn:hover {{ background: {BG_RAISED}; color: {ACCENT}; }}
#miniCaption {{ color: {CAPTION}; font-size: 9px; letter-spacing: 2px; font-weight: 800; }}
#toast {{ background: {TOAST_BG}; border: 1px solid {TOAST_BORDER};
          color: {TOAST_TEXT}; border-radius: 10px; padding: 7px 13px; }}

#dpad {{ background: qradialgradient(cx:0.5, cy:0.42, radius:0.8, fx:0.5, fy:0.42,
                                     stop:0 {BG_RAISED_HOVER}, stop:1 {BG_RAISED});
         border: 1px solid {BORDER}; border-radius: 74px; }}
#dpad QToolButton {{ background: transparent; border: none; font-size: 20px; padding: 2px;
                     border-radius: 23px; color: {TEXT_MUTED}; }}
#dpad QToolButton:hover {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
#dpad QToolButton:pressed {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT}, stop:1 {ACCENT_2});
    color: {ON_ACCENT}; }}
#dpadCenter {{ color: {DPAD_CENTER}; font-size: 9px; font-weight: 800; letter-spacing: 2px; }}

#presetBtn {{ background: transparent; border: 1px dashed {BORDER}; border-radius: 12px;
              font-size: 15px; font-weight: 700; color: {TEXT_MUTED}; }}
#presetBtn:hover {{ border: 1px solid {ACCENT}; color: {ACCENT}; background: {ACCENT_SOFT}; }}
#presetBtn[preset="here"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {ACCENT}, stop:1 {ACCENT_2});
    border: none; color: {ON_ACCENT}; }}
#presetBtn:disabled {{ color: {DISABLED_TEXT}; border-color: {DISABLED_BORDER}; background: transparent; }}

#closeBtn {{ background: transparent; border: none; padding: 0 6px; color: #a89a92; }}
#closeBtn:hover {{ color: {ACCENT_2}; background: transparent; }}
QLineEdit, QComboBox {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 9px;
                        padding: 8px 11px; selection-background-color: {ACCENT}; }}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QCheckBox {{ background: transparent; spacing: 8px; }}
QCheckBox::indicator {{ width: 17px; height: 17px; border: 1px solid {BORDER};
                        border-radius: 5px; background: {BG}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QMenu {{ background: {BG_PANEL}; border: 1px solid {BORDER}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 8px 20px; border-radius: 7px; }}
QMenu::item:selected {{ background: {ACCENT}; color: {ON_ACCENT}; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""


def build_style(theme: str) -> str:
    return STYLE_TEMPLATE.format(**THEMES[theme])


def apply_theme(app: QApplication, theme: str) -> str:
    """Aplica o QSS e guarda o tema na propriedade `theme` do app (o spinner pinta com ela)."""
    theme = theme if theme in THEMES else "dark"
    app.setProperty("theme", theme)
    app.setStyleSheet(build_style(theme))
    return theme


class _Dispatcher(QObject):
    invoke = Signal(object, object)

    def __init__(self):
        super().__init__()
        self.invoke.connect(lambda callback, result: callback(result))


_dispatcher: _Dispatcher | None = None


def run_async(fn, on_done):
    """Roda fn() numa thread e entrega o resultado (ou a exceção) a on_done na thread da UI."""
    def target():
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001 — a UI decide como exibir
            result = e
        _dispatcher.invoke.emit(on_done, result)

    threading.Thread(target=target, daemon=True).start()


def rtsp_url(camera: store.LanCamera, quality: str) -> str:
    return f"rtsp://{quote(camera.user, safe='')}:{quote(camera.password, safe='')}@{camera.ip}:554/{STREAMS[quality]}"


class LoginDialog(QDialog):
    def __init__(self, parent=None, email: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Entrar na conta Yoosee")
        self.account: store.Account | None = None

        self.email = QLineEdit(email)
        self.email.setPlaceholderText("e-mail")
        self.password = QLineEdit()
        self.password.setPlaceholderText("senha")
        self.password.setEchoMode(QLineEdit.Password)
        self.remember = QCheckBox("Manter conectado (reconecta sozinho quando a sessão expirar)")
        self.remember.setChecked(True)
        self.status = QLabel()
        self.status.setWordWrap(True)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("Entrar")
        self.buttons.button(QDialogButtonBox.Cancel).setText("Cancelar")
        self.buttons.accepted.connect(self._submit)
        self.buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow("E-mail", self.email)
        form.addRow("Senha", self.password)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.remember)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)
        self.setMinimumWidth(380)
        (self.password if email else self.email).setFocus()

    def _submit(self):
        email, password = self.email.text().strip(), self.password.text()
        if not email or not password:
            self.status.setText("Preencha e-mail e senha.")
            return
        self.buttons.setEnabled(False)
        self.status.setText("Entrando…")
        run_async(lambda: YooseeClient().login_account(email, password).json(),
                  lambda result: self._on_result(email, result))

    def _on_result(self, email, result):
        self.buttons.setEnabled(True)
        if isinstance(result, Exception):
            self.status.setText(f"Falha de conexão: {result}")
            return
        if result.get("code") != 0:
            self.status.setText(f"Login recusado: {result.get('msg')}")
            return
        data = result["data"]
        # guardamos o md5 (o que a API de login recebe como "pwd"), nunca a senha em claro
        pwd_md5 = hashlib.md5(self.password.text().encode("utf-8")).hexdigest() if self.remember.isChecked() else ""
        self.account = store.Account(email=email, access_id=str(data["accessId"]),
                                     access_token=data["accessToken"], pwd_md5=pwd_md5)
        self.accept()


class CameraDialog(QDialog):
    """Acesso direto na LAN: IP + usuário/senha de RTSP/ONVIF (a senha de "conexão NVR" no app Yoosee)."""

    def __init__(self, parent, device_name: str, current: store.LanCamera | None, taken_ips: set[str]):
        super().__init__(parent)
        self.setWindowTitle(f"Câmera na rede local — {device_name}")
        self.current_ip = current.ip if current else None
        self.taken_ips = taken_ips
        self.combo = QComboBox()
        self.combo.setEditable(True)
        if self.current_ip:
            self.combo.addItem(self.current_ip)
        self.combo.setCurrentText(self.current_ip or "")
        self.user = QLineEdit(current.user if current else DEFAULT_RTSP_USER)
        self.password = QLineEdit(current.password if current else "")
        self.password.setEchoMode(QLineEdit.Password)
        self.hint = QLabel("Procurando câmeras na rede local…")
        self.hint.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form = QFormLayout()
        form.addRow("IP", self.combo)
        form.addRow("Usuário", self.user)
        form.addRow("Senha", self.password)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(QLabel("A senha é a definida em Configurações → Conexão NVR, no app Yoosee."))
        layout.addWidget(self.hint)
        layout.addWidget(buttons)
        self.setMinimumWidth(420)
        known = sorted(taken_ips | ({self.current_ip} if self.current_ip else set()))
        run_async(lambda: lan.scan_rtsp_hosts(known), self._on_scan)

    def camera(self) -> store.LanCamera | None:
        ip = self.ip()
        if not ip:
            return None
        return store.LanCamera(ip=ip, user=self.user.text().strip() or DEFAULT_RTSP_USER, password=self.password.text())

    def _on_scan(self, hosts):
        if isinstance(hosts, Exception) or not hosts:
            self.hint.setText("Nenhuma câmera encontrada automaticamente; digite o IP.")
            return
        free = [ip for ip in hosts if ip not in self.taken_ips]
        for ip in free + [ip for ip in hosts if ip in self.taken_ips]:
            if self.combo.findText(ip) < 0:
                label = ip if ip in free else f"{ip} (já associado a outra câmera)"
                self.combo.addItem(label, ip)
        if not self.current_ip:
            if free:
                self.combo.setCurrentIndex(self.combo.findData(free[0]))
            else:
                self.combo.setEditText("")
        self.hint.setText(f"{len(hosts)} câmera(s) com RTSP encontradas, {len(free)} sem associação.")

    def ip(self) -> str:
        if self.combo.currentIndex() >= 0 and self.combo.currentText() == self.combo.itemText(self.combo.currentIndex()):
            data = self.combo.currentData()
            if data:
                return data
        return self.combo.currentText().split(" ", 1)[0].strip()


class LiveCatchUp(QObject):
    """Recupera o atraso do início de um stream ao vivo no QMediaPlayer.

    O FFmpeg gasta 2-4 s analisando o stream antes do 1º quadro, e os pacotes lidos nesse
    tempo ficam numa fila que o player toca desde o começo, em tempo real: a imagem fica
    atrasada exatamente pelo tempo da análise e nunca alcança o ao vivo (medido na
    câmera real: +2,8 s sobre o piso físico de ~0,6 s; `probeSize` não mexe nisso, e
    `LowLatencyStreaming` só tira ~0,25 s e atrasa o 1º quadro em ~7 s). Aqui aceleramos a reprodução por tempo suficiente pra consumir
    essa fila e voltamos a 1.0 — medido: atraso extra cai de +2,8 s para +0,7 s.
    """
    RATE = 2.0
    CONNECT_S = 0.1     # RTSP DESCRIBE/SETUP/PLAY até o 1º pacote
    MIN_LAG_S = 0.8     # abaixo disso não vale a pena acelerar
    MAX_LAG_S = 6.0     # teto de segurança (estimativa absurda não trava o canal em 2x)
    ENABLED = True      # interruptor pra depuração/medição
    MARGIN_S = 0.3

    def __init__(self, player: QMediaPlayer, sink):
        super().__init__(player)
        self.player, self.sink = player, sink
        self._t_open = 0.0
        self._armed = False
        self._restore = QTimer(self)
        self._restore.setSingleShot(True)
        self._restore.timeout.connect(self._back_to_normal)

    def arm(self):
        """Chamar logo antes de `player.setSource(...)` de um stream ao vivo."""
        self.reset()
        if not self.ENABLED:
            return
        self._t_open = time.monotonic()
        self._armed = True
        self.sink.videoFrameChanged.connect(self._on_frame)

    def reset(self):
        self._restore.stop()
        if self._armed:
            self._armed = False
            try:
                self.sink.videoFrameChanged.disconnect(self._on_frame)
            except (RuntimeError, TypeError):
                pass
        if self.player.playbackRate() != 1.0:
            self.player.setPlaybackRate(1.0)

    def _on_frame(self, frame):
        if not self._armed:
            return
        self._armed = False
        try:
            self.sink.videoFrameChanged.disconnect(self._on_frame)
        except (RuntimeError, TypeError):
            pass
        start = frame.startTime()
        pts = start / 1e6 if start >= 0 else self.player.position() / 1000.0
        lag = (time.monotonic() - self._t_open - self.CONNECT_S) - pts
        if lag < self.MIN_LAG_S:
            return
        lag = min(lag, self.MAX_LAG_S)
        self.player.setPlaybackRate(self.RATE)
        self._restore.start(int((lag / (self.RATE - 1.0) + self.MARGIN_S) * 1000))

    def _back_to_normal(self):
        self.player.setPlaybackRate(1.0)


class Spinner(QWidget):
    """Anel giratório com cauda em degradê (laranja -> rosa da identidade, trocando com o tema).
    Só anima enquanto visível, pra não gastar CPU nos canais que já estão no ar."""
    SIZE = 46

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("spinner")
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def showEvent(self, event):
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _tick(self):
        self._angle = (self._angle - 10) % 360
        self.update()

    def paintEvent(self, _event):
        theme = THEMES.get(QApplication.instance().property("theme") or "dark", THEMES["dark"])
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        width = 4.0
        rect = QRectF(width, width, self.SIZE - 2 * width, self.SIZE - 2 * width)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(theme["BORDER"]), width))
        p.drawEllipse(rect)
        head, tail = QColor(theme["ACCENT"]), QColor(theme["ACCENT_2"])
        tail.setAlpha(0)
        grad = QConicalGradient(rect.center(), self._angle)
        grad.setColorAt(0.0, head)
        grad.setColorAt(0.65, tail)
        grad.setColorAt(1.0, tail)
        p.setPen(QPen(QBrush(grad), width, Qt.SolidLine, Qt.RoundCap))
        p.drawEllipse(rect)


class LoadingOverlay(QWidget):
    """Cobre o corpo do quadro (vídeo/placeholder) com um spinner até a imagem chegar.
    Deixa os cliques passarem: selecionar o canal e o duplo clique seguem funcionando."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("loader")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        v = QVBoxLayout(self)
        v.setAlignment(Qt.AlignCenter)
        v.setSpacing(12)
        self.spinner = Spinner()
        v.addWidget(self.spinner, 0, Qt.AlignHCenter)
        self.caption = QLabel("Carregando imagem…", objectName="loaderCaption", alignment=Qt.AlignCenter)
        self.caption.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        v.addWidget(self.caption)
        self.hide()

    def begin(self, caption: str = "Carregando imagem…"):
        self.caption.setText(caption)
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()

    def end(self):
        self.hide()

    def set_caption(self, text: str):
        """Acompanha o chip de estado do canal, só nas mensagens de progresso ("conectando…")."""
        if text.endswith("…"):
            self.caption.setText(text[:1].upper() + text[1:])


class ChannelView(QFrame):
    clicked = Signal(object)
    double_clicked = Signal(object)
    closed = Signal(object)
    message = Signal(str)

    def __init__(self, index: int):
        super().__init__()
        self.setObjectName("channel")
        self.index = index
        self.device: dict | None = None
        self.lan: store.LanCamera | None = None
        self.account_file: str | None = None
        self.remote: p2p.RemoteStream | None = None
        self._bridge_retries = 0
        self.quality = "main"

        header = QWidget(objectName="channelHeader")
        self.title = QLabel(objectName="chTitle")
        self.state = ChipLabel(objectName="stateChip")
        close_btn = QToolButton(objectName="closeBtn", text="✕")
        close_btn.setToolTip("Fechar canal")
        close_btn.clicked.connect(lambda: self.closed.emit(self))
        h = QHBoxLayout(header)
        h.setContentsMargins(10, 5, 6, 5)
        h.setSpacing(6)
        h.addWidget(self.title)
        h.addStretch()
        h.addWidget(self.state)
        h.addWidget(close_btn)
        self.header = header

        self.placeholder = QLabel(f"Canal {index + 1}", objectName="placeholder", alignment=Qt.AlignCenter)
        self.video = QVideoWidget()
        # O QGridLayout reparte o espaço proporcionalmente ao tamanho sugerido de cada item, e o do
        # QVideoWidget é a resolução nativa do vídeo: câmeras com resoluções diferentes ganhavam
        # quadros de tamanhos diferentes. Ignorar o hint (junto com o stretch igual em
        # apply_layout) deixa todos os quadros iguais.
        for w in (self.video, self.placeholder):
            w.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
            w.setMinimumSize(0, 0)
        self.placeholder.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.body = QStackedWidget()
        self.body.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.body.addWidget(self.placeholder)
        self.body.addWidget(self.video)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(self.body, 1)
        self.loader = LoadingOverlay(self.body)
        self._frame_hooked = False
        self.state.changed.connect(self.loader.set_caption)

        self.audio = QAudioOutput()
        self.audio.setMuted(True)
        self.player = QMediaPlayer()
        self.player.setVideoOutput(self.video)
        self.player.setAudioOutput(self.audio)
        # Não usar QPlaybackOptions.LowLatencyStreaming: só tira ~0,25 s de latência, mas o 1º
        # quadro passou a levar ~9 s em vez de ~2,5 s (medido). Quem resolve o atraso é o catch-up.
        self.catchup = LiveCatchUp(self.player, self.video.videoSink())
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)
        # câmeras pela nuvem: o áudio (AAC) chega num stream separado do vídeo
        self.remote_audio = QAudioOutput()
        self.remote_audio.setMuted(True)
        self.audio_player = QMediaPlayer()
        self.audio_player.setAudioOutput(self.remote_audio)

        for w in (self, self.video, self.placeholder, self.body):
            w.installEventFilter(self)
        self._refresh_header()

    def eventFilter(self, obj, event):
        if obj is self.body:  # filtro do corpo só pra redimensionar o loader; cliques já vêm dos filhos
            if event.type() == QEvent.Resize:
                self.loader.setGeometry(self.body.rect())
            return False
        if event.type() == QEvent.MouseButtonPress:
            self.clicked.emit(self)
        elif event.type() == QEvent.MouseButtonDblClick:
            self.double_clicked.emit(self)
        return False

    def _begin_loading(self):
        self.loader.begin()
        if not self._frame_hooked:
            self.video.videoSink().videoFrameChanged.connect(self._on_first_frame)
            self._frame_hooked = True

    def _on_first_frame(self, _frame):
        self._end_loading()

    def _end_loading(self):
        self.loader.end()
        if self._frame_hooked:
            self._frame_hooked = False
            try:
                self.video.videoSink().videoFrameChanged.disconnect(self._on_first_frame)
            except (RuntimeError, TypeError):
                pass

    def set_selected(self, selected: bool):
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def open(self, device: dict, camera: store.LanCamera | None, account_file: str | None = None,
             keep_retries: bool = False):
        """Configurada na LAN e alcançável: RTSP direto. Senão: pela nuvem P2P (p2p.py)."""
        self.stop()
        if not keep_retries:
            self._bridge_retries = 0
        self.device = device
        self.account_file = account_file
        self._refresh_header()
        self._begin_loading()
        if not camera:
            self._open_cloud()
            return
        self.state.setText("conectando…")

        def decide(ok):
            if self.device is not device or self.remote:
                return  # o canal mudou enquanto testávamos
            if ok is True:
                self._open_lan(camera)
            elif account_file and p2p.available():
                self._open_cloud("fora da rede local")
            else:
                self._open_lan(camera)  # sem nuvem instalada: tenta assim mesmo e mostra o erro

        run_async(lambda: lan.reachable(camera.ip), decide)

    def _open_lan(self, camera: store.LanCamera):
        self.lan = camera
        self.catchup.arm()
        self.player.setSource(QUrl(rtsp_url(camera, self.quality)))
        self.body.setCurrentWidget(self.video)
        self.player.play()

    def _open_cloud(self, reason: str = ""):
        device, account_file = self.device, self.account_file
        self.lan = None
        if not account_file or not p2p.available():
            self._end_loading()
            self.placeholder.setText(
                f"{device['remarkName']}\n\nAcesso pela nuvem não instalado (veja o README).\n"
                "Se a câmera está na sua rede, clique nela na lista com o botão direito → "
                "Configurar na rede local.")
            return
        self.state.setText("conectando pela nuvem…")
        note = f" ({reason})" if reason else ""
        self.placeholder.setText(f"{device['remarkName']}\n\nConectando pela nuvem{note}…")
        stream = p2p.RemoteStream(account_file, device["devId"],
                                  lambda event: _dispatcher.invoke.emit(lambda e: self._on_remote_event(stream, e), event))
        self.remote = stream
        stream.start()

    def _on_remote_event(self, stream, event: str):
        if stream is not self.remote or not self.device:
            return
        kind, _, value = event.partition(" ")
        if event == "STATE playing":
            self.state.setText("aguardando vídeo…")
        elif event == "STATE video" and self.player.source().isEmpty():
            # só abre o player quando o 1º keyframe já está a caminho: algumas
            # câmeras H.264 demoram >20 s e o probe do QMediaPlayer desistiria antes
            self.catchup.arm()
            self.player.setSource(QUrl(stream.url))
            self.body.setCurrentWidget(self.video)
            self.player.play()
        elif event.startswith("STATE audio") and self.audio_player.source().isEmpty():
            self.audio_player.setSource(QUrl(stream.audio_url))
            self.audio_player.play()
        elif event == "STATE reconnecting":
            self.state.setText("reconectando…")
        elif event == "ERROR bridge" and self._bridge_retries < 3:
            # o processo da ponte caiu (raro, corrida dentro do SDK): reabre numa ponte nova
            self._bridge_retries += 1
            self.state.setText("reconectando…")
            d, acc = self.device, self.account_file
            QTimer.singleShot(2000, lambda: self.device is d and self.open(d, None, acc, keep_retries=True))
        elif kind == "ERROR":
            self._end_loading()
            self.state.setText("erro")
            self.body.setCurrentWidget(self.placeholder)
            self.placeholder.setText(f"{self.device['remarkName']}\n\nFalha ao conectar pela nuvem ({value}).")
        elif kind == "PTZ" and value != "0":
            self.message.emit(f"PTZ falhou em {self.device['remarkName']}")

    def is_muted(self) -> bool:
        return self.audio.isMuted()

    def set_muted(self, muted: bool):
        self.audio.setMuted(muted)
        self.remote_audio.setMuted(muted)

    def set_audio_device(self, device):
        self.audio.setDevice(device)
        self.remote_audio.setDevice(device)

    def has_audio(self) -> bool:
        if self.remote:
            return not self.audio_player.source().isEmpty()
        return self.player.hasAudio()

    def effective_quality(self) -> str:
        return "main" if self.remote else self.quality

    def stop(self):
        if self.remote:
            self.remote.stop()
            self.remote = None
        self.catchup.reset()
        self._end_loading()
        self.player.stop()
        self.player.setSource(QUrl())
        self.audio_player.stop()
        self.audio_player.setSource(QUrl())
        self.device = None
        self.lan = None
        self.body.setCurrentWidget(self.placeholder)
        self.placeholder.setText(f"Canal {self.index + 1}")
        self._refresh_header()

    def toggle_quality(self):
        if self.remote:
            return
        self.quality = "sub" if self.quality == "main" else "main"
        if self.device and self.lan:
            self.open(self.device, self.lan, self.account_file)

    def _refresh_header(self):
        self.title.setText(self.device["remarkName"] if self.device else "")
        self.state.setText("")
        self.header.setVisible(self.device is not None)

    def _on_status(self, status):
        if status in (QMediaPlayer.BufferedMedia, QMediaPlayer.BufferingMedia) and self.device:
            quality = "HD" if self.effective_quality() == "main" else "SD"
            self.state.setText(f"{quality} · nuvem" if self.remote else quality)
        elif status == QMediaPlayer.EndOfMedia and self.device:
            d, lan, acc = self.device, self.lan, self.account_file
            QTimer.singleShot(2000, lambda: self.device is d and self.open(d, lan, acc))

    def _on_error(self, _error, message):
        if self.device and self.lan and not self.remote and self.account_file and p2p.available():
            self.player.stop()
            self.player.setSource(QUrl())
            self._open_cloud("rede local falhou")
            return
        if self.device and self.remote and self._bridge_retries < 3:
            # câmera remota: o probe do player às vezes falha logo após reconectar; tenta de novo
            self._bridge_retries += 1
            self.state.setText("reconectando…")
            d, acc = self.device, self.account_file
            QTimer.singleShot(2000, lambda: self.device is d and self.open(d, None, acc, keep_retries=True))
            return
        if self.device:
            self._end_loading()
            self.state.setText("erro")
            self.body.setCurrentWidget(self.placeholder)
            self.placeholder.setText(f"{self.device['remarkName']}\n\nFalha no vídeo: {message}")


class ChipLabel(QLabel):
    """QLabel que some quando o texto está vazio — pra chips e toasts não deixarem
    um "pílula" vazia ocupando espaço na tela. Emite `changed` a cada texto novo."""
    changed = Signal(str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setVisible(bool(self.text()))

    def setText(self, text: str):
        super().setText(text)
        self.setVisible(bool(text))
        self.changed.emit(text)


class DPad(QFrame):
    """Emite `moved(direção, é_repetição)` ao clicar, e repetidamente enquanto o
    botão fica pressionado — a câmera não tem um comando de "parar" que funcione
    (ver ARQUITETURA.md), então "segurar para mover" é aproximado reenviando o
    comando de movimento no maior ritmo que a câmera aceita."""
    moved = Signal(str, bool)
    SIZE = 148
    BTN_SIZE = 46
    REPEAT_MS = 50  # só tentativas; quem decide se aceita é o lado que já sabe o cooldown real

    def __init__(self):
        super().__init__(objectName="dpad")
        self.setFixedSize(self.SIZE, self.SIZE)
        self._held: str | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(self.REPEAT_MS)
        self._timer.timeout.connect(self._repeat)
        grid = QGridLayout(self)
        grid.setContentsMargins(8, 8, 8, 8)
        grid.setSpacing(2)
        for direction, glyph, row, col, tip in (
            ("up", "▲", 0, 1, "Cima"), ("left", "◀", 1, 0, "Esquerda"),
            ("right", "▶", 1, 2, "Direita"), ("down", "▼", 2, 1, "Baixo"),
        ):
            btn = QToolButton(text=glyph)
            btn.setFixedSize(self.BTN_SIZE, self.BTN_SIZE)
            btn.setToolTip(f"{tip} (segure para mover continuamente)")
            btn.pressed.connect(lambda d=direction: self._start(d))
            btn.released.connect(self._stop)
            grid.addWidget(btn, row, col, Qt.AlignCenter)
        grid.addWidget(QLabel("PTZ", objectName="dpadCenter", alignment=Qt.AlignCenter), 1, 1, Qt.AlignCenter)

    def _start(self, direction: str):
        self._held = direction
        self.moved.emit(direction, False)
        self._timer.start()

    def _repeat(self):
        if self._held:
            self.moved.emit(self._held, True)

    def _stop(self):
        self._timer.stop()
        self._held = None


class MainWindow(QMainWindow):
    LAYOUTS = (1, 4, 9)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Câmeras Yoosee")
        self.resize(1400, 860)
        self.account: store.Account | None = None
        self.devices: list[dict] = []
        self.cameras = store.load_cameras()
        self.ptz: dict[str, PtzController] = {}
        self._ptz_cache: dict[str, tuple[float, float]] = {}  # posição aproximada (pan, tilt)
        self._cloud_ptz_last: dict[str, float] = {}  # devId -> monotonic() do último nudge aceito
        self.channels = [ChannelView(i) for i in range(max(self.LAYOUTS))]
        self._media_devices = QMediaDevices(self)
        self._media_devices.audioOutputsChanged.connect(self._follow_default_output)
        self.layout_size = 4
        self.theme = store.get_setting("theme", "dark")
        if self.theme not in THEMES:
            self.theme = "dark"
        self._relogin_last: dict[str, float] = {}  # access_id -> monotonic() da última reconexão
        self.slots: dict[int, dict] = {}   # arrumação da grade: quadro -> {"devId", "quality"} (ver _save_grid)
        self._grid_key: str | None = None  # conta cuja arrumação já foi restaurada nesta sessão
        self.selected: ChannelView | None = None
        self.focused: ChannelView | None = None

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_topbar())
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())
        right = QVBoxLayout()
        right.setContentsMargins(10, 10, 10, 10)
        right.setSpacing(10)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(10)
        right.addWidget(self.grid_host, 1)
        right.addWidget(self._build_bottombar())
        body.addLayout(right, 1)
        root.addLayout(body, 1)
        self.setCentralWidget(central)

        for ch in self.channels:
            ch.clicked.connect(self.select_channel)
            ch.double_clicked.connect(self.toggle_focus)
            ch.closed.connect(self._on_channel_closed)
            ch.message.connect(self._flash)
        self.apply_layout()
        self.select_channel(self.channels[0])

        QTimer.singleShot(0, self.startup)

    def _build_topbar(self):
        bar = QWidget(objectName="topbar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 9, 14, 9)
        h.setSpacing(10)
        logo = QLabel("Y", objectName="logoBadge", alignment=Qt.AlignCenter)
        logo.setFixedSize(32, 32)
        h.addWidget(logo)
        self.title_label = QLabel(objectName="appTitle")
        h.addWidget(self.title_label)
        h.addStretch()
        self.theme_btn = QToolButton(objectName="ghostBtn")
        self.theme_btn.clicked.connect(self.toggle_theme)
        h.addWidget(self.theme_btn)
        self.account_btn = QToolButton(objectName="accountBtn")
        self.account_btn.setPopupMode(QToolButton.InstantPopup)
        self.account_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.account_menu = QMenu(self)
        self.account_btn.setMenu(self.account_menu)
        h.addWidget(self.account_btn)
        self._refresh_theme_widgets()
        return bar

    def toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        store.set_setting("theme", self.theme)
        apply_theme(QApplication.instance(), self.theme)
        self._refresh_theme_widgets()

    def _refresh_theme_widgets(self):
        """O que não dá pra trocar só pelo QSS: o rich text do título e o botão do tema."""
        accent = THEMES[self.theme]["ACCENT"]
        self.title_label.setText(f'Câmeras <span style="color:{accent};">Yoosee</span>')
        self.theme_btn.setText("☀" if self.theme == "dark" else "☾")
        self.theme_btn.setToolTip("Mudar para o tema claro" if self.theme == "dark" else "Mudar para o tema escuro")

    def _build_sidebar(self):
        side = QWidget(objectName="sidebar")
        side.setFixedWidth(272)
        v = QVBoxLayout(side)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        header = QWidget(objectName="sidebarHeader")
        hh = QHBoxLayout(header)
        hh.setContentsMargins(8, 4, 4, 4)
        hh.addWidget(QLabel("DISPOSITIVOS"))
        hh.addStretch()
        refresh = QToolButton(text="⟳", objectName="ghostBtn")
        refresh.setToolTip("Atualizar lista")
        refresh.clicked.connect(self.load_devices)
        hh.addWidget(refresh)
        v.addWidget(header)
        self.device_list = QListWidget()
        self.device_list.itemDoubleClicked.connect(self._open_item)
        self.device_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.device_list.customContextMenuRequested.connect(self._device_menu)
        v.addWidget(self.device_list, 1)
        self.sidebar_status = QLabel()
        self.sidebar_status.setWordWrap(True)
        self.sidebar_status.setContentsMargins(8, 8, 8, 8)
        v.addWidget(self.sidebar_status)
        return side

    BOTTOMBAR_ICON = 26
    BOTTOMBAR_BTN = 44

    def _bottombar_btn(self, **kwargs) -> QToolButton:
        """Botão padrão da barra inferior, no tamanho maior e com ícone grande."""
        btn = QToolButton(objectName="bottomBtn", **kwargs)
        btn.setIconSize(QSize(self.BOTTOMBAR_ICON, self.BOTTOMBAR_ICON))
        btn.setMinimumSize(self.BOTTOMBAR_BTN, self.BOTTOMBAR_BTN)
        return btn

    @staticmethod
    def _vsep() -> QFrame:
        line = QFrame(objectName="vsep")
        line.setFixedWidth(1)
        line.setFixedHeight(64)
        return line

    def _build_bottombar(self):
        bar = QWidget(objectName="bottombar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 10, 16, 10)
        h.setSpacing(12)
        st = self.style()

        stop = self._bottombar_btn(text="■")
        stop.setToolTip("Parar canal selecionado")
        stop.clicked.connect(self.stop_selected)
        h.addWidget(stop)
        h.addWidget(self._vsep())

        self.dpad = DPad()
        self.dpad.moved.connect(self.ptz_move)
        h.addWidget(self.dpad)

        self.preset_btns: dict[str, QToolButton] = {}
        presets = QWidget()
        pv = QVBoxLayout(presets)
        pv.setContentsMargins(8, 2, 2, 2)
        pv.setSpacing(6)
        pv.addWidget(QLabel("POSIÇÕES", objectName="miniCaption", alignment=Qt.AlignHCenter))
        grid_holder = QWidget()
        pg = QGridLayout(grid_holder)
        pg.setContentsMargins(0, 0, 0, 0)
        pg.setSpacing(6)
        for i, slot in enumerate(store.PTZ_SLOTS):
            btn = QToolButton(text=slot, objectName="presetBtn")
            btn.setToolTip(f"Ir para a predefinição {slot}\n"
                            "Clique com o botão direito para salvar/apagar")
            btn.setFixedSize(46, 46)
            btn.setEnabled(False)
            btn.clicked.connect(lambda _=False, s=slot: self.goto_ptz_preset(s))
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(lambda pos, s=slot, b=btn: self._preset_menu(s, b))
            self.preset_btns[slot] = btn
            pg.addWidget(btn, i // 2, i % 2)
        pv.addWidget(grid_holder)
        h.addWidget(presets)
        h.addWidget(self._vsep())

        shot = self._bottombar_btn(text="Foto")
        shot.setToolTip("Capturar imagem do canal selecionado")
        shot.clicked.connect(self.screenshot)
        h.addWidget(shot)

        self.message = ChipLabel(objectName="toast")
        h.addWidget(self.message, 0, Qt.AlignVCenter)
        h.addStretch()
        h.addWidget(self._vsep())

        self.mute_btn = self._bottombar_btn(icon=st.standardIcon(QStyle.SP_MediaVolumeMuted))
        self.mute_btn.setToolTip("Som do canal selecionado")
        self.mute_btn.clicked.connect(self.toggle_mute)
        h.addWidget(self.mute_btn)

        self.quality_btn = self._bottombar_btn(text="HD")
        self.quality_btn.setToolTip("Alternar HD/SD no canal selecionado")
        self.quality_btn.clicked.connect(self.toggle_quality)
        h.addWidget(self.quality_btn)

        self.layout_btn = self._bottombar_btn(text="4")
        self.layout_btn.setToolTip("Alternar grade 1 / 4 / 9")
        self.layout_btn.clicked.connect(self.cycle_layout)
        h.addWidget(self.layout_btn)

        full = self._bottombar_btn(text="⛶")
        full.setToolTip("Tela cheia")
        full.clicked.connect(lambda: self.showNormal() if self.isFullScreen() else self.showFullScreen())
        h.addWidget(full)
        return bar

    # ---- contas ----

    def startup(self):
        self.account = store.get_active_account()
        self._rebuild_account_menu()
        if self.account is None:
            self.add_account()
        else:
            self.load_devices()

    def _rebuild_account_menu(self):
        menu = self.account_menu
        menu.clear()
        self.account_btn.setText(self.account.email if self.account else "Entrar")
        group = QActionGroup(menu)
        for acc in store.list_accounts():
            act = QAction(acc.email, menu, checkable=True)
            act.setChecked(self.account is not None and acc.access_id == self.account.access_id)
            act.triggered.connect(lambda _=False, a=acc: self.switch_account(a))
            group.addAction(act)
            menu.addAction(act)
        if store.list_accounts():
            menu.addSeparator()
        menu.addAction("Adicionar conta…", self.add_account)
        if self.account:
            menu.addAction("Sair desta conta", self.logout)
        if os.environ.get("FLATPAK_ID") and p2p.WINE and not p2p.available():
            menu.addSeparator()
            menu.addAction("Configurar câmeras pela nuvem…", self.import_cloud_sdk)

    def import_cloud_sdk(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Selecione o arquivo IotVideoSDK.dll",
            os.path.expanduser("~"), "IotVideoSDK.dll (IotVideoSDK.dll);;Todos os arquivos (*)")
        if not path:
            return
        ok, message = p2p.import_sdk(path)
        self._flash(message)
        if ok:
            self._rebuild_account_menu()
            self._fill_device_list()

    def add_account(self, email: str = ""):
        dlg = LoginDialog(self, email)
        if dlg.exec() == QDialog.Accepted and dlg.account:
            store.save_account(dlg.account)
            self.switch_account(dlg.account)
        elif self.account is None:
            self.sidebar_status.setText("Nenhuma conta conectada. Use o menu no canto superior direito para entrar.")
            self._rebuild_account_menu()

    def switch_account(self, account: store.Account):
        self.account = account
        store.set_active_id(account.access_id)
        for ch in self.channels:
            ch.stop()
        self.slots, self._grid_key = {}, None  # a arrumação é por conta: nada vaza pra próxima
        self._rebuild_account_menu()
        self.load_devices()

    def logout(self):
        if not self.account:
            return
        if QMessageBox.question(self, "Sair", f"Sair da conta {self.account.email}?") != QMessageBox.Yes:
            return
        store.remove_account(self.account.access_id)
        for ch in self.channels:
            ch.stop()
        self.device_list.clear()
        self.devices = []
        self.slots, self._grid_key = {}, None
        self.account = store.get_active_account()
        self._rebuild_account_menu()
        if self.account:
            store.set_active_id(self.account.access_id)
            self.load_devices()
        else:
            self.sidebar_status.setText("Nenhuma conta conectada.")

    # ---- dispositivos ----

    def load_devices(self):
        if not self.account:
            return
        acc = self.account
        self.sidebar_status.setText("Carregando dispositivos…")

        def fetch():
            resp = YooseeClient().device_list_pc(acc.access_token, acc.access_id).json()
            found = lan.discover()
            return resp, found

        run_async(fetch, lambda result: self._on_devices(acc, result))

    def _on_devices(self, acc, result):
        if acc is not self.account:
            return
        if isinstance(result, Exception):
            self.sidebar_status.setText(f"Falha ao buscar dispositivos: {result}")
            return
        resp, discovered = result
        if resp.get("code") != 0:
            # "Manter conectado": renova a sessão sozinho com o hash guardado, no
            # máximo 1x por minuto por conta (evita loop se nem o login novo valer).
            if acc.pwd_md5 and time.monotonic() - self._relogin_last.get(acc.access_id, -999) > 60:
                self._relogin_last[acc.access_id] = time.monotonic()
                self.sidebar_status.setText("Sessão expirou — reconectando…")
                run_async(lambda: YooseeClient().login_account(acc.email, pwd_md5=acc.pwd_md5).json(),
                          lambda result: self._on_relogin(acc, result))
                return
            self.sidebar_status.setText(f"Sessão inválida ({resp.get('msg')}). Entre novamente.")
            self.add_account(acc.email)
            return
        for dev_id, ip in discovered.items():
            known = self.cameras.get(dev_id)
            if known is None or known.ip != ip:
                store.save_camera(dev_id, store.LanCamera(ip=ip, user=known.user, password=known.password)
                                  if known else store.LanCamera(ip=ip))
        self.cameras = store.load_cameras()
        self.devices = resp["data"]["deviceList"]
        self._fill_device_list()
        self.sidebar_status.setText("")
        key = self._grid_account_key()
        if key != self._grid_key or not any(ch.device for ch in self.channels[: self.layout_size]):
            # 1ª carga desta conta na sessão: restaura a arrumação. Atualizar a lista (⟳) com
            # canais abertos NÃO reabre tudo do zero (antes resetava a grade pra ordem da lista).
            self._grid_key = key
            self._autoplay()

    def _on_relogin(self, acc, result):
        if acc is not self.account:
            return
        data = result.get("data") if isinstance(result, dict) and result.get("code") == 0 else None
        if not data:
            motivo = result if isinstance(result, Exception) else (result or {}).get("msg", "?")
            self.sidebar_status.setText(f"Reconexão automática falhou ({motivo}). Entre novamente.")
            self.add_account(acc.email)
            return
        renewed = store.Account(email=acc.email, access_id=str(data["accessId"]),
                                access_token=data["accessToken"], pwd_md5=acc.pwd_md5)
        store.save_account(renewed)  # remove a conta antiga de mesmo e-mail
        store.set_active_id(renewed.access_id)
        self._relogin_last[renewed.access_id] = self._relogin_last.get(acc.access_id, 0)
        self.account = renewed
        self._rebuild_account_menu()
        self.load_devices()

    def _fill_device_list(self):
        self.device_list.clear()
        for dev in self.devices:
            cam = self.cameras.get(dev["devId"])
            if cam:
                where = cam.ip if lan.is_on_local_network(cam.ip) else f"{cam.ip} · via VPN"
            else:
                where = "pela nuvem" if p2p.available() else "não configurada"
            item = QListWidgetItem(f"{dev['remarkName']}\n{where}")
            item.setData(Qt.UserRole, dev)
            if not cam and not p2p.available():
                item.setForeground(QColor("#888888"))
            self.device_list.addItem(item)
            if cam:
                run_async(lambda ip=cam.ip: lan.reachable(ip),
                          lambda ok, item=item, dev=dev, ip=cam.ip: self._set_reach_label(item, dev, ip, ok))

    def _set_reach_label(self, item, dev, ip, ok):
        if self.device_list.row(item) < 0:
            return  # lista recriada nesse meio-tempo
        if ok is True:
            where = ip if lan.is_on_local_network(ip) else f"{ip} · via VPN"
        else:
            where = "pela nuvem (fora da rede)" if p2p.available() else f"{ip} · inacessível"
        item.setText(f"{dev['remarkName']}\n{where}")

    def _account_file(self) -> str | None:
        return store.account_path(self.account.access_id) if self.account else None

    # ---- arrumação da grade (gravada) ----
    #
    # O usuário monta a grade do jeito que quer: qual câmera em qual quadro, quais quadros fechou,
    # HD/SD de cada um e 1/4/9. Isso é gravado por conta em settings.json ("grids") a cada ação
    # dele — e NÃO em closeEvent, troca de conta ou stop() internos, que também param canais e
    # apagariam a arrumação. `self.slots` é a memória dela e inclui os quadros "estacionados"
    # (além do layout atual): voltam sozinhos quando o layout cresce de novo.

    def _grid_account_key(self) -> str | None:
        return self.account.email.lower() if self.account else None  # e-mail: o access_id muda em cada login

    def _sync_slots(self):
        """Os quadros visíveis definem exatamente o estado dos seus slots; os estacionados ficam."""
        for i, ch in enumerate(self.channels[: self.layout_size]):
            if ch.device:
                self.slots[i] = {"devId": ch.device["devId"], "quality": ch.quality}
            else:
                self.slots.pop(i, None)

    def _save_grid(self):
        key = self._grid_account_key()
        if key is None:
            return
        self._sync_slots()
        grids = store.get_setting("grids", {})
        grids[key] = {"layout": self.layout_size,
                      "slots": {str(i): spec for i, spec in sorted(self.slots.items())}}
        store.set_setting("grids", grids)

    def _open_slot(self, i: int) -> bool:
        spec = self.slots.get(i)
        if not spec or not 0 <= i < self.layout_size:
            return False
        dev = next((d for d in self.devices if d["devId"] == spec.get("devId")), None)
        if dev is None:
            return False  # câmera sumiu da conta: o slot fica na memória, só não abre
        cam = self.cameras.get(dev["devId"])
        if not cam and not p2p.available():
            return False
        ch = self.channels[i]
        ch.quality = spec.get("quality") if spec.get("quality") in ("main", "sub") else "main"
        ch.open(dev, cam, self._account_file())
        return True

    def _open_pending_slots(self) -> int:
        """Abre os quadros visíveis que têm câmera na memória mas estão vazios."""
        return sum(1 for i in range(self.layout_size) if not self.channels[i].device and self._open_slot(i))

    def _restore_grid(self) -> bool:
        """Restaura a arrumação gravada. False = nada gravado (ou nada aproveitável): usar o padrão."""
        key = self._grid_account_key()
        saved = store.get_setting("grids", {}).get(key) if key else None
        if not isinstance(saved, dict):
            return False
        layout = saved.get("layout")
        if layout in self.LAYOUTS and layout != self.layout_size:
            self.layout_size, self.focused = layout, None
            self.apply_layout()
        slots = {}
        for k, spec in (saved.get("slots") or {}).items():
            try:
                idx = int(k)
            except ValueError:
                continue
            if 0 <= idx < len(self.channels) and isinstance(spec, dict):
                slots[idx] = spec
        self.slots = slots
        if not slots:
            return True  # arrumação gravada vazia: o usuário fechou tudo, respeita
        return self._open_pending_slots() > 0

    def _on_channel_closed(self, ch):
        ch.stop()
        self._save_grid()

    def _autoplay(self):
        if self._restore_grid():
            return
        free = iter(self.channels[: self.layout_size])
        for dev in self.devices:
            cam = self.cameras.get(dev["devId"])
            if not cam and not p2p.available():
                continue
            ch = next(free, None)
            if ch is None:
                break
            ch.open(dev, cam, self._account_file())

    def _open_item(self, item):
        dev = item.data(Qt.UserRole)
        target = self.selected or self.channels[0]
        target.open(dev, self.cameras.get(dev["devId"]), self._account_file())
        self.select_channel(target)
        self._save_grid()

    def _device_menu(self, pos):
        item = self.device_list.itemAt(pos)
        if not item:
            return
        dev = item.data(Qt.UserRole)
        menu = QMenu(self)
        menu.addAction("Abrir no canal selecionado", lambda: self._open_item(item))
        menu.addAction("Configurar na rede local…", lambda: self._configure_lan(dev))
        if dev["devId"] in self.cameras:
            menu.addAction("Remover configuração da rede local", lambda: self._set_camera(dev, None))
        menu.exec(self.device_list.mapToGlobal(pos))

    def _configure_lan(self, dev):
        taken = {cam.ip for dev_id, cam in self.cameras.items() if dev_id != dev["devId"]}
        dlg = CameraDialog(self, dev["remarkName"], self.cameras.get(dev["devId"]), taken)
        if dlg.exec() == QDialog.Accepted and dlg.camera():
            self._set_camera(dev, dlg.camera())

    def _set_camera(self, dev, camera):
        store.save_camera(dev["devId"], camera)
        self.cameras = store.load_cameras()
        self.ptz.clear()
        self._fill_device_list()

    # ---- grade de canais ----

    def apply_layout(self):
        for ch in self.channels:
            self.grid.removeWidget(ch)
            ch.hide()
        if self.focused is not None:
            for i in range(3):
                self.grid.setRowStretch(i, 1 if i == 0 else 0)
                self.grid.setColumnStretch(i, 1 if i == 0 else 0)
            self.grid.addWidget(self.focused, 0, 0)
            self.focused.show()
            return
        cols = {1: 1, 4: 2, 9: 3}[self.layout_size]
        for i in range(3):  # linhas/colunas iguais; as que sobram de um layout maior zeram
            self.grid.setRowStretch(i, 1 if i < cols else 0)
            self.grid.setColumnStretch(i, 1 if i < cols else 0)
        for i, ch in enumerate(self.channels[: self.layout_size]):
            self.grid.addWidget(ch, i // cols, i % cols)
            ch.show()
        for ch in self.channels[self.layout_size:]:
            ch.stop()
        self.layout_btn.setText(str(self.layout_size))

    def cycle_layout(self):
        self._sync_slots()  # guarda o que está aberto antes do layout encolher (apply_layout para os de fora)
        idx = self.LAYOUTS.index(self.layout_size)
        self.layout_size = self.LAYOUTS[(idx + 1) % len(self.LAYOUTS)]
        self.focused = None
        self.apply_layout()
        self._open_pending_slots()  # quadros estacionados voltam quando o layout cresce
        if self.selected not in self.channels[: self.layout_size]:
            self.select_channel(self.channels[0])
        self._save_grid()

    def toggle_focus(self, ch):
        self.focused = None if self.focused is ch else ch
        self.apply_layout()

    def stop_selected(self):
        if self.selected:
            self.selected.stop()
            self._refresh_preset_buttons()
            self._save_grid()

    def select_channel(self, ch):
        if self.selected is not None:
            self.selected.set_selected(False)
        self.selected = ch
        ch.set_selected(True)
        self.quality_btn.setText("HD" if ch.effective_quality() == "main" else "SD")
        self.mute_btn.setIcon(self.style().standardIcon(
            QStyle.SP_MediaVolumeMuted if ch.is_muted() else QStyle.SP_MediaVolume))
        self._refresh_preset_buttons()

    def _refresh_preset_buttons(self):
        ch = self.selected
        dev = ch.device if ch else None
        saved = store.get_ptz_presets(dev["devId"]) if dev else {}
        for slot, btn in self.preset_btns.items():
            btn.setEnabled(dev is not None)
            btn.setProperty("preset", "here" if slot in saved else "empty")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    # ---- controles ----

    def _flash(self, text: str):
        self.message.setText(text)
        QTimer.singleShot(3000, lambda: self.message.text() == text and self.message.setText(""))

    def ptz_move(self, direction: str, is_repeat: bool = False):
        """Um passo do motor por comando, igual a um clique no app oficial do Windows.
        Segurar o botão repete o passo a cada ptz.HOLD_INTERVAL (0,5s): nesse ritmo a
        câmera emenda os passos num movimento quase contínuo, sobrando no máximo 1-2
        passos de "cauda" ao soltar (medido no vídeo — ver ptz.py). Presets usam o
        STEP_INTERVAL folgado (0,8s), porque lá a contagem exata importa mais que a
        velocidade. `is_repeat`: disparo automático do DPad; rejeição por ritmo é
        normal e fica em silêncio. Posição: 1 passo aceito = 1 unidade, nos dois
        caminhos."""
        ch = self.selected
        if ch and ch.remote:
            dev_id = ch.device["devId"]
            now = time.monotonic()
            # a nuvem não tem trava própria (a rede local usa PtzController); mesmo
            # motor, mesmo limite de ritmo.
            if now - self._cloud_ptz_last.get(dev_id, 0.0) < ptz.HOLD_INTERVAL:
                return
            if not ch.remote.ptz(direction):
                if not is_repeat:
                    self._flash("A conexão pela nuvem ainda não está pronta.")
                return
            self._cloud_ptz_last[dev_id] = now
            dx, dy = ptz.DIRECTIONS[direction]
            self._update_ptz_position(dev_id, dx, dy)
            return
        if not ch or not ch.lan:
            if not is_repeat:
                self._flash("Selecione um canal com câmera aberta.")
            return
        cam = ch.lan
        ctrl = self.ptz.setdefault(cam.ip, PtzController(cam.ip, cam.user, cam.password))
        name = ch.device["remarkName"]
        dev_id = ch.device["devId"]

        def on_result(ok):
            if ok:
                dx, dy = ptz.DIRECTIONS[direction]
                _dispatcher.invoke.emit(lambda _: self._update_ptz_position(dev_id, dx, dy), None)
            else:
                _dispatcher.invoke.emit(lambda _: self._flash(f"PTZ falhou em {name}"), None)

        # clique avulso rejeitado: silencioso também — o passo anterior ainda está em
        # andamento e dura menos de 1s, um aviso só atrapalharia cliques rápidos.
        ctrl.move(direction, on_result, interval=ptz.HOLD_INTERVAL)

    # ---- predefinições de PTZ ----
    #
    # A câmera não tem posição absoluta nem presets confirmados (só
    # ContinuousMove funciona neste firmware — ver ARQUITETURA.md). A posição
    # é aproximada, rastreada só por este app como um acumulado de pan/tilt,
    # onde 1 clique manual (local ou pela nuvem) vale 1 unidade — os dois
    # acionam o mesmo motor físico, então tratamos como equivalentes; isso faz
    # uma predefinição salva num caminho funcionar pelo outro também. Se a
    # câmera for movida por outro cliente (ex.: app do celular), o
    # rastreamento desalinha; basta salvar a predefinição de novo.

    MAX_PRESET_STEPS = 80  # por eixo; ~1 min no ritmo da câmera

    def _ptz_position(self, dev_id: str) -> tuple[float, float]:
        if dev_id not in self._ptz_cache:
            self._ptz_cache[dev_id] = store.get_ptz_position(dev_id)
        return self._ptz_cache[dev_id]

    def _set_ptz_position(self, dev_id: str, pan: float, tilt: float):
        self._ptz_cache[dev_id] = (pan, tilt)
        store.set_ptz_position(dev_id, pan, tilt)

    def _update_ptz_position(self, dev_id: str, dpan: float, dtilt: float):
        pan, tilt = self._ptz_position(dev_id)
        self._set_ptz_position(dev_id, pan + dpan, tilt + dtilt)

    @staticmethod
    def _preset_axis_steps(dpan: float, dtilt: float) -> list[tuple[str, float]]:
        """[(direção, |delta|)] dos eixos com diferença real entre a posição atual e a predefinição."""
        steps = []
        if abs(dpan) > 1e-6:
            steps.append(("right" if dpan > 0 else "left", abs(dpan)))
        if abs(dtilt) > 1e-6:
            steps.append(("up" if dtilt > 0 else "down", abs(dtilt)))
        return steps

    def goto_ptz_preset(self, slot: str):
        ch = self.selected
        if not ch or not ch.device:
            self._flash("Selecione um canal com câmera aberta.")
            return
        dev_id = ch.device["devId"]
        target = store.get_ptz_presets(dev_id).get(slot)
        if not target:
            self._flash(f"Predefinição {slot} vazia — botão direito para salvar aqui.")
            return
        target_pan, target_tilt = target
        cur_pan, cur_tilt = self._ptz_position(dev_id)
        axis_steps = self._preset_axis_steps(target_pan - cur_pan, target_tilt - cur_tilt)
        if not axis_steps:
            self._flash(f"Já está na predefinição {slot}.")
            return
        name = ch.device["remarkName"]
        self._flash(f"Movendo para a predefinição {slot}…")
        queue = [d for d, m in axis_steps for _ in range(min(round(m), self.MAX_PRESET_STEPS))]
        if not queue:  # diferença < meio passo (sobra de versões antigas com frações)
            self._set_ptz_position(dev_id, target_pan, target_tilt)
            self._flash(f"Já está na predefinição {slot}.")
            return
        if ch.remote:
            self._goto_preset_cloud(ch.remote, dev_id, queue, target_pan, target_tilt, name)
        elif ch.lan:
            self._goto_preset_lan(ch, dev_id, queue, target_pan, target_tilt, name)
        else:
            self._flash("Câmera sem conexão ativa.")

    def _goto_preset_lan(self, ch, dev_id, queue, target_pan, target_tilt, name):
        """Um passo por vez no ritmo da câmera — um comando longo não serve: ela dá um
        passo só, qualquer que seja o Timeout."""
        cam = ch.lan
        ctrl = self.ptz.setdefault(cam.ip, PtzController(cam.ip, cam.user, cam.password))
        step_ms = int(ptz.STEP_INTERVAL * 1000) + 30

        def run_step(i):
            if i >= len(queue):
                self._set_ptz_position(dev_id, target_pan, target_tilt)
                self._flash(f"Predefinição aplicada em {name}.")
                return

            def on_result(ok):
                if not ok:
                    _dispatcher.invoke.emit(
                        lambda _: self._flash(f"PTZ falhou em {name} (predefinição incompleta)"), None)

            if not ctrl.move(queue[i], on_result):
                QTimer.singleShot(100, lambda: run_step(i))  # passo manual em andamento
                return
            QTimer.singleShot(step_ms, lambda: run_step(i + 1))

        run_step(0)

    def _goto_preset_cloud(self, stream, dev_id, queue, target_pan, target_tilt, name):
        step_ms = int(ptz.STEP_INTERVAL * 1000) + 30

        def run_step(i):
            if i >= len(queue):
                self._set_ptz_position(dev_id, target_pan, target_tilt)
                self._flash(f"Predefinição aplicada em {name}.")
                return
            if not stream.ptz(queue[i]):
                self._flash(f"PTZ pela nuvem falhou em {name} (predefinição incompleta)")
                return
            self._cloud_ptz_last[dev_id] = time.monotonic()
            QTimer.singleShot(step_ms, lambda: run_step(i + 1))

        run_step(0)

    def save_ptz_preset(self, slot: str):
        ch = self.selected
        if not ch or not ch.device:
            self._flash("Selecione um canal com câmera aberta.")
            return
        dev_id = ch.device["devId"]
        pan, tilt = self._ptz_position(dev_id)
        store.save_ptz_preset(dev_id, slot, pan, tilt)
        self._flash(f"Predefinição {slot} salva ({ch.device['remarkName']}).")
        self._refresh_preset_buttons()

    def delete_ptz_preset(self, slot: str):
        ch = self.selected
        if not ch or not ch.device:
            return
        store.delete_ptz_preset(ch.device["devId"], slot)
        self._flash(f"Predefinição {slot} apagada.")
        self._refresh_preset_buttons()

    def _preset_menu(self, slot: str, btn: QToolButton):
        ch = self.selected
        menu = QMenu(self)
        menu.addAction(f"Salvar posição atual em {slot}", lambda: self.save_ptz_preset(slot))
        if ch and ch.device and slot in store.get_ptz_presets(ch.device["devId"]):
            menu.addAction(f"Apagar predefinição {slot}", lambda: self.delete_ptz_preset(slot))
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def screenshot(self):
        ch = self.selected
        frame = ch.video.videoSink().videoFrame() if ch else None
        if not ch or not ch.device or frame is None or not frame.isValid():
            self._flash("Nada para capturar no canal selecionado.")
            return
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        path = os.path.join(SCREENSHOT_DIR, f"{ch.device['remarkName']}-{time.strftime('%Y%m%d-%H%M%S')}.png")
        frame.toImage().save(path)
        self._flash(f"Salvo em {path}")

    def toggle_mute(self):
        ch = self.selected
        if not ch or not ch.device:
            self._flash("Selecione um canal com câmera aberta.")
            return
        if not ch.has_audio():
            self._flash("Esta câmera não está enviando áudio (ainda).")
            return
        ch.set_audio_device(QMediaDevices.defaultAudioOutput())
        ch.set_muted(not ch.is_muted())
        self.select_channel(ch)
        if not ch.is_muted():
            self._flash(f"Som ligado em: {ch.audio.device().description()}")

    def _follow_default_output(self):
        """O QAudioOutput não acompanha troca de saída (ex.: fone Bluetooth conectado depois)."""
        device = QMediaDevices.defaultAudioOutput()
        for ch in self.channels:
            ch.set_audio_device(device)

    def toggle_quality(self):
        if self.selected:
            self.selected.toggle_quality()
            self.select_channel(self.selected)
            self._save_grid()

    def closeEvent(self, event):
        for ch in self.channels:
            ch.stop()
        p2p.shutdown()
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
        else:
            super().keyPressEvent(event)


def main():
    global _dispatcher
    app = QApplication(sys.argv)
    app.setApplicationName("Câmeras Yoosee")
    theme = store.get_setting("theme", "dark")
    apply_theme(app, theme)
    _dispatcher = _Dispatcher()
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
