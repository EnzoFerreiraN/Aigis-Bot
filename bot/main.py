"""
Aigis — Bot de Discord
Entry point: configura intents, carrega cogs e inicia o bot.
"""

import asyncio
import logging
import os
import sys

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("aigis")

# ---------------------------------------------------------------------------
# Configuração via variáveis de ambiente
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or os.environ.get("TOKEN")
if not DISCORD_TOKEN:
    log.critical("DISCORD_TOKEN não definido. Defina a variável de ambiente e reinicie.")
    sys.exit(1)

COMMAND_PREFIX = os.environ.get("COMMAND_PREFIX", "!")
COBALT_API_URL = os.environ.get("COBALT_API_URL", "http://cobalt:9000")

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
class Aigis(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True   # MESSAGE CONTENT INTENT — ativar no Developer Portal
        intents.voice_states = True

        super().__init__(command_prefix=COMMAND_PREFIX, intents=intents)

        self.cobalt_api_url: str = COBALT_API_URL
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        # Sessão HTTP compartilhada (reusada pelo cog de GIF)
        self.http_session = aiohttp.ClientSession()

        # Carrega os cogs
        await self.load_extension("cogs.music")
        await self.load_extension("cogs.twitter_gif")
        await self.load_extension("cogs.fun")
        log.info("Cogs carregados.")

        # Sincroniza slash commands globalmente
        synced = await self.tree.sync()
        log.info("Slash commands sincronizados: %d", len(synced))

        # Handler global de erros para slash commands (comandos de prefixo
        # usam on_command_error, definido abaixo).
        self.tree.on_error = self._on_app_command_error

    async def close(self) -> None:
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Bot online como %s (ID: %s)", self.user, self.user.id)
        log.info("Prefixo de comandos: '%s'", COMMAND_PREFIX)
        log.info("Cobalt API URL: %s", self.cobalt_api_url)

    # ------------------------------------------------------------------
    # Tratamento global de erros — evita falhas silenciosas nos comandos
    # ------------------------------------------------------------------
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return  # comando inexistente — ignora silenciosamente

        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ Esse comando só funciona dentro de um servidor, não em DM.")
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"❌ Faltou um argumento: `{error.param.name}`. Confira o uso do comando.")
            return

        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Calma! Tente de novo em {error.retry_after:.1f}s.")
            return

        if isinstance(error, commands.CheckFailure):
            await ctx.send("❌ Você não tem permissão para usar esse comando.")
            return

        log.error("Erro não tratado no comando '%s': %s", ctx.command, error, exc_info=error)
        await ctx.send("❌ Ocorreu um erro inesperado ao executar esse comando.")

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.NoPrivateMessage):
            message = "❌ Esse comando só funciona dentro de um servidor, não em DM."
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"⏳ Calma! Tente de novo em {error.retry_after:.1f}s."
        elif isinstance(error, app_commands.CheckFailure):
            message = "❌ Você não tem permissão para usar esse comando."
        else:
            log.error(
                "Erro não tratado no slash command '%s': %s",
                interaction.command.name if interaction.command else "?",
                error,
                exc_info=error,
            )
            message = "❌ Ocorreu um erro inesperado ao executar esse comando."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


def main() -> None:
    bot = Aigis()
    asyncio.run(bot.start(DISCORD_TOKEN))


if __name__ == "__main__":
    main()
