"""Câmeras fora da rede local, via SDK oficial (IotVideoSDK.dll) rodando no Wine.

winsdk/bridge.py entrega o vídeo/áudio já comprimidos (H.264/H.265, AAC) por TCP
local; _RawRelay repassa os bytes crus, sem remux, direto pro QMediaPlayer (os
demuxers dele reconhecem o formato sem precisar de contêiner).
Runtime (Python Windows, prefixo Wine, DLLs) em ~/.local/share/yoosee-viewer/.
"""
import atexit
import glob
import itertools
import os
import shutil
import socket
import subprocess
import threading
import time

from settings import user_base_dir

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.environ.get("YOOSEE_RUNTIME",
                             os.path.join(user_base_dir("XDG_DATA_HOME", "~/.local/share"), "yoosee-viewer"))


def find_wine() -> tuple[str | None, str | None]:
    """(executável, diretório do runner Bottles ou None). Ordem: $YOOSEE_WINE, wine do sistema, runners do Bottles."""
    if os.environ.get("YOOSEE_WINE"):
        return os.environ["YOOSEE_WINE"], None
    for name in ("wine64", "wine"):
        path = shutil.which(name)
        if path:
            return path, None
    for base in ("~/.var/app/com.usebottles.bottles/data/bottles/runners", "~/.local/share/bottles/runners"):
        for runner in sorted(glob.glob(os.path.join(os.path.expanduser(base), "*")), reverse=True):
            for exe in ("wine64", "wine"):
                path = os.path.join(runner, "bin", exe)
                if os.access(path, os.X_OK):
                    return path, runner
    return None, None


WINE, WINE_RUNNER = find_wine()
WIN_PYTHON = os.path.join(RUNTIME_DIR, "winpython", "python.exe")
SDK_DIR = os.path.join(RUNTIME_DIR, "sdk")
BRIDGE = os.path.join(PROJECT_DIR, "winsdk", "bridge.py")

# Só existe dentro do Flatpak (empacotado em build-time, junto com o Wine embutido).
# Fora dele, quem cuida do winpython é o install.sh --cloud (baixa com checksum verificado).
BUNDLED_WINPYTHON = "/app/share/yoosee-viewer-assets/winpython"
RUNTIME_MSVC_DLLS = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")


def ensure_winpython() -> bool:
    """Garante o Python Windows embeddable em RUNTIME_DIR/winpython. True se já existia ou foi copiado."""
    if os.path.exists(WIN_PYTHON):
        return True
    if os.path.isdir(BUNDLED_WINPYTHON):
        shutil.copytree(BUNDLED_WINPYTHON, os.path.join(RUNTIME_DIR, "winpython"), dirs_exist_ok=True)
        return os.path.exists(WIN_PYTHON)
    return False


def _wine_system32() -> str | None:
    """.../lib/wine/x86_64-windows do Wine em uso, se for o embutido no Flatpak (traz suas próprias DLLs MSVC)."""
    if not WINE or WINE_RUNNER:
        return None
    candidate = os.path.join(os.path.dirname(os.path.dirname(WINE)), "lib", "wine", "x86_64-windows")
    return candidate if os.path.isdir(candidate) else None


def import_sdk(dll_path: str) -> tuple[bool, str]:
    """Copia um IotVideoSDK.dll já extraído (de uma instalação ou instalador oficial do Yoosee)
    para SDK_DIR, junto com as DLLs do runtime MSVC (do próprio Wine embutido, se disponível,
    senão da mesma pasta do arquivo escolhido). Não redistribui nada: o arquivo vem do usuário."""
    if not os.path.basename(dll_path).lower() == "iotvideosdk.dll":
        return False, "Selecione o arquivo IotVideoSDK.dll."
    if not ensure_winpython():
        return False, "Python para Windows não encontrado (reinstale o aplicativo)."
    os.makedirs(SDK_DIR, mode=0o700, exist_ok=True)
    shutil.copy2(dll_path, os.path.join(SDK_DIR, "IotVideoSDK.dll"))
    source_dir = _wine_system32() or os.path.dirname(dll_path)
    missing = []
    for name in RUNTIME_MSVC_DLLS:
        src = os.path.join(source_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(SDK_DIR, name))
        else:
            missing.append(name)
    if missing:
        return False, f"IotVideoSDK.dll copiado, mas faltam: {', '.join(missing)}."
    return True, "Câmeras pela nuvem configuradas."

PTZ_CODES = {"left": 0, "right": 1, "up": 2, "down": 3}


def available() -> bool:
    return WINE is not None and all(os.path.exists(p) for p in (WIN_PYTHON, os.path.join(SDK_DIR, "IotVideoSDK.dll"), BRIDGE))


def _wine_path(path: str) -> str:
    return "Z:" + path.replace("/", "\\")


def _wine_env() -> dict:
    env = dict(os.environ)
    env.update({
        "WINEARCH": "win64",
        "WINEPREFIX": os.path.join(RUNTIME_DIR, "wineprefix"),
        "WINEDEBUG": os.environ.get("YOOSEE_WINEDEBUG", "-all"),
        "WINEDLLOVERRIDES": "winemenubuilder=;mscoree=;mshtml=",
    })
    if WINE_RUNNER:  # runners do Bottles trazem as próprias libs
        env["LD_LIBRARY_PATH"] = ":".join(os.path.join(WINE_RUNNER, p) for p in
                                          ("lib", "lib/wine/x86_64-unix", "lib32/wine/i386-unix"))
    return env


class _Bridge:
    """Um processo da ponte por conta (o servidor derruba logins simultâneos da mesma conta)."""

    IDLE_SECONDS = 20  # mantém o login vivo um pouco para trocas rápidas de câmera

    def __init__(self, account_file: str):
        self.account_file = account_file
        self.listeners: dict[str, callable] = {}
        self.lock = threading.Lock()
        self._idle_timer: threading.Timer | None = None
        self.proc = subprocess.Popen(
            [WINE, WIN_PYTHON, BRIDGE, _wine_path(SDK_DIR), _wine_path(account_file)],
            cwd=SDK_DIR, env=_wine_env(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        threading.Thread(target=self._read_events, daemon=True).start()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, line: str) -> bool:
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def _read_events(self):
        debug = os.environ.get("YOOSEE_BRIDGE_LOG")
        debug_file = open(debug, "a") if debug else None
        for line in self.proc.stderr:
            channel, _, event = line.strip().partition(" ")
            if not event.startswith(("STATE ", "ERROR ", "PTZ ")):
                if debug_file:
                    debug_file.write(line)
                    debug_file.flush()
                continue
            with self.lock:
                targets = list(self.listeners.values()) if channel == "*" else \
                    [self.listeners[channel]] if channel in self.listeners else []
            for callback in targets:
                callback(event)
        with self.lock:  # processo morreu: avisa quem ainda estava assistindo
            targets = list(self.listeners.values())
            self.listeners.clear()
        for callback in targets:
            callback("ERROR bridge")

    def attach(self, channel: str, callback):
        with self.lock:
            self.listeners[channel] = callback
            if self._idle_timer:
                self._idle_timer.cancel()
                self._idle_timer = None

    def detach(self, channel: str):
        with self.lock:
            self.listeners.pop(channel, None)
            if not self.listeners:
                self._idle_timer = threading.Timer(self.IDLE_SECONDS, self._stop_if_idle)
                self._idle_timer.daemon = True
                self._idle_timer.start()

    def _stop_if_idle(self):
        with self.lock:
            idle = not self.listeners
        if idle:
            self.kill()

    def kill(self):
        if self.alive():
            self.proc.kill()
        with _bridges_lock:
            if _bridges.get(self.account_file) is self:
                del _bridges[self.account_file]


_bridges: dict[str, _Bridge] = {}
_bridges_lock = threading.Lock()
_channel_ids = itertools.count(1)

# Mandar o SDK conectar várias câmeras quase ao mesmo tempo (mesma conta) faz
# algumas travarem sem conectar nunca (testado: 2 de 4 ficavam presas; espaçando,
# as 4 conectam). Fila simples: um "open" por vez, com um intervalo mínimo entre eles.
CLOUD_OPEN_SPACING = 3.0
_cloud_open_lock = threading.Lock()
_next_cloud_open = 0.0


def _throttle_cloud_open(bridge: "_Bridge", line: str) -> None:
    def worker():
        with _cloud_open_lock:
            global _next_cloud_open
            wait = max(0.0, _next_cloud_open - time.monotonic())
            _next_cloud_open = time.monotonic() + wait + CLOUD_OPEN_SPACING
        if wait:
            time.sleep(wait)
        bridge.send(line)

    threading.Thread(target=worker, daemon=True).start()


def _bridge_for(account_file: str) -> _Bridge:
    with _bridges_lock:
        bridge = _bridges.get(account_file)
        if bridge is None or not bridge.alive():
            bridge = _bridges[account_file] = _Bridge(account_file)
        return bridge


@atexit.register
def shutdown():
    """Encerra todas as pontes (chamado ao fechar o app; também roda no atexit)."""
    with _bridges_lock:
        bridges = list(_bridges.values())
    for bridge in bridges:
        bridge.kill()


class _RawRelay:
    """Repassa bytes crus (H.264/H.265 Annex-B ou AAC/ADTS) da ponte (TCP, conecta como
    cliente) direto para o QMediaPlayer (TCP, também conecta como cliente) — sem nenhum
    ffmpeg/remux no meio. Os demuxers "h264"/"hevc"/"aac" do FFmpeg reconhecem o formato
    pelos próprios bytes, sem precisar de contêiner; isso elimina uma rodada inteira de
    probe+remux, que custava vários segundos (o app oficial também decodifica direto,
    sem remuxar nada — essa etapa só existia aqui por nossa causa)."""

    MAX_BUFFER = 4_000_000  # evita crescer sem limite se o player nunca conectar

    def __init__(self):
        self._bridge_srv = self._listen()
        self._player_srv = self._listen()
        self.bridge_port = self._bridge_srv.getsockname()[1]
        self.player_port = self._player_srv.getsockname()[1]
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._player_conn: socket.socket | None = None
        self._bridge_conn: socket.socket | None = None
        self._stopped = False
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._accept_player, daemon=True).start()

    @staticmethod
    def _listen() -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return s

    def _run(self):
        # Todo envio pro player (daqui ou de _accept_player) acontece sob o mesmo lock,
        # pra garantir a ordem dos bytes: sem isso, o buffer acumulado antes do player
        # conectar podia ser mandado DEPOIS de bytes mais novos chegando em paralelo,
        # corrompendo o fluxo (o QMediaPlayer falhava com "Could not open file").
        try:
            self._bridge_srv.settimeout(60)
            self._bridge_conn, _ = self._bridge_srv.accept()
            self._bridge_conn.settimeout(None)
            while not self._stopped:
                data = self._bridge_conn.recv(65536)
                if not data:
                    break
                with self._lock:
                    if self._player_conn is None:
                        self._buffer.extend(data)
                        del self._buffer[:-self.MAX_BUFFER]
                    else:
                        try:
                            self._player_conn.sendall(data)
                        except OSError:
                            break
        except OSError:
            pass
        self.stop()

    def _accept_player(self):
        try:
            self._player_srv.settimeout(60)
            conn, _ = self._player_srv.accept()
            conn.settimeout(None)
        except OSError:
            return
        with self._lock:
            if self._stopped:
                conn.close()
                return
            pending = bytes(self._buffer)
            self._buffer.clear()
            self._player_conn = conn
            try:
                if pending:
                    conn.sendall(pending)
            except OSError:
                pass

    def stop(self):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            sockets = (self._bridge_conn, self._player_conn, self._bridge_srv, self._player_srv)
        for sock in sockets:
            try:
                if sock:
                    sock.close()
            except OSError:
                pass


class RemoteStream:
    """Uma câmera remota: um canal na ponte da conta + repasse direto (sem remux) para o
    QMediaPlayer, um para o vídeo e outro para o áudio (AAC/ADTS). Separados para o vídeo
    não esperar por câmeras sem áudio."""

    def __init__(self, account_file: str, dev_id: str, on_event=None):
        self.account_file = account_file
        self.dev_id = dev_id
        self.on_event = on_event
        self.channel = str(next(_channel_ids))
        self.bridge: _Bridge | None = None
        self.video_relay: _RawRelay | None = None
        self.audio_relay: _RawRelay | None = None
        self.url: str | None = None
        self.audio_url: str | None = None

    def start(self) -> str:
        self.video_relay = _RawRelay()
        self.audio_relay = _RawRelay()
        self.bridge = _bridge_for(self.account_file)
        self.bridge.attach(self.channel, self._event)
        # Pedir pro SDK abrir várias câmeras quase ao mesmo tempo (mesma conta) faz
        # algumas travarem sem nunca conectar — espaça os comandos "open" de verdade.
        _throttle_cloud_open(self.bridge, f"open {self.channel} {self.dev_id} "
                             f"{self.video_relay.bridge_port} {self.audio_relay.bridge_port}")
        self.url = f"tcp://127.0.0.1:{self.video_relay.player_port}"
        self.audio_url = f"tcp://127.0.0.1:{self.audio_relay.player_port}"
        return self.url

    def _event(self, event: str):
        if self.on_event:
            self.on_event(event)

    def ptz(self, direction: str) -> bool:
        if not self.bridge or not self.bridge.alive():
            return False
        return self.bridge.send(f"ptz {self.channel} {PTZ_CODES[direction]}")

    def stop(self):
        if self.bridge:
            self.bridge.send(f"close {self.channel}")
            self.bridge.detach(self.channel)
            self.bridge = None
        for relay in (self.video_relay, self.audio_relay):
            if relay:
                relay.stop()
        self.video_relay = self.audio_relay = None
