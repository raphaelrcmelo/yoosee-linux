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
  - Velocidade abaixo de 1.0 não move; usar ±1.0.
  - **Cada `ContinuousMove` é UM passo fixo do motor (~0,3 s de giro), qualquer que seja
    o `<Timeout>`** — medido no vídeo (ffmpeg `scene_score` no RTSP, que só aceita
    transporte UDP): Timeout de 1 s e de 3 s dão o mesmo passo. A câmera **enfileira**
    comandos e executa ~1 passo a cada 0,7–0,8 s quando chegam espaçados; a cada
    0,5 s ela **emenda** os passos num movimento quase contínuo (blocos medidos de
    até 1,6 s, cauda de 1–2 passos ao parar de mandar — reconfirmado 2026-11-12);
    a cada 0,3 s a fila dispara e ela continuou girando ~6 s depois do último
    comando. Por isso `ptz.HOLD_INTERVAL = 0.5` (segurar/cliques manuais) e
    `ptz.STEP_INTERVAL = 0.8` (presets, onde a contagem 1:1 importa mais).
  - O app oficial do Windows faz o mesmo: 1 clique = 1 comando (`{"type":2,"data":{"dir":N}}`),
    só tem sinais `onPtz*Click`, sem `setAutoRepeat` nem "segurar".
- **Acesso remoto nativo**: ONVIF/RTSP só alcançam a câmera na LAN; de fora, via VPN
  (ex.: WireGuard) para chegar ao IP da câmera.
- **Segurar o botão do D-pad**: como não há `Stop` que funcione, "mover continuamente"
  é aproximado por `DPad` tentando um passo a cada 50ms enquanto o botão fica
  pressionado; `MainWindow.ptz_move` só aceita um passo a cada `ptz.HOLD_INTERVAL`
  (trava de `PtzController` na rede local, `_cloud_ptz_last` pela nuvem — mesmo motor,
  mesmo limite). Nesse ritmo a câmera emenda os passos em movimento quase contínuo,
  com cauda de 1–2 passos ao soltar; mais rápido que isso a fila dela cresce e o
  movimento passa muito do ponto. Rejeições ficam em silêncio.

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

### Lado Linux (`p2p.py`) — repasse direto, sem remux

Os bytes crus (H.264/H.265 Annex-B, AAC/ADTS) vão da ponte direto pro `QMediaPlayer`,
**sem nenhum ffmpeg no meio**:

```
bridge (Wine) --TCP--> _RawRelay (vídeo) --TCP--> QMediaPlayer
             \--TCP--> _RawRelay (áudio) --TCP--> QMediaPlayer
```

`_RawRelay` é só um encaminhador de bytes em Python puro: dois sockets TCP (a ponte
conecta como cliente num, o `QMediaPlayer` conecta como cliente no outro) e uma thread
que repassa o que chega de um pro outro. Os demuxers `h264`/`hevc`/`aac` do FFmpeg
(usados internamente pelo `QMediaPlayer`) reconhecem o formato pelos próprios bytes,
sem precisar de contêiner nem de um `-f` explícito.

Isso substituiu uma versão anterior com **dois** `ffmpeg` de remux (um nosso + o do
próprio `QMediaPlayer`) — uma etapa inteira de probe/remux a mais que o app oficial
não tem. Removê-la cortou o tempo entre "o vídeo chegou" e o primeiro quadro na tela de
8-16 s para ~2-3 s. O que resta de lento (a negociação P2P em si, variável, às vezes
20-30 s neste SDK) é proprietário e fora do nosso controle.

Vídeo e áudio continuam em relés separados de propósito: se a câmera não mandar áudio,
o vídeo não espera por ele. Um `_Bridge` por conta, encerrado após ~20 s ocioso;
`atexit` garante o fim dos processos.

Abrir várias câmeras da mesma conta quase ao mesmo tempo trava algumas delas sem
nunca conectar (confirmado: testando sem espaçamento, metade ficava presa
indefinidamente). `_throttle_cloud_open` enfileira os comandos `open` com um intervalo
mínimo (`CLOUD_OPEN_SPACING`) entre eles — não ajuda a negociação em si ficar mais
rápida, mas evita que algumas câmeras travem de vez.

### Detalhes que custaram a achar

- **Keyframe sem SPS/PPS de verdade**: o jeito antigo de detectar o primeiro keyframe
  (buscar um padrão de bytes "parece SPS" numa janela de até 4096 bytes do pacote) dava
  **falso positivo** — um NAL de slice comum, grande, podia conter por acaso uma
  sequência de bytes com a cara de um cabeçalho SPS/HEVC, disparando o início do envio
  num ponto onde nunca existia um SPS/PPS real. O player então recebia slices sem o
  parâmetro que as decodifica ("non-existing PPS referenced") e nunca abria. A correção:
  `iter_nals()` percorre os NALs pelos limites reais (posições dos start codes), guarda
  o SPS/PPS/VPS mais recente visto (um NAL de parâmetro de verdade é sempre curto, o que
  também descarta o falso positivo por tamanho) e só libera o encaminhamento a partir de
  um IDR genuíno, prefixando os parâmetros guardados.
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

- `MainWindow` acumula uma posição virtual `(pan, tilt)` **por câmera**
  (`_ptz_cache`/`store.get_ptz_position`/`store.set_ptz_position`, chave =
  `devId`), onde **1 passo aceito = 1 unidade**, não importa se foi um
  `ContinuousMove` (rede local) ou um comando da nuvem — os dois acionam o
  mesmo passo do motor. Isso faz uma predefinição salva pela
  rede local funcionar pela nuvem e vice-versa — a primeira versão deste
  recurso guardava posição e predefinição por caminho de conexão (separado
  para "lan"/"cloud"), e a mesma câmera trocando de caminho sozinha (ver
  "Fallback automático de rede" acima) fazia o botão "sumir" mesmo com o dado
  intacto no arquivo.
- Cada clique manual no D-pad (`ptz_move`) soma ao acumulado da câmera ativa.
- "Salvar predefinição N" grava o acumulado atual nesse slot
  (`store.save_ptz_preset(devId, slot, pan, tilt)`).
- "Ir para N" calcula o delta entre o acumulado atual e o alvo salvo, e
  manda N passos, um eixo por vez, espaçados em `ptz.STEP_INTERVAL` (até
  `MAX_PRESET_STEPS` por eixo), pela rede local ou pela nuvem. Versões antigas
  mandavam um único `ContinuousMove` com a duração total, o que na prática
  virava **um passo só** — por isso as predefinições "não iam" pra posição.

Limitação conhecida: se outro cliente (ex.: app do celular) mover a câmera, o
rastreamento deste app desalinha — basta salvar a predefinição de novo. A
equivalência "1 clique = 1 unidade" entre os dois caminhos também é uma
aproximação (não há como medir o ângulo real que cada um move fisicamente);
funciona bem na prática, mas pode acumular um pequeno desvio com o tempo.

## Persistência

Tudo em `~/.config/yoosee-viewer/` com permissão 0600:

- `accounts/<accessId>.json` — uma conta (email, accessId, accessToken).
- `active_account` — qual conta está ativa.
- `cameras.json` — `devId → {ip, user, password}` das câmeras configuradas na LAN.
- `ptz_positions.json` — rastreamento: `"<devId>" → {pan, tilt}`; predefinições:
  `"<devId>:presets" → {"1".."4": [pan, tilt]}`.

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
