"""Conservative, dependency-free rental classification and persistent storage."""
import difflib
import hashlib
import html
import json
import logging
import math
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger('bar-rent')

SOURCES = ['realestate_bar', 'aarenda', 'saleme_realty', 'onerealestatemontenegro', 'abi_mne_realty', 'Montenegro_sell_rent']

# Bump to recalculate stored posts on the next start when parse() output gains a field.
PARSE_VERSION = '19'

# The group is dedicated to Bar, so its posts often omit the city name.
BAR_SOURCES = {'realestate_bar'}

NUMBER = r'(\d{1,3}(?:[  .,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)'
# "е"/"e" stands for euro only straight after a number; "евро" must not match "евроремонт".
EURO_AFTER = r'€|евр(?:о|а|ов)?(?!\w)|eur[oa]?\b|[еe](?![\w-])'
# Group 1: "€600". Group 2: "600 €". Group 3: no currency; parse_prices() keeps it only if the wording says it is the rent.
PRICE = re.compile(r'€\s*' + NUMBER + r'|(?<![\w.,])' + NUMBER + r'\s*(?:' + EURO_AFTER + r')'
                   r'|(?:(?:(?:цена|стоимость)(?:\s+аренды)?|💶|💰)[\s:.-]*|(?<![\w.,]))' + NUMBER + r'(?!\d)(?!\s*(?:м\b|м2|м²|кв|мин|км|эт|лет|год|г\.|%))')
# "этаж 3 700 €" is floor 3 and 700 €, while "2 спальни 1 200 €" is 1200 €.
FEATURE_BEFORE_NUMBER = re.compile(r'(?<!\d )(?:этаж\w*|площад\w*|спальн\w*|комнат\w*|санузл\w*|ванн\w*|floor|sprat\w*|bedrooms?)[\s:.-]*$')
BEDROOM = r'(?:спальн(?!\w*\s+(?:мест|кроват|диван|гарнитур))|spavac|bedroom)'
NON_HOUSING = r'офис\w*|коммерч\w*(?:\s+\w+)?|торгов\w*\s+помещ\w*|помещени\w*|склад\w*|гараж\w*|парковк\w*|паркинг\w*|магазин\w*|lokal\w*|poslovn\w*|office|авто|автомобил\w*|машин\w*|скутер\w*|мотоцикл\w*|яхт\w*|лодк\w*|катер\w*'
HOUSING = r'квартир|апарт|спальн|студи|\bдом|вилл|жиль|комнат|\bstan|apartman|apartment|\bflat|house|soban|garsonjer'

# "Центральное отопление" is not the city center.
CENTER = r'центр(?!альн)|centar|center'
OLD_TOWN = r'стар\w+\s+город|stari\s+grad'
# These words exist in every city, so they say nothing about which one a post means.
GENERIC_DISTRICTS = {'Центр', 'Старый город'}
# Shopping or business centres are buildings, not the district.
NOT_A_DISTRICT = re.compile(r'(?:торгов\w*|бизнес|спортивн\w*|медицинск\w*|фитнес|культурн\w*|развлекательн\w*|оздоровительн\w*|спа|spa)[\s-]+(?:\w+[\s-]+)?центр\w*|\bтц\b')
# "До центра Бара 3 мин" gives a distance, not the flat's location.
DISTANCE_TO_CENTER = re.compile(r'\b(?:до|от|к|ко|do|od|blizu|близко\s+к)\s+(?:\w+\s+)?(?:центр\w*|centr\w*|center|стар\w+\s+город\w*|stari\s+grad\w*)')
# Chosen by how often each name occurs in the collected posts. A specific name also identifies its city.
DISTRICTS = {
    'Бар': [('Центр', CENTER), ('Шушань', r'шушан|сушан|susanj'), ('Ильино', r'\bильин|\bилин|\bilino'), ('Белиши', r'б[ьј]?[еи]л[еи]ш|bj?elis'),
            ('Тополица', r'тополиц|topolic'), ('Полье', r'\bполье\b|\bpolje\b|(?:район|р-н|микрорайон|рајон)\w*\s+поле\b'), ('Зелёный пояс', r'зелен\w+\s+пояс|zeleni\s+pojas'), ('Старый Бар', r'стар\w+\s+бар|stari\s+bar'), ('Чань', r'\bчан[ьеи]\b|\bcanj\b'), ('Заљево', r'за(?:л[ьјя]|љ)ев|zalj?ev')],
    'Будва': [('Центр', CENTER), ('Старый город', OLD_TOWN), ('Розино', r'розино|rozino'), ('Дубовица', r'дубовиц|dubovic'), ('Лази', r'\bлази\b|\blazi\b'),
              ('Бабин До', r'бабин\s+до|babin\s+do'), ('Голубовина', r'голубовин|golubovin'), ('Подкошлюн', r'подкошл|podkosljun'), ('Пржно', r'пржно|przno|милочер|milocer'), ('Свети Стефан', r'свети\s+стефан|sveti\s+stefan'), ('Каменово', r'каменов|kamenovo')],
    'Тиват': [('Центр', CENTER), ('Селяново', r'селянов|seljanov'), ('Порто Монтенегро', r'порто\s+монтенегро|porto\s+montenegro'), ('Донья Ластва', r'дон[ья]+\s+ластв|donja\s+lastv'), ('Мрчевац', r'мрчевац|mrcevac'), ('Крашичи', r'крашич|krasic')],
    'Котор': [('Центр', CENTER), ('Старый город', OLD_TOWN), ('Доброта', r'доброт[аеуы]\b|dobrot'), ('Муо', r'\bмуо\b|\bmuo\b'), ('Прчань', r'прчан|prcanj'), ('Столив', r'столив|stoliv'), ('Шкаляри', r'шкаляр|skaljar')],
    'Подгорица': [('Центр', CENTER), ('Забело', r'забел[оеу]\b|zabjel'), ('Блок', r'\bблок\s*\d|\bblok\s*\d'), ('Преко Мораче', r'преко\s+морач|preko\s+morac'), ('Старый аэродром', r'стар\w+\s+а[еэ]родром|stari\s+aerodrom'), ('City Kvart', r'сити\s+кварт|city\s+kvart'), ('Горица', r'\bгорица\b|\bgorica\b')],
    'Херцег-Нови': [('Центр', CENTER), ('Старый город', OLD_TOWN), ('Игало', r'игало|igalo'), ('Топла', r'\bтопл[аеу]\b|\btopla\b'), ('Савина', r'\bсавин[аеу]\b|\bsavina\b'), ('Мельине', r'мельин|мелин[ея]\b|meljin'),
                    ('Биела', r'\bбиел[аеуы]\b|\bbijela\b'), ('Кумбор', r'кумбор|kumbor'), ('Дженовичи', r'дженович|djenovic|đenovic'), ('Баошичи', r'баошич|baosic'), ('Зеленика', r'зеленик[аеу]\b|\bzelenika\b')],
    'Ульцинь': [('Центр', CENTER), ('Старый город', OLD_TOWN), ('Пинеш', r'пинеш|pinjes'), ('Штой', r'\bштой\b|\bstoj\b')],
}
CITIES = [('Бар', r'\bбар(?:е|а|у)?\b|\bбарск|\bbar\b|шушан|сушан|susanj|\bильин|\bилин|\bilino'), ('Будва', r'будв|budva'), ('Тиват', r'тиват|tivat'),
          ('Котор', r'котор|kotor'), ('Подгорица', r'подгориц|podgoric'), ('Сутоморе', r'сутомор|sutomore'), ('Добра Вода', r'добра\s?вода|dobra\s?voda'),
          ('Херцег-Нови', r'[гх]ерцег(?:[\s-]*нови?\w*)?|herceg(?:[\s-]*novi)?'), ('Ульцинь', r'ульцин|ulcinj'), ('Петровац', r'петровац|petrovac'),
          ('Бечичи', r'бечич|becic'), ('Рафаиловичи', r'рафаилович|rafailovic'), ('Колашин', r'колашин|kolasin'), ('Жабляк', r'жабляк|zabljak'),
          ('Цетине', r'цетин|cetinj'), ('Никшич', r'никшич|niksic'), ('Рисан', r'рисан|risan'), ('Пераст', r'пераст|perast')]
# An empty list means any. See filter_reasons() for how missing values are treated.
DEFAULT_FILTERS = dict(cities=[], price_max=800, bedrooms_min=2, districts={}, term='annual', incomplete=True, include=[], exclude=[], show_hidden=False, page_size=5)
MAX_PAGE_SIZE = 50
PAGE_SIZE_CHOICES = (5, 10, 20, 50)

def norm(text):
    return re.sub(r'\s+', ' ', text.lower().replace('ё', 'е').replace('š', 's').replace('č', 'c').replace('ć', 'c')).strip()

# A label right before the amount, or a monthly period right after it, says this sum is the rent itself.
# "аренда 700 €, депозит 1400 €" has neither, so it stays ambiguous.
# "12 месяцев по 399 евро" is a monthly rent for a term.
RENT_LABEL = re.compile(r'(?:аренд\w*|цена|стоимость|rent|price|cijena)\s*[:\-–—]\s*[^\n\d€]{0,6}$|\d+\s*(?:мес\w*|month\w*)\s+по\s+$')
RENT_PERIOD = re.compile(r'\s*(?:€|евро|eur\w*)?\s*(?:/|в|за|per|po)\s*(?:мес|month|mjesec)')

def price_matches(text):
    """(value, start, end) of explicit EUR amounts and amounts the wording calls rent. Newlines must survive here: they separate unrelated numbers."""
    text = text.lower()
    found = []
    for match in PRICE.finditer(text):
        raw = match.group(1) or match.group(2) or match.group(3)
        explicit = match.group(3) is None
        context = text[match.start():match.end()]
        # A bare number counts only next to a rent word or a monthly period.
        if not explicit and not re.match(r'цена|стоимость|💶|💰', context) and not re.match(r'\s*/\s*мес|\s+в\s+месяц', text[match.end():]): continue
        if re.fullmatch(r'\d+[  ]\d{3}', raw) and FEATURE_BEFORE_NUMBER.search(text[:match.start()]):
            raw = raw.split()[-1]
        whole, fraction = re.match(r'(.+?)(?:[.,](\d{1,2}))?$', raw).groups()
        value = float(re.sub(r'\D', '', whole) + ('.' + fraction if fraction else ''))
        # "3е пожилых" and "цена 2 спальни" are not amounts.
        if value < 10 and not re.search(r'€|евр|eur', context) or not explicit and value < 50: continue
        found.append((value, match.start(), match.end()))
    return found

def parse_prices(text):
    return sorted({value for value, _, _ in price_matches(text)})

def parse_rent_price(text):
    """The one amount labelled as rent ("Аренда: 400 € в месяц"), else None. Several different labelled amounts stay unknown."""
    lowered = text.lower()
    rents = set()
    for value, start, end in price_matches(text):
        line_start = lowered.rfind('\n', 0, start) + 1
        if RENT_LABEL.search(lowered[line_start:start]) or RENT_PERIOD.match(lowered[end:end + 20]): rents.add(value)
    return rents.pop() if len(rents) == 1 else None

def parse_bedrooms(t):
    numbers = [int(n) for n in re.findall(r'(?<![\w.,])(\d)(?:-?х)?[\s-]*(?:(?:отдельн|просторн|больш|уютн|светл|раздельн|изолированн|комфортн|полноценн|меблированн)\w*\s+)?' + BEDROOM, t)]
    # "спальня - 1", "#спальни_2", "количество спален: 1".
    numbers += [int(n) for n in re.findall(r'\bспальн\w*?\s*[-—–:_=]+\s*(\d)(?!\d)(?!\s*(?:м\b|м2|м²|кв))(?!-?х?\s*(?:спальн|мест|кроват|диван))|(?:количество|кол-?во|число)\s+спален\s*[-—–:]*\s*(\d)(?!\d)', t) for n in n if n]
    if numbers: return max(numbers)  # A post may list several flats; keep it if any can fit.
    if re.search(r'дв(?:е|умя|ух)\s+(?:отдельн\w*\s+)?' + BEDROOM + r'|дв(?:у|ух)спальн\w*\s+(?:квартир|апартамент)|two bedrooms', t): return 2
    if re.search(r'тр(?:и|емя|ех)\s+' + BEDROOM + r'|трехспальн\w*\s+(?:квартир|апартамент)|three bedrooms', t): return 3
    # Rooms are not bedrooms, so dvosoban/trosoban stay unknown. One room cannot hold two bedrooms, so a one-room flat has one.
    # "спальня, гостиная" is one bedroom only when nothing hints at another room to sleep in.
    if len(re.findall(r'\bспальн\w*', t)) == 1 and re.search(r'\bспальн[яю]\b', t) and re.search(r'гостин', t) and not re.search(r'детск|кабинет|втор[аоу]|\bещ[её]\b|\bдве\b|\bтри\b|\b[2-9]\s*-?\s*(?:х|ух|ех)?\s*-?\s*(?:комн|ком\b|soban)', t): return 1
    # "С отдельной спальней" is one bedroom; bed phrases like "2-х спальная кровать" are not counted by BEDROOM.
    if len(re.findall(BEDROOM, t)) == 1 and re.search(r'отдельн\w*\s+спальн(?:я|ей|ю|е)\b', t) and not re.search(r'детск|кабинет|втор[аоу]|\bещ[её]\b|\bдве\b|\bтри\b', t): return 1
    if re.search(r'одн\w*\s+' + BEDROOM + r'|односпальн\w*\s+(?:квартир|апартамент)|jednosoban|one bedroom|studio|однокомнатн|(?<![\w.,])1[\s-]*(?:комн|ком\b|к\b)|одн[аоу]\s+комнат', t): return 1
    # "Квартира-студия" is one room, while "кухня-студия" or "гостиная-студия" only describes a layout.
    if re.search(r'студи(?:я|ю|и|й)\b', re.sub(r'(?:кухн\w*|гостин\w*)[\s-]+(?:\w+[\s-]+)?студи\w*', '', t)): return 1
    return None

def parse(text, bar_source=False):
    t = norm(text)
    reasons, unknown = [], []
    # A post may name several districts, and any of them can be the one a user wants.
    areas = {city: [name for name, pattern in items if name not in GENERIC_DISTRICTS and re.search(pattern, t)] for city, items in DISTRICTS.items()}
    cities = [name for name, pattern in CITIES if re.search(pattern, t) or areas.get(name)]
    if bar_source and not cities:
        cities = ['Бар']
        unknown.append('Бар понятен только из тематики группы')
    if len(cities) > 1: unknown.append('Упоминается несколько городов — проверь географию')
    nearby = NOT_A_DISTRICT.sub(' ', DISTANCE_TO_CENTER.sub(' ', t))
    for city in cities:
        generic = [name for name, pattern in DISTRICTS.get(city, []) if name in GENERIC_DISTRICTS and re.search(pattern, nearby)]
        # Specific names go first: the card shows the first one, and "до центра Бара" says where the flat is not.
        if generic: areas[city] = areas[city] + generic
    areas = {city: names for city, names in areas.items() if names and city in cities}
    district = next((names[0] for names in areas.values()), None)
    rental = bool(re.search(r'сда(?:[ею]|м\b|ч[аи]\b)|аренд|izda[jv]|najam|najma|\brent|долгосрочн|dugorocn|long.term', t))
    if re.search(r'продаж|прода[ею]тся|продам|prodaj|for sale', t):
        # Agency signatures often say "аренда и продажа", which must not hide a rental.
        if rental: unknown.append('Упоминается продажа — проверь, что квартира сдаётся')
        else: reasons.append('Продажа')
    elif not rental: reasons.append('Не объявление об аренде')
    # "ищете"/"ищет" stay allowed: agencies write "Ищете квартиру? Сдаётся...".
    # A landlord looking for a tenant is an offer, not a request.
    if not re.search(r'арендатор|квартирант|жильц|tenant', t) and re.search(r'\bсним[уе]|\b(?:ищу|ищем)\b.{0,40}(?:квартир|аренд(?!атор)|жиль[еяю]|апартамент|спальн|\bдом)|(?:\bхоч\w*|\bхотим|\bхотел\w*|\bнужн\w*|\bпланиру\w*)\s+(?:бы\s+)?(?:снять|арендовать)|\bнужн\w*\s+(?:\S+\s+)?(?:квартир|жиль[еяю]|апартамент)|\bкто\b.{0,15}\bсда|\bwanted\b|looking\s+for|\btrazim|\btražim', t):
        reasons.append('Запрос на аренду')
    if re.search(r'посуточ|в сутки|за сутки|/\s*сутки|за ночь|/\s*ночь|в день|per night|/\s*night|po noci|nocenj|dnevn', t) and not re.search(r'долгосрочн|dugorocn|long.term|на\s+год\b|годов|круглогод', t):
        reasons.append('Посуточная аренда')
    if not re.search(HOUSING, t) and re.search(r'rent.a.car|^(?:' + NON_HOUSING + r')\b|(?:аренд\w*|прокат\w*|сда\w*|izdaj\w*|izdavanje|\brent)\W+(?:\w+\W+){0,2}?(?:' + NON_HOUSING + r')(?:\W|$)', t):
        reasons.append('Не жильё')
    prices = parse_prices(text)
    price = prices[0] if len(prices) == 1 else None
    # Several amounts mean rent plus deposit or similar. Only a sum labelled as rent settles which one it is.
    if len(prices) > 1:
        rent = parse_rent_price(text)
        if rent is not None: price, prices = rent, [rent]
        else: unknown.append('В посте несколько сумм — уточни цену')
    bedrooms = parse_bedrooms(t)
    if t.endswith('?') and not prices and bedrooms is None: reasons.append('Вопрос, а не объявление')
    annual = bool(re.search(r'круглогод|на[\s_]+год\b|на\s+12\s+месяц|(?<![\d.,])12\s*(?:мес|month)|(?:на|от)\s+(?:1|одного|один)\s+год|годов\w*[\s_]+аренд|долгосрочн|долгий\s+срок|длительн\w*\s+срок|year.round|annual|godisnj', t))
    seasonal = bool(re.search(r'до\s+(?:\d{1,2}[ .]*)?(?:ма[яй]|июн|июл|апрел|сезон|лет[ао])|на лето.{0,25}(?:выезд|освобод)|летом.{0,25}(?:дорож|повыш|высел)|until\s+(?:may|june|summer)|do\s+(?:maja|juna)', t))
    # Softer signs say a flat is let for a season; a post that also offers a year is not rejected by them.
    if not annual and re.search(r'до\s+начала\s+сезона|арендалето|аренда[\s_]+(?:на\s+)?лето|сда\w*\s+в\s+несезон|на\s+[2-9]\s+месяц|на\s+\d\s*(?:-|–|или)\s*\d\s+месяц', t): seasonal = True
    stable = bool(re.search(r'без\s+(?:летнего\s+)?повышен|без изменения цены|цена.{0,20}(?:весь год|круглый год)|same price.{0,20}year|без выселен.{0,20}лет', t))
    # Explicit date evidence is still reviewed: free-form dates can refer to publication or rent end.
    start = bool(re.search(r'(?:с|заезд|from|od)\s*(?:\d{1,2}\s*)?(?:декабр|январ|decemb|januar)|(?:с|from|od)\s*\d{1,2}[./](?:12[./]2026|0?1[./]2027)', t))
    status = 'reject' if reasons else 'review'
    # City, price and bedroom limits belong to each user, so they are applied by filter_reasons().
    return dict(cities=cities, district=district, areas=areas, seasonal=seasonal, price=price, min_price=prices[0] if prices else None, bedrooms=bedrooms, annual=annual, stable=stable, start_mentioned=start, status=status, reasons=reasons, unknown=unknown)

def word_found(word, text):
    # A few letters of ending match Russian cases, while "кот" still misses "который".
    return re.search(r'(?<!\w)' + re.escape(word) + r'\w{0,3}(?!\w)', text) is not None

def filter_reasons(p, filters, text=''):
    """Why a post misses a user's filters. Missing values keep a post, since the author can be asked.
    There are two exceptions: a chosen city, because most sources cover the whole country, and the
    `incomplete` setting, which is an explicit request to hide such posts."""
    reasons = []
    if filters['cities'] and not set(filters['cities']) & set(p.get('cities') or []):
        reasons.append('Другой город' if p.get('cities') else 'Город не указан')
    if filters['term'] == 'annual' and p.get('seasonal'): reasons.append('Сезонные ограничения')
    # Season mode wants an explicit sign: a post that says nothing about the term is not shown.
    if filters['term'] == 'season' and not p.get('seasonal'): reasons.append('Не сезонная аренда')
    if not filters['incomplete'] and (p.get('min_price') is None or p.get('bedrooms') is None):
        reasons.append('Нет цены или числа спален')
    if filters['price_max'] is not None and p.get('min_price') is not None and p['min_price'] > filters['price_max']:
        reasons.append(f"Цена выше {filters['price_max']} €")
    if p.get('bedrooms') is not None and p['bedrooms'] < filters['bedrooms_min']:
        reasons.append(f"Спален меньше {filters['bedrooms_min']}")
    # A district choice narrows only its own city, so another chosen city is not affected.
    relevant = [c for c in p.get('cities') or [] if not filters['cities'] or c in filters['cities']]
    limits = filters['districts']
    if any(limits.get(c) for c in relevant) and not any(not limits.get(c) or not (p.get('areas') or {}).get(c) or set(limits[c]) & set(p['areas'][c]) for c in relevant):
        reasons.append('Другой район')
    if filters['include'] or filters['exclude']:
        normal = norm(text)
        if not all(word_found(word, normal) for word in filters['include']): reasons.append('Нет нужных слов')
        if any(word_found(word, normal) for word in filters['exclude']): reasons.append('Есть запрещённые слова')
    return reasons

def describe_filters(f):
    price = f"до {f['price_max']} €" if f['price_max'] is not None else 'любая'
    bedrooms = f"от {f['bedrooms_min']}" if f['bedrooms_min'] else 'любое число'
    lines = [f"Город: {', '.join(f['cities']) or 'любой'}", f'Цена: {price}', f'Спальни: {bedrooms}', f'Район: {district_label(f)}',
             f"Срок: {TERM_LABELS[f['term']]}", f"Неполные объявления: {'показывать' if f['incomplete'] else 'скрывать'}",
             f"Скрытые мной: {'показывать' if f['show_hidden'] else 'не показывать'}", f"Объявлений на странице: {f['page_size']}"]
    if f['include']: lines.append('Обязательные слова: ' + ', '.join(f['include']))
    if f['exclude']: lines.append('Запрещённые слова: ' + ', '.join(f['exclude']))
    return '\n'.join(lines)

def district_label(f):
    chosen = [(city, name) for city, names in f['districts'].items() for name in names]
    if not chosen: return 'любой'
    return ', '.join(name for _, name in chosen[:3]) + (f' и ещё {len(chosen)-3}' if len(chosen) > 3 else '')

PRICE_CHOICES = (500, 800, 1200, 1500)
# The term button cycles through these.
TERM_LABELS = {'annual': 'год', 'season': 'сезон', 'any': 'любой'}
TERM_NEXT = {'annual': 'season', 'season': 'any', 'any': 'annual'}
CITY_NAMES = [name for name, _ in CITIES]

def button(text, data):
    return {'text': text, 'callback_data': data}

def rows_of(buttons, width):
    return [buttons[i:i+width] for i in range(0, len(buttons), width)]

def menu(screen, f, matched=0, setup=False):
    """(text, inline keyboard) of a filter screen. Pure, so every screen can be tested without Telegram."""
    back = [button('Дальше →', 'menu:price')] if setup and screen == 'cities' else [] if setup else [button('← Назад', 'menu:main')]
    mark = lambda on, text: ('✓ ' if on else '') + text
    if screen == 'main':
        price = f"до {f['price_max']} €" if f['price_max'] is not None else 'любая'
        bedrooms = f"от {f['bedrooms_min']}" if f['bedrooms_min'] else 'любые'
        words = len(f['include']) + len(f['exclude'])
        return f'Ваши фильтры · подходит объявлений: {matched}\n\n{describe_filters(f)}', [
            [button('Город: ' + (f['cities'][0] + (f" +{len(f['cities'])-1}" if len(f['cities']) > 1 else '') if f['cities'] else 'любой'), 'menu:cities'), button('Цена: ' + price, 'menu:price')],
            [button('Спальни: ' + bedrooms, 'menu:bedrooms'), button('Район: ' + district_label(f), 'menu:districts')],
            [button('Срок: ' + TERM_LABELS[f['term']], 'set:t'), button('Неполные: ' + ('показывать' if f['incomplete'] else 'скрывать'), 'set:i')],
            [button(f'Слова: {words}' if words else 'Слова: нет', 'menu:words'), button('Скрытые: ' + ('показывать' if f['show_hidden'] else 'нет'), 'set:h')],
            [button(f"На странице: {f['page_size']}", 'menu:pagesize')],
            [button('Показать объявления', 'show')]]
    if screen == 'cities':
        title = 'Шаг 1 из 3. Где ищете? Можно выбрать несколько городов.' if setup else 'Где ищете? Можно выбрать несколько городов.'
        return title, rows_of([button(mark(name in f['cities'], name), f'set:c:{i}') for i, name in enumerate(CITY_NAMES)], 3) + [[button(mark(not f['cities'], 'Любой город'), 'set:c:all')], back]
    if screen == 'price':
        title = 'Шаг 2 из 3. Бюджет в месяц?' if setup else 'Бюджет в месяц?'
        return title, rows_of([button(mark(f['price_max'] == n, f'до {n} €'), f'set:p:{n}') for n in PRICE_CHOICES], 2) + [[button(mark(f['price_max'] is None, 'Без предела'), 'set:p:off'), button('Своя сумма', 'ask:price')]] + ([back] if back else [])
    if screen == 'bedrooms':
        title = 'Шаг 3 из 3. Сколько спален?' if setup else 'Сколько спален?'
        return title, [[button(mark(f['bedrooms_min'] == n, f'от {n}'), f'set:b:{n}') for n in (1, 2, 3)], [button(mark(not f['bedrooms_min'], 'Любое число'), 'set:b:0')]] + ([back] if back else [])
    if screen == 'districts':
        cities = [c for c in (f['cities'] or CITY_NAMES) if c in DISTRICTS]
        if not f['cities']: return 'Сначала выберите город: районы зависят от него.', [[button('Выбрать город', 'menu:cities')], back]
        if not cities: return 'Для выбранных городов районов пока нет.', [back]
        rows = []
        for city in cities:
            chosen = f['districts'].get(city, [])
            label = (lambda n: f'{city}: {n}') if len(cities) > 1 else (lambda n: n)
            rows += rows_of([button(mark(name in chosen, label(name)), f'set:d:{CITY_NAMES.index(city)}:{i}') for i, (name, _) in enumerate(DISTRICTS[city])], 2)
        return 'Какие районы подходят? Объявления без района всё равно будут показаны.', rows + [[button(mark(not any(f['districts'].values()), 'Любой район'), 'set:d:all')], back]
    if screen == 'pagesize':
        return 'Сколько объявлений показывать за раз в /find и /more?', rows_of([button(mark(f['page_size'] == n, str(n)), f'set:s:{n}') for n in PAGE_SIZE_CHOICES], 4) + [[button('Своё число', 'ask:page')], back]
    if screen == 'words':
        text = ('Объявление должно содержать все обязательные слова и ни одного запрещённого.\n\n'
                f"Обязательные: {', '.join(f['include']) or 'нет'}\nЗапрещённые: {', '.join(f['exclude']) or 'нет'}")
        return text, [[button('Задать обязательные', 'ask:include'), button('Задать запрещённые', 'ask:exclude')], [button('Очистить', 'set:w:clear')], back]
    raise ValueError(screen)

def apply_choice(f, data):
    """New filters for a `set:` button. Unknown or stale data leaves them unchanged."""
    parts = data.split(':')[1:]
    kind, value = parts[0], parts[1:]
    try:
        if kind == 'c':
            if value == ['all']: return {**f, 'cities': [], 'districts': {}}
            name = CITY_NAMES[int(value[0])]
            if name in f['cities']:
                return {**f, 'cities': [c for c in f['cities'] if c != name], 'districts': {c: d for c, d in f['districts'].items() if c != name}}
            return {**f, 'cities': [c for c in CITY_NAMES if c in f['cities'] or c == name]}
        if kind == 'p': return {**f, 'price_max': None if value == ['off'] else int(value[0])}
        if kind == 'b': return {**f, 'bedrooms_min': int(value[0])}
        if kind == 's' and 1 <= int(value[0]) <= MAX_PAGE_SIZE: return {**f, 'page_size': int(value[0])}
        if kind == 't': return {**f, 'term': TERM_NEXT[f['term']]}
        if kind == 'i': return {**f, 'incomplete': not f['incomplete']}
        if kind == 'h': return {**f, 'show_hidden': not f['show_hidden']}
        if kind == 'w': return {**f, 'include': [], 'exclude': []}
        if kind == 'd':
            if value == ['all']: return {**f, 'districts': {}}
            city = CITY_NAMES[int(value[0])]
            name = DISTRICTS[city][int(value[1])][0]
            current = f['districts'].get(city, [])
            names = [n for n, _ in DISTRICTS[city] if (n in current) != (n == name)]
            districts = {c: d for c, d in f['districts'].items() if c != city}
            return {**f, 'districts': {**districts, city: names} if names else districts}
    except (IndexError, KeyError, ValueError):
        pass
    return f

ASK_PROMPTS = {'page': f'Напишите число объявлений на странице от 1 до {MAX_PAGE_SIZE}.',
               'price': 'Напишите предел цены числом, например 950.',
               'include': 'Напишите обязательные слова через запятую, например: кот, парковка.',
               'exclude': 'Напишите запрещённые слова через запятую, например: агентство, комиссия.'}

def apply_text(f, kind, text):
    """New filters for a value typed after an `ask:` button; ValueError holds the hint for the user."""
    if kind == 'price':
        value = text.replace('€', '').strip()
        if not value.isdigit() or not 1 <= int(value) <= 100000: raise ValueError(ASK_PROMPTS['price'])
        return {**f, 'price_max': int(value)}
    if kind == 'page':
        value = text.strip()
        if not value.isdigit() or not 1 <= int(value) <= MAX_PAGE_SIZE: raise ValueError(ASK_PROMPTS['page'])
        return {**f, 'page_size': int(value)}
    words = list(dict.fromkeys(word for word in (norm(part) for part in text.split(',')) if word))
    if not words or len(words) > 20 or any(len(word) > 40 for word in words):
        raise ValueError('Нужно от 1 до 20 слов длиной до 40 символов, через запятую.')
    return {**f, kind: words}

def message_author(msg):
    """(id, display name, username) from a Telethon message; attributes are read duck-typed."""
    sender = getattr(msg, 'sender', None)
    name = getattr(sender, 'title', None) or ' '.join(x for x in (getattr(sender, 'first_name', None), getattr(sender, 'last_name', None)) if x)
    username = getattr(sender, 'username', None)
    if not username:
        username = next((u.username for u in getattr(sender, 'usernames', None) or [] if getattr(u, 'active', False)), None)
    return getattr(msg, 'sender_id', None), name or None, username

def message_links(msg):
    """Targets invisible in raw_text: links under words, hidden mentions and URL buttons."""
    links = []
    for entity in getattr(msg, 'entities', None) or []:
        if getattr(entity, 'url', None): links.append(entity.url)
        elif getattr(entity, 'user_id', None): links.append(f'tg://user?id={entity.user_id}')
    for row in getattr(getattr(msg, 'reply_markup', None), 'rows', None) or []:
        for button in row.buttons:
            # Recent Telethon layers keep the URL in button.type instead of on the button.
            url = getattr(button, 'url', None) or getattr(getattr(button, 'type', None), 'url', None)
            if url: links.append(url)
    return list(dict.fromkeys(links))

def scan_days(last_ok, days, rescan_days, now=None):
    """History is read in full once; later scans cover recent posts and any downtime."""
    if not last_ok: return days
    gap = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(last_ok)).total_seconds() / 86400
    return min(days, max(rescan_days, math.ceil(gap) + 1))

def scan_due(last_ok, every_hours, now=None):
    """Whether the time window is due again; a missing mark means it was never read."""
    if not last_ok: return True
    gap = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(last_ok)
    return gap.total_seconds() >= every_hours * 3600

def parse_post(source, text):
    return json.dumps(parse(text, source in BAR_SOURCES), ensure_ascii=False)

def same_listing(a, b):
    """One author's copy of a post in two groups, differing by a stray character or two.
    Deliberately narrow: the same author, every number identical (price, floor, area, phone) and near-identical wording.
    Two flats from one template differ in some number, so they stay apart."""
    if a['author_id'] is None or a['author_id'] != b['author_id']: return False
    ta, tb = norm(a['text']), norm(b['text'])
    if min(len(ta), len(tb)) < 80 or re.findall(r'\d+', ta) != re.findall(r'\d+', tb): return False
    matcher = difflib.SequenceMatcher(None, ta, tb, autojunk=False)
    return matcher.quick_ratio() >= 0.95 and matcher.ratio() >= 0.95

def bucket(user_filters):
    return json.loads(user_filters).get('cities') or ['любой']

POST_FIELDS = 'p.id,p.source,p.message_id,p.published,p.seen,p.changed,p.text,p.digest,p.parsed,p.author_id,p.author_name,p.author_username,p.links'

class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS posts(
          id INTEGER PRIMARY KEY, source TEXT, message_id INTEGER, published TEXT,
          seen TEXT, changed TEXT, text TEXT, digest TEXT, parsed TEXT,
          author_id INTEGER, author_name TEXT, author_username TEXT, links TEXT,
          UNIQUE(source,message_id));
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS health(source TEXT PRIMARY KEY,checked TEXT,error TEXT);
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, filters TEXT NOT NULL, started INTEGER DEFAULT 0,
          paused INTEGER DEFAULT 0, page INTEGER DEFAULT 0, notify_after TEXT DEFAULT '',
          created TEXT DEFAULT '', blocked INTEGER DEFAULT 0, awaiting TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS user_posts(
          user_id INTEGER, post_id INTEGER, hidden INTEGER DEFAULT 0, notified TEXT,
          PRIMARY KEY(user_id,post_id));
        CREATE INDEX IF NOT EXISTS user_posts_post ON user_posts(post_id);
        CREATE TABLE IF NOT EXISTS parser_reports(post_id INTEGER PRIMARY KEY, created TEXT, parsed TEXT, comment TEXT DEFAULT '');
        ''')
        if 'changed' not in self.post_columns(): self.upgrade_posts()
        user_columns = {row['name'] for row in self.db.execute('PRAGMA table_info(users)')}
        for name, definition in (('created',"TEXT DEFAULT ''"),('blocked','INTEGER DEFAULT 0'),('awaiting',"TEXT DEFAULT ''")):
            if name not in user_columns: self.db.execute(f'ALTER TABLE users ADD COLUMN {name} {definition}')
        if 'comment' not in {row['name'] for row in self.db.execute('PRAGMA table_info(parser_reports)')}:
            self.db.execute("ALTER TABLE parser_reports ADD COLUMN comment TEXT DEFAULT ''")
        self.db.commit()
        if 'confirmed' in {row['name'] for row in self.db.execute('PRAGMA table_info(user_posts)')}:
            self.db.execute('ALTER TABLE user_posts DROP COLUMN confirmed')
            self.db.commit()
        if self.get('parse_version') != PARSE_VERSION:
            self.reparse()
            self.set('parse_version', PARSE_VERSION)
    def post_columns(self):
        return {row['name'] for row in self.db.execute('PRAGMA table_info(posts)')}
    def upgrade_posts(self):
        """One-time upgrade of a single-user database. Filters moved out of parse(), hence the reparse."""
        for column in ('changed TEXT','author_id INTEGER','author_name TEXT','author_username TEXT','links TEXT'):
            self.db.execute(f'ALTER TABLE posts ADD COLUMN {column}')
        self.db.execute('UPDATE posts SET changed=published')
        # Forget scan marks so the next pass re-reads the window and fills in authors and links.
        self.db.execute("DELETE FROM meta WHERE key LIKE 'scanned:%'")
        self.db.commit()
        self.reparse()
    def adopt_owner(self, owner):
        """Create the owner; state that a single-user database kept globally becomes theirs."""
        legacy = 'hidden' in self.post_columns()
        # The owner of an older database searched only in Bar, so that search stays as it was.
        self.db.execute('INSERT OR IGNORE INTO users(id,filters,created) VALUES(?,?,?)', (owner,json.dumps({**DEFAULT_FILTERS,'cities':['Бар'] if legacy else []},ensure_ascii=False),datetime.now(timezone.utc).isoformat()))
        stored = json.loads(self.user(owner)['filters'])
        if 'cities' not in stored:
            self.db.execute('UPDATE users SET filters=? WHERE id=?', (json.dumps({**stored,'cities':['Бар']},ensure_ascii=False),owner))
        self.db.commit()
        if not legacy: return
        self.db.execute('BEGIN')
        self.db.execute('INSERT OR REPLACE INTO user_posts(user_id,post_id,hidden,notified) SELECT ?,id,hidden,notified FROM posts WHERE hidden=1 OR notified IS NOT NULL',(owner,))
        self.db.execute('UPDATE users SET started=?,paused=?,page=? WHERE id=?',(int(self.get('started') or 0),int(self.get('paused') or 0),int(self.get('page') or 0),owner))
        self.db.execute("DELETE FROM meta WHERE key IN ('started','paused','page','initialized')")
        for column in ('hidden','confirmed','notified'): self.db.execute(f'ALTER TABLE posts DROP COLUMN {column}')
        self.db.commit()
    def get(self, key, default=''):
        r = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return r[0] if r else default
    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, str(value)))
        self.db.commit()
    def last_id(self, source):
        return self.db.execute('SELECT MAX(message_id) FROM posts WHERE source=?', (source,)).fetchone()[0] or 0
    def put(self, source, mid, published, text, author=(None, None, None), links=None):
        digest = hashlib.sha256(norm(text).encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        links = json.dumps(links or [], ensure_ascii=False)
        old = self.db.execute('SELECT id,digest FROM posts WHERE source=? AND message_id=?', (source, mid)).fetchone()
        if old and old['digest'] == digest:
            self.db.execute('UPDATE posts SET seen=?,author_id=?,author_name=?,author_username=?,links=? WHERE id=?', (now,*author,links,old['id']))
        elif old:
            self.db.execute('UPDATE posts SET seen=?,changed=?,text=?,digest=?,parsed=?,author_id=?,author_name=?,author_username=?,links=? WHERE id=?', (now,now,text,digest,parse_post(source,text),*author,links,old['id']))
            self.db.execute('UPDATE user_posts SET notified=NULL WHERE post_id=?', (old['id'],))
        else:
            self.db.execute('INSERT INTO posts(source,message_id,published,seen,changed,text,digest,parsed,author_id,author_name,author_username,links) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (source,mid,published,now,now,text,digest,parse_post(source,text),*author,links))
        self.db.commit()
    def health(self, source, error=None):
        self.db.execute('INSERT OR REPLACE INTO health VALUES(?,?,?)', (source,datetime.now(timezone.utc).isoformat(),error))
        self.db.commit()
    def reparse(self):
        rows = self.db.execute('SELECT id,source,text,parsed FROM posts').fetchall()
        log.info('reparse: начало, записей %d, версия парсера %s', len(rows), PARSE_VERSION)
        changed = fields = 0
        for row in rows:
            new = parse_post(row['source'], row['text'])
            if new != row['parsed']:
                changed += 1
                try: old_p, new_p = json.loads(row['parsed'] or '{}'), json.loads(new)
                except ValueError: old_p, new_p = {}, {}
                if old_p.get('district') != new_p.get('district'):
                    fields += 1
                    log.debug('reparse: пост %s, район %r -> %r', row['id'], old_p.get('district'), new_p.get('district'))
                if old_p.get('status') != new_p.get('status'):
                    log.info('reparse: пост %s, статус %r -> %r', row['id'], old_p.get('status'), new_p.get('status'))
                self.db.execute('UPDATE posts SET parsed=? WHERE id=?', (new, row['id']))
        self.db.commit()
        log.info('reparse: готово, изменено %d из %d, район изменился у %d', changed, len(rows), fields)
        return len(rows)
    def ensure_user(self, user):
        self.add_user(user)
    def add_user(self, user):
        # Only posts that appear after joining are delivered; the rest is available through /find.
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute('INSERT OR IGNORE INTO users(id,filters,notify_after,created) VALUES(?,?,?,?)', (user,json.dumps(DEFAULT_FILTERS,ensure_ascii=False),now,now))
        self.db.commit()
    def set_blocked(self, user, blocked):
        self.add_user(user)
        self.db.execute('UPDATE users SET blocked=? WHERE id=?', (int(blocked),user))
        self.db.commit()
    def user_stats(self):
        week_ago = (datetime.now(timezone.utc)-timedelta(days=7)).isoformat()
        rows = self.users()
        active = [u for u in rows if not u['blocked']]
        cities = {}
        for user in active:
            for city in bucket(user['filters']): cities[city] = cities.get(city,0)+1
        return dict(total=len(rows), blocked=len(rows)-len(active), started=sum(1 for u in active if u['started']),
                    notified=sum(1 for u in active if u['started'] and not u['paused']),
                    new_week=sum(1 for u in rows if u['created'] >= week_ago), cities=sorted(cities.items(),key=lambda x:(-x[1],x[0])))
    def user(self, user):
        return self.db.execute('SELECT * FROM users WHERE id=?', (user,)).fetchone()
    def users(self):
        return self.db.execute('SELECT * FROM users ORDER BY id').fetchall()
    def set_user(self, user, **fields):
        for field, value in fields.items():
            assert field in ('started','paused','page','awaiting')
            self.db.execute(f'UPDATE users SET {field}=? WHERE id=?', (value,user))
        self.db.commit()
    def filters(self, user):
        row = self.user(user) if user is not None else None
        if not row: return dict(DEFAULT_FILTERS)
        filters = {**DEFAULT_FILTERS, **json.loads(row['filters'])}
        # Districts used to be a flat list of Bar's names.
        if isinstance(filters['districts'], list): filters['districts'] = {'Бар': filters['districts']} if filters['districts'] else {}
        return filters
    def set_filters(self, user, filters):
        # Moving notify_after keeps a relaxed filter from delivering the backlog; /find shows it.
        # The page size selects nothing, so changing only it must not move the boundary.
        old = self.filters(user)
        same = {k: v for k, v in old.items() if k != 'page_size'} == {k: v for k, v in filters.items() if k != 'page_size'}
        if same: self.db.execute('UPDATE users SET filters=?,page=0 WHERE id=?', (json.dumps(filters,ensure_ascii=False),user))
        else: self.db.execute('UPDATE users SET filters=?,notify_after=?,page=0 WHERE id=?', (json.dumps(filters,ensure_ascii=False),datetime.now(timezone.utc).isoformat(),user))
        self.db.commit()
    def visible(self, user, extra='', params=(), show_hidden=False):
        hidden = '' if show_hidden else 'COALESCE(u.hidden,0)=0 AND '
        return self.db.execute('SELECT ' + POST_FIELDS + ',COALESCE(u.hidden,0) AS hidden,u.notified FROM posts p LEFT JOIN user_posts u ON u.post_id=p.id AND u.user_id=? WHERE ' + hidden + 'p.published>=? ' + extra + ' ORDER BY p.published DESC', params).fetchall()
    def candidates(self, days=14, include_rejected=False, user=None):
        cutoff = (datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
        filters = self.filters(user)
        result, digests = [], set()
        for row in self.visible(user,'',(user,cutoff),filters['show_hidden']):
            p = json.loads(row['parsed'])
            if not include_rejected and (p['status']=='reject' or filter_reasons(p,filters,row['text'])): continue
            if row['digest'] in digests: continue
            digests.add(row['digest'])
            if any(same_listing(row, other) for other in result if other['author_id'] is not None and other['author_id'] == row['author_id']): continue
            result.append(row)
        return result
    def pending(self, user, days, boundary):
        """Matching posts this user has not been sent. Rechecks refresh `seen`, so `changed` decides."""
        cutoff = (datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
        filters = self.filters(user)
        rows = self.visible(user,'AND p.changed>=? AND (u.notified IS NULL OR u.notified<>p.digest)',(user,cutoff,boundary))
        return [row for row in rows if json.loads(row['parsed'])['status']!='reject' and not filter_reasons(json.loads(row['parsed']),filters,row['text'])]
    def duplicate_notified(self, user, row):
        """True if this user already got the same text, or a near copy of it from the same author."""
        if self.digest_notified(user, row['digest']): return True
        if row['author_id'] is None: return False
        others = self.db.execute('SELECT p.text,p.author_id FROM user_posts u JOIN posts p ON p.id=u.post_id WHERE u.user_id=? AND u.notified=p.digest AND p.author_id=? AND p.id<>?',(user,row['author_id'],row['id'])).fetchall()
        return any(same_listing(row, other) for other in others)
    def digest_notified(self, user, digest):
        return self.db.execute('SELECT 1 FROM user_posts u JOIN posts p ON p.id=u.post_id WHERE u.user_id=? AND p.digest=? AND u.notified=p.digest',(user,digest)).fetchone() is not None
    def mark_notified(self, user, pid, digest):
        self.db.execute('INSERT INTO user_posts(user_id,post_id,notified) VALUES(?,?,?) ON CONFLICT(user_id,post_id) DO UPDATE SET notified=excluded.notified',(user,pid,digest))
        self.db.commit()
    def is_hidden(self, user, pid):
        row = self.db.execute('SELECT hidden FROM user_posts WHERE user_id=? AND post_id=?',(user,pid)).fetchone()
        return bool(row and row['hidden'])
    def is_reported(self, pid):
        return self.db.execute('SELECT 1 FROM parser_reports WHERE post_id=?',(pid,)).fetchone() is not None
    def comment(self, pid, text):
        """Owner's note on a post; it also puts the post on the parser review list."""
        if not self.report(pid, True): return False
        self.db.execute("UPDATE parser_reports SET comment=CASE WHEN comment='' OR comment IS NULL THEN ? ELSE comment||char(10)||? END WHERE post_id=?",(text,text,pid))
        self.db.commit()
        return True
    def report(self, pid, flag):
        """Marks a post for parser review, keeping what parse() said at that moment."""
        row = self.db.execute('SELECT parsed FROM posts WHERE id=?',(pid,)).fetchone()
        if not row: return False
        if flag: self.db.execute('INSERT OR IGNORE INTO parser_reports(post_id,created,parsed) VALUES(?,?,?)',(pid,datetime.now(timezone.utc).isoformat(),row['parsed']))
        else: self.db.execute('DELETE FROM parser_reports WHERE post_id=?',(pid,))
        self.db.commit()
        return True
    def reports(self, user=None):
        return self.db.execute('SELECT ' + POST_FIELDS + ',COALESCE(u.hidden,0) AS hidden,u.notified,r.created AS reported,r.parsed AS parsed_then,r.comment AS comment FROM parser_reports r JOIN posts p ON p.id=r.post_id LEFT JOIN user_posts u ON u.post_id=p.id AND u.user_id=? ORDER BY r.created',(user,)).fetchall()
    def action(self, user, pid, action):
        if not self.db.execute('SELECT 1 FROM posts WHERE id=?',(pid,)).fetchone(): return False
        value = int(action == 'hide')
        self.db.execute('INSERT INTO user_posts(user_id,post_id,hidden) VALUES(?,?,?) ON CONFLICT(user_id,post_id) DO UPDATE SET hidden=excluded.hidden',(user,pid,value))
        self.db.commit()
        return True

def card_buttons(pid, hidden, reported=None):
    """reported is None for users who cannot flag posts; the owner gets a second button."""
    row = [button('Вернуть', f'unhide:{pid}') if hidden else button('Скрыть', f'hide:{pid}')]
    if reported is not None: row.append(button('✓ На проверке' if reported else 'На проверку парсера', f'unreport:{pid}' if reported else f'report:{pid}'))
    return [row]

def card(row):
    p = json.loads(row['parsed'])
    price = f"{p['price']:g} €" if p['price'] is not None else 'цена ?'
    bedrooms = p['bedrooms'] if p['bedrooms'] is not None else '?'
    author = row['author_name'] or (f"id {row['author_id']}" if row['author_id'] else 'неизвестен')
    if row['author_username']: author += f" (@{row['author_username']})"
    links = [link for link in json.loads(row['links'] or '[]') if len(link) <= 300][:5]
    place = ', '.join(p.get('cities') or []) or 'город ?'
    notes = ('\n' + '\n'.join('• '+x for x in p['unknown'])) if p['unknown'] else ''
    hidden = '\nВы скрыли это объявление' if row['hidden'] else ''
    term = 'сезон' if p.get('seasonal') else 'год' if p.get('annual') else '?'
    return (f"#{row['id']} · {place} · {p['district'] or 'район ?'} · {price} · спальни: {bedrooms} · срок: {term}\n"
            f"Опубликовано: {row['published'][:10]}\nАвтор: {author}{hidden}{notes}\n"
            'Условия и доступность уточняйте у автора.\n\n' + row['text'][:1700] +
            ('\n\nСсылки из поста:\n' + '\n'.join(links) if links else '') +
            f"\n\nhttps://t.me/{row['source']}/{row['message_id']}")[:4000]


EXPORT_CSS = """
:root{color-scheme:light dark;--bg:#fafafa;--fg:#1d1d1f;--mute:#6b6b70;--line:#dcdce0;--card:#fff;--accent:#0a66c2}
@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#ececf0;--mute:#9a9aa2;--line:#33333a;--card:#1f1f23;--accent:#6cb2ff}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:8px 0}.mute{color:var(--mute);font-size:13px}
article{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0}
.head{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:baseline}.price{font-weight:700;font-size:17px}
.tags span{display:inline-block;border:1px solid var(--line);border-radius:12px;padding:0 8px;margin:2px 4px 2px 0;font-size:13px}
details{margin-top:6px}summary{cursor:pointer;color:var(--accent)}pre{white-space:pre-wrap;word-break:break-word;font:inherit;margin:8px 0 0}
a{color:var(--accent)}ul{margin:6px 0;padding-left:18px}
"""

def export_html(rows, filters, generated=None):
    """One self-contained HTML page with every matching listing; pure function of its arguments."""
    e = html.escape
    generated = generated or datetime.now(timezone.utc)
    summary = [f"до {filters['price_max']:g} €" if filters.get('price_max') else None,
               f"спален от {filters['bedrooms_min']}" if filters.get('bedrooms_min') else None,
               ', '.join(filters.get('cities') or []) or 'любой город',
               'срок: ' + TERM_LABELS.get(filters.get('term'), 'любой')]
    parts = [f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
             f'<title>Аренда в Черногории</title><style>{EXPORT_CSS}</style></head><body><main>'
             f'<h1>Аренда в Черногории: {len(rows)}</h1>'
             f'<p class="mute">{e(" · ".join(x for x in summary if x))}<br>Сформировано {generated:%Y-%m-%d %H:%M} UTC. '
             'Условия и доступность уточняйте у автора.</p>']
    for row in rows:
        p = json.loads(row['parsed'])
        price = f"{p['price']:g} €" if p['price'] is not None else 'цена ?'
        tags = [', '.join(p.get('cities') or []) or 'город ?', p['district'] or 'район ?',
                f"спальни: {p['bedrooms'] if p['bedrooms'] is not None else '?'}",
                'сезон' if p.get('seasonal') else 'год' if p.get('annual') else 'срок ?']
        author = row['author_name'] or (f"id {row['author_id']}" if row['author_id'] else 'неизвестен')
        if row['author_username']: author += f" (@{row['author_username']})"
        post = f"https://t.me/{row['source']}/{row['message_id']}"
        links = [x for x in json.loads(row['links'] or '[]') if len(x) <= 300 and x.startswith(('http://', 'https://'))][:5]
        notes = ''.join(f'<li>{e(x)}</li>' for x in p['unknown'])
        parts.append(f'<article><div class="head"><span class="price">{e(price)}</span>'
                     f'<a href="{e(post)}">#{row["id"]} в Telegram</a><span class="mute">{e(row["published"][:10])} · {e(author)}</span></div>'
                     f'<div class="tags">{"".join(f"<span>{e(t)}</span>" for t in tags)}</div>'
                     + (f'<ul>{notes}</ul>' if notes else '')
                     + f'<details><summary>Текст объявления</summary><pre>{e(row["text"][:3000])}</pre>'
                     + (''.join(f'<div><a href="{e(x)}">{e(x)}</a></div>' for x in links))
                     + '</details></article>')
    if not rows: parts.append('<p>Подходящих объявлений нет.</p>')
    return ''.join(parts) + '</main></body></html>'
