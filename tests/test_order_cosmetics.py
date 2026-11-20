import asyncio
from html.parser import HTMLParser

import pytest

from proxima_bot.order_templates import template_rows
from proxima_bot.operations import load_spec
from test_api_coverage import sample
from test_extended import approved_order_data, new_app
from test_guided_order_form import options, click
from test_order_flows import OrderProvider, SCENARIOS
from test_release_flows import TelegramTransport


class BasicMessage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.visible=[]
        self.stack=[]

    def handle_starttag(self,tag,attrs):
        assert tag in {'b','code','i','a','pre','u','s','blockquote','tg-spoiler'}
        self.stack.append(tag)

    def handle_endtag(self,tag):
        assert self.stack.pop()==tag

    def handle_data(self,value):
        self.visible.append(value)


@pytest.mark.parametrize('mode',list(SCENARIOS))
@pytest.mark.parametrize('rich',[True,False])
def test_every_order_confirmation_has_safe_readable_rich_and_basic_messages(mode,rich):
    async def run():
        app=new_app()
        app.api=OrderProvider()
        try:
            await options(app,mode)
            await click(app,'wiz:quick:')
            draft=app.db.draft(7)
            data=draft['data']
            data['name']='Заказ <тест> & проверка'
            for field in ('comments','replies','keywords'):
                if data.get(field):data[field]=['Текст <>& '+('длинная строка '*40)]*10
            if mode in ('publish','reply_links','keyword_reply'):
                data=approved_order_data(app,7,mode,data)
            app.save_wizard_step(7,mode,'review',data)
            app.tg=TelegramTransport(rich)
            await app.preview(7)
            method,payload=app.tg.calls[-1]
            assert method==('sendRichMessage' if rich else 'sendMessage')
            message=payload['rich_message']['html'] if rich else payload['text']
            assert '&lt;тест&gt; &amp; проверка' in message
            if not rich:
                assert payload['parse_mode']=='HTML'
                parser=BasicMessage()
                parser.feed(message)
                assert not parser.stack
                assert len(''.join(parser.visible).encode('utf-16-le'))//2<=4096
                if any(data.get(field) for field in ('comments','replies','keywords')):
                    assert 'Показаны первые 3 строки.' in message
            for row in payload['reply_markup']['inline_keyboard']:
                assert 1<=len(row)<=4
                assert all(len(button['callback_data'].encode())<=64 for button in row)
        finally:app.db.close()
    asyncio.run(run())


def test_numeric_editor_shows_limits_and_plain_number_buttons():
    async def run():
        app=new_app()
        try:
            await options(app,'publish')
            await click(app,'wiz:form_likesPerComment:')
            _,text,markup=app.tg.out[-1]
            assert '0–100' in text and 'Сейчас' in text
            presets=[button['text'] for row in markup['inline_keyboard'] for button in row
                     if button['callback_data'].startswith('wiz:value_')]
            assert presets and all(label.isdecimal() for label in presets)
        finally:app.db.close()
    asyncio.run(run())


def test_deferred_template_does_not_display_unsaved_default_quantities():
    rows=template_rows('publish',{'name':'Шаблон','comments':['Текст'],
                                'options':{},'ask_fields':['options']})
    assert ('Объём и действия','Выбор при запуске') in rows
    assert not any(label in ('Комментариев на видео','ИИ-уникализация') for label,_ in rows)


def test_media_account_editor_keeps_instructions_and_current_choice_for_large_pools():
    async def run():
        app=new_app()
        app.tg=TelegramTransport(False)
        settings=sample(load_spec()['components']['schemas']['PostSettings'])
        settings.update(accountIds=['account-'+str(i)+'x'*100 for i in range(300)],allAccounts=False)
        app.db.save_draft(99,'media_order','review',{'token':'token','id':'media',
                           'name':'Публикация','kind':'video','settings':settings,'patch':{},'field':'accountIds'})
        try:
            await app.media_order_screen(99)
            method,payload=app.tg.calls[-1]
            assert method=='sendMessage' and payload['parse_mode']=='HTML'
            assert 'Пришли ID аккаунтов' in payload['text']
            assert 'Ещё аккаунтов: 290' in payload['text']
            assert len(payload['text'])<4096
        finally:app.db.close()
    asyncio.run(run())
