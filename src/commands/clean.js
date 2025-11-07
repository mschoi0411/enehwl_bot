// src/commands/clean.js
import { SlashCommandBuilder, PermissionFlagsBits } from 'discord.js';

export const cleanCommands = [
  new SlashCommandBuilder()
    .setName('청소')
    .setDescription('이 채널의 최근 메시지를 삭제합니다.')
    .addIntegerOption(o =>
      o.setName('개수').setDescription('삭제할 개수(1~100)').setRequired(true)
    )
    .setDefaultMemberPermissions(PermissionFlagsBits.ManageMessages),
];

export async function handleClean(interaction) {
  if (!interaction.isChatInputCommand() || interaction.commandName !== '청소') return;

  const count = interaction.options.getInteger('개수', true);
  if (count < 1 || count > 100) {
    return interaction.reply({ content: '1~100 사이로 입력해 주세요.', ephemeral: true });
  }

  try {
    await interaction.channel.bulkDelete(count, true);
    await interaction.reply({ content: `🧹 ${count}개 메시지를 삭제했습니다.`, ephemeral: true });
  } catch (e) {
    console.error(e);
    await interaction.reply({ content: '삭제 중 오류가 발생했어요.', ephemeral: true });
  }
}
