"""
Bot de Telegram -> ToDus S3 para Render, con enlace de reproducción web.

Variables obligatorias:
    API_ID, API_HASH, BOT_TOKEN

Opcionales:
    ALLOWED_IDS, MAX_MB, TODUS_NS, UPLOAD_PATH, ALT_LINK_HOST, PUBLIC_BASE

PUBLIC_BASE debe ser la URL pública de tu servicio Render, por ejemplo:
    https://nombre-de-tu-servicio.onrender.com

El enlace web incluye el enlace compartido de ToDus codificado en el parámetro
src. El servidor solo acepta URLs HTTPS de s3.todus.cu bajo /stream/users/.
"""
import asyncio
import base64
import logging
import mimetypes
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from telethon import TelegramClient, events
from telethon.sessions import MemorySession

logging.basicConfig(level=logging.INFO)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"].strip()
BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
MAX_MB = int(os.environ.get("MAX_MB", "0") or 0)

NS_FIXED = os.environ.get("TODUS_NS", "").strip()
UPLOAD_PATH = os.environ.get("UPLOAD_PATH", "").strip().strip("/")
ALT_HOST = os.environ.get("ALT_LINK_HOST", "").strip()
PUBLIC_BASE = (
    os.environ.get("PUBLIC_BASE", "").strip().rstrip("/")
    or os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
)

STATUS = {"todus": "sin comprobar", "telegram": "sin conectar"}


def ns_for(user_id):
    return NS_FIXED or f"tg_{user_id}"


def path_for(user_id):
    return UPLOAD_PATH.replace("{id}", str(user_id)).strip("/")


def check_todus():
    t = time.time()
    try:
        socket.create_connection(("s3.todus.cu", 443), timeout=10).close()
        STATUS["todus"] = f"ToDus alcanzable ({time.time() - t:.1f}s)"
    except Exception as e:
        STATUS["todus"] = f"ToDus no alcanzable desde este host: {e!r}"
    print(STATUS["todus"], flush=True)


def make_web_link(source_url):
    """Crea una ruta de Render sin convertir el servicio en un proxy abierto."""
    if not PUBLIC_BASE:
        return None

    m = re.fullmatch(
        r"https://s3\.todus\.cu/stream/users/([^/]+)/(\d+)/([^/?#]+)",
        source_url
    )
    if not m:
        return None

    shard, file_id, filename = m.groups()
    encoded = base64.urlsafe_b64encode(source_url.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{PUBLIC_BASE}/todus/v3/{file_id}/{filename}?src={encoded}"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        self._handle(head_only=False)

    def do_HEAD(self):
        self._handle(head_only=True)

    def _handle(self, head_only=False):
        if self.path == "/":
            body = f"Telegram: {STATUS['telegram']}\nToDus: {STATUS['todus']}\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
            return

        m = re.fullmatch(r"/todus/v3/(\d+)/([^/?#]+)", urlsplit(self.path).path)
        query = parse_qs(urlsplit(self.path).query)
        if not m or "src" not in query:
            self.send_error(404, "Ruta no encontrada")
            return

        file_id, filename = m.groups()
        token = query["src"][0]

        try:
            padded = token + "=" * (-len(token) % 4)
            source_url = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
            parsed = urlsplit(source_url)
            source_match = re.fullmatch(
                r"/stream/users/[^/]+/(\d+)/([^/]+)",
                parsed.path
            )
            if (
                parsed.scheme != "https"
                or parsed.hostname != "s3.todus.cu"
                or parsed.port not in (None, 443)
                or not source_match
                or source_match.group(1) != file_id
                or source_match.group(2) != filename
            ):
                raise ValueError("URL de origen no permitida")
        except Exception:
            self.send_error(400, "Enlace de origen inválido")
            return

        headers = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
        client_range = self.headers.get("Range")
        if client_range:
            headers["Range"] = client_range

        req = Request(source_url, headers=headers, method="HEAD" if head_only else "GET")
        try:
            upstream = urlopen(req, timeout=45)
        except HTTPError as exc:
            upstream = exc
        except (URLError, TimeoutError, OSError) as exc:
            self.send_error(502, f"ToDus no respondió: {exc}")
            return

        try:
            status = getattr(upstream, "status", upstream.getcode())
            self.send_response(status)

            content_type = upstream.headers.get("Content-Type", "")
            guessed_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            if not content_type or content_type == "application/octet-stream":
                content_type = guessed_type
            if filename.lower().endswith((".mp4", ".m4v")):
                content_type = "video/mp4"

            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            for header in ("Content-Length", "Content-Range", "Accept-Ranges"):
                value = upstream.headers.get(header)
                if value:
                    self.send_header(header, value)
            self.end_headers()

            if not head_only and status not in (204, 304, 416):
                while True:
                    chunk = upstream.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            logging.warning("Error transmitiendo desde ToDus: %s", exc)
        finally:
            upstream.close()

    def log_message(self, fmt, *args):
        logging.info("HTTP %s - %s", self.address_string(), fmt % args)


def start_web():
    port = int(os.environ.get("PORT", "10000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Servidor web en el puerto {port}", flush=True)


def safe_name(name):
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    base, ext = os.path.splitext(name)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    base = re.sub(r"_+", "_", base).strip("._") or "archivo"
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext)
    return base[:80] + ext


def with_alt(text):
    """Devuelve el enlace ToDus y, si se puede, el enlace de reproducción en Render."""
    found = re.search(r"https://s3\.todus\.cu/stream/users/[^\s]+", text)
    if not found:
        return text

    url = found.group(0).rstrip(").,;]")
    out = [url]

    if ALT_HOST:
        out.append("Enlace alternativo:\n" + url.replace("s3.todus.cu", ALT_HOST, 1))

    web_link = make_web_link(url)
    if web_link:
        out.append("Reproducir en navegador:\n" + web_link)

    return "\n\n".join(out)


async def todus(*args, timeout=900):
    exe = shutil.which("todus")
    cmd = [exe] if exe else [
        sys.executable, "-c",
        "import sys; from todus.cli import main; sys.exit(main())"
    ]
    p = await asyncio.create_subprocess_exec(
        *cmd, "--no-progress", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        return 124, f"Tiempo agotado ({timeout}s): ToDus no respondió desde este host."
    return p.returncode, out.decode(errors="replace").strip()


async def main():
    start_web()
    await asyncio.get_running_loop().run_in_executor(None, check_todus)

    client = TelegramClient(MemorySession(), API_ID, API_HASH)

    @client.on(events.NewMessage(pattern=r"^/start"))
    async def start(e):
        await e.reply(
            "Envíame un archivo, vídeo, audio o foto y lo subo a ToDus.\n"
            "/lista - ver tus archivos\n/enlace <nombre> - enlace de un archivo\n/id - tu ID"
        )

    @client.on(events.NewMessage(pattern=r"^/id"))
    async def my_id(e):
        await e.reply(f"Tu ID: {e.sender_id}")

    @client.on(events.NewMessage(pattern=r"^/estado"))
    async def estado(e):
        await e.reply(f"ToDus: {STATUS['todus']}")

    @client.on(events.NewMessage(pattern=r"^/lista"))
    async def lista(e):
        code, out = await todus("list", ns_for(e.sender_id), timeout=60)
        await e.reply(out[:4000] or "No tienes archivos.")

    @client.on(events.NewMessage(pattern=r"^/enlace\s+(.+)"))
    async def enlace(e):
        name = e.pattern_match.group(1).strip()
        p = path_for(e.sender_id)
        key = f"{p}/{name}" if p and not name.startswith(p + "/") else name
        code, out = await todus("share", ns_for(e.sender_id), key, timeout=60)
        await e.reply(with_alt(out)[:4000])

    @client.on(events.NewMessage(func=lambda e: e.message.media is not None))
    async def handle_file(e):
        if not e.file:
            return
        if ALLOWED and e.sender_id not in ALLOWED:
            return await e.reply("No estás autorizado para usar este bot.")
        if MAX_MB and e.file.size and e.file.size > MAX_MB * 1024 * 1024:
            return await e.reply(
                f"Archivo demasiado grande ({e.file.size // 1048576} MB, máximo {MAX_MB} MB)."
            )

        ns = ns_for(e.sender_id)
        name = safe_name(os.path.basename(e.file.name or f"{e.id}{e.file.ext or ''}"))
        status = await e.reply("Descargando...")
        await todus("ns", "create", ns, timeout=60)

        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, name)
            await e.download_media(file=path)
            await status.edit("Subiendo a ToDus...")
            up_path = path_for(e.sender_id)
            up_args = ["upload", ns, path, "--as", name]
            if up_path:
                up_args += ["--path", up_path]
            code, out = await todus(*up_args, timeout=600)
            if code != 0:
                return await status.edit(f"Error al subir:\n{out[-500:]}")
            key = f"{up_path}/{name}" if up_path else name
            code, link = await todus("share", ns, key, timeout=60)
            if code != 0:
                return await status.edit(f"Subido, pero no pude crear el enlace:\n{link[-500:]}")
            await status.edit(f"Listo:\n{with_alt(link)}")
        except Exception as ex:
            await status.edit(f"Error: {ex}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    STATUS["telegram"] = f"conectado como @{me.username}"
    print(f"Bot conectado como @{me.username}", flush=True)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
