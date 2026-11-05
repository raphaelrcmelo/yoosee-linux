"""Persistência local (~/.config/yoosee-viewer, arquivos 0600): contas e câmeras da LAN."""
import json
import os
from dataclasses import asdict, dataclass

from settings import CONFIG_DIR, DEFAULT_RTSP_USER

ACCOUNTS_DIR = os.path.join(CONFIG_DIR, "accounts")
ACTIVE_FILE = os.path.join(CONFIG_DIR, "active_account")
CAMERAS_FILE = os.path.join(CONFIG_DIR, "cameras.json")
PTZ_FILE = os.path.join(CONFIG_DIR, "ptz_positions.json")
PTZ_SLOTS = ("1", "2", "3", "4")


@dataclass
class Account:
    email: str
    access_id: str
    access_token: str


@dataclass
class LanCamera:
    """Acesso direto (RTSP/ONVIF) a uma câmera da rede local; senha = a de "conexão NVR" do app Yoosee."""
    ip: str
    user: str = DEFAULT_RTSP_USER
    password: str = ""


def _write_private(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def account_path(access_id: str) -> str:
    return os.path.join(ACCOUNTS_DIR, f"{access_id}.json")


def list_accounts() -> list[Account]:
    if not os.path.isdir(ACCOUNTS_DIR):
        return []
    accounts = []
    for name in sorted(os.listdir(ACCOUNTS_DIR)):
        if name.endswith(".json"):
            with open(os.path.join(ACCOUNTS_DIR, name)) as f:
                accounts.append(Account(**json.load(f)))
    return accounts


def save_account(account: Account) -> None:
    for existing in list_accounts():
        if existing.email.lower() == account.email.lower() and existing.access_id != account.access_id:
            remove_account(existing.access_id)
    _write_private(account_path(account.access_id), json.dumps(asdict(account)))


def remove_account(access_id: str) -> None:
    try:
        os.remove(account_path(access_id))
    except FileNotFoundError:
        pass
    if get_active_id() == access_id:
        try:
            os.remove(ACTIVE_FILE)
        except FileNotFoundError:
            pass


def get_active_id() -> str | None:
    try:
        with open(ACTIVE_FILE) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def set_active_id(access_id: str) -> None:
    _write_private(ACTIVE_FILE, access_id)


def get_active_account() -> Account | None:
    active = get_active_id()
    accounts = list_accounts()
    for acc in accounts:
        if acc.access_id == active:
            return acc
    return accounts[0] if accounts else None


def load_cameras() -> dict[str, LanCamera]:
    """devId -> acesso na LAN; câmeras sem entrada são abertas pela nuvem."""
    try:
        with open(CAMERAS_FILE) as f:
            return {dev_id: LanCamera(**cfg) for dev_id, cfg in json.load(f).items()}
    except FileNotFoundError:
        return {}


def save_camera(dev_id: str, camera: LanCamera | None) -> None:
    cameras = load_cameras()
    if camera:
        cameras[dev_id] = camera
    else:
        cameras.pop(dev_id, None)
    _write_private(CAMERAS_FILE, json.dumps({d: asdict(c) for d, c in cameras.items()}, indent=2))


# --- Predefinições de PTZ ---------------------------------------------------
#
# A câmera não expõe posição absoluta nem presets confirmados (só ContinuousMove
# funciona; GetNodes/Stop/GetStatus derrubam a conexão neste firmware — ver
# ARQUITETURA.md). Por isso a posição é um modelo aproximado, só deste app:
# um acumulado de pan/tilt em "unidades de movimento" (segundos de ContinuousMove
# na rede local; nº de comandos na nuvem). Uma predefinição guarda esse acumulado
# no momento de salvar; "ir para" calcula a diferença e manda os movimentos
# necessários. Pode desalinhar se a câmera for movida por outro cliente (o app
# do celular, por ex.) — nesse caso, basta salvar a predefinição de novo.

def _load_ptz_state() -> dict:
    try:
        with open(PTZ_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save_ptz_state(state: dict) -> None:
    _write_private(PTZ_FILE, json.dumps(state, indent=2))


def get_ptz_position(dev_id: str) -> tuple[float, float]:
    entry = _load_ptz_state().get(dev_id, {})
    return entry.get("pan", 0.0), entry.get("tilt", 0.0)


def set_ptz_position(dev_id: str, pan: float, tilt: float) -> None:
    state = _load_ptz_state()
    entry = state.setdefault(dev_id, {})
    entry["pan"], entry["tilt"] = pan, tilt
    _save_ptz_state(state)


def get_ptz_presets(dev_id: str) -> dict[str, tuple[float, float]]:
    """slot ("1".."4") -> (pan, tilt); só os slots com predefinição salva."""
    entry = _load_ptz_state().get(dev_id, {})
    return {slot: tuple(pos) for slot, pos in entry.get("presets", {}).items()}


def save_ptz_preset(dev_id: str, slot: str, pan: float, tilt: float) -> None:
    state = _load_ptz_state()
    entry = state.setdefault(dev_id, {})
    entry.setdefault("presets", {})[slot] = [pan, tilt]
    _save_ptz_state(state)


def delete_ptz_preset(dev_id: str, slot: str) -> None:
    state = _load_ptz_state()
    entry = state.get(dev_id)
    if entry and "presets" in entry:
        entry["presets"].pop(slot, None)
        _save_ptz_state(state)
