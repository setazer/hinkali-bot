import asyncio
import datetime
import os
import random
import secrets
from contextlib import contextmanager

from aiogram import Bot, Dispatcher, types
from aiogram.types import ChatType
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.emoji import emojize
from aiogram.utils.exceptions import MessageNotModified, BadRequest
from aiogram.utils.executor import Executor

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
PORT = int(os.getenv("PORT", '8443'))
DOMAIN = os.getenv("DOMAIN", "example.com")
SECRET_KEY = secrets.token_urlsafe(48)
WEBHOOK_BASE_PATH = os.getenv("WEBHOOK_BASE_PATH", "/webhook")
WEBHOOK_PATH = f"{WEBHOOK_BASE_PATH}/{SECRET_KEY}"
WEBHOOK_URL = f"https://{DOMAIN}{WEBHOOK_PATH}"

bot = Bot(token=TELEGRAM_TOKEN)
bot.users = {}
bot.orders = {}
bot.order_message = None
bot.discount = 30
dp = Dispatcher(bot)

hin_cb = CallbackData('hin', 'type', 'amount')
disc_cb = CallbackData('discount', 'multiplier')

h_types = {'ВК': 50, 'ВБ': 50, 'ЖК': 55, 'ЖБ': 55}
h_amounts = ('-4', '-2', '-1', '0', '+1', '+2', '+4')


@contextmanager
def ignored(exc):
    try:
        yield
    except exc:
        pass


def pointer_markup():
    markup = types.InlineKeyboardMarkup()
    link_chat_id = str(bot.order_message.chat.id).replace("-100", "")
    url = f'https://t.me/c/{link_chat_id}/{bot.order_message.message_id}'
    markup.add(types.InlineKeyboardButton(text='Ссылка на заказ', url=url))
    return markup


def hinkali_markup():
    markup = types.InlineKeyboardMarkup()
    for h_type in h_types:
        buttons = []
        for h_amount in h_amounts:
            text = h_amount.replace('0', h_type)
            data = hin_cb.new(type=h_type, amount=h_amount)
            buttons.append(types.InlineKeyboardButton(text=text, callback_data=data))
        markup.row(*buttons)
    d_text = emojize(':white_check_mark: Скидка 30%' if bot.discount else ':x: Скидки нет')
    markup.row(types.InlineKeyboardButton(text=d_text,
                                          callback_data=disc_cb.new(multiplier=str(30 - bot.discount))))
    return markup


def count_user(user):
    orders = bot.orders[user]
    total = sum(map(lambda x: orders.get(x) * h_types[x] * (100 - bot.discount) / 100, orders))
    return total


def gen_report():
    lines = ['Заказ:']
    total_cost = 0

    if not bot.orders:
        order = 'Ещё ничего не заказано'
        return order
    for user, user_orders in bot.orders.items():
        name = bot.users[user]
        user_cost = count_user(user)
        orders = ", ".join([f'{str(amount)} {h_type}' for h_type, amount in user_orders.items()])
        lines.append(f'{name} ({user_cost} ₽): {orders}')
        total_cost += user_cost
    total_orders = {h_type: sum(user_orders.get(h_type, 0) for user_orders in bot.orders.values()) for h_type in h_types}
    total_orders_str = ", ".join([f'{str(amount)} {h_type}' for h_type, amount in total_orders.items() if amount])
    lines.append('\nВсего заказ: {}'.format(total_orders_str))
    lines.append(f'Итого: {total_cost} ₽')
    order = '\n'.join(lines)
    return order


@dp.callback_query_handler(hin_cb.filter())
async def amount_change(call: types.CallbackQuery, callback_data):
    user, name = call.from_user.id, call.from_user.full_name or call.from_user.username
    h_type, amount = callback_data['type'], int(callback_data['amount'])
    bot.users.setdefault(user, name)
    cur_orders = bot.orders.setdefault(user, {})
    cur_amount = cur_orders.setdefault(h_type, 0) + amount
    if cur_amount <= 0:
        del cur_orders[h_type]
        if not cur_orders:
            del bot.orders[user]
            del bot.users[user]
    else:
        cur_orders[h_type] = cur_amount
    try:
        await bot.edit_message_text(gen_report(), call.message.chat.id,
                                    call.message.message_id, reply_markup=hinkali_markup())
    except MessageNotModified:
        await bot.answer_callback_query(call.id)


@dp.callback_query_handler(disc_cb.filter())
async def discount_change(call: types.CallbackQuery, callback_data):
    chat_id, mess_id = call.message.chat.id, call.message.message_id
    bot.discount = int(callback_data['multiplier'])
    if bot.orders:
        await bot.edit_message_text(gen_report(), chat_id, mess_id, reply_markup=hinkali_markup())
    else:
        await bot.edit_message_reply_markup(chat_id, mess_id, reply_markup=hinkali_markup())


@dp.message_handler(ChatType.is_group_or_super_group, commands=['hinkali'])
async def start_order(message: types.Message):
    chat_id, mess_id = message.chat.id, message.message_id
    if not bot.order_message:
        now = datetime.datetime.now()
        bot.discount = 30 if 0 <= now.weekday() <= 3 else 0
        bot.order_message = await bot.send_message(chat_id, "Начните заказывать", reply_markup=hinkali_markup())
    else:
        await bot.send_message(chat_id, "Заказ уже в процессе.\nЗакончите его командой /finish",
                               parse_mode='markdown', reply_markup=pointer_markup())


@dp.message_handler(ChatType.is_group_or_super_group, commands=['organizer'])
async def get_organizer(message: types.Message):
    chat_id, mess_id = message.chat.id, message.message_id
    if not bot.orders:
        with ignored(BadRequest):
            warning = await bot.send_message(chat_id, 'Никто ничего не заказывал!',
                                             reply_to_message_id=mess_id)
            await asyncio.sleep(5)
            await bot.delete_message(warning.chat.id, warning.message_id)
        return
    user_id = random.choice(tuple(bot.orders))
    name = bot.users[user_id]
    await bot.send_message(chat_id, f"Святым рандомом заказывающим назначается:\n"
                                    f"[{name}](tg://user?id={user_id})", parse_mode='markdown')


@dp.message_handler(ChatType.is_group_or_super_group, commands=['finish'])
async def finalize(message: types.Message):
    if not bot.order_message:
        return
    chat_id, mess_id = bot.order_message.chat.id, bot.order_message.message_id
    with ignored(BadRequest):
        await bot.edit_message_reply_markup(chat_id, mess_id, reply_markup=None)
    if bot.orders:
        mentions=', '.join([f"[{bot.users[user_id]}](tg://user?id={user_id})" for user_id in bot.orders])
        await bot.send_message(chat_id, f"Заказ приехал!\n{mentions}", parse_mode='markdown')
    bot.order_message = None
    bot.users = {}
    bot.orders = {}
    

@dp.message_handler(ChatType.is_private, commands=['start'])
async def start(message: types.Message):
    await bot.send_message(message.chat.id, "I'm alive!")


async def on_startup_webhook(dp):
    print(WEBHOOK_URL)
    dp.skip_updates()
    await bot.set_webhook(WEBHOOK_URL)


async def on_shutdown(dp):
    await bot.delete_webhook()


if __name__ == '__main__':
    runner = Executor(dp)
    runner.on_startup(on_startup_webhook, webhook=True)
    runner.on_shutdown(on_shutdown, webhook=True)
    runner.start_webhook(webhook_path=WEBHOOK_PATH, port=PORT)
