# Flatpak — Câmeras Yoosee

Empacota o app como Flatpak **auto-contido**: Wine vem embutido, então quem instalar
não precisa de Wine/Bottles separado no sistema. Veja "Câmeras fora da rede" em
`../../ARQUITETURA.md` para o porquê do Wine existir aqui.

## Como funciona

- `runtime`/`sdk`: `org.freedesktop.Sdk` (não `Platform` — o app é Python puro e
  precisa do interpretador, que só o Sdk traz; mesmo padrão usado por outros apps
  Python no Flathub).
- `base: org.winehq.Wine` (ramo `wow64-25.08`, suporta 64 bits): o build do meu
  app nasce a partir do Wine oficial, incorporando os binários dele no `/app`
  final — o usuário instala um Flatpak só, sem Wine à parte.
- `krb5`: compilado da fonte (kerberos.org). Necessário porque `libQt6Network.so`
  (parte do PySide6-Addons) depende de `libgssapi_krb5.so.2`, que o runtime não
  tem — mesmo padrão usado por LibreOffice e GNOME Epiphany no Flathub.
- `winpython-embed`: o Python Windows embeddable (mesmo zip e checksum do
  `install.sh`), pré-empacotado em `/app/share/yoosee-viewer-assets/winpython`.
  `p2p.ensure_winpython()` copia daqui na primeira vez que a nuvem é usada.
- `python-deps`: PySide6-Essentials + PySide6-Addons (o Addons é quem traz o
  QtMultimedia de verdade — o Essentials só tem os `.pyi` de tipo) + requests,
  via wheels pinados (URL + sha256) baixados do PyPI.
- `yoosee-viewer`: os `.py` do app em si.

`p2p.py`/`winsdk/bridge.py` não precisaram de nenhuma mudança de código para
funcionar dentro do Flatpak: a detecção de Wine já tentava `shutil.which("wine")`
antes de cair para o Bottles, e dentro do sandbox `/app/bin` (onde mora o Wine
embutido) já está no `PATH`.

## O SDK proprietário (`IotVideoSDK.dll`)

Continua não sendo redistribuído (nem no Flatpak, nem em lugar nenhum do repo).
Dentro do app, o menu da conta tem **"Configurar câmeras pela nuvem…"**
(só aparece dentro do Flatpak, quando a nuvem ainda não está configurada): abre
um seletor de arquivo (via portal do Flatpak, sem precisar de permissão de
filesystem) para o usuário apontar o `IotVideoSDK.dll` que já tenha extraído de
uma instalação ou instalador oficial do Yoosee. `p2p.import_sdk()` copia o
arquivo e completa com as DLLs do runtime MSVC — tiradas do próprio Wine
embutido (`/app/lib/wine/x86_64-windows/`), que já as traz, sem precisar do
instalador oficial para isso.

## Build e teste local

Requer `flatpak` e o Flatpak `org.flatpak.Builder` (instalável via
`flatpak install flathub org.flatpak.Builder`, sem precisar de root) e os
runtimes `org.freedesktop.Sdk//25.08` + `org.winehq.Wine//wow64-25.08`.

```bash
cd packaging/flatpak
flatpak run org.flatpak.Builder --disable-rofiles-fuse --force-clean \
  --repo=/caminho/para/repo build-dir io.github.raphaelrcmelo.YooseeViewer.yml

flatpak remote-add --user --no-gpg-verify --if-not-exists yoosee-local /caminho/para/repo
flatpak install --user -y yoosee-local io.github.raphaelrcmelo.YooseeViewer
flatpak run io.github.raphaelrcmelo.YooseeViewer
```

`--disable-rofiles-fuse` é necessário em ambientes sem FUSE disponível (ex.:
dentro de outro sandbox). O build final gira em torno de 1 GB — a maior parte é
o Wine embutido e o PySide6 (Qt completo com QtMultimedia).

**Gotcha de teste**: ao reexportar para um repo diferente, atualize a URL do
remote antes de reinstalar (`flatpak remote-modify --url=... yoosee-local`) —
senão `flatpak install` continua lendo o repo antigo silenciosamente.
