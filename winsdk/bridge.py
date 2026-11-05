"""Ponte para câmeras fora da rede local. Roda no Python *Windows*, dentro do Wine.

Usa a API C++ (ABI MSVC x64) da IotVideoSDK.dll do app oficial via ctypes:
IoTVideoSdk::Register -> um IoTVideoPlayer por câmera, cada um com um VideoDecoder
falso cujo send_packet recebe o H.265/H.264 comprimido e o repassa por TCP local.

Um processo por conta: o servidor derruba logins simultâneos da mesma conta,
então todas as câmeras abertas dessa conta compartilham um único Register.

Uso: python.exe bridge.py <sdk_dir> <conta.json>
stdin (uma linha por comando; EOF encerra):
    open <canal> <devId> <porta_tcp_video> [porta_tcp_audio]
    close <canal>
    ptz <canal> <0-3>
stdout: logs do próprio SDK (incluem o access_token em claro) -> o lado Linux descarta.
stderr: "<canal> STATE playing|video|audio <8 bytes hex>|reconnecting", "<canal> ERROR <msg>", "<canal> PTZ <rc>",
        "* ERROR <msg>" para falhas da conta inteira.
"""
import ctypes
import json
import os
import socket
import sys
import threading
import time
from ctypes import CFUNCTYPE, c_char_p, c_int, c_uint, c_ushort, c_void_p

sdk_dir, account_path = sys.argv[1], sys.argv[2]
os.chdir(sdk_dir)
os.add_dll_directory(sdk_dir)
sdk = ctypes.WinDLL(os.path.join(sdk_dir, "IotVideoSDK.dll"))
crt = ctypes.CDLL("ucrtbase")
crt.malloc.restype = c_void_p
crt.malloc.argtypes = [ctypes.c_size_t]
crt.calloc.restype = c_void_p
crt.calloc.argtypes = [ctypes.c_size_t, ctypes.c_size_t]

DEFINITION_HD = 2
PLAYER_PREPARED = 3
PLAYER_STOPPED = 7
PTZ_PAYLOAD = '{{"type":2,"data":{{"dir":{}}}}}'

out_lock = threading.Lock()


def report(channel: str, line: str) -> None:
    with out_lock:
        sys.stderr.write(f"{channel} {line}\n")
        sys.stderr.flush()


def fn(name, restype, *argtypes):
    f = sdk[name]
    f.restype = restype
    f.argtypes = argtypes
    return f


def std_string(value: bytes) -> int:
    """std::string MSVC no heap do CRT: parâmetros por valor são destruídos pelo callee."""
    obj = crt.calloc(1, 32)
    if len(value) <= 15:
        ctypes.memmove(obj, value, len(value))
        capacity = 15
    else:
        data = crt.malloc(len(value) + 1)
        ctypes.memmove(data, value + b"\0", len(value) + 1)
        c_void_p.from_address(obj).value = data
        capacity = len(value)
    ctypes.c_size_t.from_address(obj + 16).value = len(value)
    ctypes.c_size_t.from_address(obj + 24).value = capacity
    return obj


get_instance = fn("?getInstance@IoTVideoSdk@@SAPEAV1@XZ", c_void_p)
register = fn("?Register@IoTVideoSdk@@QEAAHV?$basic_string@DU?$char_traits@D@std@@V?$allocator@D@2@@std@@AEBV23@@Z",
              c_int, c_void_p, c_void_p, c_void_p)
player_ctor = fn("??0IoTVideoPlayer@@QEAA@XZ", c_void_p, c_void_p)
set_decoder = fn("?setVideoDecoder@IoTVideoPlayer@@QEAAXPEAVVideoDecoder@@@Z", None, c_void_p, c_void_p)
set_audio_decoder = fn("?setAudioDecoder@IoTVideoPlayer@@QEAAXPEAVAudioDecoder@@@Z", None, c_void_p, c_void_p)
set_resource = fn("?setDataResource@IoTVideoPlayer@@QEAAXPEADW4CONNECT_TYPE@@W4DEFINITION@@G@Z",
                  None, c_void_p, c_char_p, c_int, c_int, c_ushort)
prepare = fn("?prepare@IoTVideoPlayer@@QEAAXXZ", None, c_void_p)
play = fn("?play@IoTVideoPlayer@@QEAAXXZ", None, c_void_p)
stop = fn("?stop@IoTVideoPlayer@@QEAAXXZ", None, c_void_p)
get_state = fn("?getPlayState@IoTVideoPlayer@@QEAAHXZ", c_int, c_void_p)
send_user_data = fn("?sendUserData@IoTVideoPlayer@@QEAAHPEAEH@Z", c_int, c_void_p, c_char_p, c_int)

sdk_lock = threading.RLock()  # serializa as chamadas de controle dos players


def has_sps(packet: bytes) -> bool:
    """Contém parâmetros de sequência? (H.264 SPS = 7; H.265 VPS/SPS = 32/33)."""
    i = packet.find(b"\x00\x00\x01")
    while i != -1 and i + 3 < len(packet):
        header = packet[i + 3]
        if header & 0x1F == 7 or (header >> 1) & 0x3F in (32, 33):
            return True
        i = packet.find(b"\x00\x00\x01", i + 3)
    return False


channels: dict[str, "Channel"] = {}
decoders: dict[int, "Channel"] = {}
audio_decoders: dict[int, "Channel"] = {}


class Channel:
    def __init__(self, name: str, dev_id: str, port: int, audio_port: int | None = None):
        self.name = name
        self.dev_id = dev_id
        self.sink = socket.create_connection(("127.0.0.1", port))
        self.audio_sink = socket.create_connection(("127.0.0.1", audio_port)) if audio_port else None
        self.audio_started = False
        self.closed = False
        # Algumas câmeras (H.264, GOP de ~6 s) começam no meio do GOP: sem descartar até
        # o primeiro SPS, o probe do ffmpeg/QMediaPlayer desiste antes de achar um keyframe.
        self.waiting_keyframe = True
        self.decoder = crt.calloc(1, 512)
        c_void_p.from_address(self.decoder).value = ctypes.addressof(vtable)
        self.player = crt.calloc(1, 65536)
        # mesmo formato de vtable do vídeo: o áudio comprimido é repassado sem decodificar
        self.audio_decoder = crt.calloc(1, 512)
        c_void_p.from_address(self.audio_decoder).value = ctypes.addressof(audio_vtable)
        with sdk_lock:
            player_ctor(self.player)
            set_decoder(self.player, self.decoder)
            if self.audio_sink:
                set_audio_decoder(self.player, self.audio_decoder)

    def start(self) -> bool:
        """O login no servidor P2P é assíncrono: repete o prepare até o player ficar pronto."""
        for _ in range(30):
            if self.closed:
                return False
            with sdk_lock:
                set_resource(self.player, self.dev_id.encode(), 0, DEFINITION_HD, 0)
                prepare(self.player)
                self.waiting_keyframe = True
            for _ in range(100):
                time.sleep(0.05)
                state = get_state(self.player)
                if state == PLAYER_PREPARED:
                    with sdk_lock:
                        play(self.player)
                    report(self.name, "STATE playing")
                    return True
                if state == PLAYER_STOPPED:
                    break
            with sdk_lock:
                stop(self.player)
            time.sleep(1)
        return False

    def run(self):
        """Thread do canal: conecta e reconecta enquanto não for fechado."""
        connected = self.start()
        while connected and not self.closed:
            time.sleep(3)
            if not self.closed and get_state(self.player) == PLAYER_STOPPED:
                report(self.name, "STATE reconnecting")
                connected = self.start()
        if not self.closed:
            report(self.name, "ERROR connect")
            self.close()

    def on_packet(self, data_ptr: int, size: int):
        if self.closed:
            return
        if self.waiting_keyframe:
            if not has_sps(ctypes.string_at(data_ptr, min(size, 4096))):
                return
            self.waiting_keyframe = False
            report(self.name, "STATE video")
        try:
            self.sink.sendall(ctypes.string_at(data_ptr, size))
        except OSError:  # o ffmpeg do lado Linux fechou: encerra só este canal
            threading.Thread(target=self.close, daemon=True).start()

    def on_audio_packet(self, data_ptr: int, size: int):
        if self.closed or not self.audio_sink:
            return
        if not self.audio_started:
            self.audio_started = True
            report(self.name, "STATE audio " + ctypes.string_at(data_ptr, min(size, 8)).hex())
        try:
            self.audio_sink.sendall(ctypes.string_at(data_ptr, size))
        except OSError:  # o ffmpeg de áudio fechou: segue só com vídeo
            self.audio_sink = None

    def ptz(self, direction: str):
        payload = PTZ_PAYLOAD.format(direction).encode()
        with sdk_lock:
            rc = send_user_data(self.player, payload, len(payload))
        report(self.name, f"PTZ {rc}")

    def close(self):
        if self.closed:
            return
        self.closed = True
        decoders.pop(self.decoder, None)
        audio_decoders.pop(self.audio_decoder, None)
        if channels.get(self.name) is self:
            del channels[self.name]
        with sdk_lock:
            stop(self.player)
        for sock in (self.sink, self.audio_sink):
            try:
                if sock:
                    sock.close()
            except OSError:
                pass
        # player/decoder não são liberados: o SDK ainda pode tocar neles após o stop.


# vtable do VideoDecoder (ordem confirmada no VideoDecoderH264 da DLL):
# 0 dtor, 1 init(AVHeader*), 2 send_packet(AVData*), 3 receive_frame(AVData*), 4 release()
def on_dtor(this, _flags):
    return this


def on_init(_this, _header):
    pass


def on_send(this, avdata):
    data_ptr = c_void_p.from_address(avdata).value
    size = ctypes.c_uint32.from_address(avdata + 8).value
    channel = decoders.get(this)
    if channel and data_ptr and size:
        channel.on_packet(data_ptr, size)
    return 0


def on_audio_send(this, avdata):
    data_ptr = c_void_p.from_address(avdata).value
    size = ctypes.c_uint32.from_address(avdata + 8).value
    channel = audio_decoders.get(this)
    if channel and data_ptr and size:
        channel.on_audio_packet(data_ptr, size)
    return 0


def on_receive(_this, _frame):
    return -11  # AVERROR(EAGAIN): nada decodificado aqui, a decodificação é no lado Linux


def on_release(_this):
    pass


callbacks = [
    CFUNCTYPE(c_void_p, c_void_p, c_uint)(on_dtor),
    CFUNCTYPE(None, c_void_p, c_void_p)(on_init),
    CFUNCTYPE(c_int, c_void_p, c_void_p)(on_send),
    CFUNCTYPE(c_int, c_void_p, c_void_p)(on_receive),
    CFUNCTYPE(None, c_void_p)(on_release),
]
vtable = (c_void_p * len(callbacks))(*[ctypes.cast(cb, c_void_p).value for cb in callbacks])
audio_callbacks = [callbacks[0], callbacks[1], CFUNCTYPE(c_int, c_void_p, c_void_p)(on_audio_send),
                   callbacks[3], callbacks[4]]
audio_vtable = (c_void_p * len(audio_callbacks))(*[ctypes.cast(cb, c_void_p).value for cb in audio_callbacks])

with open(account_path) as f:
    account = json.load(f)
if register(get_instance(), std_string(account["access_id"].encode()),
            std_string(account["access_token"].encode())) != 0:
    report("*", "ERROR register")
    os._exit(1)


def open_channel(name: str, dev_id: str, port: str, audio_port: str | None = None):
    old = channels.get(name)
    if old:
        old.close()
    try:
        channel = Channel(name, dev_id, int(port), int(audio_port) if audio_port else None)
    except OSError:
        report(name, "ERROR sink")
        return
    channels[name] = channel
    decoders[channel.decoder] = channel
    audio_decoders[channel.audio_decoder] = channel
    channel.run()


for line in sys.stdin:
    parts = line.split()
    if len(parts) in (4, 5) and parts[0] == "open":
        threading.Thread(target=open_channel, args=parts[1:], daemon=True).start()
    elif len(parts) == 2 and parts[0] == "close" and parts[1] in channels:
        threading.Thread(target=channels[parts[1]].close, daemon=True).start()
    elif len(parts) == 3 and parts[0] == "ptz" and parts[1] in channels and parts[2] in ("0", "1", "2", "3"):
        threading.Thread(target=channels[parts[1]].ptz, args=(parts[2],), daemon=True).start()
os._exit(0)
