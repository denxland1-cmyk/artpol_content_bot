import asyncio
import os
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InputMediaPhoto, InputMediaVideo,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import CommandStart

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = int(os.getenv("GROUP_ID"))

ALLOWED_USERS = {
    800204567,    # Денис Акценин (@denxland)
    1670809909,   # Саня
    6016286196,   # Женя Смирнов
    5399664883,   # Паша
    6441794225,   # Диман
    7925638273,   # Сергей Григорьев (руководитель)
}

router = Router()


# ── FSM States ──────────────────────────────────────────────
class ObjectForm(StatesGroup):
    text = State()           # ввод текста объекта
    obj_type = State()       # выбор типа: Квартира / Дом / Коммерция
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
    from datetime import datetime
    obj_type = TYPE_LABELS.get(data.get("obj_type"), "")
    date = datetime.now().strftime("%d.%m.%Y")
    name = data.get("user_name", "")
    return f"{obj_type}\n📅 {date} | 👷 {name}\n\n{data.get('text', '')}"


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


@router.message(ObjectForm.text, F.text)
async def receive_text(message: Message, state: FSMContext):
    user = message.from_user
    user_name = user.full_name or user.username or "Неизвестный"
    await state.update_data(text=message.text, media=[], user_name=user_name)
    await message.answer("Выберите тип объекта:", reply_markup=type_keyboard())
    await state.set_state(ObjectForm.obj_type)


@router.callback_query(ObjectForm.obj_type, F.data.startswith("type_"))
async def receive_type(callback: CallbackQuery, state: FSMContext):
    await state.update_data(obj_type=callback.data)
    await callback.message.edit_text(
        f"Тип: {TYPE_LABELS[callback.data]}\n\n"
        "📸 Теперь загрузите фото и видео.\n"
        "Когда закончите — нажмите кнопку ниже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Готово", callback_data="media_done")]
        ]),
    )
    await state.set_state(ObjectForm.media)


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
    await state.clear()

    # Предлагаем создать новый объект
    await callback.message.answer(
        "Можете отправить данные следующего объекта.",
    )
    await state.set_state(ObjectForm.text)


# ── Fallback for wrong input ────────────────────────────────

@router.message(ObjectForm.media)
async def media_wrong_type(message: Message):
    await message.answer("⚠️ Отправьте фото или видео. Другие типы файлов не принимаются.")


# ── Main ────────────────────────────────────────────────────

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    print("Bot started...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
