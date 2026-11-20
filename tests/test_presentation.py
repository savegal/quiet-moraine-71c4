import json

from proxima_bot.presentation import (
    BUTTON_SYMBOLS, ERROR_LABELS, REPLY_CHECK_NOTE, REPORT_REASONS,
    days_label, pagination, report_view, keyboard, h, task_rich, task_view,
)
from proxima_bot.bot import links,safe_api_error
import pytest


def test_rich_report_html_table_and_sanitization():
    task={'display_name':'<script>alert(1)</script>','task_id':'deadbeef','state':'completed'}
    report={'comments':{'published':7,'attempts':10,'texts':[{'text':'<b>injection</b>','published':7,'attempts':10}]},
            'replies':{'published':0,'attempts':5,'texts':[]},'likes':{'confirmed':2,'total':4},'web':{'publications':3}}
    rich,plain=report_view(task,report)
    assert '<table bordered striped compact>' in rich
    assert '&lt;script&gt;' in rich
    assert '<script>' not in rich
    assert '&lt;b&gt;injection&lt;/b&gt;' in rich
    assert '7 / 10' in rich
    assert '7 / 10' in plain
    assert all(REPLY_CHECK_NOTE in view for view in (rich,plain))


@pytest.mark.parametrize(('kind','payload','snapshot','show_note'), [
    ('publish',{'assets':{'replyTexts':['Ответ']}},{'published':0},True),
    ('publish',{'replyLinks':['video:comment']},{'published':0},True),
    ('publish',{'sourceJob':'collection'},{'published':0},True),
    ('publish',{}, {'settings':{'repliesPerVideo':1}},True),
    ('keyword_reply',{}, {},True),
    ('publish',{'assets':{'commentTexts':['Комментарий']}},{'settings':{'repliesPerVideo':1}},False),
    ('fetch',{'settings':{'includeReplies':True}}, {'collected':2},False),
])
def test_reply_check_notice_for_cards_and_reports(kind,payload,snapshot,show_note):
    task={'display_name':'Задача','task_id':'abcdef12','state':'completed','kind':kind,
          'payload_json':json.dumps(payload),'snapshot_json':json.dumps(snapshot)}
    views=(task_view(task),task_rich(task),*report_view(task,{'replies':{'published':0,'attempts':0}}))
    assert all((REPLY_CHECK_NOTE in view)==show_note for view in views)


def test_keyboard_colors_and_safety():
    k=keyboard([('Do it','submit','success')],[('Stop','stop:12345678','danger')])
    assert k['inline_keyboard'][0][0]['style']=='success'
    assert k['inline_keyboard'][1][0]['style']=='danger'
    assert all(b['text'][0] in BUTTON_SYMBOLS for row in k['inline_keyboard'] for b in row)
    with pytest.raises(ValueError):keyboard([('x','x'*65,None)])


def test_pagination_and_subscription_labels():
    assert days_label(1)=='1 день'
    assert days_label(2)=='2 дня'
    assert days_label(11)=='11 дней'
    assert days_label(21)=='21 день'
    assert keyboard([],pagination('list:',1,True))['inline_keyboard'][0][1]['text']=='→ Далее'


def test_link_validation():
    assert links('https://vt.tiktok.com/ABC123/\n7420000000000000000')==['https://vt.tiktok.com/ABC123/','7420000000000000000']
    with pytest.raises(ValueError):links('https://localhost/private')
    with pytest.raises(ValueError):links('foo')


def test_no_raw_upstream_error_to_worker():
    assert 'Tokora' not in safe_api_error('api_key_invalid')
    assert 'crash: tokora secret' not in safe_api_error('crash: tokora secret')


def test_task_view_escapes_name():
    obj={'display_name':'<a href="evil">','task_id':'12345678','state':'queued','snapshot_json':'{}','error_code':''}
    assert '<a href="evil">' not in task_view(obj)


def test_order_card_separates_confirmed_comments_and_replies_from_processed_actions():
    task={'display_name':'Публикация','task_id':'abcd1234','kind':'publish','state':'running',
          'snapshot_json':json.dumps({'state':'running','phase':'likes','done':160,'total':160,
              'published':17,'attempts':274,'settings':{'commentsPerVideo':80,'repliesPerVideo':80},
              'awemes':[{'comments':17,'replies':0}]}),'error_code':''}
    rich=task_rich(task)
    assert '<td>Комментарии</td><td>17</td><td>80</td>' in rich
    assert '<td>Ответы</td><td>0</td><td>80</td>' in rich
    assert 'Лайки комментариев' in rich and 'Цель выполнена' not in rich
    assert 'попытки отправки' in rich
    assert 'Комментарии' in task_view(task) and '274' in task_view(task)


@pytest.mark.parametrize(('state','upstream_state','outcome'), [
    ('completed','completed_with_errors','Завершено с ошибками'),
    ('failed','interrupted','Выполнение прервано'),
])
def test_execution_limit_and_actual_outcome_are_visible_above_metrics(state,upstream_state,outcome):
    task={'display_name':'Публикация','task_id':'abcd1234','kind':'publish','state':state,
          'snapshot_json':json.dumps({'state':upstream_state,'executionTimeLimit':True,
                                      'executionLimitSeconds':1200,'done':9,'total':10})}
    for view in (task_rich(task),task_view(task)):
        assert view.index(outcome) < view.index('Достигнут лимит выполнения (20 минут)')
        assert view.index('Достигнут лимит выполнения (20 минут)') < view.index('Действий обработано')


def test_error_labels_and_cards_do_not_expose_upstream_brand():
    for mapping in (ERROR_LABELS, REPORT_REASONS):
        for code, text in mapping.items():
            assert 'tokora' not in text.casefold()
            assert 'токор' not in text.casefold()
    for code in ('external_pool_unknown', 'invalid_target', 'account_folder_mismatch', 'account_check_unavailable'):
        task = {'display_name': 'Заказ', 'task_id': 'abcd1234', 'kind': 'publish', 'state': 'queued',
                'snapshot_json': '{}', 'error_code': code}
        for rendered in (task_view(task), task_rich(task)):
            assert 'tokora' not in rendered.casefold()
            assert ERROR_LABELS[code] in rendered
