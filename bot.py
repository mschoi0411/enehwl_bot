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

INTENTS = discord.Intents.default()
INTENTS.message_content = True  # /청소 등 로그/메시지 확인 시 필요
bot = commands.Bot(command_prefix="!", intents=INTENTS)

YDL_OPTS = {
    "format": "bestaudio[acodec=opus]/bestaudio/best",  # 우선 opus, 그다음 best
    "quiet": True,
    "noplaylist": True,
    "extract_flat": False,
    "default_search": "ytsearch",
    "nocheckcertificate": True,
    "cachedir": False,
    # 플레이어 클라이언트를 'android'로 먼저 시도 → 시그니처 이슈 우회에 효과적
    "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    # 지역/네트워크 이슈 완화
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


@dataclass
class Track:
    title: str
    stream_url: str  # yt-dlp 추출 URL 또는 로컬 파일 경로(TTS)
    page_url: str
    duration: Optional[float] = None  # 초 단위 (알 수 없으면 None)
    requester: str = "unknown"
    start_offset: float = 0.0  # 구간이동 시 시작 위치(초)
    enqueue_id: int = field(default_factory=_next_enq_id)
    is_local_file: bool = False  # TTS 등 임시파일 재생 여부
    temp_path: Optional[str] = None  # is_local_file일 때 정리용 경로

    def display(self) -> str:
        return f"{self.title} (요청: {self.requester})"


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
        # 따옴표로 감싸서 한 옵션으로 전달 (Windows 고려)
            before = f'{before} -headers "{header_lines}"'
        if track.start_offset and track.start_offset > 0:
            before = f"-ss {track.start_offset} {before}"
        return FFmpegPCMAudio(track.stream_url, before_options=before, options="-vn")
    
    async def player_loop(self):
        while True:
            self.play_next.clear()

            if not self.queue:
                # 큐 비었으면 일정 시간 대기 후 자동 종료/퇴장
                try:
                    await asyncio.wait_for(self.play_next.wait(), timeout=300)
                    continue
                except asyncio.TimeoutError:
                    try:
                        if self.voice and self.voice.is_connected():
                            await self.voice.disconnect(force=False)
                    except Exception:
                        pass
                    return

            self.current = self.queue.popleft()
            track = self.current

            # 재생 시작
            source = self._build_source(track)

            def after_playback(_err):
                # 로컬 임시파일 정리
                if track.is_local_file and track.temp_path:
                    try:
                        os.remove(track.temp_path)
                    except Exception:
                        pass

                # 반복 모드 처리
                if self.loop_mode == "one":
                    # 같은 트랙을 다시 맨 앞에 (오프셋 초기화)
                    track.start_offset = 0.0
                    self.queue.appendleft(track)
                elif self.loop_mode == "all":
                    # 같은 트랙을 큐 뒤로
                    track.start_offset = 0.0
                    self.queue.append(track)
                else:
                    # 기록 남기기
                    self.history.append(track)

                self.current = None
                bot.loop.call_soon_threadsafe(self.play_next.set)

            try:
                if not self.voice or not self.voice.is_connected():
                    # 음성 연결이 끊겼다면 취소
                    self.current = None
                    continue
                self.voice.play(source, after=after_playback)
            except Exception:
                # 재생 실패 시 다음으로
                self.current = None
                self.play_next.set()

            await self.play_next.wait()

    # ========== 유틸 ==========

    def toggle_shuffle(self) -> bool:
        """셔플 on/off. 껐을 때는 enqueue_id 기준으로 원래 순서 복원."""
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
        # 현재 재생 중인 소스 중지
        if self.voice and (self.voice.is_playing() or self.voice.is_paused()):
            self.voice.stop()
        # 큐 비우기
        while self.queue:
            t = self.queue.popleft()
            if t.is_local_file and t.temp_path:
                try:
                    os.remove(t.temp_path)
                except Exception:
                    pass
        self.current = None

    def enqueue(self, track: Track):
        self.queue.append(track)

    def enqueue_front(self, track: Track):
        self.queue.appendleft(track)


players: dict[int, GuildPlayer] = {}


def get_player(guild: discord.Guild) -> GuildPlayer:
    gp = players.get(guild.id)
    if not gp:
        gp = GuildPlayer(guild)
        players[guild.id] = gp
    return gp


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
            return {
                "title": title,
                "url": url,
                "page": page,
                "duration": duration,
                "http_headers": http_headers,
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
        )
        # Track에 헤더를 임시로 매달아 FFmpeg로 넘길 수 있게 보관
        t._http_headers = data["http_headers"]  # type: ignore[attr-defined]
        return t
    except Exception as e:
        print("yt-dlp extract error:", e)
        return None


def parse_timestamp(ts: str) -> float:
    """
    "1:23" -> 83.0, "0:01:23" -> 83.0, "90" -> 90.0
    """
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


# =========================
# 이벤트
# =========================
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Slash commands synced: {len(synced)}")
    except Exception as e:
        print("Sync error:", e)
    print(f"Logged in as {bot.user} ({bot.user.id})")


# =========================
# 슬래시 명령
# =========================

# /입장
@bot.tree.command(name="입장", description="봇을 현재 음성 채널로 호출합니다.")
async def join_cmd(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message("먼저 음성 채널에 들어가 주세요.", ephemeral=True)

    player = get_player(interaction.guild)
    await player.connect_to(interaction.user.voice.channel)
    await player.ensure_task()
    await interaction.response.send_message(f"✅ {interaction.user.voice.channel.name} 에 연결되었습니다.")


# /퇴장
@bot.tree.command(name="퇴장", description="봇을 음성 채널에서 내보냅니다.")
async def leave_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    player.clear()
    if player.voice and player.voice.is_connected():
        await player.voice.disconnect()
    await interaction.response.send_message("👋 음성 채널에서 퇴장했습니다.")


# /재생
@bot.tree.command(name="재생", description="유튜브 URL 또는 검색어로 노래를 재생합니다.")
@app_commands.describe(query="유튜브 URL 또는 검색어")
async def play_cmd(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("먼저 음성 채널에 들어가 주세요.")

    player = get_player(interaction.guild)
    if not player.voice or not player.voice.is_connected():
        await player.connect_to(interaction.user.voice.channel)

    track = await ytdlp_extract(query, requester=interaction.user.display_name)
    if not track:
        return await interaction.followup.send("트랙을 찾지 못했어요.")
    player.enqueue(track)
    await player.ensure_task()
    player.play_next.set()  # idle 시 즉시 재생
    await interaction.followup.send(f"🎵 대기열 추가: **{track.title}**")


# /스킵
@bot.tree.command(name="스킵", description="다음 곡으로 넘어갑니다.")
async def skip_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    if player.voice and (player.voice.is_playing() or player.voice.is_paused()):
        player.voice.stop()
        player.play_next.set()
        return await interaction.response.send_message("⏭️ 스킵했습니다.")
    await interaction.response.send_message("스킵할 곡이 없습니다.", ephemeral=True)


# /일시정지
@bot.tree.command(name="일시정지", description="현재 곡을 일시정지합니다.")
async def pause_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    if player.voice and player.voice.is_playing():
        player.voice.pause()
        return await interaction.response.send_message("⏸️ 일시정지")
    await interaction.response.send_message("현재 재생 중이 아닙니다.", ephemeral=True)


# /재개
@bot.tree.command(name="재개", description="일시정지한 곡을 재개합니다.")
async def resume_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    if player.voice and player.voice.is_paused():
        player.voice.resume()
        return await interaction.response.send_message("▶️ 재개")
    await interaction.response.send_message("일시정지 상태가 아닙니다.", ephemeral=True)


# /정지
@bot.tree.command(name="정지", description="재생을 멈추고 대기열을 비웁니다.")
async def stop_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    player.clear()
    await interaction.response.send_message("⏹️ 정지하고 대기열을 비웠습니다.")


# /재생목록
@bot.tree.command(name="재생목록", description="현재 재생/대기 목록을 보여줍니다.")
async def queue_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild)

    lines = []
    if player.current:
        pos = f"(시작지점: {int(player.current.start_offset)}s)" if player.current.start_offset else ""
        lines.append(f"**지금 재생 중:** {player.current.title} {pos}")

    if player.queue:
        for i, t in enumerate(list(player.queue)[:20], start=1):
            lines.append(f"{i}. {t.title} — 요청자: {t.requester}")
    else:
        lines.append("대기열이 비었습니다.")

    status = f"셔플: {'ON' if player.shuffle else 'OFF'} / 반복: {player.loop_mode}"
    await interaction.response.send_message("**재생목록**\n" + "\n".join(lines) + f"\n\n{status}")


# /노래랜덤
@bot.tree.command(name="노래랜덤", description="셔플 재생을 켜거나 끕니다.")
async def shuffle_cmd(interaction: discord.Interaction):
    player = get_player(interaction.guild)
    on = player.toggle_shuffle()
    await interaction.response.send_message(f"🔀 셔플 {'ON' if on else 'OFF'}")


# /노래반복
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
    await interaction.response.send_message(f"🔁 반복 모드: {readable}")


# /구간이동
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

    # 현재 곡을 다시 큐 맨 앞으로 넣고 오프셋 설정 → stop() → 루프가 곡을 새 오프셋으로 재생
    track = player.current
    track.start_offset = offset
    player.enqueue_front(track)
    if player.voice and (player.voice.is_playing() or player.voice.is_paused()):
        player.voice.stop()
    await interaction.response.send_message(f"⏩ {timestamp} 시각으로 이동합니다.")


# /청소
@bot.tree.command(name="청소", description="이 채널의 최근 메시지를 삭제합니다.")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(count="삭제할 메시지 개수 (최대 100)")
async def purge_cmd(interaction: discord.Interaction, count: int = 20):
    await interaction.response.defer(ephemeral=True, thinking=True)
    limit = max(1, min(count, 100))
    deleted = await interaction.channel.purge(limit=limit)  # type: ignore[arg-type]
    await interaction.followup.send(f"🧹 {len(deleted)}개 메시지 삭제", ephemeral=True)


# /dots (TTS)
@bot.tree.command(name="dots", description="텍스트를 음성으로 읽어줍니다.")
@app_commands.describe(text="읽어줄 텍스트", voice="예: ko-KR-SunHiNeural")
async def dots_cmd(interaction: discord.Interaction, text: str, voice: str = "ko-KR-SunHiNeural"):
    await interaction.response.defer(thinking=True)
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.followup.send("먼저 음성 채널에 들어가 주세요.")

    player = get_player(interaction.guild)
    if not player.voice or not player.voice.is_connected():
        await player.connect_to(interaction.user.voice.channel)

    # Edge-TTS로 임시 mp3 생성 후 큐에 넣기
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
        player.enqueue(tts_track)
        await player.ensure_task()
        player.play_next.set()
        await interaction.followup.send(f"🗣️ TTS 대기열 추가 ({voice})")
    except Exception as e:
        await interaction.followup.send(f"TTS 오류: {e}")


# 안전망: 권한 부족 처리
@purge_cmd.error
async def purge_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.errors.MissingPermissions):
        return await interaction.response.send_message("이 명령을 사용할 권한이 없습니다. (manage_messages 필요)", ephemeral=True)
    raise error


# =========================
# 진입점
# =========================
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("환경변수 DISCORD_TOKEN 이 비었습니다 (.env 설정 필요)")
    bot.run(TOKEN)
