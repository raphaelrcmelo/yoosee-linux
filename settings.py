"""Constantes do app. Dados de cada usuário (contas, câmeras, senhas) ficam em CONFIG_DIR, nunca aqui."""
import os

DEFAULT_RTSP_USER = "admin"
STREAMS = {"main": "onvif1", "sub": "onvif2"}



def user_base_dir(xdg_var: str, legacy: str) -> str:
    """Pasta-base dos dados do usuário (config ou data).

    Dentro do Flatpak o "~/.config" e o "~/.local/share" literais são efêmeros — somem a cada
    execução; só persiste o que vai em $XDG_CONFIG_HOME / $XDG_DATA_HOME (~/.var/app/<id>/...).
    Fora do Flatpak mantém os caminhos de sempre, pra não deixar pra trás os dados de quem já usa.
    """
    if os.environ.get("FLATPAK_ID") and os.environ.get(xdg_var):
        return os.environ[xdg_var]
    return os.path.expanduser(legacy)


CONFIG_DIR = os.path.join(user_base_dir("XDG_CONFIG_HOME", "~/.config"), "yoosee-viewer")
SCREENSHOT_DIR = os.path.expanduser("~/Pictures/yoosee-screenshots")
