"""Partage des mires sur le réseau local.

La machine de régie lance le serveur ; chaque PC ouvre http://<ip-régie>:<port> dans son
navigateur (lien ou QR code) : il reçoit automatiquement un identifiant de source (01 · FALCON,
02 · ORION…) et peut lancer la boucle en plein écran dans le navigateur ou télécharger le MP4.
Aucune installation, aucune clé USB.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import pattern

DEFAULT_PORT = 8765


def lan_ip() -> str:
    """Adresse IP locale utilisée pour sortir sur le réseau (aucun paquet n'est envoyé)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


def cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    p = Path(base) / "SyncLive" / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


class Registry:
    """Attribution des identifiants de source aux machines qui se connectent."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.by_client: dict[str, int] = {}
        self.info: dict[int, dict] = {}

    def assign(self, client: str, want: int | None, ip: str, ua: str) -> int:
        with self.lock:
            if want and 1 <= want <= pattern.MAX_SOURCES:
                sid = want
                # l'identifiant demandé est libéré par son ancien propriétaire
                for c, s in list(self.by_client.items()):
                    if s == sid and c != client:
                        del self.by_client[c]
            elif client in self.by_client:
                sid = self.by_client[client]
            else:
                used = set(self.by_client.values())
                sid = next(i for i in range(1, pattern.MAX_SOURCES + 1) if i not in used)
            self.by_client[client] = sid
            self.info[sid] = {"id": sid, "label": pattern.source_label(sid), "ip": ip,
                              "agent": _short_agent(ua), "seen": time.time(), "playing": None}
            return sid

    def touch(self, sid: int, playing: str | None = None) -> None:
        with self.lock:
            if sid in self.info:
                self.info[sid]["seen"] = time.time()
                if playing is not None:
                    self.info[sid]["playing"] = playing

    def clients(self) -> list[dict]:
        with self.lock:
            active = set(self.by_client.values())
            return sorted((dict(v) for k, v in self.info.items() if k in active), key=lambda d: d["id"])


def _short_agent(ua: str) -> str:
    for k in ("Edg", "Chrome", "Firefox", "Safari"):
        if k in ua:
            os_ = "Windows" if "Windows" in ua else "Mac" if "Mac" in ua else "Linux" if "Linux" in ua else ""
            return f"{'Edge' if k == 'Edg' else k} {os_}".strip()
    return ua[:40]


class LoopServer:
    def __init__(self, port: int = DEFAULT_PORT, periods: int = 16):
        self.port = port
        self.periods = periods
        self.registry = Registry()
        self.cache = cache_dir()
        self._render_locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{lan_ip()}:{self.port}"

    @property
    def running(self) -> bool:
        return self.httpd is not None

    def start(self) -> None:
        if self.httpd:
            return
        srv = self

        class Handler(_Handler):
            server_ref = srv

        self.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def loop_file(self, sid: int, w: int, h: int, fps_label: str) -> Path:
        spec = pattern.PatternSpec(w, h, pattern.FRAME_RATES[fps_label], source=sid, periods=self.periods)
        out = self.cache / spec.filename()
        with self._locks_lock:
            lock = self._render_locks.setdefault(out.name, threading.Lock())
        with lock:
            if not out.exists():
                tmp = out.with_suffix(".part.mp4")
                pattern.render(spec, tmp)
                shutil.move(str(tmp), str(out))
        return out


RES_RE = re.compile(r"^/loop/(\d{1,2})/(\d{3,4})x(\d{3,4})/([\d.]+)\.mp4$")


class _Handler(BaseHTTPRequestHandler):
    server_ref: LoopServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silencieux
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        srv = self.server_ref
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path in ("/favicon.ico", "/favicon.svg", "/assets/logo.svg", "/assets/icon.png",
                            "/apple-touch-icon.png"):
                from .brand import asset
                name, ctype = {"/favicon.ico": ("favicon.ico", "image/x-icon"),
                               "/favicon.svg": ("favicon.svg", "image/svg+xml"),
                               "/assets/logo.svg": ("lockup-dark-bg.svg", "image/svg+xml"),
                               "/assets/icon.png": ("icon-192.png", "image/png"),
                               "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png")}[u.path]
                self._send(200, asset(name).read_bytes(), ctype)
            elif u.path == "/api/config":
                self._json({
                    "resolutions": [{"label": k, "w": w, "h": h} for k, (w, h) in pattern.RESOLUTIONS.items()],
                    "fps": list(pattern.FRAME_RATES),
                    "max": pattern.MAX_SOURCES,
                })
            elif u.path == "/api/hello":
                want = int(q["want"]) if q.get("want", "").isdigit() else None
                sid = srv.registry.assign(q.get("client", self.client_address[0]), want,
                                          self.client_address[0], self.headers.get("User-Agent", ""))
                self._json({"id": sid, "label": pattern.source_label(sid), "name": pattern.source_name(sid)})
            elif u.path == "/api/ping":
                if q.get("id", "").isdigit():
                    srv.registry.touch(int(q["id"]), q.get("playing"))
                self._json({"ok": True})
            elif u.path == "/api/clients":
                self._json(srv.registry.clients())
            elif RES_RE.match(u.path):
                m = RES_RE.match(u.path)
                sid, w, h, fps = int(m[1]), int(m[2]), int(m[3]), m[4]
                if fps not in pattern.FRAME_RATES or not (1 <= sid <= pattern.MAX_SOURCES) \
                        or not (320 <= w <= 4096 and 240 <= h <= 4096):
                    self._send(400, b"bad request", "text/plain")
                    return
                f = srv.loop_file(sid, w - w % 2, h - h % 2, fps)
                self._file(f, download=q.get("dl") == "1")
            else:
                self._send(404, b"not found", "text/plain")
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def _file(self, path: Path, download: bool) -> None:
        size = path.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        partial = False
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            if a.strip():
                start = int(a)
                end = int(b) if b.strip() else size - 1
            elif b.strip():
                start = max(0, size - int(b))
            end = min(end, size - 1)
            partial = True
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SyncLive</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="icon" href="/favicon.ico" sizes="any"><link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>
:root{--bg:#16161A;--panel:#1B1B20;--line:#2A2A30;--txt:#F2F0EB;--mut:#8A8780;--hi:#FF5A1F}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 "Segoe UI",system-ui,sans-serif}
.wrap{max-width:640px;margin:0 auto;padding:48px 20px}
.brand{font-weight:600;letter-spacing:.2px;margin-bottom:40px}
.id{display:flex;align-items:flex-end;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:28px;margin-bottom:28px}
.lab{color:var(--mut);font-size:13px;margin-bottom:6px}
.num{font-size:72px;font-weight:600;line-height:1}.name{color:var(--mut);letter-spacing:.35em;font-size:15px;margin-top:10px}
.step{display:flex;gap:8px}
button,select,a.btn{font:inherit;color:var(--txt);background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:9px 14px;cursor:pointer;text-decoration:none}
button:hover,a.btn:hover{border-color:#3a3c42}
.step button{width:40px;padding:9px 0}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}select{width:100%}
.actions{display:flex;gap:10px;margin-top:24px;flex-wrap:wrap}
.primary{background:var(--hi)!important;color:#16161A!important;border-color:var(--hi)!important;font-weight:600}
.primary:hover{background:#FF7240!important}
.note{color:var(--mut);font-size:13px;margin-top:28px;border-top:1px solid var(--line);padding-top:18px}
#stage{position:fixed;inset:0;background:#000;display:none;cursor:none}#stage video{width:100%;height:100%;object-fit:contain}
#wait{position:fixed;inset:0;display:none;place-items:center;background:var(--bg);color:var(--mut)}
@media(max-width:520px){.grid{grid-template-columns:1fr}.num{font-size:56px}}
</style></head><body>
<div class="wrap">
  <div class="brand"><img src="/assets/logo.svg" alt="SyncLive" height="30"></div>
  <div class="id">
    <div><div class="lab">Source</div><div class="num" id="num">--</div><div class="name" id="nm"></div></div>
    <div class="step"><button id="prev" aria-label="Source précédente">&lsaquo;</button><button id="next" aria-label="Source suivante">&rsaquo;</button></div>
  </div>
  <div class="grid">
    <div><div class="lab">Résolution</div><select id="res"></select></div>
    <div><div class="lab">Images par seconde</div><select id="fps"></select></div>
  </div>
  <div class="actions">
    <button class="primary" id="play">Lancer en plein écran</button>
    <a class="btn" id="dl" href="#">Télécharger le MP4</a>
  </div>
  <p class="note">Clic ou Échap pour quitter le plein écran. La première préparation d'un format prend quelques secondes.<br>
  Le navigateur a sa propre latence audio : si cette machine lit ses médias avec un autre lecteur en production
  (VLC, OBS, logiciel de régie), télécharge le MP4 et lis-le en boucle avec ce lecteur.</p>
</div>
<div id="stage"><video id="v" loop playsinline></video></div>
<div id="wait">Préparation de la boucle</div>
<script>
const $=s=>document.querySelector(s);let cfg,me={id:null};
const client=(()=>{try{let c=localStorage.getItem('sl_client');if(!c){c=Math.random().toString(36).slice(2);localStorage.setItem('sl_client',c)}return c}catch(e){return 'x'+Math.random()}})();
async function hello(want){const u=new URL('/api/hello',location);u.searchParams.set('client',client);if(want)u.searchParams.set('want',want);
 me=await (await fetch(u)).json();$('#num').textContent=String(me.id).padStart(2,'0');$('#nm').textContent=me.name;
 try{localStorage.setItem('sl_id',me.id)}catch(e){};history.replaceState(null,'','?id='+me.id);upd()}
function src(dl){const r=cfg.resolutions[$('#res').value];return `/loop/${me.id}/${r.w}x${r.h}/${$('#fps').value}.mp4`+(dl?'?dl=1':'')}
function upd(){if(me.id)$('#dl').href=src(true)}
async function init(){cfg=await (await fetch('/api/config')).json();
 const sw=Math.round(screen.width*devicePixelRatio),sh=Math.round(screen.height*devicePixelRatio);let best=3,bd=1e9;
 cfg.resolutions.forEach((r,i)=>{const o=document.createElement('option');o.value=i;o.textContent=r.label;$('#res').append(o);const d=Math.abs(r.w-sw)+Math.abs(r.h-sh);if(d<bd){bd=d;best=i}});
 $('#res').value=best;cfg.fps.forEach(f=>{const o=document.createElement('option');o.value=f;o.textContent=f;$('#fps').append(o)});$('#fps').value='60';
 $('#res').onchange=$('#fps').onchange=upd;
 const p=new URLSearchParams(location.search);let want=+p.get('id')||0;if(!want){try{want=+localStorage.getItem('sl_id')||0}catch(e){}}
 await hello(want);setInterval(()=>fetch(`/api/ping?id=${me.id}`),5000)}
$('#prev').onclick=()=>hello(Math.max(1,me.id-1));$('#next').onclick=()=>hello(Math.min(cfg.max,me.id+1));
$('#play').onclick=async()=>{const v=$('#v');$('#wait').style.display='grid';
 v.oncanplay=()=>{v.oncanplay=null;$('#wait').style.display='none';$('#stage').style.display='block';v.play();
  fetch(`/api/ping?id=${me.id}&playing=${encodeURIComponent($('#fps').value+' i/s')}`)};
 v.src=src(false);await document.documentElement.requestFullscreen().catch(()=>{})};
function stop(){const v=$('#v');v.pause();$('#stage').style.display='none';$('#wait').style.display='none';if(document.fullscreenElement)document.exitFullscreen()}
$('#stage').onclick=stop;document.addEventListener('fullscreenchange',()=>{if(!document.fullscreenElement)stop()});
init();
</script></body></html>
"""
