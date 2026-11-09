import asyncio
from proxima_bot.api import Telegram, TelegramError


class Recorder(Telegram):
    def __init__(self, fail_edit=None):
        super().__init__(None, 'x')
        self.calls = []
        self.fail_edit = fail_edit

    async def call(self, method, data, *, timeout=25):
        self.calls.append((method, data))
        if method == 'editMessageText' and self.fail_edit:
            raise TelegramError(400, self.fail_edit)
        return {'message_id': data.get('message_id', 99), 'chat': {'id': data['chat_id']}}


def run(coro):
    return asyncio.run(coro)


def test_first_message_after_button_edits_then_sends_new():
    async def scenario():
        tg = Recorder()
        token = tg.edit_in_place(1, 10)
        await tg.send(1, 'menu', {'inline_keyboard': []})
        await tg.send(1, 'second')
        await tg.send(2, 'other user')
        tg.reset_edit(token)
        await tg.send(1, 'outside button')
        return [m for m, _ in tg.calls]
    assert run(scenario()) == ['editMessageText', 'sendMessage', 'sendMessage', 'sendMessage']


def test_reply_keyboard_and_failed_edit_fall_back_to_new_message():
    async def scenario():
        tg = Recorder()
        token = tg.edit_in_place(1, 10)
        await tg.send(1, 'kb', {'keyboard': [[{'text': 'Главное меню'}]]})
        tg.reset_edit(token)
        bad = Recorder(fail_edit="message can't be edited")
        token = bad.edit_in_place(1, 10)
        await bad.rich(1, '<p>x</p>', 'x')
        bad.reset_edit(token)
        return [m for m, _ in tg.calls], [m for m, _ in bad.calls]
    first, second = run(scenario())
    assert first == ['sendMessage']
    assert second == ['editMessageText', 'editMessageText', 'sendRichMessage']


def test_not_modified_counts_as_success_and_notifies_listeners():
    async def scenario():
        tg = Recorder(fail_edit='Bad Request: message is not modified')
        seen = []
        tg.edit_listeners.append(lambda chat, mid: seen.append((chat, mid)))
        token = tg.edit_in_place(1, 10)
        result = await tg.send(1, 'same')
        tg.reset_edit(token)
        return result, seen, [m for m, _ in tg.calls]
    result, seen, calls = run(scenario())
    assert result['message_id'] == 10 and seen == [(1, 10)] and calls == ['editMessageText']
