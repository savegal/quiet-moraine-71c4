from proxima_bot.presentation import BUTTON_SYMBOLS, days_label, pagination, report_view, keyboard, h, task_view
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
