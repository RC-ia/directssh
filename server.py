#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Servidor de Armazenamento para Rede Local
==========================================

Servidor em Python + Flask que disponibiliza uma pagina web completa
para gerenciar arquivos em qualquer aparelho da mesma rede (Wi-Fi),
com visual no estilo do Explorador de Arquivos do Windows 10:

  - Upload de um ou varios arquivos (com arrastar e soltar e barra de progresso)
  - Download de arquivos
  - Criar pasta / apagar pasta (recursivo)
  - Criar arquivo de texto
  - Renomear arquivos e pastas
  - Alterar a extensao (tipo) de um arquivo
  - Pre-visualizar imagens, videos, audios e PDFs
  - Editar arquivos de texto direto no navegador
  - Barra de endereco com migalhas (breadcrumb), pesquisa, voltar/avancar/subir
  - Painel de navegacao lateral, colunas ordenaveis e menu de contexto

Feito para rodar no Termux (Android), mas funciona tambem em
Linux, Windows e macOS com Python 3.8+.

O script instala automaticamente o Flask caso ele nao esteja presente.

Uso:
    python server.py                      # porta 8080, pasta ./storage
    python server.py --port 9090
    python server.py --dir /sdcard/MeusArquivos
    python server.py --host 0.0.0.0 --port 8000

AVISO: nao existe senha. Qualquer aparelho na mesma rede pode acessar.
"""

import argparse
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

# Escreve acentuacao/emojis no console sem quebrar (Windows/Termux)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------------------
# 1) Dependencias: instala o Flask automaticamente se necessario
# ---------------------------------------------------------------------------

def _garantir_flask():
    try:
        import flask  # noqa: F401
        return
    except ImportError:
        pass

    print("[*] Flask nao encontrado. Instalando via pip (aguarde)...")

    tentativas = [
        [sys.executable, "-m", "pip", "install", "--upgrade", "flask"],
        [sys.executable, "-m", "pip", "install", "--user", "--upgrade", "flask"],
        [sys.executable, "-m", "pip", "install", "--user", "--break-system-packages", "--upgrade", "flask"],
    ]
    for cmd in tentativas:
        try:
            subprocess.check_call(cmd)
            print("[*] Flask instalado com sucesso.")
            return
        except Exception:
            continue

    # Ultima tentativa: habilita o pip (ensurepip) e tenta de novo
    try:
        subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "flask"])
        print("[*] Flask instalado com sucesso.")
        return
    except Exception:
        pass

    print()
    print("[!] Nao foi possivel instalar o Flask automaticamente.")
    print("    No Termux, rode manualmente:")
    print("        pkg install python")
    print("        pip install flask")
    sys.exit(1)


_garantir_flask()

from flask import Flask, Response, jsonify, request, send_file  # noqa: E402


# ---------------------------------------------------------------------------
# 2) Configuracao basica
# ---------------------------------------------------------------------------

RAIZ_PADRAO = Path(__file__).resolve().parent / "storage"
ROOT = RAIZ_PADRAO  # pasta raiz do armazenamento (definida de novo em main())

app = Flask(__name__, static_folder=None)

try:  # Flask >= 2.3
    app.json.ensure_ascii = False
except Exception:
    pass

# Alguns mimetypes que faltam em alguns sistemas
for _ext, _mime in {
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".m4v": "video/x-m4v",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".heic": "image/heic",
}.items():
    mimetypes.add_type(_mime, _ext)

MAX_TEXTO = 2 * 1024 * 1024  # limite para editar arquivos de texto (2 MB)
REGEX_EXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+\-]{0,15}$")
CARACTERES_INVALIDOS = '<>:"/\\|?*'


# ---------------------------------------------------------------------------
# 3) Utilitarios de arquivos
# ---------------------------------------------------------------------------

class ApiError(Exception):
    """Erro previsto da API, devolvido como JSON."""

    def __init__(self, mensagem, codigo=400):
        super().__init__(mensagem)
        self.code = codigo


def sanitizar_nome(nome):
    """Limpa um nome de arquivo/pasta, removendo caminhos e caracteres especiais."""
    nome = str(nome or "").replace("\\", "/").split("/")[-1]
    nome = "".join(c for c in nome if c not in CARACTERES_INVALIDOS and ord(c) >= 32)
    nome = nome.strip().rstrip(".").strip()
    if nome in ("", ".", ".."):
        return ""
    return nome[:180]


def caminho_seguro(rel):
    """Converte um caminho relativo (texto) em Path absoluto DENTRO da raiz."""
    rel = str(rel or "").replace("\\", "/").strip().strip("/")
    if rel in ("", "."):
        return ROOT
    p = (ROOT / rel).resolve()
    try:
        p.relative_to(ROOT)
    except ValueError:
        raise ApiError("Caminho fora da area de armazenamento.", 403)
    return p


def caminho_relativo(p):
    """Converte Path absoluto em caminho relativo com barras '/'."""
    rel = str(p.relative_to(ROOT)).replace("\\", "/")
    return "" if rel == "." else rel


def caminho_livre(p):
    """Devolve um caminho que ainda nao existe, adicionando ' (1)', ' (2)'..."""
    if not p.exists():
        return p
    i = 1
    while True:
        cand = p.parent / ("%s (%d)%s" % (p.stem, i, p.suffix))
        if not cand.exists():
            return cand
        i += 1


def corpo_json():
    """Le o corpo da requisicao como JSON (aceita form como fallback)."""
    dados = request.get_json(silent=True)
    if not isinstance(dados, dict):
        dados = request.form.to_dict()
    return dados


# ---------------------------------------------------------------------------
# 4) Tratamento de erros (sempre responde JSON)
# ---------------------------------------------------------------------------

@app.errorhandler(ApiError)
def _erro_api(e):
    return jsonify(ok=False, error=str(e)), e.code


@app.errorhandler(Exception)
def _erro_generico(e):
    try:
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return jsonify(ok=False, error=e.description or e.name), e.code
    except Exception:
        pass
    return jsonify(ok=False, error="Erro interno: %s" % e), 500


@app.after_request
def _sem_cache(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# 5) Rotas da API
# ---------------------------------------------------------------------------

@app.route("/")
def rota_index():
    return Response(INDEX_HTML, content_type="text/html; charset=utf-8")


@app.route("/api/info")
def api_info():
    try:
        uso = shutil.disk_usage(ROOT)
        livre, total = uso.free, uso.total
    except OSError:
        livre = total = 0
    return jsonify(ok=True, root=str(ROOT), free=livre, total=total, host=socket.gethostname())


@app.route("/api/list")
def api_list():
    p = caminho_seguro(request.args.get("path", ""))
    if not p.is_dir():
        raise ApiError("Pasta nao encontrada.", 404)

    entradas = []
    for item in sorted(p.iterdir(), key=lambda x: x.name.lower()):
        try:
            st = item.stat()
        except OSError:
            continue
        eh_dir = item.is_dir()
        entradas.append({
            "name": item.name,
            "is_dir": eh_dir,
            "size": None if eh_dir else st.st_size,
            "mtime": int(st.st_mtime * 1000),
        })

    # pastas primeiro, depois nome
    entradas.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return jsonify(ok=True, path=caminho_relativo(p), entries=entradas)


@app.route("/download")
def rota_download():
    p = caminho_seguro(request.args.get("path", ""))
    if not p.is_file():
        raise ApiError("Arquivo nao encontrado.", 404)
    inline = request.args.get("inline") == "1"
    return send_file(p, as_attachment=not inline, download_name=p.name, conditional=True)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    destino = caminho_seguro(request.form.get("path", ""))
    if not destino.is_dir():
        raise ApiError("Pasta de destino invalida.", 400)

    salvos = []
    for arq in request.files.getlist("files"):
        nome = sanitizar_nome(arq.filename)
        if not nome:
            continue
        alvo = caminho_livre(destino / nome)
        arq.save(str(alvo))
        salvos.append(alvo.name)

    if not salvos:
        raise ApiError("Nenhum arquivo recebido.", 400)
    return jsonify(ok=True, saved=salvos)


@app.route("/api/delete", methods=["POST"])
def api_delete():
    dados = corpo_json()
    p = caminho_seguro(dados.get("path", ""))
    if p == ROOT:
        raise ApiError("Nao e possivel apagar a raiz.", 400)
    if not p.exists():
        raise ApiError("Item nao encontrado.", 404)
    if p.is_dir():
        shutil.rmtree(p)
    else:
        p.unlink()
    return jsonify(ok=True)


@app.route("/api/mkdir", methods=["POST"])
def api_mkdir():
    dados = corpo_json()
    pai = caminho_seguro(dados.get("path", ""))
    if not pai.is_dir():
        raise ApiError("Pasta atual invalida.", 400)
    nome = sanitizar_nome(dados.get("name", ""))
    if not nome:
        raise ApiError("Nome invalido.", 400)
    alvo = pai / nome
    if alvo.exists():
        raise ApiError("Ja existe um item com esse nome.", 409)
    alvo.mkdir()
    return jsonify(ok=True, name=nome)


@app.route("/api/newfile", methods=["POST"])
def api_newfile():
    dados = corpo_json()
    pai = caminho_seguro(dados.get("path", ""))
    if not pai.is_dir():
        raise ApiError("Pasta atual invalida.", 400)
    nome = sanitizar_nome(dados.get("name", ""))
    if not nome:
        raise ApiError("Nome invalido.", 400)
    alvo = pai / nome
    if alvo.exists():
        raise ApiError("Ja existe um item com esse nome.", 409)
    alvo.write_text(str(dados.get("content", "")), encoding="utf-8")
    return jsonify(ok=True, name=nome)


@app.route("/api/rename", methods=["POST"])
def api_rename():
    dados = corpo_json()
    orig = caminho_seguro(dados.get("path", ""))
    if orig == ROOT:
        raise ApiError("Item invalido.", 400)
    if not orig.exists():
        raise ApiError("Item nao encontrado.", 404)
    novo_nome = sanitizar_nome(dados.get("new_name", ""))
    if not novo_nome:
        raise ApiError("Nome invalido.", 400)
    novo = orig.parent / novo_nome
    if novo == orig:
        return jsonify(ok=True, name=novo.name)
    if novo.exists():
        raise ApiError("Ja existe um item com esse nome.", 409)
    orig.rename(novo)
    return jsonify(ok=True, name=novo.name)


@app.route("/api/change_ext", methods=["POST"])
def api_change_ext():
    dados = corpo_json()
    p = caminho_seguro(dados.get("path", ""))
    if p == ROOT or not p.exists():
        raise ApiError("Item nao encontrado.", 404)
    if p.is_dir():
        raise ApiError("Pastas nao possuem extensao.", 400)

    ext = str(dados.get("ext", "")).strip().lstrip(".").strip()
    if ext and not REGEX_EXT.match(ext):
        raise ApiError("Extensao invalida (use letras, numeros, _ + -).", 400)

    novo = p.with_name(p.stem + ("." + ext if ext else ""))
    if novo == p:
        return jsonify(ok=True, name=novo.name)
    if novo.exists():
        raise ApiError("Ja existe um arquivo com essa extensao.", 409)
    p.rename(novo)
    return jsonify(ok=True, name=novo.name)


@app.route("/api/read")
def api_read():
    p = caminho_seguro(request.args.get("path", ""))
    if not p.is_file():
        raise ApiError("Arquivo nao encontrado.", 404)
    if p.stat().st_size > MAX_TEXTO:
        raise ApiError("Arquivo muito grande para editar (limite de 2 MB). Baixe-o.", 413)
    try:
        texto = p.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError("O arquivo nao e texto (UTF-8).", 415)
    return jsonify(ok=True, name=p.name, content=texto)


@app.route("/api/save", methods=["POST"])
def api_save():
    dados = corpo_json()
    p = caminho_seguro(dados.get("path", ""))
    if p == ROOT or not p.parent.is_dir():
        raise ApiError("Caminho invalido.", 400)
    if p.is_dir():
        raise ApiError("Esse caminho e uma pasta.", 400)
    p.write_text(str(dados.get("content", "")), encoding="utf-8")
    return jsonify(ok=True)


# ---------------------------------------------------------------------------
# 6) Interface web (pagina unica, estilo Explorador de Arquivos do Windows 10)
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Armazenamento - Explorador de Arquivos</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#128193;</text></svg>">
<style>
*{box-sizing:border-box}
html,body{margin:0;padding:0;height:100%}
body{background:#fff;color:#1a1a1a;overflow:hidden;
  font:12px/1.4 "Segoe UI","Segoe UI Variable Text",-apple-system,BlinkMacSystemFont,Roboto,"Helvetica Neue",Arial,sans-serif}
button{font:inherit;color:inherit}
::-webkit-scrollbar{width:14px;height:14px}
::-webkit-scrollbar-thumb{background:#c8c8c8;border:4px solid #fff}
::-webkit-scrollbar-thumb:hover{background:#a8a8a8}
::-webkit-scrollbar-track{background:#fff}
.win{display:flex;flex-direction:column;height:100vh;height:100dvh}

/* ---- barra de comando ---- */
.cmdbar{display:flex;align-items:center;gap:2px;padding:6px 8px 4px;flex-wrap:wrap;flex:none}
.cmd{display:inline-flex;align-items:center;gap:6px;padding:5px 9px;border:1px solid transparent;
  border-radius:2px;background:transparent;cursor:pointer;white-space:nowrap}
.cmd:hover:not(:disabled){background:#eaeaea}
.cmd:active:not(:disabled){background:#dcdcdc}
.cmd:disabled{color:#a0a0a0;cursor:default}
.cmd:disabled .ico{opacity:.45}
.ico{display:inline-flex;flex:none;align-items:center}
.ico svg{display:block}
.sep{width:1px;height:20px;background:#dcdcdc;margin:0 6px;flex:none}
.caret{font-size:9px;color:#555;margin-left:1px}

/* ---- barra de endereco ---- */
.addrbar{display:flex;align-items:center;gap:2px;padding:2px 8px 8px;border-bottom:1px solid #e8e8e8;flex:none}
.navbtn{display:inline-flex;align-items:center;justify-content:center;width:30px;height:28px;
  border:1px solid transparent;border-radius:2px;background:transparent;cursor:pointer;flex:none}
.navbtn:hover:not(:disabled){background:#eaeaea}
.navbtn:active:not(:disabled){background:#dcdcdc}
.navbtn:disabled{opacity:.35;cursor:default}
.addrwrap{flex:1;min-width:80px;display:flex;align-items:center;border:1px solid #d1d1d1;
  background:#fff;height:28px;padding:0 2px;overflow:hidden}
.addrwrap:focus-within{border-color:#0078d7}
.crumbs{display:flex;align-items:center;overflow:hidden;white-space:nowrap;flex:1;height:100%}
.crumb{display:inline-flex;align-items:center;gap:6px;border:0;background:transparent;
  padding:2px 7px;cursor:pointer;border-radius:2px;max-width:230px;overflow:hidden;height:22px}
.crumb span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.crumb:hover{background:#e5f3ff}
.crumb-sep{color:#9a9a9a;padding:0 1px}
.searchbox{display:flex;align-items:center;gap:6px;width:215px;height:28px;border:1px solid #d1d1d1;
  padding:0 8px;background:#fff;margin-left:6px;flex:none}
.searchbox:focus-within{border-color:#0078d7}
.searchbox input{border:0;outline:0;flex:1;min-width:0;font:inherit;background:transparent;color:inherit}

/* ---- conteudo ---- */
.conteudo{flex:1;display:flex;min-height:0}
.navpane{width:218px;flex:none;overflow:auto;padding:8px 4px}
.navtitulo{font-size:12px;color:#6d6d6d;padding:4px 10px 8px;user-select:none}
.titem{display:flex;align-items:center;gap:6px;padding:3px 8px;border-radius:2px;cursor:pointer;
  white-space:nowrap;user-select:none}
.titem:hover{background:#e5f3ff}
.titem.sel{background:#cce8ff}
.tchev{width:14px;font-size:10px;color:#555;flex:none;text-align:center}
.tlabel{overflow:hidden;text-overflow:ellipsis}

.filearea{flex:1;display:flex;flex-direction:column;min-width:0}
.colheader{display:grid;grid-template-columns:minmax(0,1fr) 150px 190px 90px;
  height:26px;border-bottom:1px solid #ececec;flex:none}
.colheader .col{display:flex;align-items:center;gap:4px;padding:0 8px;cursor:pointer;color:#5d5d5d;
  border-right:1px solid #f0f0f0;user-select:none;overflow:hidden;white-space:nowrap}
.colheader .col:hover{background:#f5f5f5}
.colheader .col .seta{font-size:9px;color:#0078d7}
.filelist{flex:1;overflow:auto;position:relative}
.row{display:grid;grid-template-columns:minmax(0,1fr) 150px 190px 90px;height:26px;
  align-items:center;user-select:none;cursor:default}
.row:hover{background:#e5f3ff}
.row.sel{background:#cce8ff}
.row .cell{padding:0 8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .col-name{display:flex;align-items:center;gap:8px;overflow:hidden}
.row .col-name .nome{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .col-size{color:#555}
.vazio{padding:36px 16px;color:#6d6d6d;text-align:center}

/* ---- barra de status ---- */
.statusbar{flex:none;display:flex;align-items:center;justify-content:space-between;gap:12px;
  height:24px;padding:0 10px;background:#f0f0f0;border-top:1px solid #e0e0e0;color:#3c3c3c}
.statusbar .dir{color:#6d6d6d;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ---- menus de contexto ---- */
.ctxmenu{position:fixed;z-index:500;background:#fff;border:1px solid #d1d1d1;
  box-shadow:0 4px 14px rgba(0,0,0,.18);padding:4px 0;min-width:210px}
.mi{display:block;width:100%;text-align:left;border:0;background:transparent;padding:7px 26px 7px 14px;
  cursor:pointer;white-space:nowrap}
.mi:hover:not(.off){background:#e5f3ff}
.mi.off{color:#a0a0a0;cursor:default}
.msep{height:1px;background:#ececec;margin:4px 0}

/* ---- dialogos ---- */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.28);display:flex;align-items:center;
  justify-content:center;padding:16px;z-index:600}
.modal{background:#fff;border:1px solid #d1d1d1;box-shadow:0 22px 68px rgba(0,0,0,.35);
  width:min(560px,100%);max-height:92vh;overflow:auto}
.modal-grande{width:min(780px,100%)}
.dlg-title{display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:12px 10px 6px 14px;font-size:14px;font-weight:600}
.dlg-title span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mclose{border:0;background:transparent;font-size:14px;cursor:pointer;padding:6px 9px;color:#444;flex:none}
.mclose:hover{background:#eaeaea}
.dlg-body{padding:4px 14px 0}
.dlg-acoes{display:flex;justify-content:flex-end;gap:8px;padding:14px;flex-wrap:wrap}
.label{display:block;font-size:12px;color:#444;margin:10px 0 4px}
.input,textarea.input{width:100%;border:1px solid #b8b8b8;padding:6px 8px;font:inherit;
  outline:0;background:#fff;color:inherit}
.input:focus{border-color:#0078d7}
textarea.input{min-height:100px;resize:vertical}
.preview{font-size:12px;color:#6d6d6d;margin-top:6px;word-break:break-all}
.editor{width:100%;min-height:45vh;border:1px solid #b8b8b8;padding:8px;outline:0;resize:vertical;
  font:12px/1.5 Consolas,"Courier New",monospace;background:#fff;color:inherit}
.pv-media{max-width:100%;max-height:60vh;display:block;margin:0 auto;background:#000}
.pv-frame{width:100%;height:60vh;border:1px solid #d9d9d9;background:#fff}
.hint{color:#6d6d6d;text-align:center;padding:14px}
.btn{padding:5px 18px;border:1px solid #adadad;background:#e1e1e1;cursor:pointer;min-width:86px}
.btn:hover{background:#e5f1fb;border-color:#0078d7}
.btn.primary{background:#0078d7;border-color:#0078d7;color:#fff}
.btn.primary:hover{background:#006cc1}

/* ---- uploads / avisos ---- */
.uploads{position:fixed;left:12px;bottom:34px;width:min(320px,90vw);display:none;flex-direction:column;
  gap:6px;z-index:450;max-height:50vh;overflow:auto}
.uploads.show{display:flex}
.up{background:#fff;border:1px solid #d1d1d1;box-shadow:0 4px 14px rgba(0,0,0,.15);padding:8px 10px}
.up-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.up .bar{height:5px;background:#e8e8e8;margin-top:6px;overflow:hidden}
.up .bar i{display:block;height:100%;width:0;background:#0078d7;transition:width .15s}
.up.done .bar i{background:#107c10}
.up.error .bar i{background:#d13438}
.toasts{position:fixed;right:12px;bottom:12px;display:flex;flex-direction:column;gap:8px;
  z-index:700;width:min(360px,92vw)}
.toast{background:#2b2b2b;color:#fff;padding:12px 14px;box-shadow:0 8px 24px rgba(0,0,0,.35);
  font-size:12.5px;border-left:4px solid #0078d7;transition:opacity .3s,transform .3s;word-break:break-word}
.toast.error{border-left-color:#d13438}
.toast.out{opacity:0;transform:translateY(6px)}
#drop-overlay{position:fixed;inset:8px;display:none;align-items:center;justify-content:center;
  border:2px dashed #0078d7;background:rgba(255,255,255,.85);z-index:800;color:#005a9e;
  font-size:16px;font-weight:600;text-align:center;padding:20px}
body.dragging #drop-overlay{display:flex}

/* ---- responsivo ---- */
@media (max-width:860px){
  .colheader,.row{grid-template-columns:minmax(0,1fr) 130px 80px}
  .colheader .col-type,.row .col-type{display:none}
}
@media (max-width:620px){
  .colheader,.row{grid-template-columns:minmax(0,1fr) 80px}
  .colheader .col-date,.row .col-date{display:none}
  .searchbox{width:130px}
}
@media (max-width:760px){
  .navpane{display:none}
}
@media (pointer:coarse){
  .cmd{padding:9px 11px}
  .navbtn{width:40px;height:36px}
  .addrwrap,.searchbox{height:36px}
  .titem{padding:8px}
  .row{height:auto;min-height:42px}
  .mclose{padding:10px 13px}
}
</style>
</head>
<body>
<div class="win">

  <!-- barra de comando -->
  <div class="cmdbar">
    <button class="cmd" id="cmd-enviar"><span class="ico"><svg viewBox="0 0 20 20" width="16" height="16"><path d="M10 3v8.5M10 3L6.6 6.4M10 3l3.4 3.4" stroke="#333" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 11.5v3.7c0 .4.4.8.8.8h10.4c.4 0 .8-.4.8-.8v-3.7" stroke="#333" stroke-width="1.4" fill="none" stroke-linecap="round"/></svg></span><span>Enviar</span></button>
    <div class="sep"></div>
    <button class="cmd" id="cmd-novo"><span class="ico"><svg viewBox="0 0 20 20" width="16" height="16"><path d="M10 4v12M4 10h12" stroke="#2b579a" stroke-width="1.7" fill="none" stroke-linecap="round"/></svg></span><span>Novo</span><span class="caret">&#9662;</span></button>
    <button class="cmd" id="cmd-renomear" disabled><span class="ico"><svg viewBox="0 0 20 20" width="16" height="16"><path d="M4 16l1-3.6 7.6-7.6 2.6 2.6-7.6 7.6z" fill="none" stroke="#333" stroke-width="1.3" stroke-linejoin="round"/><path d="M12.6 4.8l2.6 2.6" stroke="#333" stroke-width="1.3"/></svg></span><span>Renomear</span></button>
    <button class="cmd" id="cmd-ext" disabled><span class="ico"><svg viewBox="0 0 20 20" width="16" height="16"><path d="M3.5 3.5h6.8l6.2 6.2-6.8 6.8-6.2-6.2z" fill="none" stroke="#333" stroke-width="1.3" stroke-linejoin="round"/><circle cx="7" cy="7" r="1.2" fill="#333"/></svg></span><span>Mudar extens&#227;o</span></button>
    <button class="cmd" id="cmd-baixar" disabled><span class="ico"><svg viewBox="0 0 20 20" width="16" height="16"><path d="M10 3.5V12M10 12l-3.4-3.4M10 12l3.4-3.4" stroke="#333" stroke-width="1.4" fill="none" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 11.5v3.7c0 .4.4.8.8.8h10.4c.4 0 .8-.4.8-.8v-3.7" stroke="#333" stroke-width="1.4" fill="none" stroke-linecap="round"/></svg></span><span>Baixar</span></button>
    <div class="sep"></div>
    <button class="cmd" id="cmd-excluir" disabled><span class="ico"><svg viewBox="0 0 20 20" width="16" height="16"><path d="M4 6h12M8 6V4.6c0-.3.2-.6.6-.6h2.8c.4 0 .6.3.6.6V6" stroke="#333" stroke-width="1.3" fill="none"/><path d="M5.6 6l.7 10c0 .4.4.7.8.7h5.8c.4 0 .8-.3.8-.7l.7-10" stroke="#333" stroke-width="1.3" fill="none" stroke-linejoin="round"/></svg></span><span>Excluir</span></button>
  </div>

  <!-- barra de endereco -->
  <div class="addrbar">
    <button class="navbtn" id="nav-back" title="Voltar"><svg viewBox="0 0 20 20" width="16" height="16"><path d="M12.5 4L6.5 10l6 6" fill="none" stroke="#333" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
    <button class="navbtn" id="nav-fwd" title="Avan&#231;ar"><svg viewBox="0 0 20 20" width="16" height="16"><path d="M7.5 4l6 6-6 6" fill="none" stroke="#333" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
    <button class="navbtn" id="nav-up" title="Subir um n&#237;vel"><svg viewBox="0 0 20 20" width="16" height="16"><path d="M4.5 12.5L10 7l5.5 5.5" fill="none" stroke="#333" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
    <div class="addrwrap"><nav id="crumbs" class="crumbs"></nav></div>
    <button class="navbtn" id="nav-refresh" title="Atualizar"><svg viewBox="0 0 20 20" width="16" height="16"><path d="M15.5 10a5.5 5.5 0 1 1-1.7-4" fill="none" stroke="#333" stroke-width="1.4" stroke-linecap="round"/><path d="M15.8 3.4v3.2h-3.2" fill="none" stroke="#333" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
    <div class="searchbox">
      <svg viewBox="0 0 20 20" width="14" height="14"><circle cx="8.5" cy="8.5" r="4.6" fill="none" stroke="#777" stroke-width="1.3"/><path d="M12 12l4.4 4.4" stroke="#777" stroke-width="1.3" stroke-linecap="round"/></svg>
      <input id="busca" type="text" placeholder="Pesquisar">
    </div>
  </div>

  <!-- conteudo -->
  <div class="conteudo">
    <aside class="navpane">
      <div class="navtitulo">Acesso r&#225;pido</div>
      <div id="tree"></div>
    </aside>
    <section class="filearea">
      <div class="colheader">
        <div class="col" data-sort="name" style="cursor:pointer"><span>Nome</span><span class="seta"></span></div>
        <div class="col col-date" data-sort="date"><span>Data de modifica&#231;&#227;o</span><span class="seta"></span></div>
        <div class="col col-type" data-sort="type"><span>Tipo</span><span class="seta"></span></div>
        <div class="col col-size" data-sort="size"><span>Tamanho</span><span class="seta"></span></div>
      </div>
      <div id="filelist" class="filelist"></div>
    </section>
  </div>

  <!-- barra de status -->
  <div class="statusbar">
    <span id="st-itens"></span>
    <span class="dir" id="st-livre"></span>
  </div>
</div>

<div id="uploads" class="uploads"></div>
<div id="toasts" class="toasts"></div>
<div id="ctxmenu" class="ctxmenu" hidden></div>
<div id="drop-overlay">&#128194; Copiar para Armazenamento</div>
<input type="file" id="file-input" multiple hidden>

<script>
"use strict";
var $ = function(sel, root){ return (root || document).querySelector(sel); };
var TOQUE = !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);

var state = {
  cwd: "", entries: [], sel: null,
  sort: {key: "name", dir: 1},
  busca: "",
  historico: [""], histIdx: 0,
  expandidos: {}, treeKids: {}, semSub: {}
};

/* ================= utilitarios ================= */

function el(tag, attrs, kids){
  var n = document.createElement(tag);
  if (attrs) for (var k in attrs){
    var v = attrs[k];
    if (v === null || v === undefined) continue;
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.slice(0, 2) === "on") n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  (kids || []).forEach(function(c){
    n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  });
  return n;
}

function joinPath(dir, name){ return dir ? dir + "/" + name : name; }

function extOf(name){
  var i = name.lastIndexOf(".");
  return i > 0 ? name.slice(i + 1) : "";
}
function baseName(name){
  var i = name.lastIndexOf(".");
  return i > 0 ? name.slice(0, i) : name;
}

function fmtSize(b){
  if (b === null || b === undefined) return "";
  var u = ["B", "KB", "MB", "GB", "TB"], i = 0, n = b;
  while (n >= 1024 && i < u.length - 1){ n /= 1024; i++; }
  return (i ? n.toFixed(1).replace(".", ",") : String(n)) + " " + u[i];
}

function fmtDate(ms){
  var d = new Date(ms);
  function p(n){ return (n < 10 ? "0" : "") + n; }
  return p(d.getDate()) + "/" + p(d.getMonth() + 1) + "/" + d.getFullYear() +
         " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

function norm(s){
  try { return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase(); }
  catch(e){ return String(s).toLowerCase(); }
}

async function api(url, opts){
  var r = await fetch(url, Object.assign({cache:"no-store"}, opts || {}));
  var d = null;
  try { d = await r.json(); } catch(e){}
  if (!r.ok || !d || !d.ok) throw new Error((d && d.error) || ("Erro HTTP " + r.status));
  return d;
}

function post(url, body){
  return api(url, {method:"POST", headers:{"Content-Type":"application/json"},
                   body: JSON.stringify(body || {})});
}

async function guard(fn){
  try { await fn(); } catch(e){ toast(e.message || "Erro", "error"); }
}

function toast(msg, tipo){
  var t = el("div", {class:"toast " + (tipo || "ok"), text: msg});
  $("#toasts").appendChild(t);
  setTimeout(function(){
    t.classList.add("out");
    setTimeout(function(){ t.remove(); }, 320);
  }, 3200);
}

/* ================= icones (SVG estilo Windows) ================= */

var PAGINA = '<path d="M5 2h6.2L15.5 6.3V17c0 .55-.45 1-1 1h-9.5c-.55 0-1-.45-1-1V3c0-.55.45-1 1-1z" fill="#ffffff" stroke="#9a9a9a" stroke-width="0.9"/>' +
             '<path d="M11.2 2l4.3 4.3h-3.3c-.55 0-1-.45-1-1z" fill="#dcdcdc" stroke="#9a9a9a" stroke-width="0.9"/>';

var SVG = {
  folder: '<svg viewBox="0 0 20 20" width="20" height="20"><path d="M2 5.5C2 4.7 2.7 4 3.5 4h4L9.5 6h7c.8 0 1.5.7 1.5 1.5v7c0 .8-.7 1.5-1.5 1.5h-13C2.7 16 2 15.3 2 14.5z" fill="#f6b73c"/><path d="M2 8h16v6.5c0 .8-.7 1.5-1.5 1.5h-13C2.7 16 2 15.3 2 14.5z" fill="#ffd978"/></svg>',
  file: '<svg viewBox="0 0 20 20" width="20" height="20">' + PAGINA + '</svg>',
  text: '<svg viewBox="0 0 20 20" width="20" height="20">' + PAGINA +
        '<path d="M6 9h8M6 11.5h8M6 14h5.5" stroke="#7a7a7a" stroke-width="1"/></svg>',
  code: '<svg viewBox="0 0 20 20" width="20" height="20">' + PAGINA +
        '<path d="M8 10.5L6.2 12l1.8 1.5" stroke="#0a76d8" stroke-width="1.1" fill="none" stroke-linecap="round" stroke-linejoin="round"/>' +
        '<path d="M12 10.5l1.8 1.5-1.8 1.5" stroke="#0a76d8" stroke-width="1.1" fill="none" stroke-linecap="round" stroke-linejoin="round"/>' +
        '<path d="M10.9 9.8l-1.8 4.4" stroke="#0a76d8" stroke-width="1.1" stroke-linecap="round"/></svg>',
  image: '<svg viewBox="0 0 20 20" width="20" height="20">' + PAGINA +
        '<rect x="5.5" y="8" width="9" height="7" rx="0.8" fill="#d9ecff" stroke="#8fb7d9" stroke-width="0.6"/>' +
        '<circle cx="8" cy="10.3" r="0.9" fill="#f6b73c"/>' +
        '<path d="M6.5 14l2.2-2.4 1.6 1.6 1.7-1.9 2 2.7z" fill="#57a05e"/></svg>',
  video: '<svg viewBox="0 0 20 20" width="20" height="20">' + PAGINA +
        '<rect x="5.5" y="8.5" width="9" height="6.5" rx="0.8" fill="#3c3c3c"/>' +
        '<path d="M9 10.2l3.4 1.8-3.4 1.8z" fill="#ffffff"/></svg>',
  audio: '<svg viewBox="0 0 20 20" width="20" height="20">' + PAGINA +
        '<circle cx="8.2" cy="13.4" r="1.5" fill="#8a4bd6"/>' +
        '<path d="M9.7 13.4V9.2l3.4-.9v3.5" stroke="#8a4bd6" stroke-width="1.1" fill="none"/>' +
        '<circle cx="11.6" cy="12.6" r="1.5" fill="#8a4bd6"/></svg>',
  zip: '<svg viewBox="0 0 20 20" width="20" height="20">' + PAGINA +
        '<path d="M10 8v7" stroke="#b08f4a" stroke-width="1.2"/>' +
        '<path d="M8.8 9h2.4M8.8 11h2.4M8.8 13h2.4" stroke="#8d6e2f" stroke-width="0.7"/></svg>',
  pdf: '<svg viewBox="0 0 20 20" width="20" height="20">' + PAGINA +
        '<rect x="5.5" y="8.5" width="9" height="6.5" rx="0.8" fill="#d93025"/>' +
        '<text x="10" y="13.5" text-anchor="middle" font-size="3.8" font-weight="700" fill="#ffffff" font-family="Segoe UI,Arial">PDF</text></svg>',
  doc: '<svg viewBox="0 0 20 20" width="20" height="20">' + PAGINA +
        '<rect x="5.5" y="8.5" width="9" height="6.5" rx="0.8" fill="#2b579a"/>' +
        '<text x="10" y="13.5" text-anchor="middle" font-size="3.8" font-weight="700" fill="#ffffff" font-family="Segoe UI,Arial">DOC</text></svg>',
  xls: '<svg viewBox="0 0 20 20" width="20" height="20">' + PAGINA +
        '<rect x="5.5" y="8.5" width="9" height="6.5" rx="0.8" fill="#217346"/>' +
        '<text x="10" y="13.5" text-anchor="middle" font-size="3.8" font-weight="700" fill="#ffffff" font-family="Segoe UI,Arial">XLS</text></svg>',
  ppt: '<svg viewBox="0 0 20 20" width="20" height="20">' + PAGINA +
        '<rect x="5.5" y="8.5" width="9" height="6.5" rx="0.8" fill="#d24726"/>' +
        '<text x="10" y="13.5" text-anchor="middle" font-size="3.8" font-weight="700" fill="#ffffff" font-family="Segoe UI,Arial">PPT</text></svg>'
};

function svgIcon(tipo, tam){
  var span = document.createElement("span");
  span.className = "ico";
  span.innerHTML = SVG[tipo] || SVG.file;
  if (tam){
    var s = span.firstChild;
    s.setAttribute("width", tam);
    s.setAttribute("height", tam);
  }
  return span;
}

/* ================= tipos de arquivo ================= */

var EXT_IMG = ["png","jpg","jpeg","gif","webp","bmp","svg","ico","avif","heic"];
var EXT_VID = ["mp4","mkv","webm","mov","m4v","3gp","avi"];
var EXT_AUD = ["mp3","wav","ogg","oga","m4a","flac","aac","opus"];
var EXT_ZIP = ["zip","rar","7z","tar","gz","bz2","xz","apk"];
var EXT_CODE = ["py","js","ts","jsx","tsx","html","htm","css","scss","c","h","cpp","hpp",
  "java","kt","rs","go","rb","php","pl","lua","sql","sh","bash","zsh","bat","ps1","cmd",
  "json","xml","yml","yaml"];
var EXT_TXT = ["txt","md","markdown","log","csv","tsv","ini","cfg","conf","env","srt","vtt"];

var TIPOS = {
  txt:"Documento de texto", md:"Documento Markdown", py:"Arquivo Python",
  js:"Arquivo JavaScript", ts:"Arquivo TypeScript", json:"Arquivo JSON",
  html:"Documento HTML", htm:"Documento HTML", css:"Folha de estilos",
  pdf:"Documento PDF", doc:"Documento do Word", docx:"Documento do Word",
  odt:"Documento de texto OpenDocument", rtf:"Documento Rich Text",
  xls:"Planilha do Excel", xlsx:"Planilha do Excel", ods:"Planilha OpenDocument",
  csv:"Valores separados por virgula", ppt:"Apresentacao do PowerPoint",
  pptx:"Apresentacao do PowerPoint", odp:"Apresentacao OpenDocument",
  jpg:"Imagem JPEG", jpeg:"Imagem JPEG", png:"Imagem PNG", gif:"Imagem GIF",
  webp:"Imagem WebP", svg:"Imagem SVG", bmp:"Imagem BMP", ico:"Icone",
  mp4:"Video MP4", mkv:"Video MKV", webm:"Video WebM", mov:"Video MOV",
  avi:"Video AVI", m4v:"Video M4V", "3gp":"Video 3GP",
  mp3:"Musica MP3", wav:"Audio WAV", ogg:"Audio OGG", m4a:"Audio M4A",
  flac:"Audio FLAC", aac:"Audio AAC", opus:"Audio Opus",
  zip:"Pasta compactada ZIP", rar:"Pasta compactada RAR", "7z":"Pasta compactada 7z",
  tar:"Arquivo TAR", gz:"Arquivo GZip", bz2:"Arquivo BZip2", xz:"Arquivo XZ",
  apk:"Pacote de aplicativo Android", iso:"Imagem de disco", exe:"Aplicativo",
  bat:"Arquivo em lote", sh:"Script de shell", ps1:"Script do PowerShell",
  lua:"Script Lua", sql:"Script SQL", log:"Arquivo de log", env:"Arquivo de configuracao",
  ini:"Configuracao", cfg:"Configuracao", conf:"Configuracao", srt:"Legenda SRT",
  vtt:"Legenda WebVTT", yml:"Arquivo YAML", yaml:"Arquivo YAML", xml:"Documento XML"
};

function ehTexto(entry){
  var ext = extOf(entry.name).toLowerCase();
  var nome = entry.name.toLowerCase();
  return EXT_TXT.indexOf(ext) !== -1 || EXT_CODE.indexOf(ext) !== -1 ||
         ["dockerfile", "makefile"].indexOf(nome) !== -1;
}

function categoria(e){
  var ext = extOf(e.name).toLowerCase();
  if (EXT_IMG.indexOf(ext) !== -1) return "image";
  if (EXT_VID.indexOf(ext) !== -1) return "video";
  if (EXT_AUD.indexOf(ext) !== -1) return "audio";
  if (EXT_ZIP.indexOf(ext) !== -1) return "zip";
  if (ext === "pdf") return "pdf";
  if (["doc","docx","odt","rtf"].indexOf(ext) !== -1) return "doc";
  if (["xls","xlsx","ods"].indexOf(ext) !== -1) return "xls";
  if (["ppt","pptx","odp"].indexOf(ext) !== -1) return "ppt";
  if (EXT_CODE.indexOf(ext) !== -1) return "code";
  if (ehTexto(e)) return "text";
  return "file";
}

function iconFor(entry){
  if (entry.is_dir) return svgIcon("folder");
  return svgIcon(categoria(entry));
}

function tipoNome(e){
  if (e.is_dir) return "Pasta de arquivos";
  var ext = extOf(e.name).toLowerCase();
  var t = TIPOS[ext];
  if (t) return t + " (." + ext + ")";
  return ext ? ("Arquivo " + ext.toUpperCase() + " (." + ext + ")") : "Arquivo";
}

/* ================= navegacao ================= */

function hashDe(path){ return path ? "#/" + encodeURI(path) : "#/"; }
function pathFromHash(){
  var h = location.hash;
  if (h.indexOf("#/") === 0) return decodeURI(h.slice(2));
  return "";
}
function setHash(path){
  var h = hashDe(path);
  if (location.hash === h) load(path);
  else location.hash = h;
}
function go(path){
  path = path || "";
  if (state.historico[state.histIdx] === path){ load(path); return; }
  state.historico = state.historico.slice(0, state.histIdx + 1);
  state.historico.push(path);
  state.histIdx = state.historico.length - 1;
  setHash(path);
}
function voltar(){
  if (state.histIdx > 0){ state.histIdx--; setHash(state.historico[state.histIdx]); }
}
function avancar(){
  if (state.histIdx < state.historico.length - 1){ state.histIdx++; setHash(state.historico[state.histIdx]); }
}
function subir(){
  var partes = state.cwd ? state.cwd.split("/") : [];
  partes.pop();
  go(partes.join("/"));
}

window.addEventListener("hashchange", function(){
  var p = pathFromHash();
  var h = state.historico;
  if (h[state.histIdx] !== p){
    if (state.histIdx > 0 && h[state.histIdx - 1] === p) state.histIdx--;
    else if (state.histIdx < h.length - 1 && h[state.histIdx + 1] === p) state.histIdx++;
    else {
      state.historico = h.slice(0, state.histIdx + 1);
      state.historico.push(p);
      state.histIdx = state.historico.length - 1;
    }
  }
  load(p);
});

function atualizarNav(){
  $("#nav-back").disabled = state.histIdx <= 0;
  $("#nav-fwd").disabled = state.histIdx >= state.historico.length - 1;
  $("#nav-up").disabled = !state.cwd;
}

/* ================= carga e renderizacao ================= */

async function load(path){
  try {
    var d = await api("/api/list?path=" + encodeURIComponent(path || ""));
    var mudou = state.cwd !== d.path;
    state.cwd = d.path;
    state.entries = d.entries;
    if (mudou){ state.busca = ""; $("#busca").value = ""; }
    if (state.sel && !state.entries.some(function(e){ return e.name === state.sel; })) state.sel = null;
    renderCrumbs();
    renderTree();
    renderList();
    atualizarNav();
    loadInfo();
  } catch(e){
    toast(e.message, "error");
  }
}

async function loadInfo(){
  try {
    var d = await api("/api/info");
    $("#st-livre").textContent = "Livre: " + fmtSize(d.free) + " de " + fmtSize(d.total);
    $("#st-livre").title = "Pasta: " + d.root;
  } catch(e){}
}

function renderCrumbs(){
  var nav = $("#crumbs");
  nav.innerHTML = "";
  var partes = state.cwd ? state.cwd.split("/") : [];
  function mk(label, path, atual, comIcone){
    var b = el("button", {class:"crumb" + (atual ? " current" : "")});
    if (comIcone) b.appendChild(svgIcon("folder", 16));
    b.appendChild(el("span", {text: label, title: label}));
    b.addEventListener("click", function(){ go(path); });
    return b;
  }
  nav.appendChild(mk("Armazenamento", "", partes.length === 0, true));
  var acc = "";
  partes.forEach(function(p, i){
    acc = acc ? acc + "/" + p : p;
    nav.appendChild(el("span", {class:"crumb-sep", text:"\u203A"}));
    nav.appendChild(mk(p, acc, i === partes.length - 1, false));
  });
}

function renderTree(){
  var t = $("#tree");
  t.innerHTML = "";
  t.appendChild(titem("", "Armazenamento", 0));
  (function desenha(path, nivel){
    if (!state.expandidos[path]) return;
    var kids = state.treeKids[path];
    if (!kids) return;
    kids.forEach(function(nome){
      var p = joinPath(path, nome);
      t.appendChild(titem(p, nome, nivel));
      desenha(p, nivel + 1);
    });
  })("", 1);
}

function titem(path, nome, nivel){
  var d = el("div", {class:"titem" + (state.cwd === path ? " sel" : "")});
  d.style.paddingLeft = (8 + nivel * 14) + "px";
  var ch = el("span", {class:"tchev",
    text: state.expandidos[path] ? "\u25BE" : (state.semSub[path] ? "" : "\u25B8")});
  ch.addEventListener("click", function(ev){ ev.stopPropagation(); togglar(path); });
  d.appendChild(ch);
  d.appendChild(svgIcon("folder", 16));
  d.appendChild(el("span", {class:"tlabel", text: nome, title: nome}));
  d.addEventListener("click", function(){ go(path); });
  return d;
}

async function togglar(path){
  if (state.expandidos[path]){ state.expandidos[path] = false; renderTree(); return; }
  state.expandidos[path] = true;
  if (!state.treeKids[path]){
    try {
      var d = await api("/api/list?path=" + encodeURIComponent(path));
      var dirs = d.entries.filter(function(e){ return e.is_dir; })
                          .map(function(e){ return e.name; });
      state.treeKids[path] = dirs;
      if (!dirs.length) state.semSub[path] = true;
    } catch(e){ state.treeKids[path] = []; }
  }
  renderTree();
}

function invalidarArvore(){
  state.treeKids = {};
  state.semSub = {};
}

function filtrar(){
  var q = norm(state.busca.trim());
  var arr = state.entries;
  if (q) arr = arr.filter(function(e){ return norm(e.name).indexOf(q) !== -1; });
  return ordenar(arr);
}

function ordenar(arr){
  var k = state.sort.key, dir = state.sort.dir;
  function chave(e){
    if (k === "name") return e.name.toLowerCase();
    if (k === "date") return e.mtime;
    if (k === "size") return e.is_dir ? -1 : e.size;
    if (k === "type") return tipoNome(e).toLowerCase();
    return e.name.toLowerCase();
  }
  return arr.slice().sort(function(a, b){
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    var va = chave(a), vb = chave(b);
    if (va < vb) return -dir;
    if (va > vb) return dir;
    return a.name.localeCompare(b.name);
  });
}

function renderList(){
  var lista = filtrar();
  var cont = $("#filelist");
  cont.innerHTML = "";
  if (!lista.length){
    cont.appendChild(el("div", {class:"vazio",
      text: state.busca ? "Nenhum item corresponde \u00e0 pesquisa." : "Esta pasta est\u00e1 vazia."}));
  } else {
    lista.forEach(function(entry){ cont.appendChild(linha(entry)); });
  }
  atualizarBotoes();
  atualizarStatus(lista.length);
}

function linha(entry){
  var r = el("div", {class:"row" + (entry.name === state.sel ? " sel" : "")});
  r.dataset.nome = entry.name;
  r.tabIndex = 0;

  r.appendChild(el("div", {class:"cell col-name"},
    [iconFor(entry), el("span", {class:"nome", text: entry.name, title: entry.name})]));
  r.appendChild(el("div", {class:"cell col-date", text: fmtDate(entry.mtime)}));
  r.appendChild(el("div", {class:"cell col-type", text: tipoNome(entry)}));
  r.appendChild(el("div", {class:"cell col-size", text: entry.is_dir ? "" : fmtSize(entry.size)}));

  r.addEventListener("click", function(ev){
    ev.stopPropagation();
    if (r._ignorarClique){ r._ignorarClique = false; return; }
    selecionar(entry.name);
    if (TOQUE) abrir(entry);
    else if (ev.detail === 2) abrir(entry);
  });

  r.addEventListener("contextmenu", function(ev){
    ev.preventDefault();
    ev.stopPropagation();
    selecionar(entry.name);
    abrirMenu(entry, ev.clientX, ev.clientY);
  });

  var lpTimer = null;
  r.addEventListener("touchstart", function(ev){
    clearTimeout(lpTimer);
    var ponto = ev.touches[0];
    lpTimer = setTimeout(function(){
      r._ignorarClique = true;
      selecionar(entry.name);
      abrirMenu(entry, ponto.clientX, ponto.clientY);
    }, 550);
  }, {passive:true});
  ["touchend", "touchmove", "touchcancel"].forEach(function(ev){
    r.addEventListener(ev, function(){ clearTimeout(lpTimer); }, {passive:true});
  });

  return r;
}

function selecionado(){
  if (!state.sel) return null;
  for (var i = 0; i < state.entries.length; i++){
    if (state.entries[i].name === state.sel) return state.entries[i];
  }
  return null;
}

function selecionar(nome){
  state.sel = nome;
  var rows = document.querySelectorAll(".row");
  for (var i = 0; i < rows.length; i++){
    rows[i].classList.toggle("sel", rows[i].dataset.nome === nome);
  }
  atualizarBotoes();
  atualizarStatus();
}

function atualizarBotoes(){
  var s = selecionado();
  $("#cmd-renomear").disabled = !s;
  $("#cmd-excluir").disabled = !s;
  $("#cmd-baixar").disabled = !s || s.is_dir;
  $("#cmd-ext").disabled = !s || s.is_dir;
}

function atualizarStatus(total){
  var sel = selecionado();
  var txt;
  if (sel) txt = "1 item selecionado" + (sel.is_dir ? "" : "   " + fmtSize(sel.size));
  else {
    var n = (total !== undefined) ? total : filtrar().length;
    txt = n + (n === 1 ? " item" : " itens");
  }
  $("#st-itens").textContent = txt;
}

function atualizarSetas(){
  var cols = document.querySelectorAll(".colheader .col");
  for (var i = 0; i < cols.length; i++){
    var s = cols[i].querySelector(".seta");
    if (!s) continue;
    s.textContent = (cols[i].dataset.sort === state.sort.key)
      ? (state.sort.dir > 0 ? "\u25B2" : "\u25BC") : "";
  }
}

/* ================= acoes ================= */

function abrir(entry){
  if (entry.is_dir) go(joinPath(state.cwd, entry.name));
  else preview(entry);
}

function download(entry){
  location.href = "/download?path=" + encodeURIComponent(joinPath(state.cwd, entry.name));
}

async function apagar(entry){
  var msg = "Apagar " + (entry.is_dir ? "a pasta" : "o arquivo") + " \"" + entry.name + "\"?";
  if (entry.is_dir) msg += "\n\nTodo o conteudo dentro dela sera apagado.";
  if (!confirm(msg)) return;
  await post("/api/delete", {path: joinPath(state.cwd, entry.name)});
  invalidarArvore();
  toast("Apagado: " + entry.name);
  load(state.cwd);
}

async function renomear(entry){
  var v = await formDialog({
    title: "Renomear",
    fields: [{name:"name", label:"Novo nome", value: entry.name}],
    okLabel: "Renomear"
  });
  if (!v) return;
  var d = await post("/api/rename", {path: joinPath(state.cwd, entry.name), new_name: v.name});
  invalidarArvore();
  toast("Renomeado para: " + d.name);
  load(state.cwd);
}

async function mudarExt(entry){
  var extAtual = extOf(entry.name);
  var v = await formDialog({
    title: "Mudar extens\u00e3o",
    fields: [{
      name: "ext",
      label: "Nova extens\u00e3o (sem o ponto)",
      value: extAtual,
      placeholder: "ex: png, txt, zip \u2014 deixe vazio para remover",
      preview: function(val){
        var limpo = val.trim().replace(/^\.+/, "");
        return "Resultado: " + baseName(entry.name) + (limpo ? "." + limpo : "");
      }
    }],
    okLabel: "Aplicar"
  });
  if (!v) return;
  var d = await post("/api/change_ext", {path: joinPath(state.cwd, entry.name), ext: v.ext});
  toast("Arquivo agora \u00e9: " + d.name);
  load(state.cwd);
}

async function novaPasta(){
  var v = await formDialog({
    title: "Nova pasta",
    fields: [{name:"name", label:"Nome da pasta", placeholder:"ex: documentos"}],
    okLabel: "Criar"
  });
  if (!v) return;
  await post("/api/mkdir", {path: state.cwd, name: v.name});
  invalidarArvore();
  toast("Pasta criada: " + v.name);
  load(state.cwd);
}

async function novoArquivo(){
  var v = await formDialog({
    title: "Novo arquivo",
    fields: [
      {name:"name", label:"Nome do arquivo (com extens\u00e3o)", placeholder:"ex: notas.txt"},
      {name:"content", label:"Conte\u00fado (opcional)", type:"textarea", value:""}
    ],
    okLabel: "Criar"
  });
  if (!v) return;
  await post("/api/newfile", {path: state.cwd, name: v.name, content: v.content});
  toast("Arquivo criado: " + v.name);
  load(state.cwd);
}

/* ================= menus de contexto ================= */

function fecharMenu(){
  var m = $("#ctxmenu");
  m.hidden = true;
  m.innerHTML = "";
}

function abrirMenu(entry, x, y){
  var m = $("#ctxmenu");
  m.innerHTML = "";

  function item(txt, fn){
    var b = el("button", {class:"mi", text: txt});
    b.addEventListener("click", function(ev){ ev.stopPropagation(); fecharMenu(); fn(); });
    m.appendChild(b);
  }
  function sep(){ m.appendChild(el("div", {class:"msep"})); }

  if (entry){
    if (entry.is_dir){
      item("Abrir", function(){ abrir(entry); });
    } else {
      item("Abrir", function(){ preview(entry); });
      item("Baixar", function(){ download(entry); });
    }
    sep();
    item("Renomear", function(){ guard(function(){ renomear(entry); }); });
    if (!entry.is_dir) item("Mudar extens\u00e3o", function(){ guard(function(){ mudarExt(entry); }); });
    sep();
    item("Excluir", function(){ guard(function(){ apagar(entry); }); });
  } else {
    item("Nova pasta", function(){ guard(novaPasta); });
    item("Novo arquivo de texto", function(){ guard(novoArquivo); });
    sep();
    item("Enviar arquivos", function(){ $("#file-input").click(); });
    item("Atualizar", function(){ load(state.cwd); });
  }

  m.hidden = false;
  var w = m.offsetWidth, h = m.offsetHeight;
  m.style.left = Math.max(4, Math.min(x, window.innerWidth - w - 4)) + "px";
  m.style.top = Math.max(4, Math.min(y, window.innerHeight - h - 4)) + "px";
}

/* ================= pre-visualizacao / edicao ================= */

async function preview(entry){
  var rel = joinPath(state.cwd, entry.name);
  var url = "/download?inline=1&path=" + encodeURIComponent(rel);
  var ext = extOf(entry.name).toLowerCase();

  var overlay = el("div", {class:"overlay"});
  var box = el("div", {class:"modal modal-grande"});
  function fechar(){ overlay.remove(); document.removeEventListener("keydown", onKey); }
  function onKey(e){ if (e.key === "Escape") fechar(); }
  document.addEventListener("keydown", onKey);
  overlay.addEventListener("click", function(e){ if (e.target === overlay) fechar(); });

  var x = el("button", {class:"mclose", text:"\u2715", title:"Fechar"});
  x.addEventListener("click", fechar);
  box.appendChild(el("div", {class:"dlg-title"},
    [el("span", {text: entry.name, title: entry.name}), x]));

  var corpo = el("div", {class:"dlg-body"});
  box.appendChild(corpo);

  if (EXT_IMG.indexOf(ext) !== -1){
    corpo.appendChild(el("img", {class:"pv-media", src: url}));
  } else if (EXT_VID.indexOf(ext) !== -1){
    corpo.appendChild(el("video", {class:"pv-media", src: url, controls:"", playsinline:""}));
  } else if (EXT_AUD.indexOf(ext) !== -1){
    corpo.appendChild(el("audio", {class:"pv-media", src: url, controls:""}));
  } else if (ext === "pdf"){
    corpo.appendChild(el("iframe", {class:"pv-frame", src: url}));
  } else if (!ehTexto(entry)){
    corpo.appendChild(el("p", {class:"hint",
      text:"Sem pr\u00e9-visualiza\u00e7\u00e3o para este tipo de arquivo. Use Baixar para obter uma c\u00f3pia."}));
  }

  var acoes = el("div", {class:"dlg-acoes"});
  var baixar = el("a", {class:"btn", href: "/download?path=" + encodeURIComponent(rel), text:"Baixar"});
  baixar.setAttribute("download", entry.name);
  acoes.appendChild(baixar);
  box.appendChild(acoes);

  if (ehTexto(entry)){
    var ta = el("textarea", {class:"editor", spellcheck:"false"});
    ta.value = "Carregando...";
    ta.disabled = true;
    corpo.appendChild(ta);
    try {
      var d = await api("/api/read?path=" + encodeURIComponent(rel));
      ta.value = d.content;
      ta.disabled = false;
      var salvar = el("button", {class:"btn primary", text:"Salvar"});
      salvar.addEventListener("click", function(){
        guard(async function(){
          await post("/api/save", {path: rel, content: ta.value});
          toast("Salvo: " + entry.name);
        });
      });
      acoes.insertBefore(salvar, baixar);
    } catch(e){
      ta.value = "";
      ta.disabled = true;
      corpo.appendChild(el("p", {class:"hint", text:"N\u00e3o foi poss\u00edvel abrir: " + e.message}));
    }
  }

  overlay.appendChild(box);
  document.body.appendChild(overlay);
  if (ehTexto(entry)) setTimeout(function(){ ta.focus(); }, 60);
}

/* ================= dialogo de formulario ================= */

function formDialog(opts){
  return new Promise(function(resolve){
    var inputs = {};
    var fechado = false;

    var overlay = el("div", {class:"overlay"});
    var box = el("div", {class:"modal"});

    var x = el("button", {class:"mclose", text:"\u2715", title:"Fechar"});
    x.addEventListener("click", function(){ fim(false); });
    box.appendChild(el("div", {class:"dlg-title"}, [el("span", {text: opts.title}), x]));

    var corpo = el("div", {class:"dlg-body"});
    (opts.fields || []).forEach(function(f){
      var input = f.type === "textarea"
        ? el("textarea", {class:"input"})
        : el("input", {class:"input", type:"text"});
      input.value = f.value || "";
      if (f.placeholder) input.placeholder = f.placeholder;
      inputs[f.name] = input;

      var prev = f.preview ? el("div", {class:"preview"}) : null;
      function atualizarPrev(){ if (prev) prev.textContent = f.preview(input.value); }
      input.addEventListener("input", atualizarPrev);

      if (f.type !== "textarea"){
        input.addEventListener("keydown", function(e){
          if (e.key === "Enter"){ e.preventDefault(); fim(true); }
        });
      }

      corpo.appendChild(el("label", {class:"label", text: f.label}));
      corpo.appendChild(input);
      if (prev){ atualizarPrev(); corpo.appendChild(prev); }
    });
    box.appendChild(corpo);

    function coletar(){
      var o = {};
      for (var k in inputs) o[k] = inputs[k].value;
      return o;
    }
    function fim(ok){
      if (fechado) return;
      fechado = true;
      var valores = ok ? coletar() : null;
      overlay.remove();
      document.removeEventListener("keydown", onKey);
      resolve(valores);
    }
    function onKey(e){ if (e.key === "Escape") fim(false); }
    document.addEventListener("keydown", onKey);
    overlay.addEventListener("click", function(e){ if (e.target === overlay) fim(false); });

    var cancelar = el("button", {class:"btn", text:"Cancelar"});
    cancelar.addEventListener("click", function(){ fim(false); });
    var okBtn = el("button", {class:"btn primary", text: opts.okLabel || "OK"});
    okBtn.addEventListener("click", function(){ fim(true); });
    box.appendChild(el("div", {class:"dlg-acoes"}, [cancelar, okBtn]));

    overlay.appendChild(box);
    document.body.appendChild(overlay);

    var primeiro = opts.fields && opts.fields.length ? inputs[opts.fields[0].name] : null;
    if (primeiro){
      setTimeout(function(){
        primeiro.focus();
        if (primeiro.select && primeiro.value) primeiro.select();
      }, 40);
    }
  });
}

/* ================= upload ================= */

function addUploadItem(nome){
  var barra = el("i");
  var rotulo = el("div", {class:"up-name", text: nome});
  var caixa = el("div", {class:"up"}, [rotulo, el("div", {class:"bar"}, [barra])]);
  var painel = $("#uploads");
  if (!painel.classList.contains("show")) painel.innerHTML = "";
  painel.appendChild(caixa);
  painel.classList.add("show");
  return {
    progresso: function(p){ barra.style.width = Math.round(p * 100) + "%"; },
    fim: function(ok, err){
      barra.style.width = "100%";
      caixa.classList.add(ok ? "done" : "error");
      if (!ok && err) rotulo.textContent = nome + " \u2014 " + err;
    }
  };
}

function uploadFiles(lista){
  var fila = Array.prototype.slice.call(lista || []);
  if (!fila.length) return;
  toast(fila.length + " arquivo(s) na fila de upload");

  function proximo(){
    var f = fila.shift();
    if (!f){
      load(state.cwd);
      setTimeout(function(){ $("#uploads").classList.remove("show"); }, 2500);
      return;
    }
    var item = addUploadItem(f.name);
    var fd = new FormData();
    fd.append("path", state.cwd);
    fd.append("files", f, f.name);

    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.upload.onprogress = function(e){
      if (e.lengthComputable) item.progresso(e.loaded / e.total);
    };
    xhr.onload = function(){
      var d = null;
      try { d = JSON.parse(xhr.responseText); } catch(e){}
      var ok = xhr.status === 200 && d && d.ok;
      item.fim(ok, (d && d.error) || ("HTTP " + xhr.status));
      if (!ok) toast("Falha no upload de " + f.name, "error");
      proximo();
    };
    xhr.onerror = function(){
      item.fim(false, "erro de rede");
      toast("Erro de rede no upload de " + f.name, "error");
      proximo();
    };
    xhr.send(fd);
  }
  proximo();
}

/* ================= arrastar e soltar ================= */

["dragenter", "dragover"].forEach(function(ev){
  document.addEventListener(ev, function(e){
    e.preventDefault();
    document.body.classList.add("dragging");
  });
});
document.addEventListener("dragleave", function(e){
  if (e.relatedTarget === null) document.body.classList.remove("dragging");
});
document.addEventListener("drop", function(e){
  e.preventDefault();
  document.body.classList.remove("dragging");
  if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length){
    uploadFiles(e.dataTransfer.files);
  }
});

/* ================= teclado ================= */

document.addEventListener("keydown", function(e){
  if (document.querySelector(".overlay")) return;
  if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA")) return;
  if (e.key === "Escape"){
    if (!$("#ctxmenu").hidden){ fecharMenu(); return; }
    selecionar(null);
    return;
  }
  var s = selecionado();
  if (!s) return;
  if (e.key === "F2"){ e.preventDefault(); guard(function(){ renomear(s); }); }
  else if (e.key === "Delete"){ e.preventDefault(); guard(function(){ apagar(s); }); }
  else if (e.key === "Enter"){ e.preventDefault(); abrir(s); }
});

/* ================= inicializacao ================= */

$("#cmd-enviar").addEventListener("click", function(){ $("#file-input").click(); });
$("#file-input").addEventListener("change", function(){
  uploadFiles(this.files);
  this.value = "";
});
$("#cmd-novo").addEventListener("click", function(ev){
  ev.stopPropagation();
  var r = this.getBoundingClientRect();
  abrirMenu(null, r.left, r.bottom + 2);
});
$("#cmd-renomear").addEventListener("click", function(){ var s = selecionado(); if (s) guard(function(){ renomear(s); }); });
$("#cmd-ext").addEventListener("click", function(){ var s = selecionado(); if (s && !s.is_dir) guard(function(){ mudarExt(s); }); });
$("#cmd-baixar").addEventListener("click", function(){ var s = selecionado(); if (s && !s.is_dir) download(s); });
$("#cmd-excluir").addEventListener("click", function(){ var s = selecionado(); if (s) guard(function(){ apagar(s); }); });

$("#nav-back").addEventListener("click", voltar);
$("#nav-fwd").addEventListener("click", avancar);
$("#nav-up").addEventListener("click", subir);
$("#nav-refresh").addEventListener("click", function(){ load(state.cwd); });

$("#busca").addEventListener("input", function(){ state.busca = this.value; renderList(); });

var cols = document.querySelectorAll(".colheader .col");
for (var ci = 0; ci < cols.length; ci++){
  cols[ci].addEventListener("click", function(){
    var k = this.dataset.sort;
    if (state.sort.key === k) state.sort.dir = -state.sort.dir;
    else { state.sort.key = k; state.sort.dir = 1; }
    renderList();
    atualizarSetas();
  });
}

$("#filelist").addEventListener("click", function(ev){
  if (!ev.target.closest(".row")) selecionar(null);
});
$("#filelist").addEventListener("contextmenu", function(ev){
  if (ev.target.closest(".row")) return;
  ev.preventDefault();
  selecionar(null);
  abrirMenu(null, ev.clientX, ev.clientY);
});

document.addEventListener("click", function(){ fecharMenu(); });
$("#ctxmenu").addEventListener("click", function(ev){ ev.stopPropagation(); });

atualizarSetas();
atualizarBotoes();

var p0 = pathFromHash();
if (p0){
  state.historico = [p0];
  state.histIdx = 0;
  load(p0);
} else {
  setHash("");
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 7) Inicializacao / banner
# ---------------------------------------------------------------------------

def ip_privado(ip):
    partes = ip.split(".")
    if len(partes) != 4:
        return False
    try:
        a, b = int(partes[0]), int(partes[1])
    except ValueError:
        return False
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


def ips_da_rede():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip_privado(ip):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def main():
    global ROOT

    ap = argparse.ArgumentParser(
        description="Servidor de armazenamento acessivel pela rede local (Termux/Linux/Windows)."
    )
    ap.add_argument("-p", "--port", type=int,
                    default=int(os.environ.get("STORAGE_PORT", "8080")),
                    help="porta HTTP (padrao: 8080)")
    ap.add_argument("--host", default="0.0.0.0",
                    help="interface de rede (padrao: 0.0.0.0 = todas)")
    ap.add_argument("-d", "--dir", default=str(RAIZ_PADRAO),
                    help="pasta de armazenamento (padrao: ./storage ao lado do script)")
    args = ap.parse_args()

    ROOT = Path(args.dir).expanduser().resolve()
    ROOT.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  SERVICO DE ARMAZENAMENTO - REDE LOCAL")
    print("=" * 60)
    print("  Pasta:  %s" % ROOT)
    print("  Local:  http://127.0.0.1:%d" % args.port)
    for ip in ips_da_rede():
        print("  Rede:   http://%s:%d" % (ip, args.port))
    print("-" * 60)
    print("  Acesse pelo navegador de qualquer aparelho da mesma rede.")
    print("  Para parar: Ctrl+C")
    print("=" * 60)

    try:
        app.run(host=args.host, port=args.port, threaded=True, debug=False)
    except OSError as e:
        print("[!] Nao foi possivel iniciar na porta %d: %s" % (args.port, e))
        print("    Tente outra porta, por exemplo: python server.py --port 8090")
        sys.exit(1)


if __name__ == "__main__":
    main()
