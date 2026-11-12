#!/usr/bin/env python3
"""Cliente não-oficial da API REST do Yoosee (login + listagem de câmeras).

login_account() -- FUNCIONANDO, confirmado com conta real (code:0,
"Success", accessToken recebido). Formato do cliente PC (Yoosee.exe),
capturado ao vivo via mitmproxy (interceptação de rede transparente no
Yoosee.exe rodando em Wine/Bottles) -- sem Frida, sem tocar no binário.
Esse esquema é JSON puro, sem headers de assinatura.

device_list_pc() -- FUNCIONANDO, confirmado com conta real (code:0,
deviceList com as câmeras reais). Algoritmo de assinatura
(giote_user_signature/getStrToSign) reverso-engenheirado via Ghidra
(decompilação, Yoosee.exe real) e validado contra o servidor como
oráculo. Ver docs/API_YOOSEE.md pro processo completo.

device_list() (sem sufixo _pc) é o endpoint do cliente ANDROID, mantido só
por referência histórica -- não funciona sem a assinatura nativa do
Android (IP2PAlgorithm.sha1WithBase256), que é diferente do esquema PC e
não foi reverso-engenheirada.

As funções gen_anonymous_secure_key()/_anon_headers_and_sign() implementam
o esquema de LOGIN ANÔNIMO do cliente ANDROID (com.yoosee, v6.46.1) --
útil como referência mas não é o que é usado hoje (login_account() e
device_list_pc() usam o esquema do cliente PC). Ver docs/API_YOOSEE.md
para os detalhes completos de todos os esquemas.
"""
import hashlib
import hmac
import random
import time
import uuid

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://openapi-iot.cloudlinks.cn"

# Constantes do cliente ANDROID (com.yoosee) -- usam o esquema de
# assinatura HMAC (x-iotvideo-* headers). Mantidas por referencia/futuro
# uso, mas login_account() hoje usa o esquema do cliente PC (mais simples,
# ver abaixo), capturado via mitmproxy com o Yoosee.exe real rodando sob
# Wine/Bottles.
APP_ID_ANDROID = "d591b466644a0420e5f29aefb0cf0088"
APP_VERSION_ANDROID = "6.46.1"
PKG_NAME_ANDROID = "com.yoosee"
AREA = "cn"

# Constantes do cliente PC (Yoosee.exe, Windows) -- capturadas ao vivo via
# mitmproxy (interceptacao de rede transparente, nao precisou de Frida nem
# de nada dinamico no binario). Esse cliente usa um esquema BEM mais
# simples: POST JSON puro, sem nenhum header x-iotvideo-*/assinatura HMAC
# -- so os campos do corpo (incluindo appId/appToken) ja bastam pro
# servidor aceitar. appId e fixo (constante do app), appToken parece ser
# gerado por sessao mas nao foi validado contra um conteudo especifico.
APP_ID = "adf33ae6eaa1439b48841fc330ffef11"
APP_NAME = "Yoosee.PC"
PKG_NAME = "Yoosee.PC"
APP_VERSION = "10"
TERMINAL_OS_PC = 1

CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
XOR_CONST = 0x7E18FC2D035A4B69


def bkdr_hash(s: str) -> int:
    h = 0
    for b in s.encode("utf-8"):
        h = (h * 131 + b) & 0xFFFFFFFF
    return h


def tostr_32digit(value: int) -> str:
    v = value ^ XOR_CONST
    out = []
    shift = 60
    while shift >= 0:
        digit = (v >> shift) & 0x1F
        if digit != 0 or out:
            out.append(CROCKFORD32[digit])
        shift -= 5
    return "".join(out) if out else "0"


def _auto_xor_circular(data: bytearray):
    n = len(data)
    for i in range(n):
        data[i] ^= data[(i + 1) % n]


def gen_anonymous_secure_key(app_tag: str, terminal_os: int = 0):
    """Reimplementação de iot_gen_app_anonymous_secure_key (libiotvideomulti.so).

    Retorna (access_id, secret_key) tal como nAnonymousSecureKey() nativo.
    """
    value = (1 << 63) | (1 << 62) | (1 << 61)
    value |= random.getrandbits(5)  # bits [0:5)
    div37 = (terminal_os * 0x88888889) >> 37  # == terminal_os // 37
    value |= (div37 & 0x7FFFFFF) << 5  # bits [5:32)
    h = bkdr_hash(app_tag) & 0xFFFFFF
    value |= h << 32  # bits [32:56)

    value_bytes = bytearray(value.to_bytes(8, "little"))
    sub = bytearray(value_bytes[1:7])
    _auto_xor_circular(sub)
    value_bytes[1:7] = sub
    value_after_xor = int.from_bytes(value_bytes, "little")

    access_id = str(value_after_xor)
    msg = tostr_32digit(value_after_xor)
    digest = hmac.new(app_tag.encode("utf-8"), msg.encode("utf-8"), hashlib.md5).digest()
    secret_key = digest.hex()
    return access_id, secret_key


def hmac_sha1_base64(content: str, secret_key: str) -> str:
    import base64

    digest = hmac.new(secret_key.encode("utf-8"), content.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


class YooseeClient:
    def __init__(self, base_url: str = BASE_URL, app_tag: str = PKG_NAME_ANDROID + APP_VERSION_ANDROID):
        self.base_url = base_url
        self.app_tag = app_tag
        self.session = requests.Session()
        # o certificado de openapi-iot.cloudlinks.cn falha na validacao
        # estrita do Python ("Missing Authority Key Identifier") mesmo
        # sendo aceito por outros clientes (curl, o proprio app oficial) --
        # parece ser uma peculiaridade da cadeia de certificados do servidor,
        # nao um MITM. A conexao continua criptografada (TLS), so a
        # validacao da cadeia do certificado fica desativada.
        self.session.verify = False
        self.access_token = None

    def _anon_headers_and_sign(self, method: str, path: str, query: dict | None, body: str | None):
        access_id, secret_key = gen_anonymous_secure_key(self.app_tag, terminal_os=0)
        nonce = str(random.randint(0, 2**31 - 1))
        timestamp = str(int(time.time()))
        host = self.base_url.split("://", 1)[1]

        headers = {
            "x-iotvideo-accessid": access_id,
            "x-iotvideo-nonce": nonce,
            "x-iotvideo-timestamp": timestamp,
            "x-iotvideo-area": AREA,
            "x-iotvideo-appver": APP_VERSION_ANDROID,
            "x-iotvideo-appid": APP_ID_ANDROID,
        }

        sign_params = {}
        if method == "GET" and query:
            sign_params.update({k: v for k, v in query.items() if v})
        elif body:
            payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
            sign_params["payload"] = payload_hash

        sign_params["host"] = host
        sign_params["x-iotvideo-accessid"] = access_id
        sign_params["x-iotvideo-nonce"] = nonce
        sign_params["x-iotvideo-timestamp"] = timestamp
        sign_params["x-iotvideo-appid"] = APP_ID_ANDROID
        sign_params["x-iotvideo-appver"] = APP_VERSION_ANDROID

        content = "\n".join(f"{k}:{sign_params[k]}" for k in sorted(sign_params))
        signature = hmac_sha1_base64(content, secret_key)
        headers["x-iotvideo-signature"] = signature
        return headers

    def login_account(self, email: str, password: str | None = None, language: str = "en",
                      pwd_md5: str | None = None):
        """Login por e-mail (loginMode=email), formato do cliente PC.

        Capturado ao vivo via mitmproxy (interceptacao de rede transparente
        no trafego do Yoosee.exe real rodando em Wine/Bottles -- sem Frida,
        sem tocar no binario). Login real confirmado (code:0, "Success").

        Diferente do esquema Android: SEM headers x-iotvideo-*/assinatura
        HMAC, tudo vai so no corpo JSON. pwd = md5 hex da senha (confirmado
        batendo com o hash capturado do login real).
        """
        path = "/openapi/app/user/login/account"
        body_obj = {
            "action": "account",
            "apiVersion": "10",
            "appId": APP_ID,
            "appName": APP_NAME,
            # token de sessao -- o cliente real manda um hex de 64 chars por
            # requisicao; nao confirmei se o servidor valida o conteudo ou
            # so a presenca/formato. Gerado aqui como hex aleatorio do
            # mesmo tamanho pra imitar o formato observado.
            "appToken": hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
            "appVersion": APP_VERSION,
            "email": email,
            "language": language,
            "loginMode": "email",
            "pkgName": PKG_NAME,
            "pwd": pwd_md5 or hashlib.md5(password.encode("utf-8")).hexdigest(),
            "terminalOS": TERMINAL_OS_PC,
            "uniqueId": uuid.uuid4().hex,
        }
        import json

        body = json.dumps(body_obj, separators=(",", ":"))
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept-Language": "pt-BR,en,*",
        }
        resp = self.session.post(self.base_url + path, data=body, headers=headers, timeout=15)
        return resp

    def device_list(self, access_token: str, region: str = "us"):
        """Endpoint do cliente ANDROID (nao usado pelo PC). NAO FUNCIONA sem
        a assinatura autenticada nativa (IP2PAlgorithm.sha1WithBase256, nao
        reverso-engenheirada). Mantido so por referencia historica -- usar
        device_list_pc() abaixo, que implementa o esquema do cliente PC.
        """
        path = "/openapi/v2/user/device/list"
        params = {"accessToken": access_token, "region": region}
        resp = self.session.get(self.base_url + path, params=params, timeout=15)
        return resp

    def _giote_user_signature(self, content: str, access_token: str) -> str:
        """Reimplementacao de giote_user_signature (Yoosee.exe, 0x14017ed80),
        reverso-engenheirada via Ghidra e CONFIRMADA contra o servidor real
        em 2026-11-04 (device_list_pc() retornou code:0 com a lista real de
        cameras da conta).

        key = bytes.fromhex(access_token[96:128]) -- os ate 32 chars a
        partir do offset 96 do accessToken, decodificados de hex pra 16
        bytes crus (confirmado pelo range check `< 0x60` e o `min(len-96,32)`
        no disassembly de 0x14017ed80). accessToken precisa ter >=128 chars
        (checado antes de tudo, senao loga "accessToken is unvalid!").

        signature = base64(HMAC-SHA1(key, content)), onde `content` e
        getStrToSign() montado pelo chamador (ver device_list_pc).
        """
        import base64

        if len(access_token) < 128:
            raise ValueError(f"accessToken curto demais ({len(access_token)} < 128)")

        key = bytes.fromhex(access_token[96:128])
        digest = hmac.new(key, content.encode("utf-8"), hashlib.sha1).digest()
        return base64.b64encode(digest).decode("ascii")

    def device_list_pc(self, access_token: str, access_id: str, last_device_id: str = "0"):
        """Lista as cameras da conta logada -- esquema do cliente PC real.

        CONFIRMADO funcionando contra o servidor real em 2026-11-04 (ver
        docs/API_YOOSEE.md pro processo completo de reverse engineering +
        validacao via oraculo). Retorna code:0 com deviceList real.

        getStrToSign(): TODOS os parametros que vao na requisicao (tanto os
        "comuns" tipo appId/appToken quanto os especificos do endpoint tipo
        accessToken/lastDeviceId) MAIS o contexto HTTP (host,
        x-iotvideo-accessid, x-iotvideo-nonce, x-iotvideo-timestamp) entram
        no mesmo mapa, ordenados por chave, "chave:valor" por linha, unidos
        por "\\n" -- exatamente o mesmo esquema ja documentado pro Android,
        so que os query params do GET entram junto em vez de so os campos
        do body.
        """
        path = "/openapi/app/user/device/listDevice"
        host = self.base_url.split("://", 1)[1]
        timestamp = str(int(time.time()))
        nonce = str(random.randint(0, 2**31 - 1))

        query = {
            "accessToken": access_token,
            "lastDeviceId": last_device_id,
            "appToken": "60ded395c2bdad3a2610ac6150550f551bb911eb6e6f7106c40c3bb1457bb07d",
            "apiVersion": "10",
            "appId": APP_ID,
            "appName": APP_NAME,
            "appVersion": APP_VERSION,
            "language": "en",
            "pkgName": PKG_NAME,
            "terminalOS": str(TERMINAL_OS_PC),
        }
        sign_map = dict(query)
        sign_map["x-iotvideo-timestamp"] = timestamp
        sign_map["x-iotvideo-accessid"] = access_id
        sign_map["x-iotvideo-nonce"] = nonce
        sign_map["host"] = host
        content = "\n".join(f"{k}:{sign_map[k]}" for k in sorted(sign_map))
        signature = self._giote_user_signature(content, access_token)

        headers = {
            "x-iotvideo-accessid": access_id,
            "x-iotvideo-nonce": nonce,
            "x-iotvideo-timestamp": timestamp,
            "X-IotVideo-Signature": signature,
        }
        resp = self.session.get(self.base_url + path, headers=headers, params=query, timeout=15)
        return resp


if __name__ == "__main__":
    # teste rápido: só confirma que o algoritmo roda sem erro e imprime a
    # chave anônima gerada, pra inspeção manual
    aid, skey = gen_anonymous_secure_key("com.yoosee6.46.1", terminal_os=0)
    print("accessId:", aid)
    print("secretKey:", skey)
