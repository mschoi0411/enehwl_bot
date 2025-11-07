// src/commands/music.js
import { SlashCommandBuilder, ChannelType } from 'discord.js';
import { queueManager, RepeatMode } from '../player/queue.js';
import play from 'play-dl';

// “m:ss” 또는 “h:mm:ss” → 초
function parseTimeToSeconds(input) {
  const parts = input.trim().split(':').map(Number);
  if (parts.some(Number.isNaN)) return null;
  let s = 0;
  for (const p of parts) s = s * 60 + p;
  return s;
}

// 안전한 유튜브 검색/URL 처리
async function resolveTrack(query) {
  const kind = play.yt_validate(query); // 'video' | 'playlist' | 'search' | 'invalid'

  if (kind === 'video') {
    const info = await play.video_info(query);
    const vd = info?.video_details;
    const url = vd?.url || (vd?.id ? `https://www.youtube.com/watch?v=${vd.id}` : null);
    if (!url || !url.startsWith('http')) throw new Error('유효한 영상 URL을 만들 수 없었습니다.');
    const title = vd?.title || 'YouTube';
    return { title, url };
  }

  const results = await play.search(query, { limit: 1, source: { youtube: 'video' } });
  if (!results?.length) throw new Error('검색 결과가 없습니다.');

  const first = results[0];
  const url = first?.url || (first?.id ? `https://www.youtube.com/watch?v=${first.id}` : null);
  if (!url || !url.startsWith('http')) throw new Error('검색 결과의 URL을 찾지 못했습니다.');

  const title = first?.title || 'YouTube';
  return { title, url };
}

export const musicCommands = [
  new SlashCommandBuilder()
    .setName('재생')
    .setDescription('유튜브 URL 또는 검색어로 노래를 재생합니다.')
    .addStringOption(o =>
      o.setName('query').setDescription('유튜브 URL 또는 검색어').setRequired(true)
    )
    .addChannelOption(o =>
      o.setName('채널').setDescription('재생할 음성 채널 (생략 시 내 채널)')
        .addChannelTypes(ChannelType.GuildVoice)
    ),

  new SlashCommandBuilder().setName('스킵').setDescription('다음 곡으로 넘어갑니다.'),
  new SlashCommandBuilder().setName('일시정지').setDescription('현재 곡을 일시정지합니다.'),
  new SlashCommandBuilder().setName('정지').setDescription('재생을 완전히 멈추고 큐를 비웁니다.'),

  new SlashCommandBuilder()
    .setName('입장').setDescription('봇을 음성 채널로 호출합니다.')
    .addChannelOption(o =>
      o.setName('채널').setDescription('입장할 음성 채널 (생략 시 내 채널)')
        .addChannelTypes(ChannelType.GuildVoice)
    ),

  new SlashCommandBuilder().setName('퇴장').setDescription('봇을 음성 채널에서 내보냅니다.'),

  new SlashCommandBuilder()
    .setName('구간이동')
    .setDescription('현재 곡에서 지정한 시각으로 이동합니다 (예: 1:23 또는 0:01:23).')
    .addStringOption(o =>
      o.setName('time').setDescription('이동할 시각 (m:ss 또는 h:mm:ss)').setRequired(true)
    ),

  new SlashCommandBuilder().setName('재생목록').setDescription('현재 재생/대기 목록을 보여줍니다.'),

  new SlashCommandBuilder()
    .setName('노래랜덤').setDescription('셔플(랜덤 재생)을 켜거나 끕니다.')
    .addStringOption(o =>
      o.setName('상태').setDescription('on / off').setRequired(true)
       .addChoices({ name: 'on', value: 'on' }, { name: 'off', value: 'off' })
    ),

  new SlashCommandBuilder()
    .setName('노래반복').setDescription('반복 모드를 설정합니다 (안함/한곡/모두).')
    .addStringOption(o =>
      o.setName('상태').setDescription('none / one / all').setRequired(true)
       .addChoices(
         { name: 'none(안함)', value: 'none' },
         { name: 'one(한곡)', value: 'one' },
         { name: 'all(모두)', value: 'all' },
       )
    ),
];

export async function handleMusic(interaction) {
  if (!interaction.isChatInputCommand()) return;
  const { commandName } = interaction;
  const gq = queueManager.get(interaction.guildId);

  if (commandName === '입장') {
    const targetChannel = interaction.options.getChannel('채널') || interaction.member?.voice?.channel;
    if (!targetChannel) {
      return interaction.reply({ content: '먼저 음성 채널에 들어가거나, 채널을 지정해 주세요.', ephemeral: true });
    }
    await gq.join(targetChannel);
    return interaction.reply('✅ 입장 완료!');
  }

  if (commandName === '퇴장') {
    gq.leave();
    return interaction.reply('👋 퇴장했어요.');
  }

  if (commandName === '재생') {
    const query = interaction.options.getString('query', true);
    const targetChannel = interaction.options.getChannel('채널') || interaction.member?.voice?.channel;

    if (!targetChannel) {
      return interaction.reply({ content: '먼저 음성 채널에 들어가거나, 채널을 지정해 주세요.', ephemeral: true });
    }

    await interaction.deferReply();

    try {
      await gq.join(targetChannel);
      const track = await resolveTrack(query);

      if (!track?.url) {
        await interaction.editReply('URL 생성에 실패했어요. 다른 검색어/URL로 시도해 주세요.');
        return;
      }

      gq.enqueue(track);

      if (!gq.current && gq.player.state.status !== 'playing') {
        await gq.playNext();
      }

      await interaction.editReply(`🎵 추가됨: **${track.title}**`);
    } catch (e) {
      console.error(e);
      await interaction.editReply('재생 중 오류가 발생했어요.');
    }
    return;
  }

  if (commandName === '스킵') {
    gq.skip();
    return interaction.reply('⏭️ 다음 곡으로 넘어갈게요.');
  }

  if (commandName === '일시정지') {
    gq.pause();
    return interaction.reply('⏸️ 일시정지했습니다.');
  }

  if (commandName === '정지') {
    gq.stop();
    return interaction.reply('⏹️ 정지하고 큐를 비웠습니다.');
  }

  if (commandName === '구간이동') {
    const timeStr = interaction.options.getString('time', true);
    const seconds = parseTimeToSeconds(timeStr);
    if (seconds == null) {
      return interaction.reply({ content: '시간 형식이 올바르지 않습니다. 예) 1:23, 0:05:10', ephemeral: true });
    }
    if (!gq.current) {
      return interaction.reply({ content: '재생 중인 곡이 없어요.', ephemeral: true });
    }
    await gq.seek(seconds);
    return interaction.reply(`⏩ ${timeStr} 지점으로 이동했어요.`);
  }

  if (commandName === '재생목록') {
    const list = gq.getQueue();
    if (list.length === 0) {
      return interaction.reply('목록이 비어 있어요.');
    }
    const lines = list.map((t, i) => (t.now ? `▶️  **${t.title}**` : `${i}. ${t.title}`));
    return interaction.reply(lines.join('\n'));
  }

  if (commandName === '노래랜덤') {
    const v = interaction.options.getString('상태', true);
    gq.setShuffle(v === 'on');
    return interaction.reply(v === 'on' ? '🔀 랜덤 재생: 켜짐' : '🔁 랜덤 재생: 꺼짐');
  }

  if (commandName === '노래반복') {
    const v = interaction.options.getString('상태', true);
    gq.setRepeat(v);
    const label = v === 'none' ? '안함' : v === 'one' ? '한곡' : '모두';
    return interaction.reply(`🔁 반복 모드: ${label}`);
  }
}
