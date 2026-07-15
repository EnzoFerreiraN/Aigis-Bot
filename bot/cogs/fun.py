"""
Cog de brincadeiras — comandos sem propósito sério, só diversão no servidor.

dcrandom: sorteia alguém do canal de voz do autor (o próprio autor incluído)
e desconecta a pessoa. Restrito a quem tem a permissão "Mover membros".
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from cogs import widgets

if TYPE_CHECKING:
    from main import Aigis

log = logging.getLogger("aigis.fun")

# Cooldown do comando dcrandom (segundos) — evita spam de desconexões.
DCRANDOM_COOLDOWN_SECONDS = 10.0


class Fun(commands.Cog):
    def __init__(self, bot: "Aigis") -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Núcleo compartilhado — funciona para prefixo e slash
    # ------------------------------------------------------------------
    async def _do_dcrandom(self, ctx_or_interaction) -> None:
        if isinstance(ctx_or_interaction, commands.Context):
            author = ctx_or_interaction.author
            send = ctx_or_interaction.send
        else:
            interaction = ctx_or_interaction
            author = interaction.user
            await interaction.response.defer(thinking=True)
            send = interaction.followup.send

        # Permissão: só quem pode mover membros usa o comando
        if not author.guild_permissions.move_members:
            await send(view=widgets.simple_view(
                "❌ Você precisa da permissão **Mover membros** para usar este comando.",
                widgets.ACCENT_ERROR,
            ))
            return

        if not author.voice or not author.voice.channel:
            await send(view=widgets.simple_view(
                "❌ Você precisa estar em um canal de voz para usar este comando.",
                widgets.ACCENT_ERROR,
            ))
            return

        channel = author.voice.channel
        candidates = [m for m in channel.members if not m.bot]

        if not candidates:
            await send(view=widgets.simple_view(
                "🤷 Não há ninguém no canal para desconectar.", widgets.ACCENT_INFO
            ))
            return

        random.shuffle(candidates)

        for victim in candidates:
            try:
                await victim.move_to(None)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning(
                    "[Guild %s] Não consegui desconectar %s: %s", channel.guild.id, victim, exc
                )
                continue
            await send(view=widgets.simple_view(
                f"🎲 A roleta girou... **{victim.display_name}** foi desconectado! 👋",
                widgets.ACCENT_INFO,
            ))
            return

        # Nenhum candidato pôde ser desconectado (permissão/hierarquia do bot)
        await send(view=widgets.simple_view(
            "❌ Não consegui desconectar ninguém. Verifique se eu tenho a permissão "
            "**Mover membros** e se meu cargo está acima do de quem está no canal.",
            widgets.ACCENT_ERROR,
        ))

    # ------------------------------------------------------------------
    # Comando de prefixo
    # ------------------------------------------------------------------
    @commands.command(name="dcrandom", help="Desconecta aleatoriamente alguém do seu canal de voz.")
    @commands.guild_only()
    @commands.cooldown(1, DCRANDOM_COOLDOWN_SECONDS, commands.BucketType.guild)
    async def dcrandom_prefix(self, ctx: commands.Context) -> None:
        await self._do_dcrandom(ctx)

    # ------------------------------------------------------------------
    # Slash command
    # ------------------------------------------------------------------
    @app_commands.command(name="dcrandom", description="Desconecta aleatoriamente alguém do seu canal de voz.")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, DCRANDOM_COOLDOWN_SECONDS)
    async def dcrandom_slash(self, interaction: discord.Interaction) -> None:
        await self._do_dcrandom(interaction)


async def setup(bot: "Aigis") -> None:
    await bot.add_cog(Fun(bot))
