import asyncio
import logging
import os
import re
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InputMediaPhoto, InputMediaVideo,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import CommandStart, StateFilter

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = int(os.getenv("GROUP_ID"))

# ── Яндекс Object Storage (приватный архив контента) ────────
S3_BUCKET = os.getenv("S3_BUCKET", "artpol-content")
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "https://storage.yandexcloud.net")
S3_REGION = os.getenv("S3_REGION", "ru-central1")
S3_ENABLED = bool(os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("content-bot")

MSK = ZoneInfo("Europe/Moscow")

_s3_client = None


def get_s3():
    """Ленивая инициализация boto3-клиента Яндекс Object Storage."""
    global _s3_client
    if _s3_client is None:
        import boto3
        from botocore.config import Config
        _s3_client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", ""),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
            config=Config(signature_version="s3v4"),
            region_name=S3_REGION,
        )
    return _s3_client


def _slug(text: str) -> str:
    """Безопасный фрагмент ключа S3: буквы/цифры (вкл. кириллицу) и дефис, остальное → _."""
    return re.sub(r"[^\w\-]+", "_", (text or "").strip(), flags=re.UNICODE).strip("_") or "obj"


ALLOWED_USERS = {
    800204567,    # Денис Акценин (@denxland)
    1670809909,   # Саня
    6016286196,   # Женя Смирнов
    7573104945,   # Азиз Азизович (новая бригада вместо Паши, 08.2026)
    6441794225,   # Диман
    7925638273,   # Сергей Григорьев (руководитель)
}

# Канонические имена бригадиров по Telegram-ID — корневые папки в архиве.
# Объекты не-бригадиров (Денис/Сергей/прочие) складываются в «Прочее».
FOREMAN_NAMES = {
    1670809909: "Саня",
    6016286196: "Женя",
    7573104945: "Азиз",
    6441794225: "Диман",
}

router = Router()


# ── FSM States ──────────────────────────────────────────────
class ObjectForm(StatesGroup):
    text = State()           # ввод текста объекта
    obj_type = State()       # выбор типа: Квартира / Дом / Коммерция
    workers = State()        # сколько человек работало
    hours = State()          # сколько часов заняло
    media = State()          # загрузка фото и видео
    confirm = State()        # подтверждение / редактирование


# ── Keyboards ───────────────────────────────────────────────
def type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏢 Квартира", callback_data="type_apartment"),
            InlineKeyboardButton(text="🏠 Дом", callback_data="type_house"),
            InlineKeyboardButton(text="🏗 Коммерция", callback_data="type_commercial"),
        ]
    ])


def workers_keyboard() -> InlineKeyboardMarkup:
    """Сколько человек. Кнопками: бригадир отвечает на объекте, руки заняты."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{n}", callback_data=f"wrk_{n}") for n in (1, 2, 3)],
        [InlineKeyboardButton(text=f"{n}", callback_data=f"wrk_{n}") for n in (4, 5, 6)],
        [InlineKeyboardButton(text="7 и больше", callback_data="wrk_7+"),
         InlineKeyboardButton(text="не считал", callback_data="wrk_skip")],
    ])


def hours_keyboard() -> InlineKeyboardMarkup:
    """Сколько часов заняло. Тоже кнопками."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{n} ч", callback_data=f"hrs_{n}") for n in (3, 4, 5)],
        [InlineKeyboardButton(text=f"{n} ч", callback_data=f"hrs_{n}") for n in (6, 7, 8)],
        [InlineKeyboardButton(text="9 и больше", callback_data="hrs_9+"),
         InlineKeyboardButton(text="два дня", callback_data="hrs_2days")],
        [InlineKeyboardButton(text="не считал", callback_data="hrs_skip")],
    ])


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data="send"),
            InlineKeyboardButton(text="✏️ Править", callback_data="edit"),
        ]
    ])


def edit_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Текст", callback_data="edit_text")],
        [InlineKeyboardButton(text="🏷 Тип объекта", callback_data="edit_type")],
        [InlineKeyboardButton(text="👷 Человек и часы", callback_data="edit_crew")],
        [InlineKeyboardButton(text="📸 Фото/Видео", callback_data="edit_media")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_confirm")],
    ])


# ── Helpers ─────────────────────────────────────────────────
TYPE_LABELS = {
    "type_apartment": "🏢 Квартира",
    "type_house": "🏠 Дом",
    "type_commercial": "🏗 Коммерция",
}


def build_caption(data: dict) -> str:
    obj_type = TYPE_LABELS.get(data.get("obj_type"), "")
    date = datetime.now(MSK).strftime("%d.%m.%Y")
    name = data.get("user_name", "")
    crew = []
    if data.get("workers"):
        w = data["workers"]
        crew.append(f"👷 {w} чел." if w != "7+" else "👷 7+ чел.")
    if data.get("hours"):
        h = data["hours"]
        crew.append("⏱ два дня" if h == "2days" else (f"⏱ {h} ч" if h != "9+" else "⏱ 9+ ч"))
    crew_line = ("\n" + " · ".join(crew)) if crew else ""
    return (f"{obj_type}\n📅 {date} | 👷 {name}{crew_line}\n\n"
            f"{data.get('text', '')}")


async def archive_media_to_s3(bot: Bot, data: dict) -> int:
    """Скачивает медиа из Telegram и складывает в приватный бакет Яндекса.

    Возвращает число успешно залитых файлов. Ошибка отдельного файла не
    прерывает процесс — основная отправка в группу к этому моменту уже сделана.
    """
    if not S3_ENABLED:
        return 0
    media_list = data.get("media", [])
    if not media_list:
        return 0

    now = datetime.now(MSK)
    obj_type = TYPE_LABELS.get(data.get("obj_type"), "объект")
    type_clean = obj_type.split(" ", 1)[-1] if " " in obj_type else obj_type  # без эмодзи
    foreman = FOREMAN_NAMES.get(data.get("user_id"), "Прочее")
    # бригадир → месяц → объект (дата+время+тип в имени объекта)
    folder = f"{_slug(foreman)}/{now:%Y-%m}/{now:%d.%m}_{now:%H-%M}_{_slug(type_clean)}"

    s3 = get_s3()
    uploaded = photo_n = video_n = 0
    for item in media_list:
        try:
            if item["type"] == "photo":
                photo_n += 1
                key = f"{folder}/photo_{photo_n:02d}.jpg"
                content_type = "image/jpeg"
            else:
                video_n += 1
                key = f"{folder}/video_{video_n:02d}.mp4"
                content_type = "video/mp4"

            file = await bot.get_file(item["file_id"])
            buf = await bot.download_file(file.file_path)  # BytesIO
            buf.seek(0)
            await asyncio.to_thread(
                s3.upload_fileobj, buf, S3_BUCKET, key, {"ContentType": content_type}
            )
            uploaded += 1
        except Exception as e:
            logger.error("S3: не удалось залить %s: %s", item.get("file_id"), e)

    # Рядом кладём текстовое описание объекта
    try:
        body = build_caption(data).encode("utf-8")
        await asyncio.to_thread(
            s3.put_object,
            Bucket=S3_BUCKET,
            Key=f"{folder}/описание.txt",
            Body=body,
            ContentType="text/plain; charset=utf-8",
        )
    except Exception as e:
        logger.error("S3: не удалось залить описание: %s", e)

    logger.info("S3: загружено %d/%d файлов → %s", uploaded, len(media_list), folder)
    return uploaded


async def show_preview(message: Message, data: dict):
    """Показывает превью объекта с медиа и кнопками подтверждения."""
    caption = build_caption(data)
    media_list = data.get("media", [])

    if media_list:
        # Показываем первое фото/видео с подписью
        await message.answer(f"📋 <b>Превью:</b>\n\n{caption}\n\n"
                             f"📎 Медиафайлов: {len(media_list)}",
                             parse_mode="HTML",
                             reply_markup=confirm_keyboard())
    else:
        await message.answer(f"📋 <b>Превью:</b>\n\n{caption}",
                             parse_mode="HTML",
                             reply_markup=confirm_keyboard())


# ── Handlers ────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    if message.from_user.id not in ALLOWED_USERS:
        await message.answer("⛔ У вас нет доступа к этому боту.")
        return
    await state.clear()
    await message.answer(
        "👋 Привет! Отправь данные объекта текстом.\n\n"
        "Например:\n"
        "<i>Владимир +79601642483\n"
        "дом, за городом, 1 этаж\n"
        "Балахна Володарского 9\n"
        "Координаты: 56.487738, 43.612372\n"
        "25.0м2 87.0мм</i>",
        parse_mode="HTML",
    )
    await state.set_state(ObjectForm.text)


@router.message(StateFilter(None, ObjectForm.text), F.text)
async def receive_text(message: Message, state: FSMContext):
    # Точка входа: ловит текст и в начале диалога, и БЕЗ состояния (после
    # перезапуска бота) — поэтому /start больше не обязателен.
    if message.from_user.id not in ALLOWED_USERS:
        await message.answer("⛔ У вас нет доступа к этому боту.")
        return
    if message.text.startswith("/"):
        await message.answer(
            "Чтобы добавить объект, просто пришлите его данные текстом — "
            "адрес, клиент, площадь, координаты."
        )
        return
    user = message.from_user
    user_name = user.full_name or user.username or "Неизвестный"
    await state.update_data(text=message.text, media=[], user_name=user_name, user_id=user.id)
    await message.answer("Выберите тип объекта:", reply_markup=type_keyboard())
    await state.set_state(ObjectForm.obj_type)


@router.message(StateFilter(None))
async def no_state_hint(message: Message, state: FSMContext):
    # Любое нетекстовое сообщение без состояния (фото/видео после перезапуска) —
    # подсказываем, а не молчим.
    if message.from_user.id not in ALLOWED_USERS:
        await message.answer("⛔ У вас нет доступа к этому боту.")
        return
    await message.answer(
        "👋 Пришлите данные объекта <b>текстом</b> (адрес, клиент, площадь, координаты) — "
        "и я проведу по шагам. Команда /start не нужна.",
        parse_mode="HTML",
    )


@router.callback_query(ObjectForm.obj_type, F.data.startswith("type_"))
async def receive_type(callback: CallbackQuery, state: FSMContext):
    await state.update_data(obj_type=callback.data)
    await callback.message.edit_text(
        f"Тип: {TYPE_LABELS[callback.data]}\n\n"
        "👷 Сколько человек работало на объекте?",
        reply_markup=workers_keyboard(),
    )
    await state.set_state(ObjectForm.workers)


# ── Люди и часы ─────────────────────────────────────────────
# Эти два поля добавлены 19.08.2026 по просьбе Дениса. Без них в описании
# объекта не было ни сроков, ни состава бригады — и агент, писавший посты,
# дважды выдумывал «за один выезд». Теперь данные приходят из первых рук.

async def ask_media(message, state: FSMContext) -> None:
    await message.edit_text(
        "📸 Теперь загрузите фото и видео.\n"
        "Когда закончите — нажмите кнопку ниже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Готово", callback_data="media_done")]
        ]),
    )
    await state.set_state(ObjectForm.media)


@router.callback_query(ObjectForm.workers, F.data.startswith("wrk_"))
async def receive_workers(callback: CallbackQuery, state: FSMContext):
    value = callback.data.removeprefix("wrk_")
    await state.update_data(workers="" if value == "skip" else value)
    await callback.message.edit_text(
        "⏱ Сколько времени заняла работа?",
        reply_markup=hours_keyboard(),
    )
    await state.set_state(ObjectForm.hours)


@router.message(ObjectForm.workers)
async def workers_wrong(message: Message):
    await message.answer("Выберите кнопкой ниже 👇", reply_markup=workers_keyboard())


@router.callback_query(ObjectForm.hours, F.data.startswith("hrs_"))
async def receive_hours(callback: CallbackQuery, state: FSMContext):
    value = callback.data.removeprefix("hrs_")
    await state.update_data(hours="" if value == "skip" else value)
    data = await state.get_data()
    if data.get("media"):          # правка с экрана подтверждения — фото уже есть
        await callback.message.edit_reply_markup(reply_markup=None)
        await show_preview(callback.message, data)
        await state.set_state(ObjectForm.confirm)
        return
    await ask_media(callback.message, state)


@router.message(ObjectForm.hours)
async def hours_wrong(message: Message):
    await message.answer("Выберите кнопкой ниже 👇", reply_markup=hours_keyboard())


@router.callback_query(ObjectForm.confirm, F.data == "edit_crew")
async def edit_crew_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "👷 Сколько человек работало на объекте?",
        reply_markup=workers_keyboard(),
    )
    await state.set_state(ObjectForm.workers)


@router.message(ObjectForm.obj_type)
async def obj_type_wrong(message: Message):
    await message.answer("Выберите тип объекта кнопкой ниже 👇", reply_markup=type_keyboard())


@router.message(ObjectForm.media, F.photo)
async def receive_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    media = data.get("media", [])
    media.append({"type": "photo", "file_id": message.photo[-1].file_id})
    await state.update_data(media=media)
    await message.answer(f"📷 Фото принято (всего: {len(media)}). Ещё или нажмите «Готово».")


@router.message(ObjectForm.media, F.video)
async def receive_video(message: Message, state: FSMContext):
    data = await state.get_data()
    media = data.get("media", [])
    media.append({"type": "video", "file_id": message.video.file_id})
    await state.update_data(media=media)
    await message.answer(f"🎬 Видео принято (всего: {len(media)}). Ещё или нажмите «Готово».")


@router.message(ObjectForm.media, F.document)
async def receive_document(message: Message, state: FSMContext):
    mime = message.document.mime_type or ""
    data = await state.get_data()
    media = data.get("media", [])
    if mime.startswith("image/"):
        media.append({"type": "photo", "file_id": message.document.file_id})
        await state.update_data(media=media)
        await message.answer(f"📷 Фото принято (всего: {len(media)}). Ещё или нажмите «Готово».")
    elif mime.startswith("video/"):
        media.append({"type": "video", "file_id": message.document.file_id})
        await state.update_data(media=media)
        await message.answer(f"🎬 Видео принято (всего: {len(media)}). Ещё или нажмите «Готово».")
    else:
        await message.answer("⚠️ Отправьте фото или видео. Другие типы файлов не принимаются.")


@router.callback_query(ObjectForm.media, F.data == "media_done")
async def media_done(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("media"):
        await callback.answer("Загрузите хотя бы одно фото или видео!", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await show_preview(callback.message, data)
    await state.set_state(ObjectForm.confirm)


# ── Confirm / Edit ──────────────────────────────────────────

@router.callback_query(ObjectForm.confirm, F.data == "edit")
async def edit_menu(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Что хотите изменить?", reply_markup=edit_keyboard())


@router.callback_query(ObjectForm.confirm, F.data == "edit_text")
async def edit_text_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📝 Отправьте новый текст объекта:")
    await state.set_state(ObjectForm.text)


@router.callback_query(ObjectForm.confirm, F.data == "edit_type")
async def edit_type_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Выберите новый тип объекта:", reply_markup=type_keyboard())
    await state.set_state(ObjectForm.obj_type)


@router.callback_query(ObjectForm.confirm, F.data == "edit_media")
async def edit_media_prompt(callback: CallbackQuery, state: FSMContext):
    await state.update_data(media=[])
    await callback.message.edit_text(
        "📸 Загрузите фото и видео заново.\n"
        "Когда закончите — нажмите «Готово».",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Готово", callback_data="media_done")]
        ]),
    )
    await state.set_state(ObjectForm.media)


@router.callback_query(ObjectForm.confirm, F.data == "back_to_confirm")
async def back_to_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await callback.message.edit_reply_markup(reply_markup=None)
    await show_preview(callback.message, data)


# ── Send to group ───────────────────────────────────────────

@router.callback_query(ObjectForm.confirm, F.data == "send")
async def send_to_group(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    caption = build_caption(data)
    media_list = data.get("media", [])

    # Формируем медиа-группу
    media_group = []
    for i, item in enumerate(media_list):
        if item["type"] == "photo":
            media_group.append(InputMediaPhoto(
                media=item["file_id"],
                caption=caption if i == 0 else None,
                parse_mode="HTML" if i == 0 else None,
            ))
        elif item["type"] == "video":
            media_group.append(InputMediaVideo(
                media=item["file_id"],
                caption=caption if i == 0 else None,
                parse_mode="HTML" if i == 0 else None,
            ))

    if media_group:
        # Telegram максимум 10 медиа в альбоме — разбиваем на части
        for chunk_start in range(0, len(media_group), 10):
            chunk = media_group[chunk_start:chunk_start + 10]
            await bot.send_media_group(chat_id=GROUP_ID, media=chunk)
    else:
        await bot.send_message(chat_id=GROUP_ID, text=caption, parse_mode="HTML")

    await callback.message.edit_text("✅ Отправлено в группу!")

    # Архив в приватный бакет Яндекса (если настроены ключи)
    archived = 0
    try:
        archived = await archive_media_to_s3(bot, data)
    except Exception as e:
        logger.error("S3: архивация не удалась: %s", e)

    await state.clear()

    # Предлагаем создать новый объект
    next_msg = "Можете отправить данные следующего объекта."
    if archived:
        next_msg = f"☁️ В архив загружено: {archived} файл(ов).\n\n" + next_msg
    await callback.message.answer(next_msg)
    await state.set_state(ObjectForm.text)


# ── Fallback for wrong input ────────────────────────────────

@router.message(ObjectForm.media)
async def media_wrong_type(message: Message):
    await message.answer("⚠️ Отправьте фото или видео. Другие типы файлов не принимаются.")


@router.message(ObjectForm.confirm)
async def confirm_wrong(message: Message):
    await message.answer("Почти готово — нажмите ✅ Отправить или ✏️ Править.")


# ── Main ────────────────────────────────────────────────────

def get_s3_read():
    """Клиент для чтения бакета: отдельные read-ключи, иначе основные."""
    key = os.getenv("S3_READ_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID", "")
    secret = os.getenv("S3_READ_SECRET") or os.getenv("AWS_SECRET_ACCESS_KEY", "")
    if not os.getenv("S3_READ_KEY_ID"):
        return get_s3()
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3", endpoint_url=S3_ENDPOINT, aws_access_key_id=key,
        aws_secret_access_key=secret, config=Config(signature_version="s3v4"),
        region_name=S3_REGION,
    )


def start_reminders(bot: Bot) -> None:
    """Ежедневная сверка «план заливок ↔ присланный контент» (если настроена)."""
    if not (S3_ENABLED and os.getenv("KRONOS_API_KEY")):
        logger.info("напоминания выключены (нет KRONOS_API_KEY или ключей S3)")
        return
    try:
        from datetime import date as _date
        import reminders
        start_from = _date.fromisoformat(
            os.getenv("REMINDERS_START", datetime.now(MSK).date().isoformat()))
        foreman_ids = {name: uid for uid, name in FOREMAN_NAMES.items()}
        asyncio.create_task(reminders.reminder_loop(
            bot, get_s3_read(), get_s3(), S3_BUCKET, foreman_ids, start_from))
    except Exception as e:
        logger.error("не удалось запустить напоминания: %s", e)


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    start_reminders(bot)
    print("Bot started...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
