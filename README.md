# 📦 Servidor de Armazenamento para Rede Local

Servidor em **Python + Flask** com página web completa para guardar e
gerenciar arquivos pelo navegador, acessível por **qualquer aparelho na
mesma rede Wi-Fi** (celular, PC, tablet, TV…).

Feito especialmente para rodar no **Termux (Android)**, mas também funciona
em Windows, Linux e macOS.

---

## ✨ Funcionalidades

| Ação | Como fazer na interface |
|---|---|
| **Upload** | Botão "Enviar arquivos" ou **arraste e solte** arquivos na página (com barra de progresso) |
| **Download** | Botão ⬇️ no arquivo |
| **Criar pasta** | Botão "Nova pasta" |
| **Apagar pasta** | Botão 🗑️ (apaga tudo dentro dela) |
| **Criar arquivo** | Botão "Novo arquivo" (permite já escrever o conteúdo) |
| **Renomear** | Botão ✏️ em arquivos e pastas |
| **Mudar o tipo (extensão)** | Botão 🏷️ no arquivo — mostra prévia do novo nome em tempo real |
| **Editar texto** | Clique no nome de um `.txt`, `.py`, `.json` etc. e edite direto no navegador |
| **Ver mídia** | Clique no nome de imagens, vídeos, áudios e PDFs para pré-visualizar |

Navegação com breadcrumb ("Início › pasta › subpasta") e botão Voltar do
Android funcionando (histórico no navegador).

---

## 📱 Como rodar no Termux (Android)

1. Instale o [Termux](https://f-droid.org/packages/com.termux/) (recomendado: versão do F-Droid).

2. Abra o Termux e prepare o Python:

   ```bash
   pkg update -y && pkg upgrade -y
   pkg install -y python
   ```

3. Copie o arquivo `server.py` para o celular (ex.: para `~/server.py`) e rode:

   ```bash
   python server.py
   ```

   Na primeira execução o próprio script **instala o Flask automaticamente**
   (caso não esteja presente).

4. O terminal vai mostrar os endereços, por exemplo:

   ```
   ============================================================
     SERVICO DE ARMAZENAMENTO - REDE LOCAL
   ============================================================
     Pasta:  /data/data/com.termux/files/home/storage
     Local:  http://127.0.0.1:8080
     Rede:   http://192.168.0.15:8080
   ============================================================
   ```

5. No celular ou em outro aparelho da mesma rede, abra no navegador:
   **`http://192.168.0.15:8080`** (use o IP que apareceu no seu terminal).

### Dicas para Termux

- Para guardar os arquivos na memória compartilhada do Android (visível
  para outros apps), libere o acesso e aponte a pasta:

  ```bash
  termux-setup-storage
  python server.py --dir ~/storage/shared/MeusArquivos
  ```

- Para o Android não suspender o servidor com a tela apagada:

  ```bash
  termux-wake-lock
  ```

- Para parar o servidor: `Ctrl + C`.

---

## 💻 Como rodar no Windows / Linux / macOS

```bash
python server.py
```

(No Windows, se `python` não funcionar, use `py server.py`.)

---

## ⚙️ Opções de linha de comando

```bash
python server.py                      # porta 8080, pasta ./storage ao lado do script
python server.py --port 9090          # muda a porta
python server.py --dir /caminho/pasta # define a pasta de armazenamento
python server.py --host 0.0.0.0       # interface de rede (padrão: todas)
```

Também aceita a variável de ambiente `STORAGE_PORT`.

---

## 🔒 Segurança

- **Não há senha.** Qualquer aparelho conectado à mesma rede pode acessar,
  baixar, editar e apagar os arquivos — foi feito para uso pessoal em rede
  doméstica.
- Proteções contra caminhos maliciosos (`../`) e nomes de arquivo inválidos
  já estão implementadas no servidor.
- **Não exponha este servidor na internet** (sem abrir portas no roteador).

---

## 📄 Arquivos

- `server.py` — o servidor completo (interface web incluída; arquivo único).
- `storage/` — criada automaticamente na primeira execução; é onde os
  arquivos ficam guardados.
