#!/usr/bin/env python3
"""Câmeras Yoosee — cliente Linux (PySide6), layout inspirado no app oficial.

Vídeo: RTSP direto da câmera via QMediaPlayer (backend FFmpeg do Qt).
PTZ: ONVIF (ptz.py). Contas/lista de dispositivos: nuvem Yoosee (yoosee_api.py).
"""
import os
import sys
import threading
import time
from urllib.parse import quote

# o backend FFmpeg do Qt despeja a URL RTSP (com senha) no stderr ao abrir o stream
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.ffmpeg*=false")

import urllib3
from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
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
# em vez do cinza/azul neutro de antes.
ACCENT = "#ff8a3d"       # laranja — ações principais, seleção, destaque
ACCENT_2 = "#e8455f"     # rosa/vermelho — segundo tom da identidade, usado com moderação
BG = "#201d1c"           # fundo quente (não cinza puro)
BG_PANEL = "#2a2522"
BG_RAISED = "#352e2a"
BG_RAISED_HOVER = "#443a35"
BORDER = "#453c37"
TEXT = "#efe6e0"
TEXT_MUTED = "#a89a92"
STYLE = f"""
QWidget {{ background: {BG}; color: {TEXT}; font-size: 13px; }}
#topbar {{ background: {BG_PANEL}; border-bottom: 1px solid {BORDER}; }}
#bottombar {{ background: {BG_PANEL}; border-radius: 10px; border: 1px solid {BORDER}; }}
#sidebar {{ background: {BG_PANEL}; border-right: 1px solid {BORDER}; }}
#sidebarHeader {{ background: transparent; padding: 10px 4px 6px 4px; font-weight: 600;
                  color: {TEXT_MUTED}; letter-spacing: 1px; text-transform: uppercase; font-size: 11px; }}
#appTitle {{ font-size: 22px; font-weight: 800; color: {ACCENT}; }}
QListWidget {{ background: transparent; border: none; outline: none; }}
QListWidget::item {{ padding: 10px 10px; margin: 2px 6px; border-radius: 8px; }}
QListWidget::item:hover {{ background: {BG_RAISED}; }}
QListWidget::item:selected {{ background: {ACCENT}; color: #1a1413; }}
#channel {{ background: black; border: 2px solid {BG_PANEL}; border-radius: 6px; }}
#channel[selected="true"] {{ border: 2px solid {ACCENT}; }}
#channelHeader {{ background: rgba(32,29,28,0.85); border-top-left-radius: 4px; border-top-right-radius: 4px; }}
#channelHeader QLabel {{ background: transparent; font-size: 12px; }}
#placeholder {{ background: #15100f; color: {TEXT_MUTED}; }}
QToolButton, QPushButton {{ background: {BG_RAISED}; border: 1px solid {BORDER}; border-radius: 8px;
                            padding: 7px 12px; color: {TEXT}; }}
QToolButton:hover, QPushButton:hover {{ background: {BG_RAISED_HOVER}; border-color: {ACCENT}; }}
QToolButton:pressed, QPushButton:pressed {{ background: {ACCENT}; color: #1a1413; border-color: {ACCENT}; }}
#dpad {{ background: {BG_RAISED}; border: 1px solid {BORDER}; border-radius: 74px; }}
#dpad QToolButton {{ background: transparent; border: none; font-size: 22px; padding: 2px; border-radius: 23px; }}
#dpad QToolButton:hover {{ background: rgba(255,138,61,0.18); color: {ACCENT}; }}
#dpad QToolButton:pressed {{ background: {ACCENT}; color: #1a1413; }}
#presetBtn {{ background: {BG_RAISED}; border: 1px solid {BORDER}; border-radius: 10px;
              font-size: 16px; font-weight: 700; color: {TEXT_MUTED}; }}
#presetBtn[preset="here"] {{ border: 2px solid {ACCENT}; color: {ACCENT}; background: rgba(255,138,61,0.12); }}
#bottomBtn {{ font-size: 15px; font-weight: 600; }}
#presetBtn:disabled {{ color: #5a504a; border-color: #352e2b; background: {BG_PANEL}; }}
#closeBtn {{ background: transparent; border: none; padding: 0 4px; color: {TEXT_MUTED}; }}
#closeBtn:hover {{ color: {ACCENT_2}; background: transparent; }}
QLineEdit, QComboBox {{ background: {BG_PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 7px 10px; }}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QMenu {{ background: {BG_PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 7px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT}; color: #1a1413; }}
"""


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
        self.account = store.Account(email=email, access_id=str(data["accessId"]), access_token=data["accessToken"])
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
        self.title = QLabel()
        self.state = QLabel()
        close_btn = QToolButton(objectName="closeBtn", text="✕")
        close_btn.setToolTip("Fechar canal")
        close_btn.clicked.connect(lambda: self.closed.emit(self))
        h = QHBoxLayout(header)
        h.setContentsMargins(6, 2, 2, 2)
        h.addWidget(self.title)
        h.addStretch()
        h.addWidget(self.state)
        h.addWidget(close_btn)
        self.header = header

        self.placeholder = QLabel(f"Canal {index + 1}", objectName="placeholder", alignment=Qt.AlignCenter)
        self.video = QVideoWidget()
        self.body = QStackedWidget()
        self.body.addWidget(self.placeholder)
        self.body.addWidget(self.video)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(self.body, 1)

        self.audio = QAudioOutput()
        self.audio.setMuted(True)
        self.player = QMediaPlayer()
        self.player.setVideoOutput(self.video)
        self.player.setAudioOutput(self.audio)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.errorOccurred.connect(self._on_error)
        # câmeras pela nuvem: o áudio (AAC) chega num stream separado do vídeo
        self.remote_audio = QAudioOutput()
        self.remote_audio.setMuted(True)
        self.audio_player = QMediaPlayer()
        self.audio_player.setAudioOutput(self.remote_audio)

        for w in (self, self.video, self.placeholder):
            w.installEventFilter(self)
        self._refresh_header()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress:
            self.clicked.emit(self)
        elif event.type() == QEvent.MouseButtonDblClick:
            self.double_clicked.emit(self)
        return False

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
        self.player.setSource(QUrl(rtsp_url(camera, self.quality)))
        self.body.setCurrentWidget(self.video)
        self.player.play()

    def _open_cloud(self, reason: str = ""):
        device, account_file = self.device, self.account_file
        self.lan = None
        if not account_file or not p2p.available():
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
            self.state.setText("erro")
            self.body.setCurrentWidget(self.placeholder)
            self.placeholder.setText(f"{self.device['remarkName']}\n\nFalha no vídeo: {message}")


class DPad(QFrame):
    """Emite `moved(direção, é_repetição)` ao clicar, e repetidamente enquanto o
    botão fica pressionado — a câmera não tem um comando de "parar" que funcione
    (ver ARQUITETURA.md), então "segurar para mover" é aproximado reenviando o
    comando de movimento no maior ritmo que a câmera aceita."""
    moved = Signal(str, bool)
    SIZE = 148
    BTN_SIZE = 46
    REPEAT_MS = 200  # só uma tentativa; quem decide se aceita é o lado que já sabe o cooldown real

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
        right.setContentsMargins(0, 0, 8, 8)
        right.setSpacing(8)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(0)
        right.addWidget(self.grid_host, 1)
        right.addWidget(self._build_bottombar())
        body.addLayout(right, 1)
        root.addLayout(body, 1)
        self.setCentralWidget(central)

        for ch in self.channels:
            ch.clicked.connect(self.select_channel)
            ch.double_clicked.connect(self.toggle_focus)
            ch.closed.connect(lambda c: c.stop())
            ch.message.connect(self._flash)
        self.apply_layout()
        self.select_channel(self.channels[0])

        QTimer.singleShot(0, self.startup)

    def _build_topbar(self):
        bar = QWidget(objectName="topbar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 8, 16, 8)
        h.addWidget(QLabel("Câmeras Yoosee", objectName="appTitle"))
        h.addStretch()
        self.account_btn = QToolButton()
        self.account_btn.setPopupMode(QToolButton.InstantPopup)
        self.account_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.account_btn.setIcon(QIcon.fromTheme("avatar-default", self.style().standardIcon(QStyle.SP_DirHomeIcon)))
        self.account_menu = QMenu(self)
        self.account_btn.setMenu(self.account_menu)
        h.addWidget(self.account_btn)
        return bar

    def _build_sidebar(self):
        side = QWidget(objectName="sidebar")
        side.setFixedWidth(260)
        v = QVBoxLayout(side)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        header = QWidget(objectName="sidebarHeader")
        hh = QHBoxLayout(header)
        hh.setContentsMargins(8, 4, 4, 4)
        hh.addWidget(QLabel("Dispositivos"))
        hh.addStretch()
        refresh = QToolButton(text="⟳")
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

    def _build_bottombar(self):
        bar = QWidget(objectName="bottombar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(14, 10, 14, 10)
        h.setSpacing(12)
        st = self.style()

        stop = self._bottombar_btn(icon=st.standardIcon(QStyle.SP_MediaStop))
        stop.setToolTip("Parar canal selecionado")
        stop.clicked.connect(self.stop_selected)
        h.addWidget(stop)

        self.dpad = DPad()
        self.dpad.moved.connect(self.ptz_move)
        h.addWidget(self.dpad)

        self.preset_btns: dict[str, QToolButton] = {}
        presets = QWidget()
        pg = QGridLayout(presets)
        pg.setContentsMargins(10, 0, 0, 0)
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
        h.addWidget(presets)

        shot = self._bottombar_btn(text="Foto")
        shot.setToolTip("Capturar imagem do canal selecionado")
        shot.clicked.connect(self.screenshot)
        h.addWidget(shot)

        self.message = QLabel()
        h.addWidget(self.message)
        h.addStretch()

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

        full = self._bottombar_btn(icon=QIcon.fromTheme("view-fullscreen", st.standardIcon(QStyle.SP_TitleBarMaxButton)))
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
        self._autoplay()

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

    def _autoplay(self):
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
            self.grid.addWidget(self.focused, 0, 0)
            self.focused.show()
            return
        cols = {1: 1, 4: 2, 9: 3}[self.layout_size]
        for i, ch in enumerate(self.channels[: self.layout_size]):
            self.grid.addWidget(ch, i // cols, i % cols)
            ch.show()
        for ch in self.channels[self.layout_size:]:
            ch.stop()
        self.layout_btn.setText(str(self.layout_size))

    def cycle_layout(self):
        idx = self.LAYOUTS.index(self.layout_size)
        self.layout_size = self.LAYOUTS[(idx + 1) % len(self.LAYOUTS)]
        self.focused = None
        self.apply_layout()
        if self.selected not in self.channels[: self.layout_size]:
            self.select_channel(self.channels[0])

    def toggle_focus(self, ch):
        self.focused = None if self.focused is ch else ch
        self.apply_layout()

    def stop_selected(self):
        if self.selected:
            self.selected.stop()
            self._refresh_preset_buttons()

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

    CLOUD_NUDGE_SPACING = 0.6  # segundos; mesmo ritmo usado ao reproduzir predefinições pela nuvem

    def ptz_move(self, direction: str, is_repeat: bool = False):
        """`is_repeat`: disparo automático de "segurar o botão" (DPad.moved) — nesse caso,
        um comando rejeitado (câmera ainda ocupada com o anterior) é normal e fica em
        silêncio; só um clique avulso rejeitado mostra aviso."""
        ch = self.selected
        if ch and ch.remote:
            dev_id = ch.device["devId"]
            now = time.monotonic()
            # a nuvem não tem trava de "ocupado" própria (ver PtzController pra rede local);
            # sem isso, segurar o botão inundaria a ponte de comandos mais rápido do que a
            # câmera aceita.
            if now - self._cloud_ptz_last.get(dev_id, 0.0) < self.CLOUD_NUDGE_SPACING:
                return
            if not ch.remote.ptz(direction):
                if not is_repeat:
                    self._flash("A conexão pela nuvem ainda não está pronta.")
                return
            self._cloud_ptz_last[dev_id] = now
            dx, dy = ptz.DIRECTIONS[direction]
            self._update_ptz_position(dev_id, dx, dy)  # unidade: 1 comando
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
                # STEP_SECONDS=1, então 1 clique local também vale "1 unidade" — a mesma
                # escala usada pela nuvem (ver _update_ptz_position mais abaixo).
                _dispatcher.invoke.emit(
                    lambda _: self._update_ptz_position(dev_id, dx * ptz.STEP_SECONDS, dy * ptz.STEP_SECONDS), None)
            else:
                _dispatcher.invoke.emit(lambda _: self._flash(f"PTZ falhou em {name}"), None)

        if not ctrl.move(direction, on_result) and not is_repeat:
            self._flash("Aguarde o movimento anterior terminar.")

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

    MAX_PRESET_SECONDS = 8.0
    MAX_PRESET_NUDGES = 20
    CLOUD_PRESET_STEP_MS = 600

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
        if ch.remote:
            steps = [(d, min(round(m), self.MAX_PRESET_NUDGES)) for d, m in axis_steps]
            self._goto_preset_cloud(ch.remote, dev_id, steps, target_pan, target_tilt, name)
        elif ch.lan:
            steps = [(d, min(m, self.MAX_PRESET_SECONDS)) for d, m in axis_steps]
            self._goto_preset_lan(ch, dev_id, steps, target_pan, target_tilt, name)
        else:
            self._flash("Câmera sem conexão ativa.")

    def _goto_preset_lan(self, ch, dev_id, steps, target_pan, target_tilt, name):
        cam = ch.lan
        ctrl = self.ptz.setdefault(cam.ip, PtzController(cam.ip, cam.user, cam.password))

        def run_step(i):
            if i >= len(steps):
                self._set_ptz_position(dev_id, target_pan, target_tilt)
                self._flash(f"Predefinição aplicada em {name}.")
                return
            direction, seconds = steps[i]

            def on_result(ok):
                if not ok:
                    _dispatcher.invoke.emit(
                        lambda _: self._flash(f"PTZ falhou em {name} (predefinição incompleta)"), None)
                    return
                _dispatcher.invoke.emit(
                    lambda _: QTimer.singleShot(int(seconds * 1000) + 300, lambda: run_step(i + 1)), None)

            if not ctrl.move(direction, on_result, seconds=seconds):
                self._flash("Aguarde o movimento anterior terminar.")

        run_step(0)

    def _goto_preset_cloud(self, stream, dev_id, steps, target_pan, target_tilt, name):
        queue = [direction for direction, count in steps for _ in range(int(count))]

        def run_step(i):
            if i >= len(queue):
                self._set_ptz_position(dev_id, target_pan, target_tilt)
                self._flash(f"Predefinição aplicada em {name}.")
                return
            if not stream.ptz(queue[i]):
                self._flash(f"PTZ pela nuvem falhou em {name} (predefinição incompleta)")
                return
            QTimer.singleShot(self.CLOUD_PRESET_STEP_MS, lambda: run_step(i + 1))

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
    app.setStyleSheet(STYLE)
    _dispatcher = _Dispatcher()
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
