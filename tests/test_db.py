import json
import pytest
from proxima_bot.db import Store

@pytest.fixture
def db():
    x=Store(':memory:')
    yield x
    x.close()


def payload(ids):
    return {'assets':{'accountIds':ids},'awemeIds':['7420000000000000000']}


def test_subscription_and_isolation(db):
    db.assign(1,'acc-A')
    db.assign(2,'acc-B')
    assert not db.is_active(1,99)
    db.grant(1,30)
    assert db.is_active(1,99)
    assert db.is_active(99,99)
    a=db.queue_task(1,'Мой тест','publish',payload(['acc-A']))
    assert db.task(a,1) is not None
    assert db.task(a,2) is None
    with pytest.raises(ValueError):db.queue_task(1,'Чужой','publish',payload(['acc-B']))
    assert db.next_eligible(99)['task_id']==a


def test_reassignment_does_not_steal_active_pool(db):
    db.assign(1,'acc-A');db.grant(1,30)
    tid=db.queue_task(1,'test','publish',payload(['acc-A']))
    with pytest.raises(ValueError):db.assign(2,'acc-A')
    assert db.account_ids(1)==['acc-A']
    assert db.cancel_queued(tid,1)
    db.assign(2,'acc-A')
    assert db.account_ids(1)==[]
    assert db.account_ids(2)==['acc-A']


def test_queue_idempotent_request_id_on_restart(db):
    db.assign(1,'acc-A');db.grant(1,30)
    tid=db.queue_task(1,'test','publish',payload(['acc-A']))
    first=json.loads(db.task(tid)['payload_json'])['requestId']
    db.transition(tid,'starting')
    db.recover()
    assert db.task(tid)['state']=='reconcile'
    assert first==json.loads(db.next_eligible(99)['payload_json'])['requestId']


def test_payment_receipt_cannot_double_credit(db):
    invoice=db.create_invoice(1,120)
    assert db.invoice_valid(1,invoice,120)
    assert not db.invoice_valid(2,invoice,120)
    assert not db.invoice_valid(1,invoice,119)
    first=db.paid_invoice(1,invoice,120,'tgcharge123',30)
    assert first is not None
    assert db.paid_invoice(1,invoice,120,'tgcharge123',30) is None
    assert db.paid_invoice(1,invoice,120,'tgcharge999',30) is None
    assert db.user(1)['expires_at']==first


def test_expired_users_do_not_block_active_user_queue(db):
    db.assign(1,'a'); db.assign(2,'b');db.grant(2,7)
    one=db.queue_task(1,'expired','publish',payload(['a']))
    two=db.queue_task(2,'active','publish',payload(['b']))
    assert db.next_eligible(99)['task_id']==two


def test_cancel_only_queued_and_own_task(db):
    db.assign(1,'a');db.grant(1,7)
    tid=db.queue_task(1,'x','publish',payload(['a']))
    assert not db.cancel_queued(tid,2)
    assert db.cancel_queued(tid,1)
    assert not db.cancel_queued(tid,1)


def test_repeat_topup_guard(db):
    db.assign(1,'a');db.grant(1,30)
    parent=db.queue_task(1,'parent','publish',payload(['a']))
    db.transition(parent,'completed',upstream_id='source1')
    first=db.queue_task(1,'again','publish',payload(['a']),followup_source=parent,followup_mode='reply')
    assert db.task(first)
    with pytest.raises(Exception):
        db.queue_task(1,'duplicate','publish',payload(['a']),followup_source=parent,followup_mode='reply')
    assert len(db.recent_tasks(1))==2


def test_finished_followup_can_be_retried(db):
    db.assign(1,'a');db.grant(1,30)
    parent=db.queue_task(1,'parent','publish',payload(['a']))
    first=db.queue_task(1,'again','publish',payload(['a']),followup_source=parent,followup_mode='reply')
    db.transition(first,'completed')
    second=db.queue_task(1,'again2','publish',payload(['a']),followup_source=parent,followup_mode='reply')
    assert second!=first


def test_telegram_offset_persists(db):
    assert db.get_offset() is None
    db.update_offset(100)
    assert db.get_offset()==100
    db.update_offset(101)
    assert db.get_offset()==101


def test_quota_is_enforced_in_atomic_queue_insert(db):
    db.assign(1,'a');db.grant(1,30)
    db.queue_task(1,'one','publish',payload(['a']),max_daily=2,max_open=1)
    with pytest.raises(ValueError):db.queue_task(1,'open limit','publish',payload(['a']),max_daily=2,max_open=1)
    tid=db.recent_tasks(1)[0]['task_id']
    db.transition(tid,'completed')
    db.queue_task(1,'two','publish',payload(['a']),max_daily=2,max_open=1)
    with pytest.raises(ValueError):db.queue_task(1,'daily limit','publish',payload(['a']),max_daily=2,max_open=3)
