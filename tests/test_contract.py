"""Structural checks against the OpenAPI document supplied with the project."""
import json
from pathlib import Path
from proxima_bot.db import Store

spec=json.loads((Path(__file__).resolve().parents[1]/'docs/tokora-openapi.json').read_text(encoding='utf-8'))


def resolve_schema(name):
    return spec['components']['schemas'][name]


def test_core_routes_and_rich_report_sources_exist():
    expected={
      ('/v1/tasks','post'),('/v1/tasks','get'),('/v1/tasks/{id}','get'),
      ('/v1/tasks/{id}/publication-report','get'),('/v1/tasks/{id}/stop','post'),
      ('/v1/tasks/{id}/verify','post'),('/v1/tasks/{id}/reply-topup','get'),
      ('/v1/tasks/{id}/like-topup','get'),('/v1/links/resolve','post'),
      ('/v1/statistics','get'),('/v1/accounts','get'),('/v1/proxies','get'),
      ('/v1/accounts/import','post'),('/v1/proxies','post'),
    }
    for path,method in expected:assert method in spec['paths'][path]


def test_task_payload_contract():
    props=resolve_schema('TaskStart')['properties']
    assert {'requestId','kind','assets','awemeIds','settings','likeLinks','likeSourceJob',
       'sourceJob','replySourceMode','disableCommentPhotos','disableReplyPhotos'}.issubset(props)
    assets=resolve_schema('TaskAssets')['properties']
    assert {'accountIds','commentTexts','replyTexts'}.issubset(assets)


def test_numeric_statistics_contract():
    report=resolve_schema('PublicationView')['properties']
    assert {'comments','replies','likes','web','errors'}.issubset(report)
    assert {'text','published','attempts','moderation','pending'}.issubset(resolve_schema('TextStats')['properties'])
