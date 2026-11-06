import argparse
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from core import ASK_PROMPTS, MAX_PAGE_SIZE, SOURCES, export_html, Store, apply_choice, apply_text, card, card_buttons, filter_reasons, menu, message_author, message_links, scan_days, scan_due

log = logging.getLogger('bar-rent')
PROGRESS_EVERY = 500
DELIVERY_BATCH = 5

def config():
    from dotenv import load_dotenv
    load_dotenv()
    os.umask(0o077)
    Path('data').mkdir(exist_ok=True)
    return Store(os.getenv('DATABASE','data/rent.sqlite3'))

def client():
    from telethon import TelegramClient
    if not os.getenv('TG_API_ID') or not os.getenv('TG_API_HASH'):
        raise SystemExit('Заполни TG_API_ID и TG_API_HASH в .env (my.telegram.org)')
    return TelegramClient('data/reader', int(os.environ['TG_API_ID']), os.environ['TG_API_HASH'])

async def collect(db, tg, days, rescan_days, deep_hours):
    from telethon.errors import FloodWaitError
    for number, source in enumerate(SOURCES, 1):
        try:
            count = read = 0
            started = datetime.now(timezone.utc)
            # Only a successful scan moves this mark, so a failing source catches up later.
            last_ok = db.get(f'scanned:{source}')
            window = scan_days(last_ok,days,rescan_days,started)
            cutoff = started-timedelta(days=window)
            # The window finds edits and is expensive; between windows only unseen IDs are read.
            deep = scan_due(last_ok,deep_hours,started)
            min_id = 0 if deep else db.last_id(source)
            # Routine rescans stay quiet; a long read must show that it is alive.
            if window > rescan_days:
                log.info('[%s/%s] %s: читаю сообщения за %s дн.', number, len(SOURCES), source, window)
            async for msg in tg.iter_messages(source,min_id=min_id):
                if msg.date < cutoff: break
                read += 1
                if msg.raw_text:
                    db.put(source, msg.id, msg.date.isoformat(), msg.raw_text, message_author(msg), message_links(msg))
                    count += 1
                if read % PROGRESS_EVERY == 0:
                    log.info('%s: прочитано %s, дошли до %s', source, read, msg.date.strftime('%Y-%m-%d'))
            if deep: db.set(f'scanned:{source}',started.isoformat())
            db.health(source)
            if deep or count:
                log.info('[%s/%s] %s: готово, %s текстовых сообщений за %s с', number, len(SOURCES), source, count, int((datetime.now(timezone.utc)-started).total_seconds()))
        except FloodWaitError as e:
            db.health(source, f'FloodWait {e.seconds}s')
            log.warning('%s: Telegram просит паузу %ss', source,e.seconds)
            await asyncio.sleep(e.seconds+1)
        except Exception as e:
            # Never log exception strings that could contain tokens or session details.
            db.health(source, type(e).__name__)
            log.warning('%s: %s', source,type(e).__name__)

class BotApiError(RuntimeError):
    """Holds only the status code, so the token in the request URL cannot reach the logs."""
    def __init__(self, code):
        super().__init__(f'Telegram Bot API: {code}')
        self.code = code

async def bot_call(http, token, method, **payload):
    async with http.post(f'https://api.telegram.org/bot{token}/{method}',json=payload) as response:
        data = await response.json()
        if not data.get('ok'): raise BotApiError(data.get('error_code'))
        return data['result']

HELP = ('/filters — мои фильтры\n/find — подходящие объявления\n/more — следующая страница\n/file — все подходящие одним HTML-файлом\n/favorites — избранное\n/status — источники\n'
        '/pause — отключить уведомления\n/resume — включить\nВсе условия и доступность требуют проверки у автора.')
OWNER_HELP = '\n\nДля владельца:\n/users — статистика пользователей\n/deny ID — заблокировать\n/allow ID — разблокировать\n/reports — посты, отмеченные для проверки парсера'
NEXT_IN_SETUP = {'p':'bedrooms'}
SCREEN_OF_CHOICE = {'c':'cities','p':'price','b':'bedrooms','d':'districts','t':'main','i':'main','h':'main','w':'words','s':'main'}

async def bot_loop(db, days):
    import aiohttp
    token = os.getenv('BOT_TOKEN')
    owner = os.getenv('OWNER_ID')
    if not token or not owner: raise SystemExit('Для run нужны BOT_TOKEN и OWNER_ID')
    owner = int(owner)
    db.adopt_owner(owner)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as http:
        try: await bot_call(http,token,'setMyCommands',commands=[dict(command=c,description=d) for c,d in (('filters','Мои фильтры'),('find','Подходящие объявления'),('more','Следующая страница'),('file','Все подходящие файлом'),('favorites','Избранное'),('status','Источники'),('pause','Отключить уведомления'),('resume','Включить уведомления'))])
        except Exception as e: log.warning('Bot: меню команд не обновлено: %s',type(e).__name__)
        async def send(chat, text, buttons=None):
            payload = dict(chat_id=chat,text=text,link_preview_options={'is_disabled':True})
            if buttons: payload['reply_markup']={'inline_keyboard':buttons}
            await bot_call(http,token,'sendMessage',**payload)
        async def show(chat, row):
            await send(chat,card(row),card_buttons(row['id'],row['hidden'],bool(row['favorite'])))
        def render(uid, screen):
            user = db.user(uid)
            return menu(screen,db.filters(uid),len(db.candidates(days,user=uid)),not user['started'])
        async def open_screen(uid, screen):
            text, rows = render(uid,screen)
            await send(uid,text,rows)
        async def edit_screen(uid, message_id, screen):
            text, rows = render(uid,screen)
            try: await bot_call(http,token,'editMessageText',chat_id=uid,message_id=message_id,text=text,reply_markup={'inline_keyboard':rows})
            except BotApiError as e:
                # Telegram answers 400 when the screen did not change, for example on a repeated tap.
                if e.code!=400: raise
        async def finish_setup(uid):
            # Notifications start only here, so a half-configured search sends nothing.
            db.set_user(uid,started=1)
            await send(uid,'Готово. Новые подходящие объявления будут приходить сюда.\n\n'+HELP)
            await open_screen(uid,'main')
        async def find(uid, page):
            rows = db.candidates(days,user=uid)
            size = db.filters(uid)['page_size']
            for row in db.annotate(rows[page*size:(page+1)*size],pool=rows): await show(uid,row)
            db.set_user(uid,page=page)
            await send(uid,f'Кандидатов: {len(rows)}. Страница {page+1}. /more — дальше.' if rows else 'Пока нет кандидатов. Проверь /filters и /status.')
        async def typed_value(uid, kind, text):
            try: filters = apply_text(db.filters(uid),kind,text)
            except ValueError as e: await send(uid,str(e)); return
            db.set_filters(uid,filters)
            db.set_user(uid,awaiting='')
            setup = not db.user(uid)['started']
            await open_screen(uid,'bedrooms' if setup else 'main' if kind in ('price','page') else 'words')
        async def command(uid, text):
            parts = text.split(None,1)
            cmd = parts[0].split('@')[0].lower() if parts else ''
            arg = parts[1].strip() if len(parts)>1 else ''
            awaiting = db.user(uid)['awaiting']
            if awaiting and text and not text.startswith('/'):
                await typed_value(uid,awaiting,text); return
            if awaiting: db.set_user(uid,awaiting='')
            if cmd=='/start':
                await send(uid,'Поиск долгосрочной аренды квартир в Черногории по Telegram-группам.'+('\n\n'+HELP+(OWNER_HELP if uid==owner else '') if db.user(uid)['started'] else ''))
                await open_screen(uid,'main' if db.user(uid)['started'] else 'cities')
            elif cmd in ('/find','/more'):
                await find(uid,0 if cmd=='/find' else db.user(uid)['page']+1)
            elif cmd=='/favorites':
                rows = db.favorites(uid)
                if not rows: await send(uid,'В избранном пусто. Нажми «☆ В избранное» на карточке.'); return
                await send(uid,f'В избранном: {len(rows)}'+(f'. Показаны первые {MAX_PAGE_SIZE}, остальные есть в /file.' if len(rows)>MAX_PAGE_SIZE else ''))
                for row in db.annotate(rows[:MAX_PAGE_SIZE],pool=rows): await show(uid,row)
            elif cmd=='/file':
                rows = db.export_rows(uid,days)
                if not rows: await send(uid,'Пока нет кандидатов. Проверь /filters и /status.'); return
                form = aiohttp.FormData()
                form.add_field('chat_id',str(uid))
                form.add_field('caption',f'Подходящих объявлений: {len(rows)}')
                form.add_field('document',export_html(rows,db.filters(uid)).encode(),filename=f'rent-{datetime.now(timezone.utc):%Y%m%d}.html',content_type='text/html')
                async with http.post(f'https://api.telegram.org/bot{token}/sendDocument',data=form) as r:
                    if r.status!=200: log.warning('Bot: sendDocument не удался: %s',r.status)
            elif cmd=='/filters':
                await open_screen(uid,'main')
            elif cmd=='/status':
                rows=db.db.execute('SELECT * FROM health').fetchall()
                await send(uid,'\n'.join(f"{r['source']}: {r['error'] or 'OK'} · {r['checked'][:16]} UTC" for r in rows) or 'Сбор ещё не завершён')
            elif cmd in ('/pause','/resume'):
                db.set_user(uid,paused=int(cmd=='/pause'))
                await send(uid,'Уведомления отключены' if cmd=='/pause' else 'Уведомления включены')
            elif uid==owner and cmd in ('/allow','/deny'):
                if not arg.isdigit(): await send(uid,f'Пример: {cmd} 123456789'); return
                if int(arg)==owner: await send(uid,'Это владелец бота'); return
                db.set_blocked(int(arg),cmd=='/deny')
                await send(uid,f"{'Заблокирован' if cmd=='/deny' else 'Разблокирован'}: {arg}")
            elif uid==owner and cmd=='/reports':
                rows = db.reports(uid)
                if not rows: await send(uid,'Отмеченных постов нет'); return
                await send(uid,f'На проверке парсера: {len(rows)}')
                for row in rows: await show(uid,row)
            elif uid==owner and cmd=='/users':
                s = db.user_stats()
                cities = ', '.join(f'{city}: {count}' for city,count in s['cities'])
                await send(uid,f"Пользователей: {s['total']}\nЗавершили настройку: {s['started']}\nС уведомлениями: {s['notified']}\nНовых за 7 дней: {s['new_week']}\nЗаблокировано: {s['blocked']}\n\nВыбранные города: {cities}")
            else:
                await send(uid,HELP)
        async def on_button(uid, message_id, data):
            if data.startswith('menu:'):
                await edit_screen(uid,message_id,data[5:])
            elif data=='show':
                await find(uid,0)
            elif data.startswith('ask:') and data[4:] in ASK_PROMPTS:
                db.set_user(uid,awaiting=data[4:])
                await send(uid,ASK_PROMPTS[data[4:]])
            elif data.startswith('set:'):
                old = db.filters(uid)
                filters = apply_choice(old,data)
                if filters!=old: db.set_filters(uid,filters)
                kind = data.split(':')[1]
                setup = not db.user(uid)['started']
                if setup and kind=='b': await finish_setup(uid)
                elif setup and kind in NEXT_IN_SETUP: await edit_screen(uid,message_id,NEXT_IN_SETUP[kind])
                elif kind in SCREEN_OF_CHOICE: await edit_screen(uid,message_id,SCREEN_OF_CHOICE[kind])
        async def handle(update):
            message = update.get('message',{})
            callback = update.get('callback_query',{})
            uid = (callback.get('from') or message.get('from') or {}).get('id')
            known = db.user(uid)
            if known and known['blocked']: return
            if callback:
                if known and callback.get('message',{}).get('chat',{}).get('id')==uid:
                    data = callback.get('data','')
                    action, _, pid = data.partition(':')
                    if action in ('hide','unhide','fav','unfav') and pid.isdigit():
                        pid = int(pid)
                        if action in ('hide','unhide'): db.action(uid,pid,action)
                        elif action in ('fav','unfav'): db.set_favorite(uid,pid,action=='fav')
                        notice = {'hide':'Скрыто','unhide':'Возвращено','fav':'Добавлено в избранное','unfav':'Убрано из избранного'}[action]
                        await bot_call(http,token,'answerCallbackQuery',callback_query_id=callback['id'],text=notice)
                        # Swapping the buttons at once also gives an undo for a mistaken tap.
                        await bot_call(http,token,'editMessageReplyMarkup',chat_id=uid,message_id=callback['message']['message_id'],reply_markup={'inline_keyboard':card_buttons(pid,db.is_hidden(uid,pid),db.is_favorite(uid,pid))})
                    else:
                        await bot_call(http,token,'answerCallbackQuery',callback_query_id=callback['id'])
                        await on_button(uid,callback['message']['message_id'],data)
            elif message.get('chat',{}).get('type')=='private':
                # Anyone may use the bot: the first message registers them.
                if not known: db.add_user(uid)
                # The owner's reply to a card is a note for parser review; the card starts with "#<id> ·".
                quoted = (message.get('reply_to_message') or {}).get('text','')
                match = re.match(r'#(\d+) ·',quoted)
                if uid==owner and match and message.get('text'):
                    pid = int(match.group(1))
                    if db.comment(pid,message['text']):
                        log.info('Замечание к посту %d: %s',pid,' ⏎ '.join(message['text'].splitlines()))
                        await send(uid,f'Записал замечание к посту #{pid}')
                    else: await send(uid,f'Поста #{pid} нет в базе')
                    return
                await command(uid,message.get('text',''))
        async def deliver(user):
            # Historical load is searchable but does not flood anyone with old posts.
            boundary = max(db.get('notify_after'),user['notify_after'])
            if not db.get('notify_after'): return
            sent = 0
            for row in db.pending(user['id'],days,boundary):
                if not db.duplicate_notified(user['id'],row):
                    # A capped batch per pass keeps one user's backlog from delaying everyone else.
                    if sent==DELIVERY_BATCH: return
                    await show(user['id'],row)
                    sent += 1
                    await asyncio.sleep(0.05)
                db.mark_notified(user['id'],row['id'],row['digest'])
        while True:
            try:
                updates = await bot_call(http,token,'getUpdates',offset=int(db.get('offset','0')),timeout=20,allowed_updates=['message','callback_query'])
                for update in updates:
                    # Advance first: with several users, one failing update must not block the others.
                    db.set('offset',update['update_id']+1)
                    try: await handle(update)
                    except Exception as e: log.warning('Bot: обновление не обработано: %s',type(e).__name__)
                for user in db.users():
                    if not user['started'] or user['paused'] or user['blocked']: continue
                    try: await deliver(user)
                    except BotApiError as e:
                        log.warning('Bot: доставка пользователю %s: код %s',user['id'],e.code)
                        # 403 means the user blocked the bot; /resume turns delivery back on.
                        if e.code==403: db.set_user(user['id'],paused=1)
            except Exception as e:
                log.warning('Bot: %s; повтор через 10 секунд',type(e).__name__)
                await asyncio.sleep(10)

async def main(args):
    db=config()
    if args.command=='export':
        owner=int(os.getenv('OWNER_ID') or 0) or None
        filters=db.filters(owner)
        if args.reports:
            print(json.dumps([{**dict(r),'now':json.loads(r['parsed']),'then':json.loads(r['parsed_then'] or 'null')} for r in db.reports(owner)],ensure_ascii=False,indent=2)); return
        print(json.dumps([{**dict(r),'filter_reasons':filter_reasons(json.loads(r['parsed']),filters,r['text'])} for r in db.candidates(args.days,args.all,owner)],ensure_ascii=False,indent=2)); return
    if args.command=='reparse':
        print(f'Пересчитано записей: {db.reparse()}'); return
    tg=client()
    if args.command=='login':
        await tg.start()
        me = await tg.get_me()
        print(f'Твой OWNER_ID={me.id}. Добавь его в .env. Авторизация сохранена в data/reader.session')
        await tg.disconnect(); return
    await tg.connect()
    if not await tg.is_user_authorized():
        await tg.disconnect()
        raise SystemExit('Сначала выполни: python app.py login')
    try:
        if args.command=='scan':
            await collect(db,tg,args.days,args.rescan_days,args.deep_hours)
            return
        async def scanner():
            while True:
                await collect(db,tg,args.days,args.rescan_days,args.deep_hours)
                # The bot sends nothing until notify_after is set, so the first load stays silent.
                if not db.get('notify_after'): db.set('notify_after',datetime.now(timezone.utc).isoformat())
                await asyncio.sleep(max(60,args.interval))
        log.info('Бот запущен: отправь ему /start. Сбор идёт в фоне')
        tasks=[asyncio.create_task(scanner()),asyncio.create_task(bot_loop(db,args.days))]
        try: await asyncio.gather(*tasks)
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
    finally: await tg.disconnect()

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(name)s %(message)s')
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['login','scan','run','export','reparse'])
    parser.add_argument('--days',type=int,default=14)
    parser.add_argument('--interval',type=int,default=600)
    parser.add_argument('--rescan-days',type=int,default=3)
    parser.add_argument('--deep-hours',type=float,default=6,help='как часто перечитывать окно --rescan-days ради правок')
    parser.add_argument('--reports',action='store_true',help='export: только посты, отмеченные для проверки парсера, с результатом parse() на момент отметки')
    parser.add_argument('--all',action='store_true',help='export: включить отклонённые фильтром посты')
    asyncio.run(main(parser.parse_args()))
