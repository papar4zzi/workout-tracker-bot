import logging
import os
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, \
    CallbackQueryHandler
import sqlite3
from datetime import datetime

# Включаем логирование
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Состояния для ConversationHandler
CHOOSING_TYPE, TRAINING, CUSTOM_NAME, CUSTOM_DESCRIPTION = range(4)
WORKOUT_EDIT_CHOICE, WORKOUT_EDIT_TEXT, EDIT_SCOPE_CHOICE = range(4, 7)
EDIT_TYPE_CHOICE, EDIT_TYPE_NAME, EDIT_TYPE_DESCRIPTION = range(7, 10)

# Базовые типы тренировок
DEFAULT_WORKOUT_TYPES = ['Ноги', 'Грудь', 'Спина', 'Руки', 'Плечи', 'Кардио', 'Все тело']


# Главное меню
def get_main_menu():
    keyboard = [
        [KeyboardButton('🏋️ Начать тренировку'), KeyboardButton('📊 Статистика')],
        [KeyboardButton('📋 История'), KeyboardButton('🏆 Рейтинг')],
        [KeyboardButton('⚙️ Типы тренировок')]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# Меню типов тренировок
def get_types_menu():
    keyboard = [
        [KeyboardButton('➕ Добавить тип'), KeyboardButton('✏️ Редактировать тип')],
        [KeyboardButton('🗑 Удалить/Скрыть тип'), KeyboardButton('👁 Показать скрытые')],
        [KeyboardButton('📝 Мои типы'), KeyboardButton('🔙 Главное меню')]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# Инициализация базы данных
# Инициализация базы данных
def init_db():
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()

    # Таблица тренировок
    c.execute('''CREATE TABLE IF NOT EXISTS workouts
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  workout_type TEXT,
                  start_time TEXT,
                  end_time TEXT,
                  duration INTEGER,
                  description TEXT)''')

    # Таблица кастомных типов тренировок
    c.execute('''CREATE TABLE IF NOT EXISTS custom_workout_types
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  name TEXT,
                  description TEXT,
                  created_at TEXT)''')

    # Таблица скрытых базовых типов
    c.execute('''CREATE TABLE IF NOT EXISTS hidden_default_types
                 (user_id INTEGER,
                  type_name TEXT,
                  PRIMARY KEY (user_id, type_name))''')

    # Таблица активных тренировок
    c.execute('''CREATE TABLE IF NOT EXISTS active_workouts
                 (user_id INTEGER PRIMARY KEY,
                  workout_type TEXT,
                  start_time TEXT)''')

    # Таблица пользователей (НОВАЯ)
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  first_name TEXT,
                  last_name TEXT,
                  last_seen TEXT)''')

    conn.commit()
    conn.close()


def migrate_db():
    """Добавляет недостающие колонки в существующие таблицы"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()

    # Проверяем и добавляем колонку description в workouts
    c.execute("PRAGMA table_info(workouts)")
    columns = [column[1] for column in c.fetchall()]

    if 'description' not in columns:
        try:
            c.execute('ALTER TABLE workouts ADD COLUMN description TEXT')
            print('✅ Колонка description добавлена в таблицу workouts')
        except sqlite3.OperationalError:
            pass

    # Создаём таблицу users если её нет
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY,
                  username TEXT,
                  first_name TEXT,
                  last_name TEXT,
                  last_seen TEXT)''')

    conn.commit()
    conn.close()


# Сохранение/обновление информации о пользователе
def update_user_info(update: Update):
    user = update.effective_user

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, last_seen)
                 VALUES (?, ?, ?, ?, ?)''',
              (user.id, user.username or '', user.first_name or '', user.last_name or '', datetime.now().isoformat()))
    conn.commit()
    conn.close()


# Обновление информации о существующих пользователях (миграция)
def backfill_users():
    """Создаёт записи пользователей на основе существующих тренировок"""
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()

    # Получаем уникальные user_id из тренировок
    c.execute('SELECT DISTINCT user_id FROM workouts')
    user_ids = [row[0] for row in c.fetchall()]

    # Для каждого user_id создаём запись если её нет
    for user_id in user_ids:
        c.execute('SELECT user_id FROM users WHERE user_id = ?', (user_id,))
        if not c.fetchone():
            # Создаём запись с placeholder данными
            c.execute('''INSERT INTO users (user_id, username, first_name, last_name, last_seen)
                         VALUES (?, ?, ?, ?, ?)''',
                      (user_id, '', f'Пользователь', '', datetime.now().isoformat()))

    conn.commit()
    conn.close()
    print('✅ Информация о пользователях обновлена')

# Рейтинг пользователей
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    update_user_info(update)  # Обновляем инфо о пользователе

    keyboard = [
        [InlineKeyboardButton('🔥 По количеству тренировок', callback_data='lb_count')],
        [InlineKeyboardButton('⏱ По общему времени', callback_data='lb_time')],
        [InlineKeyboardButton('📅 За последний месяц', callback_data='lb_month')],
        [InlineKeyboardButton('❌ Закрыть', callback_data='lb_close')]
    ]

    await update.message.reply_text(
        '🏆 Рейтинг тренирующихся\n\n'
        'Выбери категорию:',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# Получение имени пользователя для отображения
def get_user_display_name(user_id, username, first_name, last_name):
    # Приоритет отображения: Имя Фамилия > Имя > @username > ID
    if first_name:
        full_name = f"{first_name}"
        if last_name:
            full_name += f" {last_name}"
        return full_name
    elif username:
        return f"@{username}"
    else:
        return f"ID: {user_id}"


# Рейтинг по количеству тренировок
async def leaderboard_by_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()

    c.execute('''
        SELECT w.user_id, u.username, u.first_name, u.last_name, COUNT(*) as workout_count, SUM(w.duration) as total_duration
        FROM workouts w
        LEFT JOIN users u ON w.user_id = u.user_id
        GROUP BY w.user_id
        ORDER BY workout_count DESC
        LIMIT 10
    ''')

    top_users = c.fetchall()

    # Получаем позицию текущего пользователя
    c.execute('''
        SELECT COUNT(*) + 1
        FROM (
            SELECT user_id, COUNT(*) as workout_count
            FROM workouts
            GROUP BY user_id
            HAVING workout_count > (
                SELECT COUNT(*) FROM workouts WHERE user_id = ?
            )
        )
    ''', (user_id,))

    user_position = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM workouts WHERE user_id = ?', (user_id,))
    user_count = c.fetchone()[0]

    conn.close()

    message = '🏆 Топ по количеству тренировок\n\n'

    medals = ['🥇', '🥈', '🥉']

    for idx, (uid, username, first_name, last_name, count, duration) in enumerate(top_users):
        medal = medals[idx] if idx < 3 else f'{idx + 1}.'
        name = get_user_display_name(uid, username, first_name, last_name)

        hours = duration // 60
        mins = duration % 60

        highlight = '👈 ЭТО ТЫ!' if uid == user_id else ''

        message += f'{medal} {name}\n'
        message += f'   💪 {count} тренировок | ⏱ {hours}ч {mins}м {highlight}\n\n'

    if user_position > 10:
        message += f'─────────────────\n'
        message += f'📍 Твоё место: {user_position}\n'
        message += f'💪 Тренировок: {user_count}\n'

    keyboard = [
        [InlineKeyboardButton('⏱ По времени', callback_data='lb_time'),
         InlineKeyboardButton('📅 За месяц', callback_data='lb_month')],
        [InlineKeyboardButton('« Назад', callback_data='lb_back')]
    ]

    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))


# Рейтинг по общему времени
async def leaderboard_by_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()

    c.execute('''
        SELECT w.user_id, u.username, u.first_name, u.last_name, SUM(w.duration) as total_duration, COUNT(*) as workout_count
        FROM workouts w
        LEFT JOIN users u ON w.user_id = u.user_id
        GROUP BY w.user_id
        ORDER BY total_duration DESC
        LIMIT 10
    ''')

    top_users = c.fetchall()

    # Получаем статистику текущего пользователя
    c.execute('''
        SELECT COUNT(*) + 1
        FROM (
            SELECT user_id, SUM(duration) as total_duration
            FROM workouts
            GROUP BY user_id
            HAVING total_duration > (
                SELECT SUM(duration) FROM workouts WHERE user_id = ?
            )
        )
    ''', (user_id,))

    user_position = c.fetchone()[0]

    c.execute('SELECT SUM(duration), COUNT(*) FROM workouts WHERE user_id = ?', (user_id,))
    user_duration, user_count = c.fetchone()

    conn.close()

    message = '🏆 Топ по общему времени тренировок\n\n'

    medals = ['🥇', '🥈', '🥉']

    for idx, (uid, username, first_name, last_name, duration, count) in enumerate(top_users):
        medal = medals[idx] if idx < 3 else f'{idx + 1}.'
        name = get_user_display_name(uid, username, first_name, last_name)

        hours = duration // 60
        mins = duration % 60

        highlight = '👈 ЭТО ТЫ!' if uid == user_id else ''

        message += f'{medal} {name}\n'
        message += f'   ⏱ {hours}ч {mins}м | 💪 {count} тренировок {highlight}\n\n'

    if user_position > 10:
        user_hours = (user_duration or 0) // 60
        user_mins = (user_duration or 0) % 60
        message += f'─────────────────\n'
        message += f'📍 Твоё место: {user_position}\n'
        message += f'⏱ Время: {user_hours}ч {user_mins}м\n'

    keyboard = [
        [InlineKeyboardButton('🔥 По количеству', callback_data='lb_count'),
         InlineKeyboardButton('📅 За месяц', callback_data='lb_month')],
        [InlineKeyboardButton('« Назад', callback_data='lb_back')]
    ]

    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))


# Рейтинг за последний месяц
async def leaderboard_by_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Дата месяц назад
    from datetime import timedelta
    month_ago = (datetime.now() - timedelta(days=30)).isoformat()

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()

    c.execute('''
        SELECT w.user_id, u.username, u.first_name, u.last_name, COUNT(*) as workout_count, SUM(w.duration) as total_duration
        FROM workouts w
        LEFT JOIN users u ON w.user_id = u.user_id
        WHERE w.start_time >= ?
        GROUP BY w.user_id
        ORDER BY workout_count DESC
        LIMIT 10
    ''', (month_ago,))

    top_users = c.fetchall()

    # Получаем статистику текущего пользователя за месяц
    c.execute('SELECT COUNT(*), SUM(duration) FROM workouts WHERE user_id = ? AND start_time >= ?',
              (user_id, month_ago))
    user_count, user_duration = c.fetchone()

    conn.close()

    message = '🏆 Топ за последний месяц\n\n'

    medals = ['🥇', '🥈', '🥉']

    user_in_top = False
    for idx, (uid, username, first_name, last_name, count, duration) in enumerate(top_users):
        medal = medals[idx] if idx < 3 else f'{idx + 1}.'
        name = get_user_display_name(uid, username, first_name, last_name)

        hours = duration // 60
        mins = duration % 60

        highlight = '👈 ЭТО ТЫ!' if uid == user_id else ''
        if uid == user_id:
            user_in_top = True

        message += f'{medal} {name}\n'
        message += f'   💪 {count} тренировок | ⏱ {hours}ч {mins}м {highlight}\n\n'

    if not user_in_top and user_count and user_count > 0:
        user_hours = (user_duration or 0) // 60
        user_mins = (user_duration or 0) % 60
        message += f'─────────────────\n'
        message += f'📍 Твоя статистика за месяц:\n'
        message += f'💪 Тренировок: {user_count}\n'
        message += f'⏱ Время: {user_hours}ч {user_mins}м\n'

    keyboard = [
        [InlineKeyboardButton('🔥 По количеству', callback_data='lb_count'),
         InlineKeyboardButton('⏱ По времени', callback_data='lb_time')],
        [InlineKeyboardButton('« Назад', callback_data='lb_back')]
    ]

    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))


# Возврат к выбору категории рейтинга
async def leaderboard_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton('🔥 По количеству тренировок', callback_data='lb_count')],
        [InlineKeyboardButton('⏱ По общему времени', callback_data='lb_time')],
        [InlineKeyboardButton('📅 За последний месяц', callback_data='lb_month')],
        [InlineKeyboardButton('❌ Закрыть', callback_data='lb_close')]
    ]

    await query.edit_message_text(
        '🏆 Рейтинг тренирующихся\n\n'
        'Выбери категорию:',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# Закрытие рейтинга
async def leaderboard_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text('✅ Закрыто')

# Получение всех типов тренировок для пользователя
def get_all_workout_types(user_id):
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()

    # Получаем скрытые базовые типы
    c.execute('SELECT type_name FROM hidden_default_types WHERE user_id = ?', (user_id,))
    hidden = [row[0] for row in c.fetchall()]

    # Фильтруем базовые типы
    visible_defaults = [t for t in DEFAULT_WORKOUT_TYPES if t not in hidden]

    # Получаем кастомные типы
    c.execute('SELECT name FROM custom_workout_types WHERE user_id = ?', (user_id,))
    custom_types = [row[0] for row in c.fetchall()]

    conn.close()

    return visible_defaults + custom_types


# Получение описания типа тренировки
def get_workout_description(user_id, workout_name):
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()

    c.execute('SELECT description FROM custom_workout_types WHERE user_id = ? AND name = ?',
              (user_id, workout_name))
    result = c.fetchone()

    conn.close()

    if result:
        return result[0]
    return None


# Проверка активной тренировки
def has_active_workout(user_id):
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('SELECT workout_type, start_time FROM active_workouts WHERE user_id = ?', (user_id,))
    result = c.fetchone()
    conn.close()
    return result


# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    update_user_info(update)  # Сохраняем инфо о пользователе

    await update.message.reply_text(
        '👋 Привет! Я твой тренировочный бот.\n\n'
        'Используй меню ниже для навигации:',
        reply_markup=get_main_menu()
    )



# Обработка кнопок главного меню
async def handle_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == '🏋️ Начать тренировку':
        return await begin_workout(update, context)
    elif text == '📊 Статистика':
        return await stats(update, context)
    elif text == '📋 История':
        return await history(update, context)
    elif text == '🏆 Рейтинг':  # ДОБАВИЛИ
        return await leaderboard(update, context)
    elif text == '⚙️ Типы тренировок':
        await update.message.reply_text(
            'Управление типами тренировок:',
            reply_markup=get_types_menu()
        )
    elif text == '🔙 Главное меню':
        await update.message.reply_text(
            'Главное меню:',
            reply_markup=get_main_menu()
        )


# Обработка меню типов
async def handle_types_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == '➕ Добавить тип':
        return await add_custom_type(update, context)
    elif text == '✏️ Редактировать тип':
        return await edit_type(update, context)
    elif text == '🗑 Удалить/Скрыть тип':
        return await remove_type_menu(update, context)
    elif text == '📝 Мои типы':
        return await my_types(update, context)
    elif text == '👁 Показать скрытые':
        return await show_hidden_types(update, context)


# Начало тренировки
async def begin_workout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    update_user_info(update)

    # Проверяем активную тренировку
    active = has_active_workout(user_id)
    if active:
        workout_type, start_time = active
        start_dt = datetime.fromisoformat(start_time)
        duration = int((datetime.now() - start_dt).total_seconds() / 60)

        keyboard = [
            [InlineKeyboardButton('⏹ Завершить текущую', callback_data='end_active_now')],
            [InlineKeyboardButton('❌ Отмена', callback_data='cancel_active')]
        ]

        await update.message.reply_text(
            f'⚠️ У тебя уже есть активная тренировка!\n\n'
            f'Тип: {workout_type}\n'
            f'Длится: {duration} мин\n\n'
            f'Сначала заверши её:',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END

    all_types = get_all_workout_types(user_id)

    if not all_types:
        await update.message.reply_text(
            '❌ У тебя нет доступных типов тренировок.\n'
            'Добавь свой тип через меню "Типы тренировок"',
            reply_markup=get_main_menu()
        )
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(workout, callback_data=f'startwork_{workout}')] for workout in all_types]
    keyboard.append([InlineKeyboardButton('❌ Отмена', callback_data='cancel_begin')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        'Выбери тип тренировки:',
        reply_markup=reply_markup
    )

    return CHOOSING_TYPE


# Обработка выбора типа тренировки
async def workout_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'cancel_begin':
        await query.edit_message_text('❌ Отменено')
        return ConversationHandler.END

    workout_type = query.data.replace('startwork_', '')
    user_id = update.effective_user.id

    # Сохраняем активную тренировку
    start_time = datetime.now().isoformat()

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO active_workouts (user_id, workout_type, start_time) VALUES (?, ?, ?)',
              (user_id, workout_type, start_time))
    conn.commit()
    conn.close()

    context.user_data['workout_type'] = workout_type
    context.user_data['start_time'] = start_time

    # Получаем описание
    description = get_workout_description(user_id, workout_type)

    message = f'✅ Тренировка "{workout_type}" началась!\n'
    message += f'🕐 Время начала: {datetime.now().strftime("%H:%M")}\n'

    if description:
        message += f'\n📋 План тренировки:\n{description}\n'

    # Кнопка завершения
    keyboard = [[InlineKeyboardButton('⏹ Завершить тренировку', callback_data='finish_current_workout')]]

    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

    return TRAINING


# Завершение активной тренировки из предупреждения
async def end_active_workout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Получаем данные активной тренировки
    active = has_active_workout(user_id)
    if not active:
        await query.edit_message_text('Нет активной тренировки.')
        return ConversationHandler.END

    workout_type, start_time = active
    context.user_data['workout_type'] = workout_type
    context.user_data['start_time'] = start_time

    return await finalize_workout(query, context, user_id)


# Обработка отмены при активной тренировке
async def cancel_active_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text('❌ Отменено')
    return ConversationHandler.END


# Завершение тренировки
async def end_workout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    return await finalize_workout(query, context, user_id)


# Финализация тренировки
async def finalize_workout(query, context, user_id):
    if 'start_time' not in context.user_data:
        await query.edit_message_text('Ошибка: данные тренировки не найдены.')
        return ConversationHandler.END

    # Вычисляем длительность
    start_time = datetime.fromisoformat(context.user_data['start_time'])
    end_time = datetime.now()
    duration = int((end_time - start_time).total_seconds() / 60)

    # Получаем описание типа
    workout_type = context.user_data['workout_type']
    description = get_workout_description(user_id, workout_type)

    # Сохраняем в базу данных
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''INSERT INTO workouts (user_id, workout_type, start_time, end_time, duration, description)
                 VALUES (?, ?, ?, ?, ?, ?)''',
              (user_id,
               workout_type,
               context.user_data['start_time'],
               end_time.isoformat(),
               duration,
               description or ''))
    workout_id = c.lastrowid

    # Удаляем активную тренировку
    c.execute('DELETE FROM active_workouts WHERE user_id = ?', (user_id,))

    conn.commit()
    conn.close()

    context.user_data['last_workout_id'] = workout_id
    context.user_data['workout_duration'] = duration

    # Спрашиваем про редактирование
    keyboard = [
        [InlineKeyboardButton('✏️ Да, хочу внести изменения', callback_data='edit_workout_yes')],
        [InlineKeyboardButton('✅ Нет, все отлично', callback_data='edit_workout_no')]
    ]

    await query.edit_message_text(
        f'🎉 Тренировка завершена!\n\n'
        f'Тип: {workout_type}\n'
        f'Длительность: {duration} минут\n\n'
        f'Хочешь внести изменения или дополнения?',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return WORKOUT_EDIT_CHOICE


# Выбор редактирования после тренировки
async def workout_edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'edit_workout_no':
        await query.edit_message_text(
            '✅ Отлично! Тренировка сохранена.\n\n'
            'Продолжай в том же духе! 💪'
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Показываем текущее описание
    workout_id = context.user_data['last_workout_id']

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('SELECT description FROM workouts WHERE id = ?', (workout_id,))
    current_desc = c.fetchone()[0]
    conn.close()

    message = '📝 Опиши, что ты делал на тренировке или какие изменения внёс:\n\n'

    if current_desc:
        message += f'Текущий план:\n{current_desc}\n\n'

    message += 'Например:\n'
    message += '• Приседания 4x12 (80кг)\n'
    message += '• Жим ногами 4x10 (120кг)\n'
    message += '• Увеличил вес на 5кг\n\n'
    message += 'Отправь описание или /cancel для отмены:'

    await query.edit_message_text(message)

    return WORKOUT_EDIT_TEXT


# Сохранение изменений тренировки
async def save_workout_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_description = update.message.text.strip()
    workout_id = context.user_data['last_workout_id']

    # Сохраняем описание тренировки
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('UPDATE workouts SET description = ? WHERE id = ?', (new_description, workout_id))
    conn.commit()
    conn.close()

    context.user_data['new_description'] = new_description

    # Спрашиваем про обновление типа
    keyboard = [
        [InlineKeyboardButton('📋 Только эту тренировку', callback_data='scope_this')],
        [InlineKeyboardButton('🔄 Обновить тип тренировки', callback_data='scope_type')],
        [InlineKeyboardButton('❌ Отмена', callback_data='scope_cancel')]
    ]

    await update.message.reply_text(
        '✅ Изменения сохранены!\n\n'
        'Хочешь обновить описание типа тренировки для будущих тренировок?',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return EDIT_SCOPE_CHOICE

# Редактирование тренировки из истории
async def edit_workout_from_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    workout_id = int(query.data.replace('editw_', ''))

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('SELECT description FROM workouts WHERE id = ?', (workout_id,))
    result = c.fetchone()
    conn.close()

    if not result:
        await query.edit_message_text('❌ Тренировка не найдена.')
        return ConversationHandler.END

    current_desc = result[0]

    context.user_data['editing_workout_id'] = workout_id

    message = '📝 Введи новое описание тренировки:\n\n'

    if current_desc:
        message += f'Текущее описание:\n{current_desc}\n\n'

    message += 'Отправь новое описание или /cancel для отмены:'

    await query.edit_message_text(message)

    return WORKOUT_EDIT_TEXT


# Сохранение редактирования из истории
async def save_workout_edit_from_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_description = update.message.text.strip()
    workout_id = context.user_data['editing_workout_id']

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('UPDATE workouts SET description = ? WHERE id = ?', (new_description, workout_id))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        '✅ Описание тренировки обновлено!',
        reply_markup=get_main_menu()
    )

    context.user_data.clear()
    return ConversationHandler.END

# Выбор области применения изменений
async def edit_scope_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'scope_cancel':
        await query.edit_message_text('✅ Тренировка сохранена!')
        context.user_data.clear()
        return ConversationHandler.END

    if query.data == 'scope_this':
        await query.edit_message_text(
            '✅ Изменения применены только к этой тренировке!\n\n'
            'Продолжай тренироваться! 💪'
        )
        context.user_data.clear()
        return ConversationHandler.END

    # Обновляем тип тренировки
    workout_type = context.user_data['workout_type']
    new_description = context.user_data['new_description']
    user_id = update.effective_user.id

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('UPDATE custom_workout_types SET description = ? WHERE user_id = ? AND name = ?',
              (new_description, user_id, workout_type))
    rows_affected = c.rowcount
    conn.commit()
    conn.close()

    if rows_affected > 0:
        await query.edit_message_text(
            f'✅ Тип тренировки "{workout_type}" обновлён!\n\n'
            'Теперь это описание будет использоваться для всех новых тренировок этого типа. 💪'
        )
    else:
        await query.edit_message_text(
            '✅ Изменения сохранены для этой тренировки!\n\n'
            '(Это базовый тип, его описание нельзя изменить)'
        )

    context.user_data.clear()
    return ConversationHandler.END


# История тренировок с пагинацией
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Проверяем, это кнопка или команда
    if update.callback_query:
        query = update.callback_query
        await query.answer()

        # Получаем номер страницы из callback_data
        page = int(query.data.replace('history_page_', ''))
    else:
        page = 0

    offset = page * 20

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()

    # Получаем общее количество тренировок
    c.execute('SELECT COUNT(*) FROM workouts WHERE user_id = ?', (user_id,))
    total_count = c.fetchone()[0]

    # Получаем тренировки для текущей страницы
    c.execute('''SELECT id, workout_type, start_time, duration 
                 FROM workouts 
                 WHERE user_id = ? 
                 ORDER BY start_time DESC 
                 LIMIT 20 OFFSET ?''',
              (user_id, offset))

    workouts = c.fetchall()
    conn.close()

    if not workouts and page == 0:
        message_text = '📭 У тебя пока нет записанных тренировок.\n\nНачни первую тренировку!'
        reply_markup = get_main_menu()

        if update.callback_query:
            await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(message_text, reply_markup=reply_markup)
        return

    keyboard = []
    for workout_id, workout_type, start_time, duration in workouts:
        date = datetime.fromisoformat(start_time).strftime('%d.%m %H:%M')
        button_text = f"{date} - {workout_type} ({duration}м)"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f'vieww_{workout_id}')])

    # Добавляем кнопки навигации
    nav_buttons = []

    if page > 0:
        nav_buttons.append(InlineKeyboardButton('⬅️ Назад', callback_data=f'history_page_{page - 1}'))

    if offset + 20 < total_count:
        nav_buttons.append(InlineKeyboardButton('Вперёд ➡️', callback_data=f'history_page_{page + 1}'))

    if nav_buttons:
        keyboard.append(nav_buttons)

    reply_markup = InlineKeyboardMarkup(keyboard)

    # Формируем текст с информацией о странице
    start_num = offset + 1
    end_num = min(offset + 20, total_count)
    message_text = f'📋 Твои тренировки ({start_num}-{end_num} из {total_count}):'

    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup)


# Просмотр тренировки
async def view_workout_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    workout_id = int(query.data.replace('vieww_', ''))

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''SELECT workout_type, start_time, end_time, duration, description 
                 FROM workouts 
                 WHERE id = ?''', (workout_id,))

    workout = c.fetchone()
    conn.close()

    if not workout:
        await query.edit_message_text('❌ Тренировка не найдена.')
        return

    workout_type, start_time, end_time, duration, description = workout

    start_dt = datetime.fromisoformat(start_time)
    end_dt = datetime.fromisoformat(end_time)

    message = f'🏋️ Детали тренировки\n\n'
    message += f'📌 Тип: {workout_type}\n'
    message += f'📅 Дата: {start_dt.strftime("%d.%m.%Y")}\n'
    message += f'🕐 Время: {start_dt.strftime("%H:%M")} - {end_dt.strftime("%H:%M")}\n'
    message += f'⏱ Длительность: {duration} мин\n'

    if description:
        message += f'\n📝 Описание:\n{description}'

    keyboard = [
        [InlineKeyboardButton('✏️ Редактировать', callback_data=f'editw_{workout_id}')],
        [InlineKeyboardButton('🗑 Удалить', callback_data=f'delw_{workout_id}')],
        [InlineKeyboardButton('« Назад', callback_data='back_history')]
    ]

    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

# Редактирование тренировки из истории
async def edit_workout_from_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    workout_id = int(query.data.replace('editw_', ''))

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('SELECT description FROM workouts WHERE id = ?', (workout_id,))
    result = c.fetchone()
    conn.close()

    if not result:
        await query.edit_message_text('❌ Тренировка не найдена.')
        return ConversationHandler.END

    current_desc = result[0]

    context.user_data['editing_workout_id'] = workout_id

    message = '📝 Введи новое описание тренировки:\n\n'

    if current_desc:
        message += f'Текущее описание:\n{current_desc}\n\n'

    message += 'Отправь новое описание или /cancel для отмены:'

    await query.edit_message_text(message)

    return WORKOUT_EDIT_TEXT

# Отмена редактирования типа
async def cancel_edit_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text('❌ Отменено')
    context.user_data.clear()
    return ConversationHandler.END

# Удаление тренировки
async def delete_workout_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    workout_id = int(query.data.replace('delw_', ''))

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('DELETE FROM workouts WHERE id = ?', (workout_id,))
    conn.commit()
    conn.close()

    await query.edit_message_text('✅ Тренировка удалена!')


# Возврат к истории
async def back_to_history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''SELECT id, workout_type, start_time, duration 
                 FROM workouts 
                 WHERE user_id = ? 
                 ORDER BY start_time DESC 
                 LIMIT 20''',
              (user_id,))

    workouts = c.fetchall()
    conn.close()

    keyboard = []
    for workout_id, workout_type, start_time, duration in workouts:
        date = datetime.fromisoformat(start_time).strftime('%d.%m %H:%M')
        button_text = f"{date} - {workout_type} ({duration}м)"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f'vieww_{workout_id}')])

    await query.edit_message_text(
        '📋 Твои тренировки:',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# Добавление кастомного типа
async def add_custom_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '➕ Добавление нового типа тренировки\n\n'
        'Как назовём новый тип?\n'
        'Например: "День ног", "Жим лёжа", "HIIT кардио"\n\n'
        'Отправь /cancel для отмены',
        reply_markup=get_types_menu()
    )
    return CUSTOM_NAME


# Название кастомного типа
async def custom_type_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    user_id = update.effective_user.id

    if name in ['🔙 Главное меню', '➕ Добавить тип', '✏️ Редактировать тип', '🗑 Удалить/Скрыть тип', '📝 Мои типы',
                '👁 Показать скрытые']:
        await update.message.reply_text('❌ Это зарезервированное название, выбери другое.')
        return CUSTOM_NAME

    all_types = get_all_workout_types(user_id)

    if name in all_types:
        await update.message.reply_text('❌ Такой тип уже существует! Придумай другое название.')
        return CUSTOM_NAME

    context.user_data['custom_type_name'] = name

    await update.message.reply_text(
        f'✅ Отлично! Теперь опиши тренировку "{name}".\n\n'
        f'Например:\n'
        f'• Приседания 4x12\n'
        f'• Жим ногами 4x10\n'
        f'• Разгибания ног 3x15\n\n'
        f'Или отправь /skip если описание не нужно'
    )

    return CUSTOM_DESCRIPTION


# Описание кастомного типа
async def custom_type_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    description = update.message.text.strip()
    name = context.user_data['custom_type_name']
    user_id = update.effective_user.id

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''INSERT INTO custom_workout_types (user_id, name, description, created_at)
                 VALUES (?, ?, ?, ?)''',
              (user_id, name, description, datetime.now().isoformat()))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f'✅ Тип тренировки "{name}" создан!\n\n'
        f'Теперь ты можешь выбрать его при старте тренировки.',
        reply_markup=get_types_menu()
    )

    context.user_data.clear()
    return ConversationHandler.END


# Пропуск описания
async def skip_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = context.user_data['custom_type_name']
    user_id = update.effective_user.id

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('''INSERT INTO custom_workout_types (user_id, name, description, created_at)
                 VALUES (?, ?, ?, ?)''',
              (user_id, name, '', datetime.now().isoformat()))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f'✅ Тип "{name}" добавлен без описания!',
        reply_markup=get_types_menu()
    )

    context.user_data.clear()
    return ConversationHandler.END


# Мои типы
async def my_types(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    all_types = get_all_workout_types(user_id)

    if not all_types:
        await update.message.reply_text(
            '❌ У тебя нет доступных типов тренировок.',
            reply_markup=get_types_menu()
        )
        return

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()

    message = '📝 Все доступные типы тренировок:\n\n'

    for workout_type in all_types:
        if workout_type in DEFAULT_WORKOUT_TYPES:
            message += f'🔸 {workout_type} (базовый)\n'
        else:
            c.execute('SELECT description FROM custom_workout_types WHERE user_id = ? AND name = ?',
                      (user_id, workout_type))
            desc = c.fetchone()
            message += f'🔹 {workout_type}\n'
            if desc and desc[0]:
                message += f'   {desc[0][:50]}...\n' if len(desc[0]) > 50 else f'   {desc[0]}\n'
        message += '\n'

    conn.close()

    await update.message.reply_text(message, reply_markup=get_types_menu())


# Редактирование типа
async def edit_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('SELECT id, name FROM custom_workout_types WHERE user_id = ?', (user_id,))
    custom_types = c.fetchall()
    conn.close()

    if not custom_types:
        await update.message.reply_text(
            '❌ У тебя нет своих типов для редактирования.\n'
            'Базовые типы редактировать нельзя.',
            reply_markup=get_types_menu()
        )
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(name, callback_data=f'et_{type_id}')] for type_id, name in custom_types]
    keyboard.append([InlineKeyboardButton('❌ Отмена', callback_data='et_cancel')])

    await update.message.reply_text(
        '✏️ Выбери тип для редактирования:',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return EDIT_TYPE_CHOICE


# Выбор типа для редактирования
# Выбор типа для редактирования
async def edit_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    type_id = int(query.data.replace('et_', ''))

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('SELECT name, description FROM custom_workout_types WHERE id = ?', (type_id,))
    name, description = c.fetchone()
    conn.close()

    context.user_data['editing_type_id'] = type_id
    context.user_data['old_type_name'] = name

    keyboard = [
        [InlineKeyboardButton('✏️ Изменить название', callback_data='et_name')],
        [InlineKeyboardButton('📝 Изменить описание', callback_data='et_desc')],
        [InlineKeyboardButton('❌ Отмена', callback_data='et_cancel2')]
    ]

    message = f'Редактирование: {name}\n\n'
    if description:
        message += f'Текущее описание:\n{description}\n\n'
    message += 'Что хочешь изменить?'

    await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup(keyboard))

    return EDIT_TYPE_CHOICE


# Редактирование названия типа
async def edit_type_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        f'Текущее название: {context.user_data["old_type_name"]}\n\n'
        f'Введи новое название или /cancel:'
    )

    return EDIT_TYPE_NAME


async def edit_type_name_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text.strip()
    type_id = context.user_data['editing_type_id']
    user_id = update.effective_user.id
    old_name = context.user_data['old_type_name']

    all_types = get_all_workout_types(user_id)
    if new_name in all_types and new_name != old_name:
        await update.message.reply_text('❌ Такое название уже существует!')
        return EDIT_TYPE_NAME

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('UPDATE custom_workout_types SET name = ? WHERE id = ?', (new_name, type_id))
    c.execute('UPDATE workouts SET workout_type = ? WHERE workout_type = ? AND user_id = ?',
              (new_name, old_name, user_id))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f'✅ Название изменено на "{new_name}"',
        reply_markup=get_types_menu()
    )

    context.user_data.clear()
    return ConversationHandler.END


# Редактирование описания типа
async def edit_type_desc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    type_id = context.user_data['editing_type_id']

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('SELECT description FROM custom_workout_types WHERE id = ?', (type_id,))
    current = c.fetchone()[0]
    conn.close()

    message = 'Текущее описание:\n'
    message += f'{current if current else "Нет описания"}\n\n'
    message += 'Введи новое описание или /cancel:'

    await query.edit_message_text(message)

    return EDIT_TYPE_DESCRIPTION


async def edit_type_desc_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_desc = update.message.text.strip()
    type_id = context.user_data['editing_type_id']

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('UPDATE custom_workout_types SET description = ? WHERE id = ?', (new_desc, type_id))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        '✅ Описание обновлено!',
        reply_markup=get_types_menu()
    )

    context.user_data.clear()
    return ConversationHandler.END


# Меню удаления типов
async def remove_type_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    # Получаем видимые базовые типы
    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('SELECT type_name FROM hidden_default_types WHERE user_id = ?', (user_id,))
    hidden = [row[0] for row in c.fetchall()]
    visible_defaults = [t for t in DEFAULT_WORKOUT_TYPES if t not in hidden]

    # Получаем кастомные типы
    c.execute('SELECT name FROM custom_workout_types WHERE user_id = ?', (user_id,))
    custom_types = [row[0] for row in c.fetchall()]
    conn.close()

    if not visible_defaults and not custom_types:
        await update.message.reply_text(
            '❌ Нет типов для удаления.',
            reply_markup=get_types_menu()
        )
        return

    keyboard = []

    if visible_defaults:
        for t in visible_defaults:
            keyboard.append([InlineKeyboardButton(f'🔸 {t} (скрыть)', callback_data=f'hide_{t}')])

    if custom_types:
        for t in custom_types:
            keyboard.append([InlineKeyboardButton(f'🔹 {t} (удалить)', callback_data=f'deltype_{t}')])

    keyboard.append([InlineKeyboardButton('❌ Отмена', callback_data='deltype_cancel')])

    await update.message.reply_text(
        '🗑 Удаление/скрытие типов:\n\n'
        '• Базовые типы можно скрыть (потом вернуть)\n'
        '• Свои типы удаляются навсегда',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# Удаление/скрытие типа
async def handle_delete_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'deltype_cancel':
        await query.edit_message_text('❌ Отменено')
        return

    user_id = update.effective_user.id

    if query.data.startswith('hide_'):
        # Скрываем базовый тип
        type_name = query.data.replace('hide_', '')

        conn = sqlite3.connect('workouts.db')
        c = conn.cursor()
        c.execute('INSERT OR IGNORE INTO hidden_default_types (user_id, type_name) VALUES (?, ?)',
                  (user_id, type_name))
        conn.commit()
        conn.close()

        await query.edit_message_text(f'✅ Тип "{type_name}" скрыт!\n\nМожешь вернуть его через "Показать скрытые"')

    elif query.data.startswith('deltype_'):
        # Удаляем кастомный тип
        type_name = query.data.replace('deltype_', '')

        conn = sqlite3.connect('workouts.db')
        c = conn.cursor()
        c.execute('DELETE FROM custom_workout_types WHERE user_id = ? AND name = ?',
                  (user_id, type_name))
        conn.commit()
        conn.close()

        await query.edit_message_text(f'✅ Тип "{type_name}" удалён!')


# Показать скрытые типы
async def show_hidden_types(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('SELECT type_name FROM hidden_default_types WHERE user_id = ?', (user_id,))
    hidden = [row[0] for row in c.fetchall()]
    conn.close()

    if not hidden:
        await update.message.reply_text(
            '✅ У тебя нет скрытых типов!',
            reply_markup=get_types_menu()
        )
        return

    keyboard = []
    for type_name in hidden:
        keyboard.append([InlineKeyboardButton(f'👁 {type_name} (вернуть)', callback_data=f'unhide_{type_name}')])
    keyboard.append([InlineKeyboardButton('❌ Отмена', callback_data='unhide_cancel')])

    await update.message.reply_text(
        '👁 Скрытые типы тренировок:\n\n'
        'Выбери, чтобы вернуть в список:',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# Обработка показа скрытого типа
async def handle_unhide_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == 'unhide_cancel':
        await query.edit_message_text('❌ Отменено')
        return

    type_name = query.data.replace('unhide_', '')
    user_id = update.effective_user.id

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()
    c.execute('DELETE FROM hidden_default_types WHERE user_id = ? AND type_name = ?',
              (user_id, type_name))
    conn.commit()
    conn.close()

    await query.edit_message_text(f'✅ Тип "{type_name}" снова доступен!')


# Статистика
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    update_user_info(update)

    conn = sqlite3.connect('workouts.db')
    c = conn.cursor()

    c.execute('SELECT COUNT(*) FROM workouts WHERE user_id = ?', (user_id,))
    total = c.fetchone()[0]

    c.execute('SELECT SUM(duration) FROM workouts WHERE user_id = ?', (user_id,))
    total_duration = c.fetchone()[0] or 0

    c.execute('''SELECT workout_type, COUNT(*), SUM(duration) 
                 FROM workouts 
                 WHERE user_id = ? 
                 GROUP BY workout_type
                 ORDER BY COUNT(*) DESC''', (user_id,))
    by_type = c.fetchall()

    conn.close()

    if total == 0:
        await update.message.reply_text(
            '📊 У тебя пока нет статистики.\n\n'
            'Начни тренироваться!',
            reply_markup=get_main_menu()
        )
        return

    hours = total_duration // 60
    mins = total_duration % 60

    message = f'📊 Твоя статистика\n\n'
    message += f'💪 Всего тренировок: {total}\n'
    message += f'⏱ Общее время: {hours}ч {mins}м\n'
    message += f'⌀ Средняя длительность: {total_duration // total}м\n\n'
    message += '📋 По типам:\n'

    for workout_type, count, duration in by_type:
        message += f'• {workout_type}: {count} ({duration}м)\n'

    await update.message.reply_text(message, reply_markup=get_main_menu())


# Отмена
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        '❌ Действие отменено.',
        reply_markup=get_main_menu()
    )
    return ConversationHandler.END


def main():
    init_db()
    migrate_db()
    backfill_users()

    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '8387695652:AAEaZfderP_304dDzZo_KZ4hdTVAQuIj6Qo')

    application = Application.builder().token(TOKEN).build()

    # Command handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('cancel', cancel))

    # ВАЖНО: ВСЕ Callback handlers ДОЛЖНЫ быть ДО ConversationHandler
    application.add_handler(CallbackQueryHandler(end_active_workout_handler, pattern='^end_active_now$'))
    application.add_handler(CallbackQueryHandler(cancel_active_handler, pattern='^cancel_active$'))
    application.add_handler(CallbackQueryHandler(end_workout_handler, pattern='^finish_current_workout$'))
    application.add_handler(CallbackQueryHandler(workout_edit_choice, pattern='^edit_workout_'))
    application.add_handler(CallbackQueryHandler(edit_scope_choice, pattern='^scope_'))
    application.add_handler(CallbackQueryHandler(history, pattern='^history_page_\\d+$'))
    application.add_handler(CallbackQueryHandler(view_workout_details, pattern='^vieww_\\d+$'))
    application.add_handler(CallbackQueryHandler(delete_workout_confirm, pattern='^delw_\\d+$'))
    application.add_handler(CallbackQueryHandler(back_to_history_handler, pattern='^back_history$'))
    application.add_handler(CallbackQueryHandler(handle_delete_type, pattern='^(hide_|deltype_)'))
    application.add_handler(CallbackQueryHandler(handle_unhide_type, pattern='^unhide_'))
    # Обработчики рейтинга
    application.add_handler(CallbackQueryHandler(leaderboard_by_count, pattern='^lb_count$'))
    application.add_handler(CallbackQueryHandler(leaderboard_by_time, pattern='^lb_time$'))
    application.add_handler(CallbackQueryHandler(leaderboard_by_month, pattern='^lb_month$'))
    application.add_handler(CallbackQueryHandler(leaderboard_back, pattern='^lb_back$'))
    application.add_handler(CallbackQueryHandler(leaderboard_close, pattern='^lb_close$'))

    # Conversation handlers
    workout_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex('^🏋️ Начать тренировку$'), begin_workout)
        ],
        states={
            CHOOSING_TYPE: [
                CallbackQueryHandler(workout_type_chosen, pattern='^startwork_'),
                CallbackQueryHandler(workout_type_chosen, pattern='^cancel_begin$')
            ],
            TRAINING: [
                CallbackQueryHandler(end_workout_handler, pattern='^finish_current_workout$')
            ],
            WORKOUT_EDIT_CHOICE: [
                CallbackQueryHandler(workout_edit_choice, pattern='^edit_workout_')
            ],
            WORKOUT_EDIT_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_workout_edit)
            ],
            EDIT_SCOPE_CHOICE: [
                CallbackQueryHandler(edit_scope_choice, pattern='^scope_')
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=True
    )

    # ConversationHandler для редактирования из истории
    edit_history_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(edit_workout_from_history, pattern='^editw_\\d+$')
        ],
        states={
            WORKOUT_EDIT_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_workout_edit_from_history)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    add_type_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex('^➕ Добавить тип$'), add_custom_type)
        ],
        states={
            CUSTOM_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_type_name)],
            CUSTOM_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, custom_type_description),
                CommandHandler('skip', skip_description)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    edit_type_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex('^✏️ Редактировать тип$'), edit_type)
        ],
        states={
            EDIT_TYPE_CHOICE: [
                CallbackQueryHandler(edit_type_chosen, pattern='^et_\\d+$'),
                CallbackQueryHandler(edit_type_name_start, pattern='^et_name$'),
                CallbackQueryHandler(edit_type_desc_start, pattern='^et_desc$'),
                CallbackQueryHandler(cancel_edit_type, pattern='^et_cancel$'),
                CallbackQueryHandler(cancel_edit_type, pattern='^et_cancel2$')
            ],
            EDIT_TYPE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_type_name_save)],
            EDIT_TYPE_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_type_desc_save)]
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # Message handlers для меню
    application.add_handler(MessageHandler(filters.Regex('^📊 Статистика$'), stats))
    application.add_handler(MessageHandler(filters.Regex('^🏆 Рейтинг$'), leaderboard))
    application.add_handler(MessageHandler(filters.Regex('^📋 История$'), history))
    application.add_handler(MessageHandler(filters.Regex('^📝 Мои типы$'), my_types))
    application.add_handler(MessageHandler(filters.Regex('^🗑 Удалить/Скрыть тип$'), remove_type_menu))
    application.add_handler(MessageHandler(filters.Regex('^👁 Показать скрытые$'), show_hidden_types))
    application.add_handler(MessageHandler(filters.Regex('^(⚙️ Типы тренировок|🔙 Главное меню)$'), handle_main_menu))

    # Conversation handlers
    application.add_handler(workout_conv)
    application.add_handler(edit_history_conv)
    application.add_handler(add_type_conv)
    application.add_handler(edit_type_conv)

    # Fallback
    application.add_handler(MessageHandler(filters.TEXT, handle_main_menu))

    print('🚀 Бот запущен!')
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':

    main()

