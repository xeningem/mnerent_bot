import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
from core import CITY_NAMES, DEFAULT_FILTERS, DISTRICTS, export_html, apply_choice, apply_text, card, card_buttons, filter_reasons, menu, message_author, message_links, parse, parse_prices, scan_days, scan_due, Store

BAR={**DEFAULT_FILTERS,'cities':['Бар']}

def verdict(text,bar_source=False):
    '''What a user searching in Bar with the default limits gets.'''
    p=parse(text,bar_source)
    return 'reject' if p['status']=='reject' or filter_reasons(p,BAR) else 'review'

class ParserTests(unittest.TestCase):
    def test_reject_price(self):
        self.assertEqual(verdict('Бар центр сдается 2 спальни 850 € на год'),'reject')
    def test_two_rooms_not_two_bedrooms(self):
        self.assertIsNone(parse('Бар Шушань сдается двухкомнатная 700 €')['bedrooms'])
    def test_explicit(self):
        p=parse('Бар Ильино сдается 2 спальни 800 € на год без повышения цены с декабря')
        self.assertEqual(p['bedrooms'],2)
        self.assertEqual(p['price'],800)
        self.assertEqual(p['status'],'review')
        self.assertTrue(p['annual'] and p['stable'] and p['start_mentioned'])
    def test_seasonal(self):
        self.assertEqual(verdict('Бар центр сдается 2 спальни 700 € до июня'),'reject')
    def test_sale(self):
        self.assertEqual(parse('Бар продажа 2 спальни 150000 €')['status'],'reject')
    def test_single(self):
        self.assertEqual(verdict('Бар центр аренда одна спальня 600 €'),'reject')
    def test_deposit_ambiguous(self):
        self.assertIsNone(parse('Бар 2 спальни аренда 700 €, депозит 1400 €')['price'])
    def test_serbian(self):
        p=parse('Izdaje se dvosoban stan Bar Susanj 750 EUR')
        self.assertEqual((p['bedrooms'],p['district'],p['status']),(None,'Шушань','review'))
    def test_other_city(self):
        self.assertEqual(parse('Будва центр аренда 2 спальни 750 €')['cities'],['Будва'])
        self.assertEqual(verdict('Будва центр аренда 2 спальни 750 €'),'reject')
    def test_thousand(self):
        self.assertEqual(parse('Бар аренда 1.200 EUR')['price'],1200)

class NoiseTests(unittest.TestCase):
    def test_rejected(self):
        for text in ['Спасибо!', 'Подскажите хорошего юриста в Баре', 'Кто-нибудь знает, сколько стоит ВНЖ?',
                     'Подгорица сдается квартира 3 спальни 700 евро на год', 'Сдается офис 50 м2 в центре Бара 500 €',
                     'Посуточно апартаменты в Баре 40 евро', 'Сколько стоит аренда квартиры в Баре?',
                     'Ищу квартиру в аренду в Баре, 2 спальни', 'Хотим снять жилье в Баре', 'Rent a car Bar 30 EUR',
                     'Кто сдает квартиру в Баре?', 'Бар сдаем 2 спальни 900 €, депозит 1800 €']:
            self.assertEqual(verdict(text),'reject',text)
    def test_kept(self):
        for text in ['Ищете квартиру? Сдается в Баре, 2 спальни, 700 €', 'Ищем арендатора, Бар Шушань 2 спальни 700 €',
                     'Аренда и продажа недвижимости. Бар Шушань 2 спальни 700 €', 'Сдам квартиру в Баре, пишите в личку',
                     'Посуточно и долгосрочно, Бар центр 2 спальни 700 €', 'Сдается 2+1 в Баре, стиральная машина, гараж, 700 €',
                     'Бар Будва Тиват: сдается 2 спальни 700 €', 'Izdajem stan Bar 700 euro']:
            self.assertEqual(parse(text)['status'],'review',text)
    def test_bar_group_implies_city(self):
        text='Центр, сдается 2 спальни 700 €'
        self.assertEqual(verdict(text),'reject')
        self.assertEqual(verdict(text,True),'review')
        self.assertEqual(verdict('Будва сдается 2 спальни 700 €',True),'reject')

class PriceTests(unittest.TestCase):
    def test_neighbouring_numbers_not_glued(self):
        for text in ['2 спальни, 65 м2 700 €', 'этаж 3, 700 €', 'этаж 3 700 €', 'площадь 80\n700 €', 'Спальни: 2 700 €']:
            self.assertEqual(parse_prices(text),[700],text)
    def test_formats(self):
        for text,value in [('1 200 €',1200),('1.200 EUR',1200),('1,200 евро',1200),('150 000 €',150000),('700 euro',700),('700,50 €',700.5),('2 спальни 1 200 €',1200),('1 200,50 €',1200.5)]:
            self.assertEqual(parse_prices(text),[value],text)
    def test_area_does_not_reject_flat(self):
        p=parse('Бар Шушань аренда 2 спальни, 65 м2 700 €')
        self.assertEqual((p['status'],p['price']),('review',700))

class BedroomTests(unittest.TestCase):
    def test_counts(self):
        for text,value in [('2 спальни',2),('2-х спальная квартира',2),('2-bedroom',2),('две спальни',2),('трехспальная квартира',3),
                           ('1 спальня и 2 спальни',2),('одна спальня',1),('студия',1),('двуспальная кровать',None),
                           ('4 спальных места',None),('кухня-студия',None),('dvosoban',None),('trosoban',None),('jednosoban',1)]:
            self.assertEqual(parse('Бар аренда '+text)['bedrooms'],value,text)
    def test_studio_with_double_bed(self):
        self.assertEqual(verdict('Бар центр сдам студия, двуспальная кровать 500 €'),'reject')

class ScanWindowTests(unittest.TestCase):
    def test_window(self):
        now=datetime(2026,11,5,tzinfo=timezone.utc)
        ago=lambda **kw:(now-timedelta(**kw)).isoformat()
        self.assertEqual(scan_days('',45,3,now),45)
        self.assertEqual(scan_days(ago(minutes=10),45,3,now),3)
        self.assertEqual(scan_days(ago(days=10),45,3,now),11)
        self.assertEqual(scan_days(ago(days=100),45,3,now),45)
    def test_window_due(self):
        now=datetime(2026,11,5,tzinfo=timezone.utc)
        ago=lambda **kw:(now-timedelta(**kw)).isoformat()
        self.assertTrue(scan_due('',6,now))
        self.assertFalse(scan_due(ago(minutes=10),6,now))
        self.assertTrue(scan_due(ago(hours=6),6,now))

class StorageTests(unittest.TestCase):
    def test_last_id_per_source(self):
        with tempfile.TemporaryDirectory() as d:
            db=Store(d+'/test.db')
            now=datetime.now(timezone.utc).isoformat()
            self.assertEqual(db.last_id('aarenda'),0)
            db.put('aarenda',5,now,'один');db.put('aarenda',9,now,'два');db.put('saleme_realty',100,now,'три')
            self.assertEqual(db.last_id('aarenda'),9)
    def test_export_html(self):
        with tempfile.TemporaryDirectory() as d:
            db=Store(d+'/test.db')
            now=datetime.now(timezone.utc).isoformat()
            db.put('aarenda',7,now,'Бар. Полье, 2 спальни 700 € аренда на год <script>alert(1)</script>')
            db.ensure_user(1)
            rows=db.candidates(14,user=1)
            self.assertEqual(len(rows),1)
            page=export_html(rows,db.filters(1))
            self.assertIn('https://t.me/aarenda/7',page)
            self.assertIn('700 €',page)
            self.assertNotIn('<script>alert',page)
            self.assertIn('&lt;script&gt;',page)
            self.assertIn('id="toggle"',page)
            self.assertNotIn('id="toggle"',export_html([],db.filters(1)))
            self.assertIn('Подходящих объявлений нет',export_html([],db.filters(1)))
    def test_zaljevo_district(self):
        for text in ('Сдаются апартаменты в районе ZALJEVO, БАР. 2 спальни 400 €','Бар, Заљево, 2 спальни, 400 €','Бар, Залjево 2 спальни 400 €'.replace('j','ј'),'Бар, Zaljevo'):
            self.assertEqual(parse(text)['district'],'Заљево',text)
        self.assertNotEqual(parse('Бар, вид на залив, 2 спальни 400 € аренда')['district'],'Заљево')
    def test_parser_reports(self):
        with tempfile.TemporaryDirectory() as d:
            db=Store(d+'/test.db')
            now=datetime.now(timezone.utc).isoformat()
            db.put('aarenda',1,now,'Бар. Полье, 2 спальни 700 € аренда')
            pid=db.db.execute('SELECT id FROM posts').fetchone()['id']
            self.assertFalse(db.report(999,True))
            self.assertTrue(db.report(pid,True));db.report(pid,True)
            self.assertTrue(db.is_reported(pid))
            rows=db.reports(1)
            self.assertEqual(len(rows),1)
            self.assertEqual(json.loads(rows[0]['parsed_then'])['district'],'Полье')
            # Hiding for the owner and reporting are independent.
            db.ensure_user(1);db.action(1,pid,'hide')
            self.assertTrue(db.is_hidden(1,pid) and db.is_reported(pid))
            self.assertTrue(db.comment(pid,'район не тот'));db.comment(pid,'и цена')
            self.assertEqual(db.reports(1)[0]['comment'],'район не тот\nи цена')
            self.assertFalse(db.comment(999,'x'))
            db.report(pid,False)
            self.assertEqual(db.reports(1),[])
    def test_report_button_only_when_requested(self):
        self.assertEqual(len(card_buttons(1,False)[0]),1)
        self.assertEqual([b['callback_data'] for b in card_buttons(1,False,False)[0]],['hide:1','report:1'])
        self.assertEqual(card_buttons(1,True,True)[0][1]['callback_data'],'unreport:1')
    def test_source_and_reparse(self):
        with tempfile.TemporaryDirectory() as d:
            db=Store(d+'/test.db')
            now=datetime.now(timezone.utc).isoformat()
            text='Центр, сдается 2 спальни 700 €'
            db.put('realestate_bar',1,now,text);db.put('aarenda',2,now,text+' срочно')
            db.ensure_user(1);db.set_filters(1,BAR)
            self.assertEqual([r['source'] for r in db.candidates(user=1)],['realestate_bar'])
            self.assertEqual(len(db.candidates()),2)
            self.assertEqual(len(db.candidates(include_rejected=True)),2)
            db.db.execute("UPDATE posts SET parsed='{\"status\":\"reject\"}'");db.db.commit()
            self.assertEqual(db.candidates(),[])
            self.assertEqual(db.reparse(),2)
            self.assertEqual(len(db.candidates(user=1)),1)
            db.db.close()
    def test_updates_dedup_hidden(self):
        with tempfile.TemporaryDirectory() as d:
            db=Store(d+'/test.db')
            now=datetime.now(timezone.utc).isoformat()
            text='Бар Шушань аренда 2 спальни 700 €'
            db.put('a',1,now,text);db.put('b',2,now,text)
            self.assertEqual(len(db.candidates()),1)
            row=db.candidates()[0]
            db.ensure_user(7)
            db.mark_notified(7,row['id'],row['digest'])
            db.put(row['source'],row['message_id'],now,text+' на год')
            updated=db.db.execute('SELECT * FROM user_posts WHERE post_id=?',(row['id'],)).fetchone()
            self.assertIsNone(updated['notified'])
            db.action(7,row['id'],'hide')
            self.assertEqual(len(db.candidates(user=7)),1)
            db.db.close()

    def test_near_duplicates_same_author(self):
        with tempfile.TemporaryDirectory() as d:
            db=Store(d+'/test.db')
            now=datetime.now(timezone.utc).isoformat()
            base='Современный апартамент в городе Бар, сдам на год и более. Находится на 2 этаже дома. Гостиная с кухней, 2 спальни и санузл. Цена 500€ в месяц. Телефон +38200000000 Анна'
            me=(5,'Мария','maria')
            db.put('a',1,now,base,me);db.put('b',2,now,base+'Я',me)
            # Same template, but another price: a different flat, even from the same author.
            db.put('a',3,now,base.replace('500€','550€'),me)
            # Another author is never matched by similarity.
            db.put('c',4,now,base+'Ю',(6,'Другой','other'))
            self.assertEqual(sorted(r['message_id'] for r in db.candidates()),[1,3,4])
            post=lambda mid:db.db.execute('SELECT * FROM posts WHERE message_id=?',(mid,)).fetchone()
            db.ensure_user(7)
            db.mark_notified(7,post(1)['id'],post(1)['digest'])
            self.assertTrue(db.duplicate_notified(7,post(2)))
            self.assertFalse(db.duplicate_notified(7,post(3)))
            self.assertFalse(db.duplicate_notified(7,post(4)))
            db.db.close()

class PriceFormTests(unittest.TestCase):
    def test_forms(self):
        for text,expected in (('600е (1+1)',[600]),('Цена 600 е',[600]),('1000e + депозит',[1000]),('350е/мес + ку',[350]),('€750 +к/у',[750]),('€171 900',[171900]),('€ 500',[500]),
                              ('150 000 евр',[150000]),('350/мес +ку',[350]),('6000 в месяц',[6000]),('цена 350 + коммунальные',[350]),('💶 900',[900]),('💶600+комунальные',[600]),
                              ('стоимость аренды - 650 +ку',[650]),('цена: 500',[500])):
            self.assertEqual(parse_prices(text),expected,text)
    def test_not_prices(self):
        for text in ('квартира 45 евроремонт','3е пожилых родителей','до моря 100м','цена 2 спальни','площадь 56 м²','10 e-mail','10 минут в месяц'.replace('в месяц','')):
            self.assertEqual(parse_prices(text),[],text)

class BedroomFormTests(unittest.TestCase):
    def bedrooms(self,text): return parse('Бар аренда '+text)['bedrooms']
    def test_explicit_number(self):
        self.assertEqual(self.bedrooms('спальня -1 площадь-38м2'),1)
        self.assertEqual(self.bedrooms('спальня — 2'),2)
        self.assertEqual(self.bedrooms('спальня -0 площадь'),0)
        self.assertEqual(self.bedrooms('#спальни_2 #бар'),2)
        self.assertEqual(self.bedrooms('количество спален: 3'),3)
        self.assertIsNone(self.bedrooms('спальные места - 4'))
    def test_single_bedroom_with_living_room(self):
        self.assertEqual(self.bedrooms('Спальня, гостиная. Терраса'),1)
        self.assertEqual(self.bedrooms('кухня-гостиная+спальня, 3 этаж'),1)
    def test_doubt_stays_unknown(self):
        for text in ('Спальня, гостиная, детская','Спальня, гостиная, кабинет','Вторая спальня и гостиная','Спальня, гостиная, 3-комнатная','Спальня, терраса','Спальня и спальня, гостиная'):
            self.assertIsNone(self.bedrooms(text),text)

class CardTests(unittest.TestCase):
    def test_only_real_gaps_are_listed(self):
        p=parse('Бар годовая аренда 2 спальни 700 €')
        self.assertEqual(p['unknown'],[])
        self.assertEqual(parse('Бар аренда 2 спальни 700 €')['unknown'],[])
        self.assertIn('В посте несколько сумм — уточни цену',parse('Бар годовая аренда 2 спальни 700 € депозит 500 €')['unknown'])
    def test_hide_and_show_hidden(self):
        d=tempfile.TemporaryDirectory();self.addCleanup(d.cleanup)
        db=Store(d.name+'/t.db');self.addCleanup(db.db.close)
        now=datetime.now(timezone.utc).isoformat()
        db.put('a',1,now,'Бар годовая аренда 2 спальни 700 €');db.ensure_user(1)
        db.action(1,1,'hide')
        self.assertEqual(db.candidates(user=1),[])
        db.set_filters(1,apply_choice(db.filters(1),'set:h'))
        row=db.candidates(user=1)[0]
        self.assertEqual(row['hidden'],1)
        self.assertIn('Вы скрыли это объявление',card(row))
        self.assertEqual(card_buttons(1,row['hidden'])[0][0]['callback_data'],'unhide:1')
        # Hidden posts are never pushed, whatever the mode.
        self.assertEqual(db.pending(1,14,''),[])
        db.action(1,1,'unhide')
        self.assertEqual(db.candidates(user=1)[0]['hidden'],0)

class FilterTests(unittest.TestCase):
    def test_unknown_values_never_reject(self):
        strict={**DEFAULT_FILTERS,'price_max':100,'bedrooms_min':5}
        self.assertEqual(filter_reasons(parse('Бар аренда квартира'),strict),[])
    def test_incomplete_can_be_hidden(self):
        hide={**DEFAULT_FILTERS,'incomplete':False}
        self.assertEqual(filter_reasons(parse('Бар аренда квартира'),hide),['Нет цены или числа спален'])
        self.assertEqual(filter_reasons(parse('Бар аренда 2 спальни 700 €'),hide),[])
    def test_term(self):
        p=parse('Бар аренда 2 спальни 700 € до июня')
        self.assertEqual(filter_reasons(p,DEFAULT_FILTERS),['Сезонные ограничения'])
        self.assertEqual(filter_reasons(p,{**DEFAULT_FILTERS,'term':'any'}),[])
    def test_daily_stays_rejected_for_everyone(self):
        self.assertEqual(parse('Бар аренда квартира посуточно 2 спальни 50 €')['status'],'reject')
    def test_bar_district_spellings(self):
        self.assertEqual(parse('Сдам квартиру Бар.Белеши 2 спальни 550 евро годовая аренда')['district'],'Белиши')
        for spelling in ('Бьелиши','Белиши','Белеши','Бјелиши','Bjelisi'):
            self.assertEqual(parse(f'Сдаётся дом, район {spelling}, Бар. 4 спальни, 800 € аренда')['district'],'Белиши',spelling)
        self.assertEqual(parse('Дом в Баре, район Поле. 2 спальни. Цена: 700 евро, аренда')['district'],'Полье')
        self.assertIsNone(parse('Сдам квартиру в Баре, рядом поле для футбола. 2 спальни 500 евро аренда')['district'])
    def test_canj_and_kamenovo(self):
        p=parse('Сдам квартиру в Чане, 2 спальни, 450 евро, долгосрочная аренда')
        self.assertEqual((p['district'],p['cities']),('Чань',['Бар']))
        p=parse('Сдается квартира Каменово, 2 спальни, 600 евро, аренда на год')
        self.assertEqual((p['district'],p['cities']),('Каменово',['Будва']))
    def test_specific_district_beats_center_mention(self):
        p=parse('#сдам #бар 📍Бар. Полье. Дом, 4 спальных комнат. Аренда в месяц: 700 €. До центра Бара 3 мин на машине')
        self.assertEqual(p['district'],'Полье')
    def test_studio_is_one_bedroom(self):
        for text in ('Сдам квартира-студия в Баре 400 евро аренда','Сдается студия 30 м2, аренда 350 €','Апартаменты-студия, сдам, 400 €'):
            self.assertEqual(parse(text)['bedrooms'],1,text)
        for text in ('Сдаются квартиры-студии в Баре, 250 €, аренда','Сдаём студии в Баре 300 € аренда','Сдам несколько студий в Баре 300 € аренда'):
            self.assertEqual(parse(text)['bedrooms'],1,text)
        self.assertIsNone(parse('Сдам квартиру в Баре, кухня-студия, 400 €, аренда')['bedrooms'])
        self.assertIsNone(parse('Сдам квартиру в Баре, гостиная-кухня-студия, 400 €, аренда')['bedrooms'])
    def test_distance_to_center_is_not_center(self):
        p=parse('Бар. Полье. Дом, 4 спальни. Аренда 700 €. До центра Бара 3 мин на машине')
        self.assertEqual(p['areas'],{'Бар':['Полье']})
        self.assertEqual(filter_reasons(p,{**DEFAULT_FILTERS,'districts':{'Бар':['Центр']}}),['Другой район'])
        self.assertEqual(parse('Сдам в центре Бара 2 спальни 500 €, 5 минут до моря, аренда')['district'],'Центр')
        self.assertEqual(parse('Сдам в центре Бара, от центра до Шушани 2 км, 2 спальни 500 €, аренда')['areas'],{'Бар':['Шушань','Центр']})
    def test_bed_size_is_not_bedroom_count(self):
        text='Бар. Илино. 2 этаж. Квартира с отдельной спальней: 2-х спальная кровать и шкаф. Светлая гостиная. 12 месяцев по 399 евро'
        self.assertEqual(parse(text)['bedrooms'],1)
        self.assertEqual(parse('Сдам, спальня: 2-х спальная кровать, гостиная, 400 евро аренда')['bedrooms'],None)
        self.assertEqual(parse('Сдам квартиру, спальни: 2, 400 евро, аренда')['bedrooms'],2)
    def test_one_room_is_one_bedroom(self):
        for text in ('Сдаются однокомнатные апартаменты в Баре, 500 € аренда','Сдаётся 1-комнатная квартира 44 м² в Баре, 600 € аренда','Сдам 1к квартиру в Баре 450 € аренда','Сдаётся новая однокомнатная квартира, 500 € в месяц'):
            self.assertEqual(parse(text)['bedrooms'],1,text)
        self.assertIsNone(parse('Сдам квартиру в Баре, 11-комнатная, 600 € аренда, до моря 1 км')['bedrooms'])
    def test_labelled_rent_among_several_amounts(self):
        p=parse('Сдается 2-комнатный этаж в Баре, долгосрочная аренда\n• Аренда: 400 € в месяц\n• Коммунальные услуги: + 50 €')
        self.assertEqual((p['price'],p['min_price']),(400,400))
        self.assertNotIn('В посте несколько сумм — уточни цену',p['unknown'])
        # Deposit next to the rent is not the rent; unlabelled sums stay ambiguous.
        p=parse('Сдам 2 спальни в Баре, аренда на год\nАренда 500 €/мес, депозит 500 € и 100 € за уборку')
        self.assertEqual(p['price'],500)
        p=parse('Сдам 2 спальни в Баре, аренда на год. 450 € и 600 € в зависимости от этажа')
        self.assertIsNone(p['price'])
        self.assertIn('В посте несколько сумм — уточни цену',p['unknown'])
    def test_annual_term_wordings(self):
        for text in ('Сдам 2 спальни в Баре. 12 месяцев по 450 евро','Сдам 2 спальни в Баре, 450 €, срок аренды от 1 года','Сдам 2 спальни в Баре, 450 € на 1 год'):
            self.assertTrue(parse(text)['annual'],text)
        self.assertFalse(parse('Сдам 2 спальни в Баре, 450 €, 112 месяцев не нужно')['annual'])
    def test_center_word_is_not_always_the_district(self):
        for text in ('Район Белиши. 2 спальни, 500 €, аренда. Центральное отопление и охлаждение','Район Белиши. 2 спальни, 500 €, аренда. Рядом торговый центр и ТЦ','Белиши, Бар, 2 спальни, 500 €, аренда, рядом спортивно-оздоровительный центр'):
            self.assertEqual(parse(text)['areas'],{'Бар':['Белиши']},text)
        self.assertEqual(parse('Квартира в центре Бара, 2 спальни, 500 €, аренда, центральное отопление')['areas'],{'Бар':['Центр']})
    def test_adjective_between_number_and_bedrooms(self):
        self.assertEqual(parse('Сдаётся дом в Баре. В доме: 4 просторные спальни; 3 ванные комнаты. 800 € аренда')['bedrooms'],4)
        self.assertEqual(parse('Сдам в Баре 2 отдельные спальни, 500 € аренда')['bedrooms'],2)
    def test_term_price_is_rent(self):
        p=parse('Аренда в Баре, 2 спальни. 12 месяцев по 399 евро. +50 евро (все коммунальные включены)')
        self.assertEqual((p['price'],p['min_price']),(399,399))
        p=parse('Аренда в Баре, 2 спальни. 399 евро и 50 евро коммунальные')
        self.assertIsNone(p['price'])
    def test_term_wordings_extended(self):
        for text in ('Сдам 2 спальни в Баре 450 €, долгосрочная аренда','Сдам 2 спальни в Баре 450 €, на длительный срок','Сдам 2 спальни в Баре 450 €, аренда на один год','#на_годовую_аренду Бар 2 спальни 450 €'):
            p=parse(text)
            self.assertTrue(p['annual'] and not p['seasonal'],text)
        for text in ('Сдам 2 спальни в Баре 450 €, аренда до начала сезона','#арендалето Бар 2 спальни 450 € сдам','Сдам 2 спальни в Баре 450 €, на 3 месяца','Сдам 2 спальни в Баре 450 €, срок: на 2 или 3 месяца'):
            self.assertTrue(parse(text)['seasonal'],text)
        # A post offering a year as well is not rejected by the softer signs.
        self.assertFalse(parse('Сдам 2 спальни в Баре 450 €, на год, либо на 3 месяца дороже')['seasonal'])
    def test_card_shows_term(self):
        def row(text): return dict(id=1,author_name=None,author_id=None,author_username=None,links='[]',hidden=0,published='2026-11-05',source='x',message_id=1,text=text,parsed=json.dumps(parse(text)))
        self.assertIn('срок: год',card(row('Бар 2 спальни 450 € аренда на год')).split('\n')[0])
        self.assertIn('срок: сезон',card(row('Бар 2 спальни 450 € аренда до июня')).split('\n')[0])
        self.assertIn('срок: ?',card(row('Бар 2 спальни 450 € аренда')).split('\n')[0])
    def test_term_modes(self):
        year=parse('Бар 2 спальни 450 € аренда на год');season=parse('Бар 2 спальни 450 € аренда до июня');unknown=parse('Бар 2 спальни 450 € аренда')
        mode=lambda term:{**DEFAULT_FILTERS,'term':term}
        self.assertEqual([filter_reasons(p,mode('annual')) for p in (year,season,unknown)],[[],['Сезонные ограничения'],[]])
        self.assertEqual([filter_reasons(p,mode('season')) for p in (year,season,unknown)],[['Не сезонная аренда'],[],['Не сезонная аренда']])
        self.assertEqual([filter_reasons(p,mode('any')) for p in (year,season,unknown)],[[],[],[]])
    def test_any_named_district_passes(self):
        p=parse('Бар аренда 2 спальни 700 €, Шушань, рядом центр')
        self.assertEqual(filter_reasons(p,{**DEFAULT_FILTERS,'districts':{'Бар':['Центр']}}),[])
        self.assertEqual(filter_reasons(p,{**DEFAULT_FILTERS,'districts':{'Бар':['Ильино']}}),['Другой район'])
    def test_district_narrows_only_its_city(self):
        f={**DEFAULT_FILTERS,'cities':['Бар','Будва'],'districts':{'Бар':['Шушань']}}
        self.assertEqual(filter_reasons(parse('Будва, Розино аренда 2 спальни 700 €'),f),[])
        self.assertEqual(filter_reasons(parse('Бар, Ильино аренда 2 спальни 700 €'),f),['Другой район'])
        self.assertEqual(filter_reasons(parse('Бар аренда 2 спальни 700 €'),f),[])
    def test_center_belongs_to_the_named_city(self):
        p=parse('Будва центр аренда 2 спальни 700 €')
        self.assertEqual(p['areas'],{'Будва':['Центр']})
        self.assertEqual(parse('центр аренда 2 спальни 700 €')['areas'],{})
    def test_district_identifies_city(self):
        self.assertEqual(parse('Сдам квартиру в Игало, 2 спальни 700 €')['cities'],['Херцег-Нови'])
        self.assertEqual(parse('Сдам квартиру в Розино, 2 спальни 700 €')['areas'],{'Будва':['Розино']})
    def test_words(self):
        f={**DEFAULT_FILTERS,'include':['кот']}
        p=parse('x')
        self.assertEqual(filter_reasons(p,f,'Можно с котом'),[])
        self.assertEqual(filter_reasons(p,f,'Квартира, которая рядом'),['Нет нужных слов'])
        self.assertEqual(filter_reasons(p,{**DEFAULT_FILTERS,'exclude':['агентство']},'Агентство недвижимости'),['Есть запрещённые слова'])
    def test_cities(self):
        budva=parse('Будва аренда 2 спальни 700 €')
        self.assertEqual(budva['status'],'review')
        self.assertEqual(filter_reasons(budva,DEFAULT_FILTERS),[])
        self.assertEqual(filter_reasons(budva,{**DEFAULT_FILTERS,'cities':['Будва','Тиват']}),[])
        self.assertEqual(filter_reasons(budva,BAR),['Другой город'])
        self.assertEqual(filter_reasons(parse('аренда 2 спальни 700 €'),BAR),['Город не указан'])
        self.assertEqual(filter_reasons(parse('аренда 2 спальни 700 €'),DEFAULT_FILTERS),[])
        self.assertEqual(filter_reasons(parse('Шушань аренда 2 спальни 700 €'),BAR),[])
        both=parse('Бар и Будва аренда 2 спальни 700 €')
        self.assertEqual(filter_reasons(both,BAR),[])
        self.assertIn('Упоминается несколько городов — проверь географию',both['unknown'])

class MenuTests(unittest.TestCase):
    def datas(self,rows): return [b['callback_data'] for row in rows for b in row]
    def test_every_button_fits_telegram_and_is_handled(self):
        f={**DEFAULT_FILTERS,'cities':list(DISTRICTS)}
        for screen in ('main','cities','price','bedrooms','districts','words','pagesize'):
            for setup in (False,True):
                _,rows=menu(screen,f,3,setup)
                for data in self.datas(rows):
                    self.assertLessEqual(len(data.encode()),64)
                    self.assertTrue(data=='show' or data.split(':')[0] in ('menu','set','ask'),data)
                    if data.startswith('set:') and data!='set:w:clear': self.assertIsInstance(apply_choice(f,data),dict)
    def test_page_size(self):
        self.assertEqual(DEFAULT_FILTERS['page_size'],5)
        f=DEFAULT_FILTERS
        self.assertEqual(apply_choice(f,'set:s:20')['page_size'],20)
        self.assertEqual(apply_choice(f,'set:s:51'),f)
        self.assertEqual(apply_text(f,'page',' 50 ')['page_size'],50)
        for bad in ('0','51','много','-3'):
            with self.assertRaises(ValueError): apply_text(f,'page',bad)
    def test_page_size_keeps_notify_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            db=Store(d+'/test.db');db.ensure_user(1)
            before=db.user(1)['notify_after']
            db.set_filters(1,{**db.filters(1),'page_size':10})
            self.assertEqual((db.user(1)['notify_after'],db.filters(1)['page_size']),(before,10))
            db.set_filters(1,{**db.filters(1),'price_max':900})
            self.assertGreater(db.user(1)['notify_after'],before)
    def test_setup_has_no_way_back(self):
        self.assertIn('menu:price',self.datas(menu('cities',DEFAULT_FILTERS,setup=True)[1]))
        self.assertNotIn('menu:main',self.datas(menu('price',DEFAULT_FILTERS,setup=True)[1]))
        self.assertIn('menu:main',self.datas(menu('price',DEFAULT_FILTERS)[1]))
    def test_choices(self):
        f=DEFAULT_FILTERS;bar=CITY_NAMES.index('Бар');budva=CITY_NAMES.index('Будва')
        f=apply_choice(f,f'set:c:{budva}');f=apply_choice(f,f'set:c:{bar}')
        self.assertEqual(f['cities'],['Бар','Будва'])
        self.assertEqual(apply_choice(f,'set:p:off')['price_max'],None)
        self.assertEqual(apply_choice(f,'set:b:3')['bedrooms_min'],3)
        self.assertEqual(apply_choice(f,'set:t')['term'],'season')
        self.assertEqual(apply_choice(apply_choice(f,'set:t'),'set:t')['term'],'any')
        self.assertEqual(apply_choice(apply_choice(apply_choice(f,'set:t'),'set:t'),'set:t')['term'],'annual')
        self.assertFalse(apply_choice(f,'set:i')['incomplete'])
        shushan=[n for n,_ in DISTRICTS['Бар']].index('Шушань')
        f=apply_choice(f,f'set:d:{bar}:{shushan}')
        self.assertEqual(f['districts'],{'Бар':['Шушань']})
        self.assertEqual(apply_choice(f,f'set:d:{bar}:{shushan}')['districts'],{})
        # Dropping a city drops its districts, so no hidden limit remains.
        self.assertEqual(apply_choice(f,f'set:c:{bar}')['districts'],{})
        self.assertEqual(apply_choice(f,'set:c:all')['cities'],[])
        for stale in ('set:c:99','set:d:99:0','set:p:много'):
            self.assertEqual(apply_choice(f,stale),f)
    def test_typed_values(self):
        f=DEFAULT_FILTERS
        self.assertEqual(apply_text(f,'price','950 €')['price_max'],950)
        self.assertEqual(apply_text(f,'include','Кот, ПАРКОВКА, кот')['include'],['кот','парковка'])
        for kind,text in (('price','дорого'),('price','0'),('include',' , '),('exclude','x'*41)):
            with self.assertRaises(ValueError): apply_text(f,kind,text)

class MessageFieldsTests(unittest.TestCase):
    def test_author(self):
        person=NS(first_name='Ана',last_name=None,username='ana')
        self.assertEqual(message_author(NS(sender=person,sender_id=5)),(5,'Ана','ana'))
        self.assertEqual(message_author(NS(sender=NS(title='Агентство',username=None),sender_id=-9)),(-9,'Агентство',None))
        self.assertEqual(message_author(NS(sender=None,sender_id=None)),(None,None,None))
    def test_hidden_links(self):
        msg=NS(entities=[NS(url='https://a.example'),NS(url='https://a.example'),NS(user_id=42),NS(offset=0)],
               reply_markup=NS(rows=[NS(buttons=[NS(url='https://b.example'),NS(type=NS(url='https://c.example')),NS(text='нет ссылки')])]))
        self.assertEqual(message_links(msg),['https://a.example','tg://user?id=42','https://b.example','https://c.example'])
        self.assertEqual(message_links(NS(entities=None,reply_markup=None)),[])

class UserTests(unittest.TestCase):
    TEXT='Бар центр аренда 2 спальни 700 €'
    def store(self):
        d=tempfile.TemporaryDirectory();self.addCleanup(d.cleanup)
        db=Store(d.name+'/test.db');self.addCleanup(db.db.close)
        return db
    def test_filters_and_hiding_are_per_user(self):
        db=self.store();now=datetime.now(timezone.utc).isoformat()
        db.put('a',1,now,self.TEXT)
        db.ensure_user(1);db.ensure_user(2)
        db.set_filters(2,apply_text(db.filters(2),'price','500'))
        self.assertEqual(len(db.candidates(user=1)),1)
        self.assertEqual(db.candidates(user=2),[])
        pid=db.candidates(user=1)[0]['id']
        db.ensure_user(3);db.action(1,pid,'hide')
        self.assertEqual(db.candidates(user=1),[])
        self.assertEqual(len(db.candidates(user=3)),1)
    def test_stats_and_blocking(self):
        db=self.store()
        db.ensure_user(1);db.add_user(2);db.add_user(3)
        db.set_user(1,started=1);db.set_user(2,started=1,paused=1)
        db.set_filters(1,BAR)
        db.set_blocked(3,True);db.set_blocked(4,True)
        s=db.user_stats()
        self.assertEqual((s['total'],s['blocked'],s['started'],s['notified'],s['new_week']),(4,2,2,1,4))
        self.assertEqual(s['cities'],[('Бар',1),('любой',1)])
        db.set_blocked(3,False)
        self.assertEqual(db.user(3)['blocked'],0)
    def test_parser_change_recalculates_stored_posts(self):
        d=tempfile.TemporaryDirectory();self.addCleanup(d.cleanup)
        now=datetime.now(timezone.utc).isoformat()
        db=Store(d.name+'/t.db');db.put('a',1,now,self.TEXT)
        db.db.execute("UPDATE posts SET parsed='{}'");db.set('parse_version','1');db.db.close()
        db=Store(d.name+'/t.db');self.addCleanup(db.db.close)
        self.assertEqual(json.loads(db.db.execute('SELECT parsed FROM posts').fetchone()[0])['cities'],['Бар'])
    def test_new_user_gets_only_new_posts(self):
        db=self.store();old=datetime.now(timezone.utc).isoformat()
        db.put('a',1,old,self.TEXT)
        db.add_user(1);boundary=db.user(1)['notify_after']
        # A recheck of an unchanged post refreshes `seen` and must not look new.
        db.put('a',1,old,self.TEXT)
        self.assertEqual(db.pending(1,14,boundary),[])
        db.put('a',2,old,self.TEXT+' на год')
        pending=db.pending(1,14,boundary)
        self.assertEqual([r['message_id'] for r in pending],[2])
        db.mark_notified(1,pending[0]['id'],pending[0]['digest'])
        self.assertEqual(db.pending(1,14,boundary),[])
        # An edit is delivered again.
        db.put('a',2,old,self.TEXT+' на год, с декабря')
        self.assertEqual(len(db.pending(1,14,boundary)),1)
    def test_author_and_links_fill_in_on_recheck(self):
        db=self.store();now=datetime.now(timezone.utc).isoformat()
        db.put('a',1,now,self.TEXT);db.ensure_user(1)
        db.mark_notified(1,1,db.candidates(user=1)[0]['digest'])
        db.put('a',1,now,self.TEXT,(5,'Ана','ana'),['https://a.example'])
        row=db.candidates(user=1)[0]
        self.assertEqual(row['notified'],row['digest'])
        text=card(row)
        self.assertIn('Автор: Ана (@ana)',text)
        self.assertIn('https://a.example',text)
    def test_upgrade_of_single_user_database(self):
        d=tempfile.TemporaryDirectory();self.addCleanup(d.cleanup)
        path=d.name+'/old.db'
        old=sqlite3.connect(path)
        now=datetime.now(timezone.utc).isoformat()
        old.executescript('''CREATE TABLE posts(id INTEGER PRIMARY KEY, source TEXT, message_id INTEGER, published TEXT,
          seen TEXT, text TEXT, digest TEXT, parsed TEXT, hidden INTEGER DEFAULT 0,
          confirmed TEXT, notified TEXT, UNIQUE(source,message_id));
          CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);''')
        old.execute("INSERT INTO posts(source,message_id,published,seen,text,digest,parsed,hidden,notified) VALUES('a',1,?,?,?,'d','{}',1,'d')",(now,now,self.TEXT))
        old.executemany('INSERT INTO meta VALUES(?,?)',[('scanned:a',now),('started','1'),('paused','1'),('page','3'),('notify_after',now),('offset','5')])
        old.commit();old.close()
        db=Store(path);self.addCleanup(db.db.close)
        self.assertEqual(db.get('scanned:a'),'')
        self.assertEqual(db.db.execute('SELECT changed FROM posts').fetchone()[0],now)
        self.assertEqual(json.loads(db.db.execute('SELECT parsed FROM posts').fetchone()[0])['price'],700)
        db.adopt_owner(9)
        u=db.user(9)
        self.assertEqual((u['started'],u['paused'],u['page'],u['notify_after']),(1,1,3,''))
        self.assertEqual(db.filters(9)['cities'],['Бар'])
        self.assertEqual(db.db.execute('SELECT filters FROM users WHERE id=9').fetchone()[0].count('Бар'),1)
        self.assertEqual(db.db.execute('SELECT hidden,notified FROM user_posts WHERE user_id=9').fetchone()[:],(1,'d'))
        self.assertEqual(db.candidates(user=9),[])
        self.assertEqual(db.get('offset'),'5')
        db.adopt_owner(9)
        self.assertNotIn('hidden',db.post_columns())

if __name__=='__main__': unittest.main()
