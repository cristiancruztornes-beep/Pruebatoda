"""
Bot de Telegram -> ToDus S3 para Render (Web Service gratuito)

Variables de entorno en Render:
    API_ID, API_HASH, BOT_TOKEN      (obligatorias)
    ALLOWED_IDS="123,456"            (opcional: solo esos usuarios)
    MAX_MB="100"                     (opcional: límite de tamaño)

Start Command:  python main.py
Build Command:  pip install -r requirements.txt

Al arrancar comprueba si el host llega a ToDus y lo escribe en los Logs.
La página principal (/) muestra el estado.
También puedes enviar un enlace de s3.todus.cu para descargar el video completo con httpx y volverlo a subir a tu espacio de ToDus.
"""
import asyncio
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, urlsplit

import httpx
from telethon import TelegramClient, events
from telethon.sessions import MemorySession

logging.basicConfig(level=logging.INFO)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"].strip()
BOT_TOKEN = os.environ["BOT_TOKEN"].strip()
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
MAX_MB = int(os.environ.get("MAX_MB", "0") or 0)

STATUS = {"todus": "sin comprobar", "telegram": "sin conectar"}


def check_todus():
    t = time.time()
    try:
        socket.create_connection(("s3.todus.cu", 443), timeout=10).close()
        STATUS["todus"] = f"✅ ToDus alcanzable ({time.time() - t:.1f}s)"
    except Exception as e:
        STATUS["todus"] = f"❌ ToDus NO alcanzable desde este host: {e!r}"
    print(STATUS["todus"], flush=True)


# ---------- servidor web mínimo (Render exige un puerto abierto) ----------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"Telegram: {STATUS['telegram']}\nToDus: {STATUS['todus']}\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_web():
    port = int(os.environ.get("PORT", "10000"))
    srv = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Servidor web en el puerto {port}", flush=True)


# ---------- utilidades ----------
def safe_name(name):
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    base, ext = os.path.splitext(name)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    base = re.sub(r"_+", "_", base).strip("._") or "archivo"
    ext = re.sub(r"[^A-Za-z0-9.]", "", ext)
    return base[:80] + ext


async def todus(*args, timeout=900):
    exe = shutil.which("todus")
    cmd = [exe] if exe else [sys.executable, "-c", "import sys; from todus.cli import main; sys.exit(main())"]
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


async def descargar_video_todus(url, destination):
    """Descarga un video de ToDus por streaming, sin cargarlo entero en RAM."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "s3.todus.cu":
        raise ValueError("Solo se permiten enlaces HTTPS de s3.todus.cu.")

    headers = {
        "Host": "s3.todus.cu",
        "User-Agent": "Wget/1.21.3",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    extensions = {"sni_hostname": "s3.todus.cu"}

    total = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=20.0),
        follow_redirects=False,
    ) as session:
        async with session.stream(
            "GET", url, headers=headers, extensions=extensions
        ) as response:
            response.raise_for_status()

            # No seguir redirecciones evita que un enlace externo redirija el bot
            # hacia una dirección distinta de ToDus.
            if response.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"ToDus respondió HTTP {response.status_code}",
                    request=response.request,
                    response=response,
                )

            content_length = response.headers.get("content-length")
            if MAX_MB and content_length and content_length.isdigit():
                if int(content_length) > MAX_MB * 1024 * 1024:
                    raise ValueError(
                        f"El video supera el límite de {MAX_MB} MB."
                    )

            with open(destination, "wb") as output:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if MAX_MB and total > MAX_MB * 1024 * 1024:
                        raise ValueError(
                            f"El video supera el límite de {MAX_MB} MB."
                        )
                    output.write(chunk)

    if total == 0:
        raise ValueError("ToDus devolvió un archivo vacío.")

    return total


# ---------- bot ----------
async def main():
    start_web()
    await asyncio.get_running_loop().run_in_executor(None, check_todus)

    client = TelegramClient(MemorySession(), API_ID, API_HASH)

    @client.on(events.NewMessage(pattern=r"^/start"))
    async def start(e):
        await e.reply(
            "👋 Envíame un archivo, video, audio o foto y lo subo a ToDus.\n"
            "También puedes enviarme un enlace https://s3.todus.cu/... para descargar el video completo y volverlo a subir a tu ToDus.\n"
            "/lista - ver tus archivos\n/enlace <nombre> - enlace de un archivo\n/id - tu ID"
        )

    @client.on(events.NewMessage(pattern=r"^/id"))
    async def my_id(e):
        await e.reply(f"Tu ID: {e.sender_id}")

    @client.on(events.NewMessage(pattern=r"^/estado"))
    async def estado(e):
        await e.reply(f"ToDus: {STATUS['todus']}")

    @client.on(events.NewMessage(func=lambda e: bool(re.search(r"https?://s3\.todus\.cu/\S+", e.raw_text or "", re.I))))
    async def procesar_enlace_todus(e):
        if ALLOWED and e.sender_id not in ALLOWED:
            return await e.reply("⛔ No estás autorizado para usar este bot.")

        match = re.search(r"https?://s3\.todus\.cu/\S+", e.raw_text or "", re.I)
        url = match.group(0).rstrip(".,)")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "s3.todus.cu":
            return await e.reply("❌ Solo se admiten enlaces HTTPS de s3.todus.cu.")

        # Nombre de archivo basado en la ruta del enlace.
        filename = safe_name(os.path.basename(unquote(parsed.path)) or "video.mp4")
        if "." not in filename:
            filename += ".mp4"

        status = await e.reply("⬇️ Descargando el video completo desde ToDus...")
        ns = f"tg_{e.sender_id}"
        tmp = tempfile.mkdtemp()

        try:
            path = os.path.join(tmp, filename)
            size = await descargar_video_todus(url, path)
            size_mb = size / (1024 * 1024)

            await status.edit(
                f"✅ Descarga completa ({size_mb:.2f} MB).\n"
                "⬆️ Subiendo el video a tu espacio de ToDus..."
            )

            code, out = await todus("ns", "create", ns, timeout=60)
            if code != 0:
                return await status.edit(f"❌ No pude preparar tu espacio de ToDus:\n{out[-500:]}")

            code, out = await todus("upload", ns, path, "--as", filename, timeout=1800)
            if code != 0:
                return await status.edit(f"❌ Error al subir el video:\n{out[-700:]}")

            code, link_out = await todus("share", ns, filename, timeout=60)
            if code != 0:
                return await status.edit(
                    f"✅ Video subido, pero no pude crear el enlace:\n{link_out[-500:]}"
                )

            found = re.search(r"https?://\S+", link_out)
            new_link = found.group(0).rstrip(".,)") if found else link_out
            await status.edit(
                f"✅ Video descargado y subido nuevamente a ToDus.\n"
                f"📦 Tamaño: {size_mb:.2f} MB\n"
                f"🔗 Nuevo enlace:\n{new_link}"
            )

        except httpx.HTTPStatusError as ex:
            code = ex.response.status_code if ex.response else "desconocido"
            await status.edit(f"❌ ToDus respondió con HTTP {code}. El enlace puede estar vencido o no permitir la descarga.")
        except httpx.HTTPError as ex:
            await status.edit(f"❌ Error de red al descargar desde ToDus: {ex}")
        except Exception as ex:
            await status.edit(f"❌ No se pudo procesar el video: {ex}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @client.on(events.NewMessage(pattern=r"^/lista"))
    async def lista(e):
        code, out = await todus("list", f"tg_{e.sender_id}", timeout=60)
        await e.reply(out[:4000] or "No tienes archivos.")

    @client.on(events.NewMessage(pattern=r"^/enlace\s+(.+)"))
    async def enlace(e):
        code, out = await todus("share", f"tg_{e.sender_id}", e.pattern_match.group(1).strip(), timeout=60)
        await e.reply(out[:4000])

    @client.on(events.NewMessage(func=lambda e: e.message.media is not None))
    async def handle_file(e):
        if not e.file:
            return
        if ALLOWED and e.sender_id not in ALLOWED:
            return await e.reply("⛔ No estás autorizado para usar este bot.")
        if MAX_MB and e.file.size and e.file.size > MAX_MB * 1024 * 1024:
            return await e.reply(f"⚠️ Archivo demasiado grande ({e.file.size // 1048576} MB, máximo {MAX_MB} MB).")

        ns = f"tg_{e.sender_id}"
        name = safe_name(os.path.basename(e.file.name or f"{e.id}{e.file.ext or ''}"))
        status = await e.reply("⬇️ Descargando...")
        await todus("ns", "create", ns, timeout=60)

        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, name)
            await e.download_media(file=path)
            await status.edit("⬆️ Subiendo a ToDus...")
            code, out = await todus("upload", ns, path, "--as", name, timeout=600)
            if code != 0:
                return await status.edit(f"❌ Error al subir:\n{out[-500:]}")
            code, link = await todus("share", ns, name, timeout=60)
            if code != 0:
                return await status.edit(f"✅ Subido, pero no pude crear el enlace:\n{link[-500:]}")
            found = re.search(r"https?://\S+", link)
            await status.edit(f"✅ Listo:\n{found.group(0) if found else link}")
        except Exception as ex:
            await status.edit(f"❌ Error: {ex}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    STATUS["telegram"] = f"✅ conectado como @{me.username}"
    print(f"✅ Bot conectado como @{me.username}", flush=True)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
