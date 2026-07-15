"""
Componentes de UI (Components V2) reutilizados pelos cogs.

Fornece:
- simple_view()   — cartão simples de status/erro (substitui `send("texto")`).
- NowPlayingView  — painel "Tocando agora" com thumbnail e botões de controle.
- QueueView       — cartão de fila paginado.

Requer discord.py >= 2.6 (Components V2 / LayoutView). Uma mensagem enviada com
`view=` de um LayoutView não pode ter `content`/`embed` — o layout É o conteúdo.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from cogs.music import GuildMusicState, Music, Track

log = logging.getLogger("aigis.widgets")

# ---------------------------------------------------------------------------
# Cores de destaque (borda do Container)
# ---------------------------------------------------------------------------
ACCENT_PLAYING = discord.Colour.blurple()
ACCENT_QUEUE = discord.Colour.gold()
ACCENT_ERROR = discord.Colour.red()
ACCENT_INFO = discord.Colour.greyple()

QUEUE_PAGE_SIZE = 10


# ---------------------------------------------------------------------------
# Autorização — só quem está no mesmo canal de voz do bot controla o player
# ---------------------------------------------------------------------------
async def _authorize_voice_control(interaction: discord.Interaction, guild_id: int, cog: "Music") -> bool:
    """Verifica se quem clicou está no mesmo canal de voz do bot na guild.
    Se não estiver, responde com um aviso ephemeral e retorna False."""
    state = cog._states.get(guild_id)
    voice_client = state.voice_client if state else None
    member = interaction.user

    same_channel = (
        voice_client is not None
        and voice_client.is_connected()
        and isinstance(member, discord.Member)
        and member.voice is not None
        and member.voice.channel is not None
        and member.voice.channel.id == voice_client.channel.id
    )
    if not same_channel:
        await interaction.response.send_message(
            "❌ Entre no canal de voz do bot para controlar o player.", ephemeral=True
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Cartão simples — status/erro
# ---------------------------------------------------------------------------
class SimpleView(discord.ui.LayoutView):
    """Cartão de uma linha só, usado para status e mensagens de erro."""

    def __init__(self, text: str, accent: discord.Colour = ACCENT_INFO) -> None:
        super().__init__(timeout=None)
        container = discord.ui.Container(accent_colour=accent)
        container.add_item(discord.ui.TextDisplay(text))
        self.add_item(container)


def simple_view(text: str, accent: discord.Colour = ACCENT_INFO) -> SimpleView:
    return SimpleView(text, accent)


# ---------------------------------------------------------------------------
# Painel "Tocando agora"
# ---------------------------------------------------------------------------
class NowPlayingView(discord.ui.LayoutView):
    """Painel interativo com thumbnail da faixa e botões de controle."""

    def __init__(self, cog: "Music", guild_id: int, track: "Track", paused: bool = False) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.track = track
        self.paused = paused

        from cogs.music import _format_duration  # import tardio evita ciclo no load do módulo

        container = discord.ui.Container(accent_colour=ACCENT_PLAYING)
        container.add_item(discord.ui.TextDisplay("## 🎵 AIGIS PLAYER"))

        dur = _format_duration(track.duration)
        meta_parts = [p for p in (dur, f"pedido por {track.requested_by}" if track.requested_by else "") if p]
        body = f"**{track.title}**"
        if meta_parts:
            body += "\n" + " · ".join(meta_parts)

        if track.thumbnail:
            section = discord.ui.Section(accessory=discord.ui.Thumbnail(track.thumbnail))
            section.add_item(discord.ui.TextDisplay(body))
            container.add_item(section)
        else:
            container.add_item(discord.ui.TextDisplay(body))

        container.add_item(discord.ui.Separator())

        row = discord.ui.ActionRow()
        row.add_item(_PauseResumeButton(self))
        row.add_item(_SkipButton(self))
        row.add_item(_StopButton(self))
        row.add_item(_QueueButton(self))
        container.add_item(row)

        self.add_item(container)


class _PauseResumeButton(discord.ui.Button):
    def __init__(self, parent: NowPlayingView) -> None:
        label = "Retomar" if parent.paused else "Pausar"
        emoji = "▶️" if parent.paused else "⏸️"
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary)
        # Nome distinto de `_parent`: discord.ui.Item já usa `_parent` internamente
        # para o bookkeeping da árvore de componentes e o sobrescreve após __init__.
        self._panel = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self._panel
        cog = parent.cog
        if not await _authorize_voice_control(interaction, parent.guild_id, cog):
            return
        state = cog._states.get(parent.guild_id)
        if state is None or state.current is None:
            await interaction.response.edit_message(
                view=simple_view("❌ Nada está tocando no momento.", ACCENT_ERROR)
            )
            return

        if parent.paused:
            ok = cog._perform_resume(state)
        else:
            ok = cog._perform_pause(state)

        if not ok:
            await interaction.response.edit_message(
                view=simple_view("❌ Não foi possível atualizar a reprodução.", ACCENT_ERROR)
            )
            return

        await interaction.response.edit_message(
            view=NowPlayingView(cog, parent.guild_id, state.current, paused=not parent.paused)
        )


class _SkipButton(discord.ui.Button):
    def __init__(self, parent: NowPlayingView) -> None:
        super().__init__(label="Pular", emoji="⏭️", style=discord.ButtonStyle.secondary)
        self._panel = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self._panel
        cog = parent.cog
        if not await _authorize_voice_control(interaction, parent.guild_id, cog):
            return
        state = cog._states.get(parent.guild_id)
        if state is None or not cog._perform_skip(state):
            await interaction.response.edit_message(
                view=simple_view("❌ Nada está tocando no momento.", ACCENT_ERROR)
            )
            return
        # O loop de player edita este painel para a próxima faixa (ou fila vazia)
        # assim que o callback `after` da faixa atual disparar.
        await interaction.response.defer()


class _StopButton(discord.ui.Button):
    def __init__(self, parent: NowPlayingView) -> None:
        super().__init__(label="Parar", emoji="⏹️", style=discord.ButtonStyle.danger)
        self._panel = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self._panel
        cog = parent.cog
        if not await _authorize_voice_control(interaction, parent.guild_id, cog):
            return
        ok = await cog._perform_stop(parent.guild_id)
        if not ok:
            await interaction.response.edit_message(
                view=simple_view("❌ Nada está tocando no momento.", ACCENT_ERROR)
            )
            return
        await interaction.response.edit_message(
            view=simple_view("⏹️ Reprodução encerrada e fila limpa.", ACCENT_INFO)
        )


class _QueueButton(discord.ui.Button):
    def __init__(self, parent: NowPlayingView) -> None:
        super().__init__(label="Fila", emoji="📋", style=discord.ButtonStyle.secondary)
        self._panel = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self._panel
        cog = parent.cog
        state = cog._states.get(parent.guild_id)
        current = state.current if state else None
        queue_items = list(state.queue) if state else []
        await interaction.response.send_message(view=QueueView(current, queue_items), ephemeral=True)


# ---------------------------------------------------------------------------
# Cartão de fila (paginado)
# ---------------------------------------------------------------------------
class QueueView(discord.ui.LayoutView):
    def __init__(self, current: "Track | None", queue_items: list["Track"], page: int = 0) -> None:
        super().__init__(timeout=180)
        self.current = current
        self.queue_items = queue_items

        total_pages = max(1, math.ceil(len(queue_items) / QUEUE_PAGE_SIZE))
        self.page = max(0, min(page, total_pages - 1))

        container = discord.ui.Container(accent_colour=ACCENT_QUEUE)
        container.add_item(discord.ui.TextDisplay("## 📋 Fila de reprodução"))

        lines: list[str] = []
        if current:
            lines.append(f"▶️ **Tocando agora:** {current.title}")
        if queue_items:
            start = self.page * QUEUE_PAGE_SIZE
            chunk = queue_items[start : start + QUEUE_PAGE_SIZE]
            queue_lines = [f"`{start + i + 1}.` {t.title}" for i, t in enumerate(chunk)]
            lines.append("**Na fila:**\n" + "\n".join(queue_lines))
        if not lines:
            lines.append("A fila está vazia.")
        container.add_item(discord.ui.TextDisplay("\n\n".join(lines)))

        if total_pages > 1:
            container.add_item(discord.ui.Separator())
            container.add_item(discord.ui.TextDisplay(f"-# Página {self.page + 1}/{total_pages}"))
            row = discord.ui.ActionRow()
            row.add_item(_QueuePrevButton(self, disabled=self.page == 0))
            row.add_item(_QueueNextButton(self, disabled=self.page >= total_pages - 1))
            container.add_item(row)

        self.add_item(container)


class _QueuePrevButton(discord.ui.Button):
    def __init__(self, parent: QueueView, disabled: bool) -> None:
        super().__init__(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary, disabled=disabled)
        self._panel = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self._panel
        await interaction.response.edit_message(
            view=QueueView(parent.current, parent.queue_items, parent.page - 1)
        )


class _QueueNextButton(discord.ui.Button):
    def __init__(self, parent: QueueView, disabled: bool) -> None:
        super().__init__(label="Próximo", emoji="▶️", style=discord.ButtonStyle.secondary, disabled=disabled)
        self._panel = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self._panel
        await interaction.response.edit_message(
            view=QueueView(parent.current, parent.queue_items, parent.page + 1)
        )
