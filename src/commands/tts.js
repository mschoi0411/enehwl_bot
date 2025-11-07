// src/commands/tts.js
import { SlashCommandBuilder, ChannelType } from 'discord.js';
import { queueManager } from '../player/queue.js';
import googleTTS from 'google-tts-api';
import { createAudioResource, StreamType } from '@discordjs/voice';
import https from 'https';

export const ttsCommands = [
  new SlashCommandBuilder()
    .setName('tts')
    .setDescription('텍스트를 음성으로 읽어줍니다.')
    .addStringOption(o => o.setName('text').setDescription('읽을 문장').setRequired(true))
    .addStringOption(o =>
      o.setName('lang')
       .setDescription('언어 코드 (기본 ko)')
       .setRequired(false)
    )
    .addChannelOption(o =>
      o.setName('채널')
       .setDescription('재생할 음성 채널 (생략 시 내 채널)')
       .addChannelTypes(ChannelType.GuildVoice)
    ),
];

async function resourceFromUrl(mp3Url) {
  return await new Promise((resolve, reject) => {
    https.get(mp3Url, (res) => {
      const chunks = [];
      res.on('data', (d) => chunks.push(d));
      res.on('end', () => {
        const buffer = Buffer.concat(chunks);
        const resource = createAudioResource(buffer, { inputType: StreamType.Arbitrary });
        resolve(resource);
      });
    }).on('error', reject);
  });
}

export async function handleTTS(interaction) {
  if (!interaction.isChatInputCommand() || interaction.commandName !== 'tts') return;

  const text = interaction.options.getString('text', true);
  const lang = interaction.options.getString('lang') || 'ko';
  const targetChannel = interaction.options.getChannel('채널') || interaction.member?.voice?.channel;

  if (!targetChannel) {
    return interaction.reply({ content: '먼저 음성 채널에 들어가거나, 채널을 지정해 주세요.', ephemeral: true });
  }

  await interaction.deferReply();

  const gq = queueManager.get(interaction.guildId);
  await gq.join(targetChannel);

  try {
    const url = googleTTS.getAudioUrl(text, { lang, slow: false, host: 'https://translate.google.com' });
    const resource = await resourceFromUrl(url);
    gq.player.play(resource);
    await interaction.editReply(`🗣️ TTS: "${text.slice(0, 100)}${text.length > 100 ? '…' : ''}"`);
  } catch (e) {
    console.error(e);
    await interaction.editReply('TTS 생성 중 오류가 발생했어요.');
  }
}
