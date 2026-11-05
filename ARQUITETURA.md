# Arquitetura

Cliente Linux não-oficial para câmeras Yoosee/Gwell. Documento técnico de como o
programa funciona por dentro, para quem for manter ou contribuir. Nenhum dado de
conta/câmera fica no código — tudo isso vive em `~/.config/yoosee-viewer/`.

## Visão geral

```
                 ┌───────────────────────────── app.py (PySide6) ─────────────────────────────┐
                 │  LoginDialog · MainWindow · grade de ChannelView · DPad · barra inferior     │
                 └───────┬───────────────────────┬────────────────────────────┬────────────────┘
                         │ REST (login/lista)     │ RTSP + ONVIF (rede local)  │ P2P (fora da rede)
                   yoosee_api.py              ptz.py / lan.py                  p2p.py
                                                                                 │
                                                                   winsdk/bridge.py (Wine, SDK oficial)
```

Cada câmera abre por um de dois caminhos, decidido em runtime:

- **Rede local** — a câmera responde no IP salvo (teste rápido de TCP 554). Vídeo
  por RTSP, PTZ por ONVIF. Tudo nativo, sem Wine.
- **Pela nuvem** — a câmera não está acessível na rede (fora de casa, sem VPN) ou
  nunca foi configurada na LAN. Usa o SDK oficial rodando no Wine (ver `p2p.py`).

## Módulos

| Arquivo | Papel |
|---|---|
| `app.py` | UI inteira em PySide6: contas, grade 1/4/9, barra de controles, PTZ, foto, som. |
| `yoosee_api.py` | API REST: login da conta e listagem de câmeras (assinatura do cliente PC). |
| `lan.py` | Descoberta na rede (broadcast), teste de alcance (`reachable`), detecção de qual IP é local. |
| `ptz.py` | PTZ na rede local via ONVIF (TCP 5000). |
| `store.py` | Persistência em `~/.config/yoosee-viewer/` (contas e câmeras), arquivos 0600. |
| `settings.py` | Só constantes; nenhum dado pessoal. |
| `p2p.py` | Lado Linux das câmeras remotas: ponte no Wine + ffmpeg de remux. |
| `winsdk/bridge.py` | Roda no Python Windows dentro do Wine; fala com a `IotVideoSDK.dll` oficial. |

## Rede local (nativo)

- **Vídeo**: RTSP em `rtsp://usuário:senha@IP:554/onvif1` (HD) ou `/onvif2` (SD),
  só por UDP (a câmera recusa TCP). A senha é a de "conexão NVR" do app do celular.
- **PTZ**: ONVIF no TCP 5000 (gSOAP). Quirks deste firmware:
  - Toda requisição exige WS-Security (UsernameToken PasswordDigest); sem isso o
    servidor fecha a conexão sem responder.
  - Só `ContinuousMove` funciona. `Stop`/`RelativeMove` derrubam a conexão, então o
    movimento para sozinho pelo `<Timeout>`.
  - Velocidade abaixo de 1.0 não move; usar ±1.0. Comandos sobrepostos são descartados.
- **Acesso remoto nativo**: ONVIF/RTSP só alcançam a câmera na LAN; de fora, via VPN
  (ex.: WireGuard) para chegar ao IP da câmera.

## Câmeras fora da rede (SDK oficial no Wine)

Em vez de reimplementar o protocolo P2P proprietário (Tencent IoTVideo / Gwell), o
programa usa a `IotVideoSDK.dll` do app oficial, que é a API pública do SDK. Ela roda
num **Python Windows embeddable** dentro de um **prefixo Wine próprio**, ambos em
`~/.local/share/yoosee-viewer/` (não toca no Bottles do app oficial).

`winsdk/bridge.py` chama a API C++ (ABI MSVC x64) via `ctypes`:

1. `IoTVideoSdk::getInstance()` → `Register(accessId, accessToken)` — os mesmos tokens
   do login REST. É assíncrono.
2. Um `IoTVideoPlayer` por câmera: `setDataResource(devId, 0, HD, 0)`, `prepare()`,
   `play()`. Como o `Register` é assíncrono, repete `prepare` até o estado ficar
   PREPARED (3) e só então chama `play`.
3. **Vídeo**: `setVideoDecoder` com um `VideoDecoder` falso (vtable de callbacks ctypes).
   O `send_packet` recebe o H.264/H.265 já comprimido e o repassa por TCP local; não
   decodifica nada no Windows (`receive_frame` devolve EAGAIN).
4. **Áudio**: `setAudioDecoder` com o mesmo truque. O áudio vem em AAC-LC (ADTS),
   16 kHz mono, repassado por outra conexão TCP local.
5. **PTZ**: `sendUserData` com o JSON `{"type":2,"data":{"dir":N}}` (0 esq, 1 dir,
   2 cima, 3 baixo).

Um processo de ponte **por conta** (o servidor derruba logins simultâneos da mesma
conta): todas as câmeras abertas da conta compartilham um `Register`, cada uma num
"canal". Comandos pela stdin da ponte: `open <canal> <devId> <porta_vídeo> [porta_áudio]`,
`close <canal>`, `ptz <canal> <0-3>`.

### Lado Linux (`p2p.py`)

Para cada câmera remota, dois `ffmpeg` fazem remux dos streams locais para MPEG-TS em
UDP, que o `QMediaPlayer` toca:

```
bridge (Wine) --TCP--> ffmpeg (-c copy -> mpegts) --UDP--> QMediaPlayer (vídeo)
             \--TCP--> ffmpeg (-f aac  -> mpegts) --UDP--> QMediaPlayer (áudio)
```

Vídeo e áudio são separados de propósito: se a câmera não mandar áudio, o vídeo não
espera por ele. Um `_Bridge` por conta, encerrado após ~20 s ocioso; `atexit` garante
o fim dos processos.

### Detalhes que custaram a achar

- **Keyframe**: algumas câmeras H.264 (GOP de ~6 s) começam no meio do GOP. Sem
  descartar os pacotes até o primeiro SPS, o probe do ffmpeg/QMediaPlayer desiste antes
  de achar um keyframe e o vídeo nunca aparece. A ponte segura até o 1º SPS e só então
  emite `STATE video`; o app só abre o player nesse momento.
- **Token no stdout**: o SDK escreve o `accessToken` em claro no stdout. A ponte manda
  eventos pelo stderr (`STATE …`/`ERROR …`/`PTZ …`) e o lado Linux descarta o stdout.
- **Reconexão**: se a ponte cair, cada câmera reconecta sozinha (até 3 tentativas).

## Fallback automático de rede

`ChannelView.open()` testa o alcance da câmera em segundo plano (`lan.reachable`, < 1 s):

- acessível → RTSP direto;
- inacessível e nuvem instalada → abre pela nuvem;
- se o RTSP falhar no meio do caminho, cai para a nuvem uma vez.

A lista lateral mostra onde cada câmera será aberta ("via VPN", "pela nuvem (fora da
rede)", etc.), atualizada por um teste de alcance em segundo plano.

## Predefinições de PTZ

Este firmware não confirma suporte a posição absoluta nem a presets de hardware
(`GetNodes`/`GetStatus`/`SetPreset` nunca foram testados com sucesso — só
`ContinuousMove` funciona). Por isso as predefinições (4 por câmera, botões 1–4 na
barra inferior) são um modelo **aproximado, rastreado só pelo app**:

- `MainWindow` acumula uma posição virtual `(pan, tilt)` por câmera, em
  `_ptz_cache`/`store.set_ptz_position`. A unidade depende do caminho de conexão:
  **segundos de `ContinuousMove`** na rede local, **nº de comandos** pela nuvem
  (lá não há controle de duração, só um nudge por clique). Por isso a chave de
  rastreamento é `f"{devId}:{'cloud' if remoto else 'lan'}"` — a mesma câmera tem
  posições e predefinições independentes para cada caminho.
- Cada clique manual no D-pad (`ptz_move`) soma ao acumulado da câmera ativa.
- "Salvar predefinição N" grava o acumulado atual nesse slot
  (`store.save_ptz_preset`).
- "Ir para N" calcula o delta entre o acumulado atual e o alvo salvo, e dispara os
  movimentos necessários num eixo por vez: um `ContinuousMove` com a duração exata
  (rede local, clampada em `MAX_PRESET_SECONDS`) ou uma sequência de nudges
  espaçados em `CLOUD_PRESET_STEP_MS` (nuvem, clampada em `MAX_PRESET_NUDGES`).

Limitação conhecida: se outro cliente (ex.: app do celular) mover a câmera, o
rastreamento deste app desalinha — basta salvar a predefinição de novo.

## Persistência

Tudo em `~/.config/yoosee-viewer/` com permissão 0600:

- `accounts/<accessId>.json` — uma conta (email, accessId, accessToken).
- `active_account` — qual conta está ativa.
- `cameras.json` — `devId → {ip, user, password}` das câmeras configuradas na LAN.
- `ptz_positions.json` — `"<devId>:<lan|cloud>" → {pan, tilt, presets: {"1".."4": [pan, tilt]}}`.

## Runtime da nuvem

Criado pelo `install.sh --cloud` em `~/.local/share/yoosee-viewer/`:

- `winpython/` — Python 3.12 embeddable (python.org, com checksum verificado).
- `wineprefix/` — prefixo Wine próprio.
- `sdk/` — `IotVideoSDK.dll` + runtime MSVC, extraídos de uma instalação oficial do
  Yoosee ou do instalador `.exe`. **Essas DLLs não são redistribuídas** com o projeto.

## Empacotamento Flatpak (`packaging/flatpak/`)

Alternativa ao `install.sh`: um Flatpak **auto-contido**, com o Wine embutido
(`base: org.winehq.Wine`) — quem instala não precisa de Wine/Bottles separado.
`p2p.py` não precisou de nenhuma mudança: a detecção de Wine já tentava
`shutil.which("wine")` antes do Bottles, e dentro do sandbox `/app/bin` (onde
mora o Wine embutido) já está no `PATH`. O `IotVideoSDK.dll` continua não sendo
redistribuído; dentro do app, o menu da conta ganha "Configurar câmeras pela
nuvem…" (só no Flatpak) para o usuário apontar o arquivo via seletor nativo.
Detalhes completos em `packaging/flatpak/README.md`.
