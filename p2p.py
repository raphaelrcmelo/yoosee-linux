"""Câmeras fora da rede local, via SDK oficial (IotVideoSDK.dll) rodando no Wine.

winsdk/bridge.py entrega o vídeo comprimido por TCP local; um ffmpeg o reempacota
em MPEG-TS com timestamps de chegada e serve em UDP local para o QMediaPlayer.
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

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.environ.get("YOOSEE_RUNTIME", os.path.expanduser("~/.local/share/yoosee-viewer"))


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


def _free_port(kind: int) -> int:
    with socket.socket(socket.AF_INET, kind) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


class RemoteStream:
    """Uma câmera remota: um canal na ponte da conta + um ffmpeg de remux para o vídeo
    e outro para o áudio (AAC/ADTS). Separados para o vídeo não esperar por câmeras sem áudio."""

    def __init__(self, account_file: str, dev_id: str, on_event=None):
        self.account_file = account_file
        self.dev_id = dev_id
        self.on_event = on_event
        self.channel = str(next(_channel_ids))
        self.bridge: _Bridge | None = None
        self.ffmpeg: subprocess.Popen | None = None
        self.audio_ffmpeg: subprocess.Popen | None = None
        self.url: str | None = None
        self.audio_url: str | None = None

    @staticmethod
    def _remux(tcp_port: int, udp_port: int, input_args: list[str]) -> subprocess.Popen:
        return subprocess.Popen(
            ["ffmpeg", "-loglevel", "error", "-use_wallclock_as_timestamps", "1",
             "-fflags", "nobuffer", "-flags", "low_delay", *input_args,
             "-i", f"tcp://127.0.0.1:{tcp_port}?listen=1",
             "-c", "copy", "-muxdelay", "0", "-flush_packets", "1",
             "-f", "mpegts", f"udp://127.0.0.1:{udp_port}?pkt_size=1316"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def start(self) -> str:
        tcp_port, udp_port = _free_port(socket.SOCK_STREAM), _free_port(socket.SOCK_DGRAM)
        audio_tcp, audio_udp = _free_port(socket.SOCK_STREAM), _free_port(socket.SOCK_DGRAM)
        self.ffmpeg = self._remux(tcp_port, udp_port, ["-probesize", "2M", "-analyzeduration", "2M"])
        self.audio_ffmpeg = self._remux(audio_tcp, audio_udp, ["-f", "aac"])
        _wait_listening(tcp_port)
        _wait_listening(audio_tcp)
        self.bridge = _bridge_for(self.account_file)
        self.bridge.attach(self.channel, self._event)
        self.bridge.send(f"open {self.channel} {self.dev_id} {tcp_port} {audio_tcp}")
        self.url = f"udp://127.0.0.1:{udp_port}"
        self.audio_url = f"udp://127.0.0.1:{audio_udp}"
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
        for proc in (self.ffmpeg, self.audio_ffmpeg):
            if proc and proc.poll() is None:
                proc.kill()
        self.ffmpeg = self.audio_ffmpeg = None


def _wait_listening(port: int, timeout: float = 3) -> None:
    """Espera o ffmpeg abrir a porta TCP, para a ponte não conectar antes dele."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open("/proc/net/tcp") as f:
                if any(line.split()[1].endswith(f":{port:04X}") and line.split()[3] == "0A"
                       for line in f.readlines()[1:]):
                    return
        except OSError:
            return
        time.sleep(0.05)
