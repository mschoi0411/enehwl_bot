# bot.py
# -*- coding: utf-8 -*-
"""
필수 준비물
- Python 3.10+
- FFmpeg 설치 (PATH 등록)
- Discord 봇 토큰 (.env: DISCORD_TOKEN=... )
- pip install -r requirements.txt
  discord.py==2.4.0
  yt-dlp==2025.01.01
  edge-tts==6.1.12
  python-dotenv==1.0.1
"""

import os
import asyncio
import random
import tempfile
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Deque, Literal, List

import time  # 진행 바용
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

import yt_dlp
from discord import FFmpegPCMAudio

import edge_tts

from discord import opus
try:
    if not opus.is_loaded():
        opus.load_opus("opus")  # 같은 폴더의 opus.dll 또는 PATH에서 로드
except Exception as e:
    print("Opus 로드 실패:", e)
    
# =========================
# 환경설정
# =========================
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# ▶ 추가: 시작 시 명령어 초기화 여부 ( .env에 RESET_COMMANDS_ON_START=1 로 켜기 )
RESET_COMMANDS_ON_START = os.getenv("RESET_COMMANDS_ON_START", "0") in ("1", "true", "True")

INTENTS = discord.Intents.default()
INTENTS.message_content = True  # /청소 등 로그/메시지 확인 시 필요
bot = commands.Bot(command_prefix="!", intents=INTENTS)

YDL_OPTS = {
    "format": "bestaudio[acodec=opus]/bestaudio/best",
    "quiet": True,
    "noplaylist": True,
    "extract_flat": False,
    "default_search": "ytsearch",
    "nocheckcertificate": True,
    "cachedir": False,
    "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    "source_address": "0.0.0.0",
}

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTS = {"before_options": FFMPEG_BEFORE, "options": "-vn"}

LoopMode = Literal["none", "one", "all"]

# 트랙 고유 순서 복원을 위한 전역 인덱스
_GLOBAL_ENQ_ID = 0


def _next_enq_id() -> int:
    global _GLOBAL_ENQ_ID
    _GLOBAL_ENQ_ID += 1
    return _GLOBAL_ENQ_ID


# =========================
# 공용 유틸
# =========================

def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "알 수 없음"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def build_progress_bar(position: float, duration: Optional[float], length: int = 20) -> str:
    if not duration or duration <= 0:
        return "▱" * length
    ratio = max(0.0, min(1.0, position / duration))
    filled = int(length * ratio)
    return "▰" * filled + "▱" * (length - filled)


@dataclass
class Track:
    title: str
    stream_url: str  # yt-dlp 추출 URL 또는 로컬 파일 경로(TTS)
    page_url: str
    duration: Optional[float] = None  # 초 단위 (알 수 없으면 None)
    requester: str = "unknown"
    start_offset: float = 0.0
    enqueue_id: int = field(default_factory=_next_enq_id)
    is_local_file: bool = False
    temp_path: Optional[str] = None

    # ▶ 추가 메타데이터
    thumbnail: Optional[str] = None
    channel: Optional[str] = None

    def display(self) -> str:
        return f"{self.title} (요청: {self.requester})"


players: dict[int, "GuildPlayer"] = {}


def get_player(guild: discord.Guild) -> "GuildPlayer":
    gp = players.get(guild.id)
    if not gp:
        gp = GuildPlayer(guild)
        players[guild.id] = gp
    return gp


# =========================
# yt-dlp 추출
# =========================

async def ytdlp_extract(query: str, requester: str) -> Optional[Track]:
    loop = asyncio.get_running_loop()

    def _extract():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            title = info.get("title", "Unknown")
            url = info.get("url")
            page = info.get("webpage_url", query)
            duration = info.get("duration")
            http_headers = info.get("http_headers") or {}
            thumbnail = info.get("thumbnail")
            uploader = info.get("uploader")
            return {
                "title": title,
                "url": url,
                "page": page,
                "duration": duration,
                "http_headers": http_headers,
                "thumbnail": thumbnail,
                "uploader": uploader,
            }

    try:
        data = await loop.run_in_executor(None, _extract)
        if not data:
            return None
        t = Track(
            title=data["title"],
            stream_url=data["url"],
            page_url=data["page"],
            duration=data["duration"],
            requester=requester,
            thumbnail=data.get("thumbnail"),
            channel=data.get("uploader"),
        )
        # Track에 헤더를 임시로 매달아 FFmpeg로 넘길 수 있게 보관
        t._http_headers = data["http_headers"]  # type: ignore[attr-defined]
        return t
    except Exception as e:
        print("yt-dlp extract error:", e)
        return None


# =========================
# GuildPlayer
# =========================

class GuildPlayer:
    def __init__(self, guild: discord.Guild):
        self.guild = guild
        self.voice: Optional[discord.VoiceClient] = None
        self.queue: Deque[Track] = deque()
        self.current: Optional[Track] = None
        self.shuffle: bool = False
        self.loop_mode: LoopMode = "none"
        self.history: List[Track] = []
        self.task: Optional[asyncio.Task] = None
        self.play_next = asyncio.Event()
        self.lock = asyncio.Lock()

        # ▶ UI 관련 필드
        self.text_channel: Optional[discord.TextChannel] = None
        self.now_playing_message: Optional[discord.Message] = None
        self.progress_task: Optional[asyncio.Task] = None
        self.view: Optional["PlayerView"] = None

        # ▶ 재생 위치 추적용
        self.started_at: Optional[float] = None
        self.paused_at: Optional[float] = None

    # ========= 재생 위치 관련 =========

    def on_start_playback(self):
        self.started_at = time.monotonic()
        self.paused_at = None

    def on_pause(self):
        if self.started_at is not None and self.paused_at is None:
            self.paused_at = time.monotonic()

    def on_resume(self):
        if self.started_at is not None and self.paused_at is not None:
            paused_duration = time.monotonic() - self.paused_at
            self.started_at += paused_duration
            self.paused_at = None

    def reset_timing(self):
        self.started_at = None
        self.paused_at = None

    def get_position(self) -> float:
        if not self.current or self.started_at is None:
            return 0.0
        base = self.current.start_offset or 0.0
        if self.voice and self.voice.is_paused() and self.paused_at is not None:
            elapsed = self.paused_at - self.started_at
        else:
            elapsed = time.monotonic() - self.started_at
        return max(0.0, base + elapsed)

    # ========= UI 관련 =========

    async def refresh_now_playing_message(self):
        if not self.now_playing_message or not self.current:
            return
        embed = build_now_playing_embed(self)
        try:
            await self.now_playing_message.edit(embed=embed, view=self.view)
        except discord.HTTPException:
            pass

    def _stop_progress_task(self):
        if self.progress_task and not self.progress_task.done():
            self.progress_task.cancel()
        self.progress_task = None

    async def _progress_loop(self):
        try:
            while True:
                await asyncio.sleep(5)
                if not self.current or not self.voice or not self.now_playing_message:
                    break
                if not (self.voice.is_playing() or self.voice.is_paused()):
                    break
                await self.refresh_now_playing_message()
        except asyncio.CancelledError:
            pass

    async def _start_now_playing_ui(self):
        if not self.text_channel or not self.current:
            return

        self.view = PlayerView(self)
        embed = build_now_playing_embed(self)

        if self.now_playing_message:
            try:
                self.now_playing_message = await self.now_playing_message.edit(
                    embed=embed,
                    view=self.view,
                )
            except discord.HTTPException:
                self.now_playing_message = await self.text_channel.send(
                    embed=embed,
                    view=self.view,
                )
        else:
            self.now_playing_message = await self.text_channel.send(
                embed=embed,
                view=self.view,
            )

        self._stop_progress_task()
        self.progress_task = asyncio.create_task(self._progress_loop())

    async def ensure_task(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.player_loop())

    async def connect_to(self, channel: discord.VoiceChannel):
        if self.voice and self.voice.is_connected():
            await self.voice.move_to(channel)
        else:
            self.voice = await channel.connect()

    def _build_source(self, track: Track) -> FFmpegPCMAudio:
        before = FFMPEG_BEFORE
        if getattr(track, "_http_headers", None):
            header_lines = "".join(f"{k}: {v}\r\n" for k, v in track._http_headers.items())
            before = f'{before} -headers "{header_lines}"'
        if track.start_offset and track.start_offset > 0:
            before = f"-ss {track.start_offset} {before}"
        return FFmpegPCMAudio(track.stream_url, before_options=before, options="-vn")
    
    async def player_loop(self):
        while True:
            self.play_next.clear()

            if not self.queue:
                try:
                    await asyncio.wait_for(self.play_next.wait(), timeout=300)
                    continue
                except asyncio.TimeoutError:
                    try:
                        if self.voice and self.voice.is_connected():
                            await self.voice.disconnect(force=False)
                    except Exception:
                        pass
                    self._stop_progress_task()
                    self.reset_timing()
                    return

            self.current = self.queue.popleft()
            track = self.current
            track.start_offset = track.start_offset or 0.0

            source = self._build_source(track)

            def after_playback(_err):
                if track.is_local_file and track.temp_path:
                    try:
                        os.remove(track.temp_path)
                    except Exception:
                        pass

                if self.loop_mode == "one":
                    track.start_offset = 0.0
                    self.queue.appendleft(track)
                elif self.loop_mode == "all":
                    track.start_offset = 0.0
                    self.queue.append(track)
                else:
                    self.history.append(track)

                self.current = None
                self.reset_timing()
                bot.loop.call_soon_threadsafe(self.play_next.set)

            try:
                if not self.voice or not self.voice.is_connected():
                    self.current = None
                    self.reset_timing()
                    continue

                self.voice.play(source, after=after_playback)
                self.on_start_playback()
                await self._start_now_playing_ui()

            except Exception:
                self.current = None
                self.reset_timing()
                self.play_next.set()

            await self.play_next.wait()

    # ========== 유틸 ==========

    def toggle_shuffle(self) -> bool:
        self.shuffle = not self.shuffle
        if self.shuffle:
            qlist = list(self.queue)
            random.shuffle(qlist)
            self.queue = deque(qlist)
        else:
            qlist = sorted(list(self.queue), key=lambda t: t.enqueue_id)
            self.queue = deque(qlist)
        return self.shuffle

    def set_loop_mode(self, mode: LoopMode):
        self.loop_mode = mode

    def clear(self):
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()
        while self.queue:
            t = self.queue.popleft()
            if t.is_local_file and t.temp_path:
                try:
                    os.remove(t.temp_path)
                except Exception:
                    pass
        self.current = None
        self._stop_progress_task()
        self.reset_timing()

    def enqueue(self, track: Track):
        self.queue.append(track)

    def enqueue_front(self, track: Track):
        self.queue.appendleft(track)


# =========================
# 임베드 / View 빌더
# =========================

def build_now_playing_embed(player: GuildPlayer) -> discord.Embed:
    track = player.current
    if not track:
        return discord.Embed(
            title="지금 재생 중인 곡이 없습니다.",
            color=discord.Color.dark_grey()
        )

    position = player.get_position()
    duration = track.duration
    bar = build_progress_bar(position, duration)
    is_paused = bool(player.voice and player.voice.is_paused())
    queue_len = len(player.queue)

    embed = discord.Embed(
        title="지금 재생 중 🎧" + (" (일시정지)" if is_paused else ""),
        description=f"[{track.title}]({track.page_url})",
        color=discord.Color.blurple(),
    )

    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)

    embed.add_field(
        name="채널",
        value=track.channel or "정보 없음",
        inline=True,
    )
    embed.add_field(
        name="길이",
        value=format_duration(duration),
        inline=True,
    )
    embed.add_field(
        name="요청자",
        value=track.requester,
        inline=True,
    )

    embed.add_field(
        name="진행도",
        value=f"`{format_duration(position)} / {format_duration(duration)}`\n{bar}",
        inline=False,
    )

    status = f"셔플: {'ON' if player.shuffle else 'OFF'} / 반복: {player.loop_mode}"
    embed.set_footer(text=f"대기열 {queue_len}곡 • {status}")
    return embed


def build_added_to_queue_embed(track: Track, position: int) -> discord.Embed:
    embed = discord.Embed(
        description=f"`{position}번째` 곡으로 **{track.title}** 를 추가했어요 ✅",
        color=discord.Color.green(),
    )
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    embed.add_field(
        name="길이",
        value=format_duration(track.duration),
        inline=True,
    )
    embed.add_field(
        name="요청자",
        value=track.requester,
        inline=True,
    )
    return embed


# =========================
# UI 컴포넌트들
# =========================

class PlayerView(discord.ui.View):
    def __init__(self, player: GuildPlayer, timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.player = player

    async def _update_interaction_message(self, interaction: discord.Interaction):
        embed = build_now_playing_embed(self.player)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="⏯", label="재생 / 일시정지", style=discord.ButtonStyle.secondary)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = self.player.voice
        if not v:
            return await interaction.response.send_message("현재 재생 중이 아닙니다.", ephemeral=True)
        if v.is_playing():
            v.pause()
            self.player.on_pause()
        elif v.is_paused():
            v.resume()
            self.player.on_resume()
        else:
            return await interaction.response.send_message("재생 중이 아닙니다.", ephemeral=True)
        await self._update_interaction_message(interaction)

    @discord.ui.button(emoji="⏭", label="다음 곡", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        v = self.player.voice
        if v and (v.is_playing() or v.is_paused()):
            v.stop()
            self.player.play_next.set()
            await interaction.response.send_message("⏭️ 스킵했습니다.", ephemeral=True)
        else:
            await interaction.response.send_message("스킵할 곡이 없습니다.", ephemeral=True)

    @discord.ui.button(emoji="⏹", label="정지", style=discord.ButtonStyle.danger)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.clear()
        if self.player.voice:
            try:
                await self.player.voice.disconnect()
            except Exception:
                pass

        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

        embed = discord.Embed(
            description="⏹ 재생을 종료했습니다.",
            color=discord.Color.red(),
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="🔁", label="반복 모드", style=discord.ButtonStyle.secondary)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        mode = self.player.loop_mode
        if mode == "none":
            self.player.set_loop_mode("one")
            label = "한곡 반복"
        elif mode == "one":
            self.player.set_loop_mode("all")
            label = "전체 반복"
        else:
            self.player.set_loop_mode("none")
            label = "반복 없음"
        button.label = label
        await self._update_interaction_message(interaction)

    @discord.ui.button(emoji="📃", label="재생목록", style=discord.ButtonStyle.secondary)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        q = list(self.player.queue)
        if not q:
            desc = "현재 대기열이 비어있어요."
        else:
            lines = []
            for i, t in enumerate(q[:10], start=1):
                lines.append(
                    f"`{i:02d}.` [{t.title}]({t.page_url}) — {format_duration(t.duration)} / 요청자: {t.requester}"
                )
            if len(q) > 10:
                lines.append(f"... 외 {len(q) - 10}곡")
            desc = "\n".join(lines)

        embed = discord.Embed(
            title="대기열 📃",
            description=desc,
            color=discord.Color.dark_teal(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(emoji="🕒", label="최근", style=discord.ButtonStyle.secondary)
    async def recent(self, interaction: discord.Interaction, button: discord.ui.Button):
        history_tracks = [t for t in self.player.history if not t.is_local_file]
        if not history_tracks:
            return await interaction.response.send_message("최근에 재생한 곡이 없습니다.", ephemeral=True)

        last_tracks = history_tracks[-10:][::-1]
        view = RecentView(self.player, last_tracks)

        lines = []
        for i, t in enumerate(last_tracks, start=1):
            lines.append(f"`{i}.` {t.title} — {format_duration(t.duration)}")
        desc = "\n".join(lines)

        embed = discord.Embed(
            title="최근 재생한 노래",
            description=desc,
            color=discord.Color.dark_gold(),
        )

        await interaction.response.send_message(
            content="🎶 음악을 재생하려면 아래에서 선택하세요.",
            embed=embed,
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(emoji="➕", label="음악 추가하기", style=discord.ButtonStyle.success)
    async def add_music(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AddMusicModal(self.player, interaction.user)
        await interaction.response.send_modal(modal)


class RecentSelect(discord.ui.Select):
    def __init__(self, player: GuildPlayer, tracks: List[Track]):
        self.player = player
        self.tracks = tracks

        options: List[discord.SelectOption] = []
        for idx, t in enumerate(tracks):
            label = t.title[:90]
            desc = f"{t.channel or '채널 정보 없음'} • {format_duration(t.duration)}"
            options.append(
                discord.SelectOption(
                    label=label,
                    description=desc[:100],
                    value=str(idx),
                )
            )

        super().__init__(
            placeholder="음악을 재생하려면 선택하세요",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        idx = int(self.values[0])
        base = self.tracks[idx]

        # ▶ 추가: 현재 재생 중인지 여부 확인
        was_idle = self.player.current is None

        new_track = Track(
            title=base.title,
            stream_url=base.stream_url,
            page_url=base.page_url,
            duration=base.duration,
            requester=interaction.user.display_name,
            is_local_file=base.is_local_file,
            temp_path=base.temp_path,
            thumbnail=base.thumbnail,
            channel=base.channel,
        )

        self.player.enqueue(new_track)
        await self.player.ensure_task()
        # ▶ 수정: 이미 재생 중이면 play_next 를 건드리지 않는다
        if was_idle:
            self.player.play_next.set()

        position = len(self.player.queue)
        embed = build_added_to_queue_embed(new_track, position)

        await interaction.response.edit_message(
            content="✅ 선택한 곡을 대기열에 추가했어요.",
            embed=embed,
            view=None,
        )


class RecentView(discord.ui.View):
    def __init__(self, player: GuildPlayer, tracks: List[Track]):
        super().__init__(timeout=60)
        self.add_item(RecentSelect(player, tracks))


class AddMusicModal(discord.ui.Modal, title="음악 추가하기"):
    query: discord.ui.TextInput

    def __init__(self, player: GuildPlayer, user: discord.abc.User):
        super().__init__()
        self.player = player
        self.user = user

        self.query = discord.ui.TextInput(
            label="노래 제목 또는 유튜브 링크",
            placeholder="예: NewJeans Ditto, https://youtu.be/...",
            style=discord.TextStyle.short,
            required=True,
            max_length=200,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        track = await ytdlp_extract(self.query.value, requester=self.user.display_name)
        if not track:
            return await interaction.followup.send("트랙을 찾지 못했어요.", ephemeral=True)

        # ▶ 추가: 현재 재생 여부 확인
        was_idle = self.player.current is None

        self.player.enqueue(track)
        await self.player.ensure_task()
        # ▶ 수정: 재생 중이 아닐 때만 다음 곡 재생 이벤트 발생
        if was_idle:
            self.player.play_next.set()

        position = len(self.player.queue)
        embed = build_added_to_queue_embed(track, position)
        await interaction.followup.send(
            content="🎵 음악을 대기열에 추가했어요.",
            embed=embed,
            ephemeral=True,
        )


# =========================
# (추가) 명령어 초기화 유틸
# =========================
async def wipe_all_app_commands():
    try:
        app_id = bot.application_id
        await bot.http.bulk_upsert_global_commands(app_id, [])
        for g in bot.guilds:
            try:
                await bot.http.bulk_upsert_guild_commands(app_id, g.id, [])
            except Exception as ge:
                print(f"[경고] 길드({g.id}) 명령어 초기화 실패:", ge)
        print("✅ 모든 전역/길드 Slash 명령어 초기화 완료")
    except Exception as e:
        print("명령어 초기화 중 오류:", e)


# =========================
# 이벤트
# =========================
@bot.event
async def on_ready():
    if RESET_COMMANDS_ON_START:
        await asyncio.sleep(1.0)
        await wipe_all_app_commands()
        await asyncio.sleep(1.0)

    try:
        synced = await bot.tree.sync()
        print(f"Slash commands synced: {len(synced)}")
    except Exception as e:
        print("Sync error:", e)
    print(f"Logged in as {bot.user} ({bot.user.id})")


# =========================
# 슬래시 명령
# =========================

@bot.tree.command(name="입장", description="봇을 현재 음성 채널로 호출합니다.")
async def join_cmd(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message("먼저 음성 채널에 들어가 주세요.", ephemeral=True)

    player = get_player(interaction.guild)
    await player.connect_to(interaction.user.voice.channel)
    player.text_channel = interaction.channel  # type: ignore[assignment]
    await player.ensure_task()
    await interaction.response.send_message(
        f"✅ {interaction.user.voice.channel.name} 에 연결되었습니다.",
        ephemeral=True,
    )


@bot.tree.command(name="퇴장", description="봇을 음성 채널에서 내보냅니다.")
async def leave_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    player.clear()
    if player.voice and player.voice.is_connected():
        await player.voice.disconnect()
    await interaction.response.send_message("👋 음성 채널에서 퇴장했습니다.", ephemeral=True)


@bot.tree.command(name="재생", description="유튜브 URL 또는 검색어로 노래를 재생합니다.")
@app_commands.describe(query="유튜브 URL 또는 검색어")
async def play_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True, ephemeral=True)
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("먼저 음성 채널에 들어가 주세요.", ephemeral=True)

    player = get_player(interaction.guild)
    if not player.voice or not player.voice.is_connected():
        await player.connect_to(interaction.user.voice.channel)

    player.text_channel = interaction.channel  # type: ignore[assignment]

    track = await ytdlp_extract(query, requester=interaction.user.display_name)
    if not track:
        return await interaction.followup.send("트랙을 찾지 못했어요.", ephemeral=True)

    # ▶ 재생 중인지 여부 판단
    was_idle = (not player.current) and (not player.queue) and (
        not player.voice or not player.voice.is_playing()
    )
    position = len(player.queue) + 1

    player.enqueue(track)
    await player.ensure_task()
    # ▶ 수정: idle 상태에서만 다음 곡 재생 이벤트
    if was_idle:
        player.play_next.set()

    if was_idle:
        await interaction.followup.send(f"🎧 **{track.title}** 재생을 시작할게요!", ephemeral=True)
    else:
        embed = build_added_to_queue_embed(track, position)
        await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="스킵", description="다음 곡으로 넘어갑니다.")
async def skip_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    if player.voice and (player.voice.is_playing() or player.voice.is_paused()):
        player.voice.stop()
        player.play_next.set()
        return await interaction.response.send_message("⏭️ 스킵했습니다.", ephemeral=True)
    await interaction.response.send_message("스킵할 곡이 없습니다.", ephemeral=True)


@bot.tree.command(name="일시정지", description="현재 곡을 일시정지합니다.")
async def pause_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    if player.voice and player.voice.is_playing():
        player.voice.pause()
        player.on_pause()
        await player.refresh_now_playing_message()
        return await interaction.response.send_message("⏸️ 일시정지", ephemeral=True)
    await interaction.response.send_message("현재 재생 중이 아닙니다.", ephemeral=True)


@bot.tree.command(name="재개", description="일시정지한 곡을 재개합니다.")
async def resume_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    if player.voice and player.voice.is_paused():
        player.voice.resume()
        player.on_resume()
        await player.refresh_now_playing_message()
        return await interaction.response.send_message("▶️ 재개", ephemeral=True)
    await interaction.response.send_message("일시정지 상태가 아닙니다.", ephemeral=True)


@bot.tree.command(name="정지", description="재생을 멈추고 대기열을 비웁니다.")
async def stop_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    player.clear()
    await interaction.response.send_message("⏹️ 정지하고 대기열을 비웠습니다.", ephemeral=True)


@bot.tree.command(name="재생목록", description="현재 재생/대기 목록을 보여줍니다.")
async def queue_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild)

    lines = []
    if player.current:
        pos = format_duration(player.get_position())
        dur = format_duration(player.current.duration)
        lines.append(f"**지금 재생 중:** {player.current.title}  `{pos} / {dur}`")

    if player.queue:
        for i, t in enumerate(list(player.queue)[:20], start=1):
            lines.append(f"{i}. {t.title} — 요청자: {t.requester}")
    else:
        lines.append("대기열이 비었습니다.")

    status = f"셔플: {'ON' if player.shuffle else 'OFF'} / 반복: {player.loop_mode}"
    await interaction.response.send_message(
        "**재생목록**\n" + "\n".join(lines) + f"\n\n{status}",
        ephemeral=True,
    )


@bot.tree.command(name="노래랜덤", description="셔플 재생을 켜거나 끕니다.")
async def shuffle_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    on = player.toggle_shuffle()
    await player.refresh_now_playing_message()
    await interaction.response.send_message(f"🔀 셔플 {'ON' if on else 'OFF'}", ephemeral=True)


@bot.tree.command(name="노래반복", description="반복 모드를 설정합니다. (안함/한곡/모두)")
@app_commands.describe(mode="안함 / 한곡 / 모두")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="안함", value="none"),
        app_commands.Choice(name="한곡", value="one"),
        app_commands.Choice(name="모두", value="all"),
    ]
)
async def repeat_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    player = get_player(interaction.guild)
    player.set_loop_mode(mode.value)  # type: ignore[arg-type]
    readable = {"none": "안함", "one": "한곡", "all": "모두"}[mode.value]
    await player.refresh_now_playing_message()
    await interaction.response.send_message(f"🔁 반복 모드: {readable}", ephemeral=True)


@bot.tree.command(name="구간이동", description="현재 곡에서 지정한 시각으로 이동합니다. (예: 1:23 또는 0:01:23)")
@app_commands.describe(timestamp="이동할 시각 (예: 1:23 또는 0:01:23)")
async def seek_cmd(interaction: discord.Interaction, timestamp: str):
    player = get_player(interaction.guild)
    if not player.current:
        return await interaction.response.send_message("현재 재생 중인 곡이 없습니다.", ephemeral=True)

    try:
        offset = parse_timestamp(timestamp)
    except Exception:
        return await interaction.response.send_message("형식이 잘못되었습니다. 예) 1:23 또는 0:01:23", ephemeral=True)

    track = player.current
    track.start_offset = offset
    player.enqueue_front(track)
    if player.voice and (player.voice.is_playing() or player.voice.is_paused()):
        player.voice.stop()
    await interaction.response.send_message(f"⏩ {timestamp} 시각으로 이동합니다.", ephemeral=True)


def parse_timestamp(ts: str) -> float:
    ts = ts.strip()
    if ":" not in ts:
        return float(int(ts))
    parts = [int(p) for p in ts.split(":")]
    if len(parts) == 2:
        m, s = parts
        return m * 60 + s
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    raise ValueError("잘못된 시각 형식입니다. 예) 1:23 또는 0:01:23")


@bot.tree.command(name="청소", description="이 채널의 최근 메시지를 삭제합니다.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(count="삭제할 메시지 개수 (최대 100)")
async def purge_cmd(interaction: discord.Interaction, count: int = 20):
    await interaction.response.defer(ephemeral=True, thinking=True)
    limit = max(1, min(count, 100))
    deleted = await interaction.channel.purge(limit=limit)  # type: ignore[arg-type]
    await interaction.followup.send(f"🧹 {len(deleted)}개 메시지 삭제", ephemeral=True)


@bot.tree.command(name="dots", description="텍스트를 음성으로 읽어줍니다.")
@app_commands.describe(text="읽어줄 텍스트", voice="예: ko-KR-SunHiNeural")
async def dots_cmd(interaction: discord.Interaction, text: str, voice: str = "ko-KR-SunHiNeural"):
    await interaction.response.defer(thinking=True, ephemeral=True)
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("먼저 음성 채널에 들어가 주세요.", ephemeral=True)

    player = get_player(interaction.guild)
    if not player.voice or not player.voice.is_connected():
        await player.connect_to(interaction.user.voice.channel)

    player.text_channel = interaction.channel  # type: ignore[assignment]

    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            out_path = f.name

        comm = edge_tts.Communicate(text, voice=voice, rate="+0%", volume="+0%")
        await comm.save(out_path)

        tts_track = Track(
            title=f"TTS: {text[:24]}{'...' if len(text) > 24 else ''}",
            stream_url=out_path,
            page_url="tts://local",
            requester=interaction.user.display_name,
            is_local_file=True,
            temp_path=out_path,
        )

        # ▶ 추가: 현재 재생 여부 확인
        was_idle = player.current is None

        player.enqueue(tts_track)
        await player.ensure_task()
        # ▶ 수정: idle 상태에서만 즉시 재생
        if was_idle:
            player.play_next.set()

        await interaction.followup.send(f"🗣️ TTS 대기열 추가 ({voice})", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"TTS 오류: {e}", ephemeral=True)


@purge_cmd.error
async def purge_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions):
        return await interaction.response.send_message(
            "이 명령을 사용할 권한이 없습니다. (manage_messages 필요)",
            ephemeral=True,
        )
    raise error


# =========================
# 진입점
# =========================
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("환경변수 DISCORD_TOKEN 이 비었습니다 (.env 설정 필요)")
    bot.run(TOKEN)
