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
from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QStackedWidget, QStyle, QToolButton, QVBoxLayout, QWidget,
)

import lan
import p2p
import store
from ptz import PtzController
from settings import DEFAULT_RTSP_USER, SCREENSHOT_DIR, STREAMS
from yoosee_api import YooseeClient

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ACCENT = "#2f7bf6"
STYLE = f"""
QWidget {{ background: #2b2d31; color: #e3e3e3; font-size: 13px; }}
#topbar, #bottombar {{ background: #1f2023; }}
#bottombar {{ border-radius: 6px; }}
#sidebar {{ background: #232428; }}
#sidebarHeader {{ background: #3a3c42; padding: 8px; font-weight: bold; }}
#appTitle {{ font-size: 22px; font-weight: bold; }}
QListWidget {{ background: #232428; border: none; }}
QListWidget::item {{ padding: 10px 8px; border-bottom: 1px solid #2f3136; }}
QListWidget::item:selected {{ background: #34363c; color: white; }}
#channel {{ background: black; border: 1px solid #2b2d31; }}
#channel[selected="true"] {{ border: 1px solid {ACCENT}; }}
#channelHeader {{ background: #1f2023; }}
#channelHeader QLabel {{ background: transparent; font-size: 12px; }}
#placeholder {{ background: black; color: #d0d0d0; }}
QToolButton, QPushButton {{ background: #3a3c42; border: none; border-radius: 4px; padding: 6px 10px; }}
QToolButton:hover, QPushButton:hover {{ background: #4a4d55; }}
QToolButton:pressed, QPushButton:pressed {{ background: {ACCENT}; }}
#dpad {{ background: #3a3c42; border-radius: 40px; }}
#dpad QToolButton {{ background: transparent; font-size: 14px; padding: 2px; }}
#dpad QToolButton:hover {{ color: {ACCENT}; }}
#closeBtn {{ background: transparent; padding: 0 4px; }}
QLineEdit, QComboBox {{ background: #1f2023; border: 1px solid #3a3c42; border-radius: 4px; padding: 6px; }}
QMenu {{ background: #2b2d31; border: 1px solid #3a3c42; }}
QMenu::item:selected {{ background: {ACCENT}; }}
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
    moved = Signal(str)

    def __init__(self):
        super().__init__(objectName="dpad")
        self.setFixedSize(80, 80)
        grid = QGridLayout(self)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setSpacing(0)
        for direction, glyph, row, col, tip in (
            ("up", "▲", 0, 1, "Cima"), ("left", "◀", 1, 0, "Esquerda"),
            ("right", "▶", 1, 2, "Direita"), ("down", "▼", 2, 1, "Baixo"),
        ):
            btn = QToolButton(text=glyph)
            btn.setToolTip(tip)
            btn.clicked.connect(lambda _=False, d=direction: self.moved.emit(d))
            grid.addWidget(btn, row, col, Qt.AlignCenter)


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

    def _build_bottombar(self):
        bar = QWidget(objectName="bottombar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(10, 6, 10, 6)
        st = self.style()

        stop = QToolButton(icon=st.standardIcon(QStyle.SP_MediaStop))
        stop.setToolTip("Parar canal selecionado")
        stop.clicked.connect(lambda: self.selected and self.selected.stop())
        h.addWidget(stop)

        self.dpad = DPad()
        self.dpad.moved.connect(self.ptz_move)
        h.addWidget(self.dpad)

        shot = QToolButton(text="Foto")
        shot.setToolTip("Capturar imagem do canal selecionado")
        shot.clicked.connect(self.screenshot)
        h.addWidget(shot)

        self.message = QLabel()
        h.addWidget(self.message)
        h.addStretch()

        self.mute_btn = QToolButton(icon=st.standardIcon(QStyle.SP_MediaVolumeMuted))
        self.mute_btn.setToolTip("Som do canal selecionado")
        self.mute_btn.clicked.connect(self.toggle_mute)
        h.addWidget(self.mute_btn)

        self.quality_btn = QToolButton(text="HD")
        self.quality_btn.setToolTip("Alternar HD/SD no canal selecionado")
        self.quality_btn.clicked.connect(self.toggle_quality)
        h.addWidget(self.quality_btn)

        self.layout_btn = QToolButton(text="4")
        self.layout_btn.setToolTip("Alternar grade 1 / 4 / 9")
        self.layout_btn.clicked.connect(self.cycle_layout)
        h.addWidget(self.layout_btn)

        full = QToolButton(icon=QIcon.fromTheme("view-fullscreen", st.standardIcon(QStyle.SP_TitleBarMaxButton)))
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

    def select_channel(self, ch):
        if self.selected is not None:
            self.selected.set_selected(False)
        self.selected = ch
        ch.set_selected(True)
        self.quality_btn.setText("HD" if ch.effective_quality() == "main" else "SD")
        self.mute_btn.setIcon(self.style().standardIcon(
            QStyle.SP_MediaVolumeMuted if ch.is_muted() else QStyle.SP_MediaVolume))

    # ---- controles ----

    def _flash(self, text: str):
        self.message.setText(text)
        QTimer.singleShot(3000, lambda: self.message.text() == text and self.message.setText(""))

    def ptz_move(self, direction: str):
        ch = self.selected
        if ch and ch.remote:
            if not ch.remote.ptz(direction):
                self._flash("A conexão pela nuvem ainda não está pronta.")
            return
        if not ch or not ch.lan:
            self._flash("Selecione um canal com câmera aberta.")
            return
        cam = ch.lan
        ctrl = self.ptz.setdefault(cam.ip, PtzController(cam.ip, cam.user, cam.password))
        name = ch.device["remarkName"]

        def on_result(ok):
            if not ok:
                _dispatcher.invoke.emit(lambda _: self._flash(f"PTZ falhou em {name}"), None)

        if not ctrl.move(direction, on_result):
            self._flash("Aguarde o movimento anterior terminar.")

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
