"""
Cog de GIF — extrai GIFs do X/Twitter via instância self-hosted do Cobalt.

Fluxo:
  1. Valida que o link é de x.com / twitter.com.
  2. POST para o Cobalt com { url, convertGif: true }.
  3. Baixa o conteúdo (tunnel/redirect) para um io.BytesIO em memória.
  4. Se > 8 MB, tenta comprimir via ffmpeg pipe→pipe sem escrever em disco.
  5. Envia como discord.File e descarta o buffer.

Nenhum arquivo é escrito em disco em nenhuma etapa.
"""

from __future__ import annotations

import asyncio
import io
import logging
import urllib.parse
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from main import Aigis

log = logging.getLogger("aigis.twitter_gif")

# Tamanho máximo aceito pelo Discord (sem boost de servidor)
DISCORD_MAX_BYTES = 8 * 1024 * 1024  # 8 MB

# Teto de download em memória — protege contra mídia enorme (ou resposta
# maliciosa) esgotar a RAM do processo antes mesmo da checagem de 8 MB acima.
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100 MB

# Cooldown do comando gif (segundos) — evita rajadas que abrem muitos
# downloads/recompressões ffmpeg simultâneos.
GIF_COOLDOWN_SECONDS = 5.0

# Hosts aceitos como "link do X/Twitter", incluindo mirrors comuns. Mirrors
# são normalizados de volta para x.com antes de chamar o Cobalt (ver
# _normalize_twitter_url), então o Cobalt sempre recebe um link canônico.
VALID_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
MIRROR_HOSTS = {
    "fxtwitter.com", "www.fxtwitter.com",
    "vxtwitter.com", "www.vxtwitter.com",
    "fixupx.com", "www.fixupx.com",
}

# Configuração de recompressão de GIF via ffmpeg
FFMPEG_COMPRESS_ARGS = [
    "ffmpeg",
    "-hide_banner",
    "-loglevel", "error",
    "-f", "gif",
    "-i", "pipe:0",          # stdin
    "-vf", "fps=12,scale=480:-1:flags=lanczos",
    "-f", "gif",
    "pipe:1",                # stdout
]


def _normalize_twitter_url(url: str) -> str | None:
    """Valida o link e o normaliza para x.com. Aceita hosts oficiais e
    mirrors conhecidos (fxtwitter, vxtwitter, fixupx). Retorna None se o
    link não for de um host suportado."""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc in VALID_HOSTS:
        return url
    if parsed.netloc in MIRROR_HOSTS:
        return urllib.parse.urlunparse(parsed._replace(netloc="x.com"))
    return None


async def _compress_gif(data: bytes) -> bytes | None:
    """
    Tenta recomprimir o GIF via ffmpeg pipe→pipe sem escrever em disco.
    Retorna os bytes comprimidos ou None em caso de falha.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *FFMPEG_COMPRESS_ARGS,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=data),
            timeout=60.0,
        )
        if proc.returncode != 0:
            log.warning("ffmpeg recompressão falhou: %s", stderr.decode(errors="replace"))
            return None
        return stdout
    except asyncio.TimeoutError:
        log.warning("Timeout ao recomprimir GIF via ffmpeg.")
        try:
            proc.kill()
            await proc.communicate()
        except Exception:
            pass
        return None
    except Exception as exc:
        log.error("Erro ao recomprimir GIF: %s", exc)
        return None


class TwitterGif(commands.Cog):
    def __init__(self, bot: "Aigis") -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Lógica compartilhada
    # ------------------------------------------------------------------
    async def _do_gif(self, ctx_or_interaction, url: str) -> None:
        """Núcleo do comando gif — funciona para prefixo e slash."""
        if isinstance(ctx_or_interaction, commands.Context):
            send = ctx_or_interaction.send
        else:
            interaction = ctx_or_interaction
            await interaction.response.defer(thinking=True)
            send = interaction.followup.send

        # 1. Validação do link (mirrors são normalizados para x.com)
        normalized_url = _normalize_twitter_url(url)
        if normalized_url is None:
            await send(
                "❌ Esse comando só funciona com links do **X/Twitter** (`x.com`, `twitter.com` "
                "ou mirrors como `fxtwitter.com`/`vxtwitter.com`)."
            )
            return
        url = normalized_url

        # Mensagem de status única — editada em caso de erro, apagada no sucesso
        status_msg = await send("⏳ Extraindo GIF...")

        session = self.bot.http_session
        if session is None or session.closed:
            await status_msg.edit(content="❌ Serviço temporariamente indisponível. Tente novamente.")
            return

        cobalt_url = self.bot.cobalt_api_url

        # 2. Chamada ao Cobalt
        try:
            async with session.post(
                cobalt_url,
                json={"url": url, "convertGif": True},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp_timeout(30),
            ) as resp:
                if resp.status != 200:
                    await status_msg.edit(content="❌ Não consegui processar esse link agora. Tente novamente.")
                    return
                cobalt_data: dict = await resp.json()
        except Exception as exc:
            log.error("Erro ao contactar Cobalt: %s", exc)
            await status_msg.edit(content="❌ Serviço de extração indisponível no momento.")
            return

        status = cobalt_data.get("status")

        # 3. Tratar resposta por status
        if status == "error":
            error_code = cobalt_data.get("error", {}).get("code", "desconhecido")
            log.warning("Cobalt retornou erro: %s", error_code)
            await status_msg.edit(content="❌ Não há nenhum GIF/mídia extraível nesse link.")
            return

        if status in ("tunnel", "redirect"):
            media_url = cobalt_data.get("url")
            if not media_url:
                await status_msg.edit(content="❌ Não encontrei mídia nesse link.")
                return

        elif status == "picker":
            # Múltiplos itens — pega o primeiro
            items = cobalt_data.get("picker", [])
            if not items:
                await status_msg.edit(content="❌ Não encontrei mídia nesse link.")
                return
            media_url = items[0].get("url")
            if not media_url:
                await status_msg.edit(content="❌ Não encontrei mídia nesse link.")
                return

        elif status == "local-processing":
            await status_msg.edit(content="❌ Esse link não é suportado para extração de GIF.")
            return

        else:
            log.warning("Status inesperado do Cobalt: %s", status)
            await status_msg.edit(content="❌ Não consegui extrair o GIF desse link.")
            return

        # 4. Download para memória (sem disco), com teto de tamanho — lido em
        # streaming e abortado assim que ultrapassar MAX_DOWNLOAD_BYTES, já
        # que Content-Length pode estar ausente ou não ser confiável.
        try:
            async with session.get(
                media_url,
                timeout=aiohttp_timeout(120),
            ) as media_resp:
                if media_resp.status != 200:
                    await status_msg.edit(content="❌ Falha ao baixar o GIF. Tente novamente.")
                    return

                if (media_resp.content_length or 0) > MAX_DOWNLOAD_BYTES:
                    await status_msg.edit(content="❌ Mídia grande demais para processar.")
                    return

                chunks = bytearray()
                async for chunk in media_resp.content.iter_chunked(64 * 1024):
                    chunks.extend(chunk)
                    if len(chunks) > MAX_DOWNLOAD_BYTES:
                        await status_msg.edit(content="❌ Mídia grande demais para processar.")
                        return
                raw_data = bytes(chunks)
                del chunks
        except Exception as exc:
            log.error("Erro ao baixar mídia do Cobalt: %s", exc)
            await status_msg.edit(content="❌ Falha ao baixar o GIF. Tente novamente.")
            return

        gif_data = raw_data  # referência local; raw_data liberado abaixo
        del raw_data

        # 5. Verificar tamanho e, se necessário, comprimir
        if len(gif_data) > DISCORD_MAX_BYTES:
            await status_msg.edit(content="⚙️ GIF grande — comprimindo...")
            compressed = await _compress_gif(gif_data)
            del gif_data  # libera memória do original

            if compressed and len(compressed) <= DISCORD_MAX_BYTES:
                gif_data = compressed
            else:
                del compressed
                await status_msg.edit(
                    content="❌ Esse GIF é grande demais para o Discord (acima de 8 MB)."
                )
                return

        # 6. Enviar como discord.File e descartar buffer; apaga o status no sucesso
        buf = io.BytesIO(gif_data)
        del gif_data  # GC pode liberar agora

        try:
            await send(file=discord.File(buf, filename="gif.gif"))
            await status_msg.delete()  # remove o "Extraindo..." deixando só o GIF
        except discord.HTTPException as exc:
            log.error("Erro ao enviar GIF para o Discord: %s", exc)
            await status_msg.edit(content="❌ Não consegui enviar o GIF no chat.")
        finally:
            buf.close()  # fecha o buffer explicitamente

    # ------------------------------------------------------------------
    # Comandos de prefixo
    # ------------------------------------------------------------------
    @commands.command(name="gif", help="Extrai e envia um GIF de um link do X/Twitter.")
    @commands.cooldown(1, GIF_COOLDOWN_SECONDS, commands.BucketType.user)
    async def gif_prefix(self, ctx: commands.Context, *, url: str) -> None:
        await self._do_gif(ctx, url)

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------
    @app_commands.command(name="gif", description="Extrai e envia um GIF de um link do X/Twitter.")
    @app_commands.describe(url="Link do X/Twitter (x.com ou twitter.com)")
    @app_commands.checks.cooldown(1, GIF_COOLDOWN_SECONDS)
    async def gif_slash(self, interaction: discord.Interaction, url: str) -> None:
        await self._do_gif(interaction, url)


# ---------------------------------------------------------------------------
# Helper — timeout do aiohttp sem importar a classe diretamente no topo
# ---------------------------------------------------------------------------
def aiohttp_timeout(total: float):
    import aiohttp
    return aiohttp.ClientTimeout(total=total)


async def setup(bot: "Aigis") -> None:
    await bot.add_cog(TwitterGif(bot))
