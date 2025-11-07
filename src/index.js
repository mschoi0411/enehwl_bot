// src/index.js
import ffmpeg from 'ffmpeg-static';
if (ffmpeg) process.env.FFMPEG_PATH = ffmpeg; // @discordjs/voice가 ffmpeg 경로 인식

import 'dotenv/config';
import { Client, GatewayIntentBits, Events, ChannelType } from 'discord.js';
import { handleMusic, musicCommands } from './commands/music.js';
import { handleDots, dotsCommands } from './commands/dots.js';
import { handleTTS, ttsCommands } from './commands/tts.js';
import { handleClean, cleanCommands } from './commands/clean.js';

const { DISCORD_TOKEN } = process.env;

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildVoiceStates, // 음성 연결
  ],
});

client.once(Events.ClientReady, (c) => {
  console.log(`✅ Logged in as ${c.user.tag}`);
});

client.on(Events.InteractionCreate, async (interaction) => {
  try {
    if (!interaction.isChatInputCommand()) return;

    // 명령 라우팅
    const name = interaction.commandName;
    if (musicCommands.some(c => c.name === name)) return handleMusic(interaction);
    if (ttsCommands.some(c => c.name === name)) return handleTTS(interaction);
    if (dotsCommands.some(c => c.name === name)) return handleDots(interaction);
    if (cleanCommands.some(c => c.name === name)) return handleClean(interaction);
  } catch (e) {
    console.error('[Interaction Error]', e);
    if (interaction.deferred || interaction.replied) {
      await interaction.editReply('오류가 발생했어요.');
    } else {
      await interaction.reply({ content: '오류가 발생했어요.', ephemeral: true });
    }
  }
});

client.login(DISCORD_TOKEN);
