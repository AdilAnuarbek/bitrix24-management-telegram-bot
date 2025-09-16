import os
import logging
import requests
import asyncio
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BITRIX_WEBHOOK = os.getenv("BITRIX_WEBHOOK")
MANAGER_TELEGRAM_ID = os.getenv("MANAGER_TELEGRAM_ID")

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

periodic_check_task = None

async def periodic_check(interval_seconds):
    while True:
        try:
            await send_leads_to_manager()
        except Exception as e:
            logging.error(f"Ошибка в ходе периодической проверки: {e}")

        await asyncio.sleep(interval_seconds)

def get_overdue_leads():
    method = 'crm.lead.list'
    url = f"{BITRIX_WEBHOOK}{method}"

    two_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

    params = {
        'order': {"DATE_CREATE": "ASC"},
        'filter': {
            "STATUS_ID": "NEW",
            "<DATE_CREATE": two_hours_ago
        },
        'select': ["ID", "TITLE", "PHONE"]
    }

    try:
        response = requests.post(url, json=params)
        response.raise_for_status()
        data = response.json()

        if 'result' in data and data['result']:
            leads = data['result']
            logging.info(f"Найдено {len(leads)} просроченных лидов.")
            return leads
        else:
            logging.info("Просроченных лидов не найдено.")
            return []

    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка при запросе к Bitrix24 API: {e}")
        return None


def add_comment_to_lead(lead_id, text):
    method = 'crm.timeline.comment.add'
    url = f"{BITRIX_WEBHOOK}{method}"
    params = {
        'fields': {
            "ENTITY_ID": lead_id,
            "ENTITY_TYPE": "lead",
            "COMMENT": text
        }
    }
    try:
        response = requests.post(url, json=params)
        response.raise_for_status()
        logging.info(f"Комментарий '{text}' добавлен к лиду ID {lead_id}")
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка при добавлении комментария к лиду ID {lead_id}: {e}")
        return None


def create_task_for_lead(lead_id):
    method = 'tasks.task.add'
    url = f"{BITRIX_WEBHOOK}{method}"

    deadline = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    params = {
        'fields': {
            "TITLE": f"Связаться с клиентом по лиду №{lead_id}",
            "RESPONSIBLE_ID": 1,
            "DEADLINE": deadline,
            "UF_CRM_TASK": [f"L_{lead_id}"]
        }
    }
    try:
        response = requests.post(url, json=params)
        response.raise_for_status()
        logging.info(f"Задача создана для лида ID {lead_id}")
        return response.json()
    except requests.exceptions.RequestException as e:
        logging.error(f"Ошибка при создании задачи для лида ID {lead_id}: {e}")
        return None


# Telegram Bot Section

def get_lead_keyboard(lead_id):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Позвонил", callback_data=f"called:{lead_id}"),
        InlineKeyboardButton(text="💬 Написал", callback_data=f"wrote:{lead_id}"),
        InlineKeyboardButton(text="⏳ Отложить 2 часа", callback_data=f"delay:{lead_id}")
    )
    return builder.as_markup()


async def send_leads_to_manager():
    leads = get_overdue_leads()
    if leads is None:
        await bot.send_message(MANAGER_TELEGRAM_ID, "Не удалось подключиться к Bitrix24. Проверьте настройки или API.")
        return

    if not leads:
        return

    for lead in leads:
        lead_id = lead['ID']
        title = lead['TITLE']
        phone = lead.get('PHONE', [{}])[0].get('VALUE', 'не указан')

        text = (
            f"**Просроченный лид!**\n\n"
            f"**ID:** `{lead_id}`\n"
            f"**Название:** {title}\n"
            f"**Телефон:** `{phone}\n`"
        )

        await bot.send_message(
            chat_id=MANAGER_TELEGRAM_ID,
            text=text,
            reply_markup=get_lead_keyboard(lead_id),
            parse_mode='Markdown'
        )


@dp.callback_query(F.data.startswith('called:'))
async def process_callback_called(callback: CallbackQuery):
    lead_id = callback.data.split(':')[1]
    add_comment_to_lead(lead_id, "Менеджер позвонил клиенту.")
    await callback.message.edit_text(f"{callback.message.text}\n\n*Статус: ✅ Обработан (звонок)*",
                                     parse_mode='Markdown')
    await callback.answer(f"Отметили звонок по лиду {lead_id}")


@dp.callback_query(F.data.startswith('wrote:'))
async def process_callback_wrote(callback: CallbackQuery):
    lead_id = callback.data.split(':')[1]
    add_comment_to_lead(lead_id, "Менеджер написал клиенту.")
    await callback.message.edit_text(f"{callback.message.text}\n\n*Статус: 💬 Обработан (написал)*",
                                     parse_mode='Markdown')
    await callback.answer(f"Отметили сообщение по лиду {lead_id}")


@dp.callback_query(F.data.startswith('delay:'))
async def process_callback_delay(callback: CallbackQuery):
    lead_id = callback.data.split(':')[1]
    create_task_for_lead(lead_id)
    await callback.message.edit_text(f"{callback.message.text}\n\n*Статус: ⏳ Задача на перезвон создана*",
                                     parse_mode='Markdown')
    await callback.answer(f"Создана задача для лида {lead_id}")


@dp.message(CommandStart())
async def send_welcome(message: Message):
    await message.answer(
        "Привет! Я бот для уведомлений о лидах из Bitrix24.\nИспользуй /check_leads для ручной проверки.\n" +
        "Используй /turn_on_periodic_check чтобы включить периодическую проверку лидов\n" +
        "Используй /turn_off_periodic_check чтобы выключить периодическую проверку лидов")

@dp.message(F.text == '/check_leads')
async def manual_check(message: Message):
    await message.answer("Начинаю проверку лидов...")
    await send_leads_to_manager()

@dp.message(F.text == '/turn_on_periodic_check')
async def turn_on_periodic_check(message: Message):
    global periodic_check_task
    if periodic_check_task and not periodic_check_task.done():
        await message.answer("Периодическая проверка уже включена.")
        return

    periodic_check_task = asyncio.create_task(periodic_check(10))  # 30 minutes
    await message.answer("Включил периодическую проверку лидов.\n" +
                         "Используй /turn_off_periodic_check чтобы выключить периодическую проверку лидов\n")

@dp.message(F.text == '/turn_off_periodic_check')
async def turn_off_periodic_check(message: Message):
    global periodic_check_task
    if not periodic_check_task or periodic_check_task.done():
        await message.answer("Периодическая проверка уже выключена.")
        return

    periodic_check_task.cancel()
    periodic_check_task = None
    await message.answer("Выключил периодическую проверку лидов.\n" +
                         "Используй /turn_on_periodic_check чтобы включить периодическую проверку лидов\n")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())