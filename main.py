import asyncio
import datetime
import os
import random
import secrets
import redis
from contextlib import contextmanager

from aiogram import Bot, Dispatcher, types
from aiogram.types import ChatType
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.emoji import emojize
from aiogram.utils.exceptions import MessageNotModified, BadRequest
from aiogram.utils.executor import start_webhook

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
PORT = int(os.getenv("PORT", '8443'))
DOMAIN = os.getenv("DOMAIN", "example.com")
SECRET_KEY = secrets.token_urlsafe(48)
WEBHOOK_BASE_PATH = os.getenv("WEBHOOK_BASE_PATH", "/webhook")
WEBHOOK_PATH = f"{WEBHOOK_BASE_PATH}/{SECRET_KEY}"
WEBHOOK_URL = f"https://{DOMAIN}{WEBHOOK_PATH}"
REDIS_URL = os.getenv('REDIS_URL', '')
LAST_ORG = 'last_organizer_id'

bot = Bot(token=TELEGRAM_TOKEN)
bot.users = {}
bot.payments = set()
bot.orders = {}
bot.organizer = None
bot.order_message = None
bot.payments_message = None
bot.discount = 30
dp = Dispatcher(bot)
rds = redis.Redis.from_url(REDIS_URL)

hin_cb = CallbackData('hin', 'type', 'amount')
disc_cb = CallbackData('discount', 'multiplier')
pay_cb = CallbackData('payment', 'status')

h_types = {'ВК': 50, 'ВБ': 50, 'ЖК': 55, 'ЖБ': 55}
h_amounts = ('-4', '-2', '-1', '0', '+1', '+2', '+4')


@contextmanager
def ignored(exc):
    try:
        yield
    except exc:
        pass


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


def pointer_markup():
    markup = types.InlineKeyboardMarkup()
    link_chat_id = str(bot.order_message.chat.id).replace("-100", "")
    url = f'https://t.me/c/{link_chat_id}/{bot.order_message.message_id}'
    markup.add(types.InlineKeyboardButton(text='Ссылка на заказ', url=url))
    return markup


def payments_markup():
    markup = types.InlineKeyboardMarkup()
    link_chat_id = str(bot.order_message.chat.id).replace("-100", "")
    url = f'https://t.me/c/{link_chat_id}/{bot.order_message.message_id}'
    markup.row(
        types.InlineKeyboardButton(text=emojize(':dollar: Оплачено'),
                                   callback_data=pay_cb.new(status='paid')),
        types.InlineKeyboardButton(text=emojize(':money_with_wings: Отмена'),
                                   callback_data=pay_cb.new(status='canceled')),
    )
    markup.add(types.InlineKeyboardButton(text='Ссылка на заказ', url=url))
    return markup


def payment_report():
    lines = []
    if bot.organizer:
        lines.append(f"Орг: {bot.users[bot.organizer]}")
        payers = set(bot.orders) - {bot.organizer}
    else:
        payers = tuple(bot.orders)
    for user_id in payers:
        mark = emojize(':white_check_mark:' if user_id in bot.payments else ':x:')
        lines.append(f"{mark} {bot.users[user_id]}")
    report = '\n'.join(lines)
    return report


def order_report():
    lines = ['Заказ:']
    total_cost = 0

    if not bot.orders:
        order = 'Ещё ничего не заказано'
        return order
    for user_id, user_orders in bot.orders.items():
        name, orders = bot.users[user_id], bot.orders[user_id]
        user_cost = sum(map(lambda x: orders[x] * h_types[x] * (100 - bot.discount) / 100, orders))
        orders = ", ".join([f'{str(amount)} {h_type}' for h_type, amount in user_orders.items()])
        lines.append(f'{name} ({user_cost} ₽): {orders}')
        total_cost += user_cost
    total_orders = {h_type: sum(user_orders.get(h_type, 0)
                                for user_orders in bot.orders.values())
                    for h_type in h_types}
    total_orders_str = ", ".join([f'{str(amount)} {h_type}'
                                  for h_type, amount in total_orders.items()
                                  if amount])
    lines.append(f'\nИтого: {total_cost} ₽ - {total_orders_str}')
    order = '\n'.join(lines)
    return order


@dp.callback_query_handler(hin_cb.filter())
async def amount_change(call: types.CallbackQuery, callback_data):
    user_id, name = call.from_user.id, call.from_user.full_name or call.from_user.username
    h_type, amount = callback_data['type'], callback_data['amount']
    bot.users.setdefault(user_id, name)
    cur_orders = bot.orders.setdefault(user_id, {})
    cur_amount = cur_orders.setdefault(h_type, 0) + int(amount)
    if cur_amount <= 0:
        del cur_orders[h_type]
        if not cur_orders:
            del bot.orders[user_id]
            del bot.users[user_id]
    else:
        cur_orders[h_type] = cur_amount
    try:

        order_message = await bot.edit_message_text(order_report(), call.message.chat.id,
                                                    call.message.message_id, reply_markup=hinkali_markup())
        if not bot.order_message:
            bot.order_message = order_message
        await bot.answer_callback_query(call.id, amount)
    except MessageNotModified:
        await bot.answer_callback_query(call.id)


@dp.callback_query_handler(disc_cb.filter())
async def discount_change(call: types.CallbackQuery, callback_data):
    chat_id, mess_id = call.message.chat.id, call.message.message_id
    bot.discount = int(callback_data['multiplier'])
    if bot.orders:
        order_message = await bot.edit_message_text(order_report(), chat_id, mess_id, reply_markup=hinkali_markup())
        if not bot.order_message:
            bot.order_message = order_message
    else:
        await bot.edit_message_reply_markup(chat_id, mess_id, reply_markup=hinkali_markup())


@dp.callback_query_handler(pay_cb.filter())
async def payment_change(call: types.CallbackQuery, callback_data):
    user_id, status = call.from_user.id, callback_data['status']
    chat_id, mess_id = call.message.chat.id, call.message.message_id
    if status == 'paid':
        bot.payments.add(user_id)
    elif status == 'canceled':
        with ignored(KeyError):
            bot.payments.remove(user_id)
    try:
        payments_message = await bot.edit_message_text(payment_report(), chat_id, mess_id,
                                                       reply_markup=payments_markup())
        if not bot.payments_message:
            bot.payments_message = payments_message
        await bot.answer_callback_query(call.id, "Учтено")
    except MessageNotModified:
        await bot.answer_callback_query(call.id)


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
    last_org = rds.get(LAST_ORG)
    random_victims = set(bot.orders)
    if last_org and random_victims != {int(last_org)}:
        random_victims -= {int(last_org)}
    bot.organizer = random.choice(tuple(random_victims))
    name = bot.users[bot.organizer]
    msg = (
        f"Святым рандомом заказывающим назначается:\n" 
        f"[{name}](tg://user?id={bot.organizer})\n"
        + f"[Прошлый организатор](tg://user?id={int(last_org)})" if last_org else ""
    )
    await bot.send_message(chat_id, msg, parse_mode='markdown')


@dp.message_handler(ChatType.is_group_or_super_group, commands=['pay'])
async def make_payments(message: types.Message):
    chat_id, mess_id = message.chat.id, message.message_id
    if not bot.payments_message:
        bot.payments_message = await bot.send_message(chat_id, payment_report(), reply_markup=payments_markup())


async def org_filter(message: types.Message):
    return not bot.organizer or message.from_user.id == bot.organizer

async def orderers_filter(message: types.Message):
    return message.from_user.id in bot.orders


@dp.message_handler(ChatType.is_group_or_super_group, commands=['notify'])
async def notify(message: types.Message):
    chat_id, mess_id = bot.order_message.chat.id, bot.order_message.message_id
    if bot.orders:
        mentions = ', '.join([f"[{bot.users[user_id]}](tg://user?id={user_id})" for user_id in bot.orders])
        await bot.send_message(chat_id, f"Заказ приехал!\n{mentions}", parse_mode='markdown')


@dp.message_handler(ChatType.is_group_or_super_group, org_filter, orderers_filter, commands=['finish'])
async def finalize(message: types.Message):
    if not bot.order_message:
        return
    chat_id, mess_id = bot.order_message.chat.id, bot.order_message.message_id
    f_user_id, f_user_name = message.from_user.id, message.from_user.full_name or message.from_user.username
    with ignored(BadRequest):
        await bot.edit_message_reply_markup(chat_id, mess_id, reply_markup=None)
    if bot.payments_message:
        chat_id, mess_id = bot.payments_message.chat.id, bot.payments_message.message_id
        with ignored(BadRequest):
            await bot.edit_message_reply_markup(chat_id, mess_id, reply_markup=None)
    if bot.orders:
        mentions = ', '.join([f"[{bot.users[user_id]}](tg://user?id={user_id})" for user_id in bot.orders])
        await bot.send_message(chat_id, f"[{f_user_name}](tg://user?id={f_user_id}) завершил заказ.\n"
                                        f"Заказ приехал!\n{mentions}", parse_mode='markdown')
    bot.order_message = None
    bot.payments_message = None
    bot.users = {}
    bot.orders = {}
    bot.payments = set()
    if bot.organizer:
        rds.set(LAST_ORG, str(bot.organizer))
        bot.organizer = None


@dp.message_handler(ChatType.is_private, commands=['start'])
async def start(message: types.Message):
    await bot.send_message(message.chat.id, "I'm alive!")


async def on_startup_webhook(dp):
    print(WEBHOOK_URL)
    await bot.delete_webhook()
    await bot.set_webhook(WEBHOOK_URL)


if __name__ == '__main__':
    start_webhook(dp, webhook_path=WEBHOOK_PATH, port=PORT, on_startup=on_startup_webhook, skip_updates=True)
