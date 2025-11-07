// src/player/queue.js
import {
  createAudioPlayer,
  NoSubscriberBehavior,
  createAudioResource,
  AudioPlayerStatus,
  joinVoiceChannel,
  VoiceConnectionStatus,
  entersState,
  StreamType,
} from '@discordjs/voice';
import { ChannelType } from 'discord.js';
import play from 'play-dl';

export const RepeatMode = {
  NONE: 'none',
  ONE: 'one',
  ALL: 'all',
};

class GuildQueue {
  constructor(guildId) {
    this.guildId = guildId;
    this.connection = null;

    this.player = createAudioPlayer({
      behaviors: { noSubscriber: NoSubscriberBehavior.Pause },
    });

    this.queue = [];
    this.current = null;
    this.repeat = RepeatMode.NONE;
    this.shuffle = false;

    this.seekSeconds = 0;
    this.startedAt = 0;

    this.player.on(AudioPlayerStatus.Idle, () => this._onFinish());
    this.player.on('error', (e) => {
      console.error('[Player Error]', e);
      this._onFinish(true);
    });

    // 상태 로그
    this.player.on('stateChange', (oldS, newS) => {
      if (oldS.status !== newS.status) {
        console.log(`[AudioPlayer] ${oldS.status} -> ${newS.status}`);
      }
    });
  }

  async join(voiceChannel) {
    if (this.connection) return this.connection;

    this.connection = joinVoiceChannel({
      channelId: voiceChannel.id,
      guildId: voiceChannel.guild.id,
      adapterCreator: voiceChannel.guild.voiceAdapterCreator,
      selfDeaf: true,
    });

    await entersState(this.connection, VoiceConnectionStatus.Ready, 20_000);
    this.connection.subscribe(this.player);

    // 스테이지 채널이면 억제 해제 (스피커 권한 필요)
    try {
      if (voiceChannel.type === ChannelType.GuildStageVoice) {
        const me = await voiceChannel.guild.members.fetchMe();
        if (me?.voice?.suppress) {
          await me.voice.setSuppressed(false);
          try { await me.voice.setRequestToSpeak(true); } catch {}
        }
      }
    } catch (e) {
      console.warn('[StageVoice] 억제 해제 실패(권한 필요):', e?.message);
    }

    return this.connection;
  }

  leave() {
    try {
      if (this.connection) this.connection.destroy();
    } finally {
      this.connection = null;
      this.queue = [];
      this.current = null;
      this.seekSeconds = 0;
      this.startedAt = 0;
      this.player.stop();
    }
  }

  enqueue(track) {
    this.queue.push(track);
  }

  async playNext() {
    if (this.repeat === RepeatMode.ONE && this.current) {
      await this._startStream(this.current.url, 0);
      return;
    }

    if (this.repeat === RepeatMode.ALL && this.current) {
      this.queue.push(this.current);
    }

    if (this.shuffle && this.queue.length > 1) {
      const idx = Math.floor(Math.random() * this.queue.length);
      const [picked] = this.queue.splice(idx, 1);
      this.current = picked;
    } else {
      this.current = this.queue.shift() || null;
    }

    if (!this.current) {
      this.player.stop(true);
      return;
    }

    await this._startStream(this.current.url, this.seekSeconds);
    this.seekSeconds = 0;
  }

  async _startStream(url, beginSeconds = 0) {
    if (typeof url !== 'string' || !/^https?:\/\//.test(url)) {
      throw new Error(`Invalid track URL: ${url}`);
    }
    console.log('[STREAM] start', { url, seek: beginSeconds });

    const stream = await play.stream(url, {
      quality: 2,
      seek: beginSeconds > 0 ? Math.floor(beginSeconds) : undefined,
    });

    const resource = createAudioResource(stream.stream, {
      inputType: stream.type ?? StreamType.Arbitrary,
      inlineVolume: false,
    });

    this.startedAt = Date.now() - (beginSeconds * 1000);
    this.player.play(resource);
  }

  _onFinish(hadError = false) {
    if (this.repeat === RepeatMode.ONE && this.current && !hadError) {
      // playNext에서 처리
    } else if (!this.current && this.queue.length === 0) {
      return;
    }
    this.playNext().catch((e) => console.error('[playNext error]', e));
  }

  pause() {
    try { this.player.pause(true); } catch {}
  }
  resume() {
    try { this.player.unpause(); } catch {}
  }
  stop() {
    this.queue = [];
    this.current = null;
    this.seekSeconds = 0;
    this.player.stop(true);
  }
  skip() {
    this.player.stop(true);
  }
  getCurrentPositionSeconds() {
    if (!this.startedAt) return 0;
    return Math.max(0, Math.floor((Date.now() - this.startedAt) / 1000));
  }
  async seek(seconds) {
    if (!this.current) return;
    this.seekSeconds = Math.max(0, Math.floor(seconds));
    await this._startStream(this.current.url, this.seekSeconds);
  }
  setShuffle(on) {
    this.shuffle = !!on;
  }
  setRepeat(mode) {
    this.repeat = mode;
  }
  getQueue() {
    const now = this.current ? [{ now: true, ...this.current }] : [];
    return [...now, ...this.queue];
  }
}

class QueueManager {
  constructor() { this.queues = new Map(); }
  get(guildId) {
    if (!this.queues.has(guildId)) this.queues.set(guildId, new GuildQueue(guildId));
    return this.queues.get(guildId);
  }
}

export const queueManager = new QueueManager();
