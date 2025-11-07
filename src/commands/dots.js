// src/commands/dots.js
import { SlashCommandBuilder, ChannelType } from 'discord.js';
import { queueManager } from '../player/queue.js';
import { createAudioResource, StreamType } from '@discordjs/voice';
import https from 'https';

export const dotsCommands = [
  new SlashCommandBuilder()
    .setName('dots')
    .setDescription('간단한 효과음을 재생합니다.')
    .addStringOption(o =>
      o.setName('메시지').setDescription('화면에 출력할 메시지').setRequired(false)
    )
    .addChannelOption(o =>
      o.setName('언어').setDescription('(옵션)').
        addChannelTypes(ChannelType.GuildVoice) // placeholder, 실제 옵션 아님
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

export async function handleDots(interaction) {
  if (!interaction.isChatInputCommand() || interaction.commandName !== 'dots') return;

  const msg = interaction.options.getString('메시지') || '…';
  const targetChannel = interaction.member?.voice?.channel;
  if (!targetChannel) {
    return interaction.reply({ content: '먼저 음성 채널에 들어가 주세요.', ephemeral: true });
  }

  await interaction.deferReply();
  const gq = queueManager.get(interaction.guildId);
  await gq.join(targetChannel);

  try {
    // 샘플 mp3 (원하는 효과음 URL로 교체 가능)
    const sample = 'https://www2.cs.uic.edu/~i101/SoundFiles/StarWars60.wav';
    const resource = await resourceFromUrl(sample);
    gq.player.play(resource);
    await interaction.editReply(`✨ ${msg}`);
  } catch (e) {
    console.error(e);
    await interaction.editReply('dots 재생 중 오류가 발생했어요.');
  }
}
