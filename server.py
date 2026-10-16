#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Servidor de Armazenamento para Rede Local
==========================================

Servidor em Python + Flask que disponibiliza uma pagina web completa
para gerenciar arquivos em qualquer aparelho da mesma rede (Wi-Fi):

  - Upload de um ou varios arquivos (com arrastar e soltar e barra de progresso)
  - Download de arquivos
  - Criar pasta / apagar pasta (recursivo)
  - Criar arquivo de texto
  - Renomear arquivos e pastas
  - Alterar a extensao (tipo) de um arquivo
  - Pre-visualizar imagens, videos, audios e PDFs
  - Editar arquivos de texto direto no navegador

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
# 6) Interface web (pagina unica)
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Armazenamento</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>&#128230;</text></svg>">
<style>
*{box-sizing:border-box}
:root{
  --bg:#0f1115; --panel:#161a22; --panel2:#1d2330; --border:#2a3140;
  --text:#e8ecf3; --muted:#8b94a7; --accent:#5b8cff; --accent2:#3ad6a5; --danger:#ff5d6c;
}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--text);min-height:100vh;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Ubuntu,"Helvetica Neue",Arial,sans-serif}
.top{position:sticky;top:0;z-index:10;display:flex;align-items:center;justify-content:space-between;
  gap:12px;padding:12px 16px;background:rgba(22,26,34,.97);border-bottom:1px solid var(--border)}
.brand{font-weight:800;font-size:18px;display:flex;gap:8px;align-items:center;white-space:nowrap}
.info{font-size:12px;color:var(--muted);text-align:right;max-width:48vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
main{max-width:980px;margin:0 auto;padding:16px 16px 90px}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:9px 14px;border-radius:10px;
  border:1px solid var(--border);background:var(--panel2);color:var(--text);font-size:14px;
  font-weight:600;cursor:pointer;text-decoration:none;line-height:1.2}
.btn:hover{border-color:var(--accent)}
.btn.primary{background:var(--accent);border-color:transparent;color:#fff}
.btn.primary:hover{filter:brightness(1.1)}
.btn.ghost{background:transparent}
.btn:disabled{opacity:.5;cursor:default}
.crumbs{display:flex;flex-wrap:wrap;align-items:center;gap:2px;margin:0 0 12px}
.crumb{background:transparent;border:none;color:var(--muted);font-size:14px;padding:6px 8px;
  border-radius:8px;cursor:pointer;font-weight:500}
.crumb:hover{background:var(--panel2);color:var(--text)}
.crumb.current{color:var(--text);font-weight:800}
.crumb-sep{color:#3a4356}
.files{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.item{display:flex;align-items:center;gap:12px;padding:10px 12px;background:var(--panel);
  border:1px solid var(--border);border-radius:12px}
.item:hover{border-color:#39435a}
.item-icon{font-size:22px;width:30px;text-align:center;flex:none}
.item-main{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.item-name{background:none;border:none;color:var(--text);font-size:15px;font-weight:600;
  text-align:left;padding:0;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  max-width:100%;font-family:inherit}
.item-name:hover{color:var(--accent)}
.item-meta{font-size:12px;color:var(--muted);display:flex;gap:6px;flex-wrap:wrap}
.item-actions{display:flex;gap:2px;flex:none}
.act{background:transparent;border:1px solid transparent;border-radius:8px;padding:6px 7px;
  font-size:15px;cursor:pointer;color:var(--text);line-height:1}
.act:hover{background:var(--panel2);border-color:var(--border)}
.empty{padding:44px 16px;text-align:center;color:var(--muted);border:1px dashed var(--border);border-radius:12px}
.overlay{position:fixed;inset:0;background:rgba(5,7,10,.72);display:flex;align-items:center;
  justify-content:center;padding:16px;z-index:100}
.modal{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:18px;
  width:min(600px,100%);max-height:92vh;overflow:auto;box-shadow:0 20px 60px rgba(0,0,0,.55)}
.modal h3{margin:0 0 14px;font-size:17px}
.modal-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:12px}
.modal-head h3{margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:16px;flex-wrap:wrap}
.field{display:flex;flex-direction:column;gap:6px;margin-bottom:12px}
.label{font-size:13px;color:var(--muted)}
.input,textarea.input{width:100%;background:#0c0f14;border:1px solid var(--border);border-radius:10px;
  color:var(--text);padding:10px 12px;font-size:14px;outline:none;font-family:inherit}
.input:focus{border-color:var(--accent)}
textarea.input{min-height:110px;resize:vertical}
.preview{font-size:12px;color:var(--muted);margin:-4px 0 10px;word-break:break-all}
.editor{width:100%;min-height:45vh;background:#0c0f14;border:1px solid var(--border);border-radius:10px;
  color:var(--text);padding:12px;font-family:ui-monospace,Consolas,"Courier New",monospace;
  font-size:13px;resize:vertical;outline:none}
.pv-media{max-width:100%;max-height:60vh;display:block;margin:0 auto;border-radius:10px;background:#000}
.pv-frame{width:100%;height:60vh;border:1px solid var(--border);border-radius:10px;background:#fff}
.hint{color:var(--muted);font-size:14px;text-align:center;padding:14px}
.uploads{position:fixed;left:12px;bottom:12px;width:min(340px,90vw);display:none;flex-direction:column;
  gap:8px;z-index:40;max-height:50vh;overflow:auto}
.uploads.show{display:flex}
.up{background:var(--panel2);border:1px solid var(--border);border-radius:10px;padding:8px 10px;font-size:13px}
.up-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.up .bar{height:6px;background:#0c0f14;border-radius:4px;margin-top:6px;overflow:hidden}
.up .bar i{display:block;height:100%;width:0;background:var(--accent);transition:width .15s}
.up.done .bar i{background:var(--accent2)}
.up.error .bar i{background:var(--danger)}
.toasts{position:fixed;top:64px;left:50%;transform:translateX(-50%);display:flex;flex-direction:column;
  gap:8px;z-index:200;width:min(420px,92vw)}
.toast{background:var(--panel2);border:1px solid var(--border);border-left:4px solid var(--accent2);
  padding:10px 14px;border-radius:10px;font-size:14px;box-shadow:0 10px 30px rgba(0,0,0,.45);
  transition:opacity .3s,transform .3s;word-break:break-word}
.toast.error{border-left-color:var(--danger)}
.toast.out{opacity:0;transform:translateY(8px)}
#drop-overlay{position:fixed;inset:0;display:none;align-items:center;justify-content:center;
  background:rgba(10,12,16,.85);border:3px dashed var(--accent);z-index:300;font-size:20px;
  font-weight:800;color:var(--accent);text-align:center;padding:20px}
body.dragging #drop-overlay{display:flex}
@media (max-width:520px){
  .act{padding:8px 8px;font-size:16px}
  main{padding:12px 10px 90px}
}
</style>
</head>
<body>
<header class="top">
  <div class="brand">&#128230; <span>Armazenamento</span></div>
  <div id="info" class="info"></div>
</header>

<main>
  <div class="toolbar">
    <button class="btn primary" id="btn-upload">&#11014;&#65039; Enviar arquivos</button>
    <button class="btn" id="btn-folder">&#128193; Nova pasta</button>
    <button class="btn" id="btn-file">&#128196; Novo arquivo</button>
    <button class="btn ghost" id="btn-refresh">&#128260; Atualizar</button>
    <input type="file" id="file-input" multiple hidden>
  </div>
  <nav id="crumbs" class="crumbs"></nav>
  <ul id="files" class="files"></ul>
</main>

<div id="uploads" class="uploads"></div>
<div id="toasts" class="toasts"></div>
<div id="drop-overlay">&#128229; Solte os arquivos para enviar</div>

<script>
"use strict";
var $ = function(sel, root){ return (root || document).querySelector(sel); };
var state = { cwd: "", entries: [] };

/* ---------------- utilitarios ---------------- */

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
  try {
    return new Date(ms).toLocaleString("pt-BR", {day:"2-digit", month:"2-digit",
      year:"2-digit", hour:"2-digit", minute:"2-digit"});
  } catch(e){ return ""; }
}

async function api(url, opts){
  var r = await fetch(url, Object.assign({cache:"no-store"}, opts || {}));
  var d = null;
  try { d = await r.json(); } catch(e){}
  if (!r.ok || !d || !d.ok) throw new Error((d && d.error) || ("Erro HTTP " + r.status));
  return d;
}

function post(url, body){
  return api(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body || {})});
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

/* ---------------- icones ---------------- */

var EXT_IMG = ["png","jpg","jpeg","gif","webp","bmp","svg","ico","avif","heic"];
var EXT_VID = ["mp4","mkv","webm","mov","m4v","3gp","avi"];
var EXT_AUD = ["mp3","wav","ogg","oga","m4a","flac","aac","opus"];
var EXT_ZIP = ["zip","rar","7z","tar","gz","bz2","xz","apk"];
var EXT_TXT = ["txt","md","markdown","log","csv","tsv","json","xml","yml","yaml","ini","cfg",
  "conf","env","sh","bash","zsh","py","js","ts","jsx","tsx","html","htm","css","scss","c","h",
  "cpp","hpp","java","kt","rs","go","rb","php","pl","lua","sql","bat","ps1","cmd","srt","vtt"];

function ehTexto(entry){
  var ext = extOf(entry.name).toLowerCase();
  var nome = entry.name.toLowerCase();
  return EXT_TXT.indexOf(ext) !== -1 || ["dockerfile","makefile"].indexOf(nome) !== -1;
}

function iconFor(entry){
  var ext = extOf(entry.name).toLowerCase();
  if (EXT_IMG.indexOf(ext) !== -1) return "\uD83D\uDDBC\uFE0F";
  if (EXT_VID.indexOf(ext) !== -1) return "\uD83C\uDFAC";
  if (EXT_AUD.indexOf(ext) !== -1) return "\uD83C\uDFB5";
  if (EXT_ZIP.indexOf(ext) !== -1) return "\uD83D\uDDDC\uFE0F";
  if (ext === "pdf") return "\uD83D\uDCD5";
  if (["doc","docx","odt","rtf"].indexOf(ext) !== -1) return "\uD83D\uDCD8";
  if (["xls","xlsx","ods"].indexOf(ext) !== -1) return "\uD83D\uDCD7";
  if (["ppt","pptx","odp"].indexOf(ext) !== -1) return "\uD83D\uDCD9";
  if (ext === "py") return "\uD83D\uDC0D";
  if (ehTexto(entry)) return "\uD83D\uDCDD";
  return "\uD83D\uDCC4";
}

/* ---------------- navegacao ---------------- */

function pathFromHash(){
  var h = location.hash;
  if (h.indexOf("#/") === 0) return decodeURI(h.slice(2));
  return "";
}

function go(path){
  var alvo = path ? "#/" + encodeURI(path) : "#/";
  if (location.hash === alvo) load(path);
  else location.hash = alvo;
}

async function load(path){
  try {
    var d = await api("/api/list?path=" + encodeURIComponent(path || ""));
    state.cwd = d.path;
    state.entries = d.entries;
    renderCrumbs();
    renderEntries();
    loadInfo();
  } catch(e){
    toast(e.message, "error");
  }
}

async function loadInfo(){
  try {
    var d = await api("/api/info");
    var elInfo = $("#info");
    elInfo.textContent = "Livre: " + fmtSize(d.free) + " de " + fmtSize(d.total);
    elInfo.title = "Pasta: " + d.root;
  } catch(e){}
}

function renderCrumbs(){
  var nav = $("#crumbs");
  nav.innerHTML = "";
  var partes = state.cwd ? state.cwd.split("/") : [];
  function mk(label, path, atual){
    var b = el("button", {class:"crumb" + (atual ? " current" : ""), text: label, title: label});
    b.addEventListener("click", function(){ go(path); });
    return b;
  }
  nav.appendChild(mk("\uD83C\uDFE0 In\u00edcio", "", partes.length === 0));
  var acc = "";
  partes.forEach(function(p, i){
    acc = acc ? acc + "/" + p : p;
    nav.appendChild(el("span", {class:"crumb-sep", text:"\u203A"}));
    nav.appendChild(mk(p, acc, i === partes.length - 1));
  });
}

function renderEntries(){
  var list = $("#files");
  list.innerHTML = "";
  if (!state.entries.length){
    list.appendChild(el("li", {class:"empty", text:"Pasta vazia \u2014 envie arquivos ou crie itens."}));
    return;
  }
  state.entries.forEach(function(entry){
    var ehDir = entry.is_dir;

    var nome = el("button", {class:"item-name", title: entry.name, text: entry.name});
    nome.addEventListener("click", function(){
      if (ehDir) go(joinPath(state.cwd, entry.name));
      else previewFile(entry);
    });

    var meta = el("div", {class:"item-meta"}, [
      el("span", {text: ehDir ? "pasta" : fmtSize(entry.size)}),
      el("span", {text:"\u2022"}),
      el("span", {text: fmtDate(entry.mtime)})
    ]);

    var botoes = el("div", {class:"item-actions"});
    if (!ehDir){
      botoes.appendChild(iconBtn("\u2B07\uFE0F", "Baixar", function(){ guard(function(){ download(entry); }); }));
      botoes.appendChild(iconBtn("\uD83C\uDFF7\uFE0F", "Mudar extens\u00e3o", function(){ guard(function(){ mudarExt(entry); }); }));
    }
    botoes.appendChild(iconBtn("\u270F\uFE0F", "Renomear", function(){ guard(function(){ renomear(entry); }); }));
    botoes.appendChild(iconBtn("\uD83D\uDDD1\uFE0F", "Apagar", function(){ guard(function(){ apagar(entry); }); }));

    list.appendChild(el("li", {class:"item"}, [
      el("span", {class:"item-icon", text: ehDir ? "\uD83D\uDCC1" : iconFor(entry)}),
      el("div", {class:"item-main"}, [nome, meta]),
      botoes
    ]));
  });
}

function iconBtn(icone, titulo, fn){
  var b = el("button", {class:"act", title: titulo, text: icone});
  b.addEventListener("click", function(ev){ ev.stopPropagation(); fn(); });
  return b;
}

/* ---------------- acoes ---------------- */

function download(entry){
  location.href = "/download?path=" + encodeURIComponent(joinPath(state.cwd, entry.name));
}

async function apagar(entry){
  var msg = "Apagar " + (entry.is_dir ? "a pasta" : "o arquivo") + " \"" + entry.name + "\"?";
  if (entry.is_dir) msg += "\n\nTodo o conteudo dentro dela sera apagado.";
  if (!confirm(msg)) return;
  await post("/api/delete", {path: joinPath(state.cwd, entry.name)});
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

/* ---------------- pre-visualizacao / edicao ---------------- */

async function previewFile(entry){
  var rel = joinPath(state.cwd, entry.name);
  var url = "/download?inline=1&path=" + encodeURIComponent(rel);
  var ext = extOf(entry.name).toLowerCase();

  var overlay = el("div", {class:"overlay"});
  var box = el("div", {class:"modal"});
  function fechar(){ overlay.remove(); document.removeEventListener("keydown", onKey); }
  function onKey(e){ if (e.key === "Escape") fechar(); }
  document.addEventListener("keydown", onKey);
  overlay.addEventListener("click", function(e){ if (e.target === overlay) fechar(); });

  var btnFechar = el("button", {class:"btn ghost", text:"\u2715"});
  btnFechar.addEventListener("click", fechar);
  box.appendChild(el("div", {class:"modal-head"}, [
    el("h3", {text: entry.name, title: entry.name}),
    btnFechar
  ]));

  var corpo = el("div", {class:"preview-body"});
  box.appendChild(corpo);

  if (EXT_IMG.indexOf(ext) !== -1){
    corpo.appendChild(el("img", {class:"pv-media", src: url}));
  } else if (EXT_VID.indexOf(ext) !== -1){
    corpo.appendChild(el("video", {class:"pv-media", src: url, controls:"", playsinline:""}));
  } else if (EXT_AUD.indexOf(ext) !== -1){
    corpo.appendChild(el("audio", {class:"pv-media", src: url, controls:""}));
  } else if (ext === "pdf"){
    corpo.appendChild(el("iframe", {class:"pv-frame", src: url}));
  } else if (ehTexto(entry)){
    var ta = el("textarea", {class:"editor", spellcheck:"false"});
    ta.value = "Carregando...";
    ta.disabled = true;
    corpo.appendChild(ta);
    try {
      var d = await api("/api/read?path=" + encodeURIComponent(rel));
      ta.value = d.content;
      ta.disabled = false;
      var salvar = el("button", {class:"btn primary", text:"\uD83D\uDCBE Salvar"});
      salvar.addEventListener("click", function(){
        guard(async function(){
          await post("/api/save", {path: rel, content: ta.value});
          toast("Salvo: " + entry.name);
        });
      });
      corpo.appendChild(el("div", {class:"modal-actions"}, [salvar]));
    } catch(e){
      ta.value = "";
      ta.disabled = true;
      corpo.appendChild(el("p", {class:"hint", text:"N\u00e3o foi poss\u00edvel abrir: " + e.message}));
    }
  } else {
    corpo.appendChild(el("p", {class:"hint", text:"Sem pr\u00e9-visualiza\u00e7\u00e3o para este tipo. Use o bot\u00e3o abaixo para baixar."}));
  }

  var baixar = el("a", {class:"btn ghost", href: "/download?path=" + encodeURIComponent(rel), text:"\u2B07\uFE0F Baixar"});
  baixar.setAttribute("download", entry.name);
  box.appendChild(el("div", {class:"modal-actions"}, [baixar]));

  overlay.appendChild(box);
  document.body.appendChild(overlay);
}

/* ---------------- dialogo generico ---------------- */

function formDialog(opts){
  return new Promise(function(resolve){
    var inputs = {};
    var fechado = false;

    var overlay = el("div", {class:"overlay"});
    var box = el("div", {class:"modal"});
    box.appendChild(el("h3", {text: opts.title}));

    var corpo = el("div", {class:"form"});
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

      corpo.appendChild(el("label", {class:"field"}, [
        el("span", {class:"label", text: f.label}),
        input
      ]));
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

    var cancelar = el("button", {class:"btn ghost", text:"Cancelar"});
    cancelar.addEventListener("click", function(){ fim(false); });
    var okBtn = el("button", {class:"btn primary", text: opts.okLabel || "OK"});
    okBtn.addEventListener("click", function(){ fim(true); });
    box.appendChild(el("div", {class:"modal-actions"}, [cancelar, okBtn]));

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

/* ---------------- upload ---------------- */

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

/* ---------------- arrastar e soltar ---------------- */

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

/* ---------------- inicializacao ---------------- */

document.getElementById("btn-upload").addEventListener("click", function(){ $("#file-input").click(); });
document.getElementById("file-input").addEventListener("change", function(){
  uploadFiles(this.files);
  this.value = "";
});
document.getElementById("btn-folder").addEventListener("click", function(){ guard(novaPasta); });
document.getElementById("btn-file").addEventListener("click", function(){ guard(novoArquivo); });
document.getElementById("btn-refresh").addEventListener("click", function(){ load(state.cwd); });

window.addEventListener("hashchange", function(){ load(pathFromHash()); });

if (location.hash) load(pathFromHash());
else location.hash = "#/";
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
