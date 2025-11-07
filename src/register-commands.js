// src/register-commands.js
import 'dotenv/config';
import { REST, Routes } from 'discord.js';
import { musicCommands } from './commands/music.js';
import { ttsCommands } from './commands/tts.js';
import { dotsCommands } from './commands/dots.js';
import { cleanCommands } from './commands/clean.js';

const { DISCORD_TOKEN, CLIENT_ID, GUILD_ID } = process.env;

async function main() {
  const rest = new REST({ version: '10' }).setToken(DISCORD_TOKEN);
  const commands = [
    ...musicCommands,
    ...ttsCommands,
    ...dotsCommands,
    ...cleanCommands,
  ].map(c => c.toJSON());

  try {
    if (GUILD_ID) {
      console.log('🔧 길드 커맨드 등록 중…');
      await rest.put(Routes.applicationGuildCommands(CLIENT_ID, GUILD_ID), { body: commands });
      console.log('✅ 길드 커맨드 등록 완료');
    } else {
      console.log('🔧 전역 커맨드 등록 중…');
      await rest.put(Routes.applicationCommands(CLIENT_ID), { body: commands });
      console.log('✅ 전역 커맨드 등록 완료');
    }
  } catch (e) {
    console.error(e);
  }
}
main();
