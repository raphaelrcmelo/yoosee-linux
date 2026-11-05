"""Constantes do app. Dados de cada usuário (contas, câmeras, senhas) ficam em CONFIG_DIR, nunca aqui."""
import os

DEFAULT_RTSP_USER = "admin"
STREAMS = {"main": "onvif1", "sub": "onvif2"}

CONFIG_DIR = os.path.expanduser("~/.config/yoosee-viewer")
SCREENSHOT_DIR = os.path.expanduser("~/Pictures/yoosee-screenshots")
