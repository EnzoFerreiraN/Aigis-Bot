"""
Cog de música — player de YouTube com fila por servidor.

Estratégia:
- yt-dlp extrai a URL direta de stream (sem download).
  A URL expira, portanto só é resolvida no momento de tocar (não na hora do !play).
  A fila guarda apenas (webpage_url, título) e resolve o stream na hora.
- ffmpeg recebe a URL de stream e transmite o áudio em tempo real.
- Estado por guild vive inteiramente em memória; nada é escrito em disco.
- Após 20 minutos sem reprodução o bot desconecta e libera o estado.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from cogs import widgets

if TYPE_CHECKING:
    from main import Aigis

log = logging.getLogger("aigis.music")

# Timeout de inatividade (segundos)
INACTIVITY_TIMEOUT = 20 * 60  # 20 minutos

# Cooldown do comando play (segundos) — evita rajadas que abrem muitos
# processos yt-dlp/ffmpeg simultâneos.
PLAY_COOLDOWN_SECONDS = 5.0

# Hosts aceitos quando o usuário passa uma URL (em vez de um termo de busca).
# Qualquer outro host é rejeitado antes de chegar ao yt-dlp — evita que o bot
# seja usado como proxy para extractors arbitrários (SSRF, file://, etc.).
VALID_QUERY_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be",
}


def _is_query_url_allowed(query: str) -> bool:
    """Se `query` parece uma URL, só permite hosts do YouTube. Texto puro
    (busca) sempre passa — vira ytsearch."""
    parsed = urllib.parse.urlparse(query)
    if not parsed.scheme:
        return True  # não é URL — é um termo de busca
    return parsed.scheme in ("http", "https") and parsed.netloc in VALID_QUERY_HOSTS


# Opções do yt-dlp (sem download)
YTDL_OPTIONS: dict = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",   # permite busca por texto além de URLs
    "source_address": "0.0.0.0",    # evitar erros de IPv6 em alguns ambientes
}

# Opções passadas ao ffmpeg pelo discord.py
FFMPEG_BEFORE_OPTIONS = (
    "-reconnect 1 "
    "-reconnect_streamed 1 "
    "-reconnect_delay_max 5"
)
FFMPEG_OPTIONS = "-vn"  # descarta a trilha de vídeo


def _format_duration(seconds: int | None) -> str:
    """Formata segundos como [h:]mm:ss. Retorna '' se desconhecido."""
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"`{h}:{m:02d}:{s:02d}`"
    return f"`{m}:{s:02d}`"


# ---------------------------------------------------------------------------
# Track — metadados de uma faixa na fila
# ---------------------------------------------------------------------------
@dataclass
class Track:
    webpage_url: str          # URL original (YouTube, etc.) — para re-extrair o stream
    title: str
    duration: int | None = None  # segundos
    requested_by: str = ""
    thumbnail: str | None = None


# ---------------------------------------------------------------------------
# GuildMusicState — estado por servidor
# ---------------------------------------------------------------------------
@dataclass
class GuildMusicState:
    guild_id: int
    queue: deque[Track] = field(default_factory=deque)
    current: Track | None = None
    voice_client: discord.VoiceClient | None = None
    # Evento sinalizado quando uma faixa termina (para o loop de player)
    track_finished: asyncio.Event = field(default_factory=asyncio.Event)
    # Task do loop de reprodução
    player_task: asyncio.Task | None = None
    # Handle do timer de inatividade
    inactivity_task: asyncio.Task | None = None
    # Mensagem do painel "Tocando agora" — sempre reenviada (não editada) a cada
    # troca de faixa, para permanecer como a última mensagem do canal.
    now_playing_message: discord.Message | None = None
    text_channel: discord.abc.Messageable | None = None

    def is_playing(self) -> bool:
        return self.voice_client is not None and self.voice_client.is_playing()

    def is_paused(self) -> bool:
        return self.voice_client is not None and self.voice_client.is_paused()


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------
class Music(commands.Cog):
    def __init__(self, bot: "Aigis") -> None:
        self.bot = bot
        # guild_id -> GuildMusicState
        self._states: dict[int, GuildMusicState] = {}

    # ------------------------------------------------------------------
    # Helpers de estado
    # ------------------------------------------------------------------
    def _get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self._states:
            self._states[guild_id] = GuildMusicState(guild_id=guild_id)
        return self._states[guild_id]

    def _remove_state(self, guild_id: int) -> None:
        state = self._states.pop(guild_id, None)
        if state:
            if state.player_task and not state.player_task.done():
                state.player_task.cancel()
            if state.inactivity_task and not state.inactivity_task.done():
                state.inactivity_task.cancel()

    # ------------------------------------------------------------------
    # Extração de metadados via yt-dlp (executor — chamada bloqueante)
    # ------------------------------------------------------------------
    async def _fetch_info(self, query: str) -> dict | None:
        """Extrai informações da faixa sem baixar. Retorna o dict de info ou None."""
        loop = asyncio.get_event_loop()

        def _extract() -> dict | None:
            with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
                try:
                    info = ydl.extract_info(query, download=False)
                except yt_dlp.utils.DownloadError as exc:
                    log.error("yt-dlp DownloadError: %s", exc)
                    return None
            # Busca retorna 'entries'; pegar o primeiro resultado
            if "entries" in info:
                entries = [e for e in info["entries"] if e]
                if not entries:
                    return None
                info = entries[0]
            return info

        return await loop.run_in_executor(None, _extract)

    async def _resolve_stream_url(self, track: Track) -> str | None:
        """Re-extrai a URL de stream (expira) no momento de tocar."""
        info = await self._fetch_info(track.webpage_url)
        if info is None:
            return None
        return info.get("url")

    # ------------------------------------------------------------------
    # Reenvio do painel "Tocando agora" como última mensagem do canal
    # ------------------------------------------------------------------
    async def _repost_now_playing(self, state: GuildMusicState, view: discord.ui.LayoutView) -> None:
        """Envia `view` como uma mensagem nova e apaga a anterior, para que o
        painel sempre fique como a última mensagem do canal (em vez de editado
        em uma posição antiga, possivelmente enterrada por mensagens novas)."""
        old_message = state.now_playing_message
        channel = state.text_channel or (old_message.channel if old_message else None)
        if channel is None:
            return
        try:
            state.now_playing_message = await channel.send(view=view)
        except discord.HTTPException as exc:
            log.error("[Guild %s] Falha ao reenviar painel: %s", state.guild_id, exc)
            return
        if old_message is not None:
            try:
                await old_message.delete()
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------
    # Loop de reprodução por guild
    # ------------------------------------------------------------------
    async def _player_loop(self, state: GuildMusicState) -> None:
        """Task que consome a fila e toca uma faixa de cada vez."""
        while True:
            state.track_finished.clear()

            if not state.queue:
                # Fila vazia — inicia timer de inatividade
                self._reset_inactivity_timer(state)
                if state.now_playing_message is not None:
                    await self._repost_now_playing(
                        state,
                        widgets.simple_view(
                            "⏹️ Fila vazia — aguardando novas músicas.", widgets.ACCENT_INFO
                        ),
                    )
                # Aguarda até a fila ter algo (via sinal de track_finished re-usado)
                # Na prática o loop encerra: uma nova faixa inicia nova task via _ensure_player_running
                break

            track = state.queue.popleft()
            state.current = track

            # Cancela timer de inatividade (estamos tocando)
            if state.inactivity_task and not state.inactivity_task.done():
                state.inactivity_task.cancel()
                state.inactivity_task = None

            # Resolve URL de stream agora (evita 403 por expiração)
            stream_url = await self._resolve_stream_url(track)
            if stream_url is None:
                log.warning("Não foi possível resolver stream de '%s'. Pulando.", track.title)
                if state.text_channel is not None:
                    try:
                        await state.text_channel.send(
                            view=widgets.simple_view(
                                f"⚠️ Não consegui tocar **{track.title}**, pulando.", widgets.ACCENT_ERROR
                            )
                        )
                    except discord.HTTPException:
                        pass
                continue

            # Prepara a fonte de áudio
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(
                    stream_url,
                    before_options=FFMPEG_BEFORE_OPTIONS,
                    options=FFMPEG_OPTIONS,
                )
            )

            # Callback called from a non-async thread when a track ends
            def _after(error: Exception | None) -> None:
                if error:
                    log.error("Erro no player (guild %s): %s", state.guild_id, error)
                state.track_finished.set()

            if state.voice_client and state.voice_client.is_connected():
                state.voice_client.play(source, after=_after)
                log.info("[Guild %s] Tocando: %s", state.guild_id, track.title)
                if state.now_playing_message is not None:
                    await self._repost_now_playing(
                        state, widgets.NowPlayingView(self, state.guild_id, track, paused=False)
                    )
            else:
                log.warning("[Guild %s] VoiceClient não conectado.", state.guild_id)
                break

            # Aguarda a faixa terminar (sinalizado pelo after callback)
            await state.track_finished.wait()

        state.current = None
        state.player_task = None

    # ------------------------------------------------------------------
    # Conexão de voz com retry (trata 502/4006 transitório do Discord)
    # ------------------------------------------------------------------
    async def _connect_voice(
        self,
        voice_channel: discord.VoiceChannel,
        state: GuildMusicState,
    ) -> discord.VoiceClient:
        """
        Conecta ao canal de voz com até 3 tentativas e backoff crescente.
        Trata WSServerHandshakeError (502), ConnectionClosed (4006) e timeout.
        Lança VoiceConnectionError em caso de falha persistente.
        """
        _RETRIES = 3
        _BACKOFF = [1.0, 2.0, 4.0]  # segundos entre tentativas

        last_exc: Exception | None = None
        for attempt in range(1, _RETRIES + 1):
            try:
                log.info(
                    "[Guild %s] Tentativa %d/%d de conectar ao canal de voz '%s'.",
                    state.guild_id, attempt, _RETRIES, voice_channel.name,
                )
                vc = await voice_channel.connect(timeout=20.0, reconnect=True)
                log.info("[Guild %s] Conectado ao canal de voz com sucesso.", state.guild_id)
                return vc
            except (
                aiohttp.WSServerHandshakeError,
                discord.errors.ConnectionClosed,
                asyncio.TimeoutError,
            ) as exc:
                last_exc = exc
                log.warning(
                    "[Guild %s] Falha na conexão de voz (tentativa %d/%d): %s — %s",
                    state.guild_id, attempt, _RETRIES, type(exc).__name__, exc,
                )
                # Tenta desconectar o VoiceClient parcialmente inicializado
                try:
                    existing = discord.utils.get(self.bot.voice_clients, guild=voice_channel.guild)
                    if existing and not existing.is_connected():
                        await existing.disconnect(force=True)
                except Exception:
                    pass

                if attempt < _RETRIES:
                    await asyncio.sleep(_BACKOFF[attempt - 1])

        raise discord.errors.ConnectionClosed(None, shard_id=None) from last_exc  # type: ignore[arg-type]

    def _ensure_player_running(self, state: GuildMusicState) -> None:
        """Garante que o loop de player esteja rodando para esta guild."""
        if state.player_task is None or state.player_task.done():
            state.player_task = asyncio.create_task(self._player_loop(state))

    # ------------------------------------------------------------------
    # Timer de inatividade
    # ------------------------------------------------------------------
    def _reset_inactivity_timer(self, state: GuildMusicState) -> None:
        if state.inactivity_task and not state.inactivity_task.done():
            state.inactivity_task.cancel()
        state.inactivity_task = asyncio.create_task(
            self._inactivity_timeout(state)
        )

    async def _inactivity_timeout(self, state: GuildMusicState) -> None:
        await asyncio.sleep(INACTIVITY_TIMEOUT)
        log.info("[Guild %s] Timeout de inatividade — desconectando.", state.guild_id)
        if state.voice_client and state.voice_client.is_connected():
            await state.voice_client.disconnect(force=True)
        now_playing_message = state.now_playing_message
        self._remove_state(state.guild_id)
        if now_playing_message is not None:
            try:
                await now_playing_message.edit(
                    view=widgets.simple_view("💤 Desconectado por inatividade.", widgets.ACCENT_INFO)
                )
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------
    # Lógica compartilhada dos comandos
    # ------------------------------------------------------------------
    async def _do_play(self, ctx_or_interaction, query: str) -> None:
        """Núcleo do comando play — funciona para prefixo e slash."""
        # Detectar quem chamou e em qual canal de voz está
        if isinstance(ctx_or_interaction, commands.Context):
            ctx = ctx_or_interaction
            author = ctx.author
            guild = ctx.guild
            send = ctx.send
        else:
            interaction = ctx_or_interaction
            author = interaction.user
            guild = interaction.guild
            send = interaction.followup.send
            await interaction.response.defer(thinking=True)

        if not author.voice or not author.voice.channel:
            await send(view=widgets.simple_view(
                "❌ Você precisa estar em um canal de voz para usar este comando.", widgets.ACCENT_ERROR
            ))
            return

        if not _is_query_url_allowed(query):
            await send(view=widgets.simple_view(
                "❌ Só aceito links do YouTube ou termos de busca — esse link não é suportado.",
                widgets.ACCENT_ERROR,
            ))
            return

        voice_channel = author.voice.channel
        state = self._get_state(guild.id)
        state.text_channel = ctx.channel if isinstance(ctx_or_interaction, commands.Context) else interaction.channel

        # Conectar/mover para o canal de voz do usuário (com retry para 502/4006)
        if state.voice_client and state.voice_client.is_connected():
            if state.voice_client.channel != voice_channel:
                await state.voice_client.move_to(voice_channel)
        else:
            try:
                state.voice_client = await self._connect_voice(voice_channel, state)
            except Exception as exc:
                log.error("[Guild %s] Falhou ao conectar na voz após retries: %s", guild.id, exc)
                # Limpa estado fantasma para não bloquear tentativas futuras
                self._remove_state(guild.id)
                await send(view=widgets.simple_view(
                    "❌ Não consegui conectar ao canal de voz (instabilidade do Discord na região). "
                    "Tente novamente em alguns segundos.",
                    widgets.ACCENT_ERROR,
                ))
                return

        # Mensagem de status única — editada com o resultado (não ecoa o link)
        status_msg = await send(view=widgets.simple_view("🔍 Procurando...", widgets.ACCENT_INFO))

        info = await self._fetch_info(query)
        if info is None:
            await status_msg.edit(view=widgets.simple_view(
                "❌ Não encontrei nada para tocar. Verifique o link ou o termo de busca.",
                widgets.ACCENT_ERROR,
            ))
            return

        track = Track(
            webpage_url=info.get("webpage_url") or query,
            title=info.get("title", "Desconhecido"),
            duration=info.get("duration"),
            requested_by=str(author),
            thumbnail=info.get("thumbnail"),
        )
        dur = _format_duration(track.duration)
        dur_suffix = f" {dur}" if dur else ""

        if state.is_playing() or state.is_paused():
            state.queue.append(track)
            pos = len(state.queue)
            await status_msg.edit(view=widgets.simple_view(
                f"➕ Adicionado à fila **#{pos}** — **{track.title}**{dur_suffix}", widgets.ACCENT_QUEUE
            ))
            # Reenvia o painel "Tocando agora" para que fique após esta confirmação,
            # permanecendo como a última mensagem do canal.
            if state.now_playing_message is not None and state.current is not None:
                await self._repost_now_playing(
                    state,
                    widgets.NowPlayingView(self, guild.id, state.current, paused=state.is_paused()),
                )
        else:
            state.queue.appendleft(track)
            # Rastreia esta mensagem como o painel "Tocando agora" ANTES de iniciar
            # o loop de player, para que ele já a encontre ao editar com a faixa real.
            state.now_playing_message = status_msg
            self._ensure_player_running(state)
            await status_msg.edit(view=widgets.simple_view(
                f"▶️ Iniciando — **{track.title}**{dur_suffix}", widgets.ACCENT_PLAYING
            ))

    # ------------------------------------------------------------------
    # Operações puras de estado — reusadas tanto pelos comandos quanto
    # pelos botões do painel "Tocando agora" (widgets.NowPlayingView).
    # ------------------------------------------------------------------
    def _perform_pause(self, state: GuildMusicState) -> bool:
        if not state.is_playing():
            return False
        state.voice_client.pause()
        self._reset_inactivity_timer(state)
        return True

    def _perform_resume(self, state: GuildMusicState) -> bool:
        if not state.is_paused():
            return False
        state.voice_client.resume()
        if state.inactivity_task and not state.inactivity_task.done():
            state.inactivity_task.cancel()
            state.inactivity_task = None
        return True

    def _perform_skip(self, state: GuildMusicState) -> bool:
        if not (state.is_playing() or state.is_paused()):
            return False
        state.voice_client.stop()  # dispara o after callback → próxima faixa
        return True

    async def _perform_stop(self, guild_id: int) -> bool:
        """Para a reprodução, limpa a fila e desconecta. Não mexe em mensagens —
        cada chamador (comando ou botão) decide como comunicar o resultado."""
        state = self._states.get(guild_id)
        if not state:
            return False
        state.queue.clear()
        if state.voice_client:
            state.voice_client.stop()
            await state.voice_client.disconnect(force=False)
        self._remove_state(guild_id)
        return True

    async def _do_skip(self, ctx_or_interaction) -> None:
        if isinstance(ctx_or_interaction, commands.Context):
            guild = ctx_or_interaction.guild
            send = ctx_or_interaction.send
        else:
            guild = ctx_or_interaction.guild
            send = ctx_or_interaction.response.send_message

        state = self._states.get(guild.id)
        if not state or not self._perform_skip(state):
            await send(view=widgets.simple_view("❌ Nada está tocando no momento.", widgets.ACCENT_ERROR))
            return

        # O painel "Tocando agora" é atualizado pelo loop de player assim que a
        # próxima faixa iniciar (ou pela fila vazia, se não houver mais nada).
        await send(view=widgets.simple_view("⏭️ Pulando para a próxima faixa...", widgets.ACCENT_INFO))

    async def _do_queue(self, ctx_or_interaction) -> None:
        if isinstance(ctx_or_interaction, commands.Context):
            guild = ctx_or_interaction.guild
            send = ctx_or_interaction.send
        else:
            guild = ctx_or_interaction.guild
            send = ctx_or_interaction.response.send_message

        state = self._states.get(guild.id)
        current = state.current if state else None
        queue_items = list(state.queue) if state else []
        await send(view=widgets.QueueView(current, queue_items))

    async def _do_stop(self, ctx_or_interaction) -> None:
        if isinstance(ctx_or_interaction, commands.Context):
            guild = ctx_or_interaction.guild
            send = ctx_or_interaction.send
        else:
            guild = ctx_or_interaction.guild
            send = ctx_or_interaction.response.send_message

        state = self._states.get(guild.id)
        if not state:
            await send(view=widgets.simple_view("❌ Nada está tocando no momento.", widgets.ACCENT_ERROR))
            return

        now_playing_message = state.now_playing_message
        await self._perform_stop(guild.id)

        closed_view = widgets.simple_view("⏹️ Reprodução encerrada e fila limpa.", widgets.ACCENT_INFO)
        if now_playing_message is not None:
            try:
                await now_playing_message.edit(view=closed_view)
            except discord.HTTPException:
                pass
        await send(view=closed_view)

    async def _do_pause(self, ctx_or_interaction) -> None:
        if isinstance(ctx_or_interaction, commands.Context):
            guild = ctx_or_interaction.guild
            send = ctx_or_interaction.send
        else:
            guild = ctx_or_interaction.guild
            send = ctx_or_interaction.response.send_message

        state = self._states.get(guild.id)
        if not state or not self._perform_pause(state):
            await send(view=widgets.simple_view("❌ Nada está tocando no momento.", widgets.ACCENT_ERROR))
            return

        if state.now_playing_message is not None and state.current is not None:
            try:
                await state.now_playing_message.edit(
                    view=widgets.NowPlayingView(self, guild.id, state.current, paused=True)
                )
            except discord.HTTPException:
                pass
        await send(view=widgets.simple_view("⏸️ Pausado.", widgets.ACCENT_INFO))

    async def _do_resume(self, ctx_or_interaction) -> None:
        if isinstance(ctx_or_interaction, commands.Context):
            guild = ctx_or_interaction.guild
            send = ctx_or_interaction.send
        else:
            guild = ctx_or_interaction.guild
            send = ctx_or_interaction.response.send_message

        state = self._states.get(guild.id)
        if not state or not self._perform_resume(state):
            await send(view=widgets.simple_view("❌ A reprodução não está pausada.", widgets.ACCENT_ERROR))
            return

        if state.now_playing_message is not None and state.current is not None:
            try:
                await state.now_playing_message.edit(
                    view=widgets.NowPlayingView(self, guild.id, state.current, paused=False)
                )
            except discord.HTTPException:
                pass
        await send(view=widgets.simple_view("▶️ Retomando.", widgets.ACCENT_INFO))

    async def _do_nowplaying(self, ctx_or_interaction) -> None:
        if isinstance(ctx_or_interaction, commands.Context):
            guild = ctx_or_interaction.guild
            send = ctx_or_interaction.send
        else:
            guild = ctx_or_interaction.guild
            send = ctx_or_interaction.response.send_message

        state = self._states.get(guild.id)
        if not state or state.current is None:
            await send(view=widgets.simple_view("❌ Nada está tocando no momento.", widgets.ACCENT_ERROR))
            return
        await send(view=widgets.NowPlayingView(self, guild.id, state.current, paused=state.is_paused()))

    async def _do_volume(self, ctx_or_interaction, volume: int) -> None:
        if isinstance(ctx_or_interaction, commands.Context):
            guild = ctx_or_interaction.guild
            send = ctx_or_interaction.send
        else:
            guild = ctx_or_interaction.guild
            send = ctx_or_interaction.response.send_message

        if not 0 <= volume <= 200:
            await send(view=widgets.simple_view("❌ O volume deve estar entre 0 e 200.", widgets.ACCENT_ERROR))
            return

        state = self._states.get(guild.id)
        if not state or state.voice_client is None or state.voice_client.source is None:
            await send(view=widgets.simple_view("❌ Nada está tocando no momento.", widgets.ACCENT_ERROR))
            return

        state.voice_client.source.volume = volume / 100
        await send(view=widgets.simple_view(f"🔊 Volume ajustado para **{volume}%**.", widgets.ACCENT_INFO))

    # ------------------------------------------------------------------
    # Comandos de prefixo
    # ------------------------------------------------------------------
    @commands.command(name="play", aliases=["p"], help="Toca uma música ou adiciona à fila.")
    @commands.guild_only()
    @commands.cooldown(1, PLAY_COOLDOWN_SECONDS, commands.BucketType.user)
    async def play_prefix(self, ctx: commands.Context, *, query: str) -> None:
        await self._do_play(ctx, query)

    @commands.command(name="skip", aliases=["s"], help="Pula a faixa atual.")
    @commands.guild_only()
    async def skip_prefix(self, ctx: commands.Context) -> None:
        await self._do_skip(ctx)

    @commands.command(name="queue", aliases=["q", "fila"], help="Mostra a fila de reprodução.")
    @commands.guild_only()
    async def queue_prefix(self, ctx: commands.Context) -> None:
        await self._do_queue(ctx)

    @commands.command(name="stop", help="Para a reprodução e sai do canal de voz.")
    @commands.guild_only()
    async def stop_prefix(self, ctx: commands.Context) -> None:
        await self._do_stop(ctx)

    @commands.command(name="pause", help="Pausa a reprodução.")
    @commands.guild_only()
    async def pause_prefix(self, ctx: commands.Context) -> None:
        await self._do_pause(ctx)

    @commands.command(name="resume", help="Retoma a reprodução pausada.")
    @commands.guild_only()
    async def resume_prefix(self, ctx: commands.Context) -> None:
        await self._do_resume(ctx)

    @commands.command(name="nowplaying", aliases=["np"], help="Mostra o painel da faixa atual.")
    @commands.guild_only()
    async def nowplaying_prefix(self, ctx: commands.Context) -> None:
        await self._do_nowplaying(ctx)

    @commands.command(name="volume", aliases=["vol"], help="Ajusta o volume (0-200).")
    @commands.guild_only()
    async def volume_prefix(self, ctx: commands.Context, volume: int) -> None:
        await self._do_volume(ctx, volume)

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------
    @app_commands.command(name="play", description="Toca uma música do YouTube ou adiciona à fila.")
    @app_commands.describe(query="Link do YouTube ou termo de busca")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, PLAY_COOLDOWN_SECONDS)
    async def play_slash(self, interaction: discord.Interaction, query: str) -> None:
        await self._do_play(interaction, query)

    @app_commands.command(name="skip", description="Pula a faixa atual.")
    @app_commands.guild_only()
    async def skip_slash(self, interaction: discord.Interaction) -> None:
        await self._do_skip(interaction)

    @app_commands.command(name="queue", description="Mostra a fila de reprodução.")
    @app_commands.guild_only()
    async def queue_slash(self, interaction: discord.Interaction) -> None:
        await self._do_queue(interaction)

    @app_commands.command(name="stop", description="Para a reprodução e sai do canal de voz.")
    @app_commands.guild_only()
    async def stop_slash(self, interaction: discord.Interaction) -> None:
        await self._do_stop(interaction)

    @app_commands.command(name="pause", description="Pausa a reprodução.")
    @app_commands.guild_only()
    async def pause_slash(self, interaction: discord.Interaction) -> None:
        await self._do_pause(interaction)

    @app_commands.command(name="resume", description="Retoma a reprodução pausada.")
    @app_commands.guild_only()
    async def resume_slash(self, interaction: discord.Interaction) -> None:
        await self._do_resume(interaction)

    @app_commands.command(name="nowplaying", description="Mostra o painel da faixa atual.")
    @app_commands.guild_only()
    async def nowplaying_slash(self, interaction: discord.Interaction) -> None:
        await self._do_nowplaying(interaction)

    @app_commands.command(name="volume", description="Ajusta o volume (0-200).")
    @app_commands.describe(volume="Volume em porcentagem, de 0 a 200")
    @app_commands.guild_only()
    async def volume_slash(self, interaction: discord.Interaction, volume: int) -> None:
        await self._do_volume(interaction, volume)

    # ------------------------------------------------------------------
    # Cleanup ao descarregar o cog
    # ------------------------------------------------------------------
    async def cog_unload(self) -> None:
        for guild_id, state in list(self._states.items()):
            if state.voice_client and state.voice_client.is_connected():
                await state.voice_client.disconnect(force=True)
        self._states.clear()


async def setup(bot: "Aigis") -> None:
    await bot.add_cog(Music(bot))
