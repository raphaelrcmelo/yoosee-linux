# Câmeras Yoosee para Linux

Cliente **não-oficial** para câmeras Yoosee/Gwell no Linux: vídeo ao vivo em HD,
PTZ (mover a câmera), várias contas e grade de 1/4/9 câmeras. Também abre câmeras
que estão em outro lugar ou foram compartilhadas com você.

> Não tem ligação com a Yoosee/Gwell. Use por sua conta e risco.

## Instalar

Requisitos: Linux x86_64, Python 3.10+ (com `venv`).

```bash
tar xzf yoosee-viewer.tar.gz      # ou: git clone <repositório>
cd yoosee-viewer
./install.sh
```

Depois abra **Câmeras Yoosee** no menu de aplicativos (ou rode `yoosee-viewer`).
Não precisa de `sudo`; tudo fica na sua pasta pessoal.

**Alternativa Flatpak**: `packaging/flatpak/` traz um empacotamento auto-contido,
com o Wine das câmeras fora da rede já embutido — não precisa instalar Wine ou
Bottles separado. Veja `packaging/flatpak/README.md`.

### Câmeras fora da sua rede (outra casa, compartilhadas)

Essas câmeras só abrem pelo SDK oficial da Yoosee, que é para Windows e roda aqui
via Wine. Instale antes:

- **Wine**, ou o [Bottles](https://flathub.org/apps/com.usebottles.bottles) pelo Flathub
- o instalador oficial para Windows, **Yoosee-x.y.z.exe**, baixado de
  <https://www.yoosee.co>. O script só tira dele a biblioteca do SDK.

```bash
./install.sh --cloud ~/Downloads/Yoosee-1.0.0.8.exe
```

Se o Yoosee oficial já estiver instalado no Wine/Bottles, basta `./install.sh --cloud`.

## Usar

1. Entre com o e-mail e a senha da sua conta Yoosee. Para adicionar outras contas,
   use o menu da conta no topo.
2. Clique duas vezes numa câmera para abrir. As setas e o teclado movem a câmera.
3. **Câmeras da sua rede local** abrem direto, mais rápido e sem passar pela nuvem.
   A primeira vez, clique com o botão direito na câmera e escolha
   **Configurar na rede local…**:
   - **IP**: o programa procura as câmeras da rede sozinho.
   - **Usuário**: `admin`.
   - **Senha**: a senha de "Conexão NVR" / "Senha do visitante" que aparece no app
     Yoosee do celular, nas configurações da câmera. Não é a senha da conta.

   Câmeras sem essa configuração abrem pela nuvem, se você rodou `--cloud`.

Fotos ficam em `~/Pictures/yoosee-screenshots`. As contas e câmeras ficam em
`~/.config/yoosee-viewer`, com permissão só para o seu usuário.

## Desinstalar

```bash
./uninstall.sh            # mantém contas/câmeras salvas
./uninstall.sh --purge    # apaga tudo
```

## Problemas comuns

| Sintoma | O que fazer |
|---|---|
| Câmera da rede local fica preta | Confira a senha NVR. A câmera aceita só um visualizador por vez, então feche o app do celular ou outro PC. |
| "Wine não encontrado" | Instale o `wine` pela sua distro ou o Bottles pelo Flathub. |
| Câmera remota demora | Normal na primeira conexão (5–15 s). O PTZ remoto tem ~1 s de atraso. |
