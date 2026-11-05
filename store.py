"""Persistência local (~/.config/yoosee-viewer, arquivos 0600): contas e câmeras da LAN."""
import json
import os
from dataclasses import asdict, dataclass

from settings import CONFIG_DIR, DEFAULT_RTSP_USER

ACCOUNTS_DIR = os.path.join(CONFIG_DIR, "accounts")
ACTIVE_FILE = os.path.join(CONFIG_DIR, "active_account")
CAMERAS_FILE = os.path.join(CONFIG_DIR, "cameras.json")


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
