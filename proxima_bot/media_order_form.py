"""Guided preparation of video and photo publication plans."""
from __future__ import annotations

import re
from secrets import token_hex

from .api import ApiError, Telegram
from .operations import VALID_ID
from .presentation import h, keyboard, table_html
from .rich_ui import screen


MEDIA_SWITCHES={'comments':'Разрешить комментарии','duet':'Разрешить дуэты',
                'stitch':'Разрешить сшивание','aiGenerated':'Пометка ИИ-контента',
                'uniquify':'Уникализация медиа','effects':'Эффекты',
                'mobileOnly':'Только мобильные сессии'}
MEDIA_TEXT_LIMITS={'description':2200,'hashtags':500}
MEDIA_TEXT_FIELDS=tuple(MEDIA_TEXT_LIMITS)
MEDIA_DRAFT_SETTINGS_FIELDS=('visibility','comments','duet','stitch','aiGenerated')


def parse_media_text(field:str,text:str)->str:
    limit=MEDIA_TEXT_LIMITS[field]
    if len(text)>limit:
        raise ValueError(f'Допустимо до {limit} символов.')
    return text.strip()


class MediaOrderFormMixin:
    async def media_draft_for_plan(self,oid:str,*,kind:str|None=None)->dict:
        if not VALID_ID.fullmatch(oid):raise ValueError('Некорректный ID черновика.')
        draft=await self.api.call('GET','/v1/posts/drafts/'+oid)
        if draft.get('id')!=oid or draft.get('kind') not in ('video','photo'):
            raise ApiError('invalid_response')
        if kind and draft['kind']!=kind:
            raise ValueError('Выбери черновик нужного типа.')
        if draft.get('activeTasks'):
            raise ValueError('У этого черновика уже есть активные публикации.')
        if draft['kind']=='photo' and not draft.get('photos'):
            raise ValueError('Добавь фотографии в альбом перед созданием публикации.')
        return draft

    async def load_media_order(self,oid:str,*,kind:str|None=None)->dict:
        draft=await self.media_draft_for_plan(oid,kind=kind)
        settings=dict(await self.api.call('GET','/v1/posts/settings'))
        self._allowed_fields(settings,'PostSettings')
        if draft.get('ownText'):
            settings.update({key:draft[key] for key in MEDIA_TEXT_FIELDS if key in draft})
        if draft.get('ownSettings'):
            settings.update({key:draft[key] for key in MEDIA_DRAFT_SETTINGS_FIELDS if draft.get(key) is not None})
        self._allowed_fields(settings,'PostSettings')
        return {'id':oid,'name':draft.get('name') or oid,'kind':draft['kind'],
                'settings':settings,'patch':{},'token':token_hex(6)}

    async def begin_media_order(self,uid:int,oid:str)->None:
        if not self.owner(uid) or not VALID_ID.fullmatch(oid):return
        try:
            self.db.save_draft(uid,'media_order','review',await self.load_media_order(oid))
            await self.media_order_screen(uid)
        except (ApiError,ValueError) as exc:
            message=self.account_error_message(uid,exc) if isinstance(exc,ApiError) else h(exc)
            await self.say(uid,message,self.owner_keyboard([('↶ Черновик','draft:'+oid,None)]))

    async def media_order_screen(self,uid:int,*,error:str='')->None:
        draft=self.db.draft(uid)
        if not self.owner(uid) or not draft or draft['mode']!='media_order':return
        data=draft['data']
        token,settings=data['token'],data['settings']
        route=lambda verb:'mo:'+verb+':'+token
        field=data.get('field')
        prompts={
            'perAccount':'Сколько публикаций сделать на каждом аккаунте? Введи целое число от 1 до 100.',
            'accountIds':'Пришли ID аккаунтов, по одному на строку или через запятую. Для общего пула нажми «Все подходящие аккаунты».',
            'description':f'Пришли подпись к публикации, до {MEDIA_TEXT_LIMITS["description"]} символов. Она сохранится в черновике.',
            'hashtags':f'Пришли хештеги одной строкой, до {MEDIA_TEXT_LIMITS["hashtags"]} символов. Они сохранятся в черновике.',
        }
        if field:
            rows=[]
            if field=='perAccount':rows.append([(str(n),route('amount_'+str(n)),None) for n in (1,2,3,5)])
            if field=='accountIds':rows.append([('Все подходящие аккаунты',route('all'),None)])
            if field in MEDIA_TEXT_FIELDS:rows.append([('Очистить',route('clear'),None)])
            rows.append([('↶ К параметрам',route('back'),None)])
            text='<b>Параметры публикации</b>\n\n'+prompts[field]
            current=settings[field]
            if field=='accountIds':
                ids=current
                current='Все подходящие аккаунты' if settings['allAccounts'] else '\n'.join(ids[:10]) or 'Аккаунты не выбраны'
                if not settings['allAccounts'] and len(ids)>10:current+=f'\nЕщё аккаунтов: {len(ids)-10}'
            text+='\n\n<b>Сейчас</b>\n<code>'+h(current if current!='' else 'Не задано')+'</code>'
            if error:text+='\n\n<b>Проверь ввод</b>\n'+h(error)
            rich,_=screen('Параметры публикации',subtitle=prompts[field])
            rich+='<p>Сейчас · <b>'+h(current if current!='' else 'Не задано')+'</b></p>'
            if error:rich+='<blockquote><b>Проверь ввод</b><br>'+h(error)+'</blockquote>'
            return await self.tg.rich(uid,rich,text,keyboard(*rows))
        kind='Фотоальбом' if data['kind']=='photo' else 'Видео'
        rows=[('Черновик',data['name']),('Тип',kind),('Публикаций на аккаунт',settings['perAccount']),
              ('Аккаунты','Все подходящие' if settings['allAccounts'] else len(settings['accountIds'])),
              ('Подпись',(settings['description'][:200]+('…' if len(settings['description'])>200 else '')) or 'Без подписи'),
              ('Хештеги',(settings['hashtags'][:150]+('…' if len(settings['hashtags'])>150 else '')) or 'Без хештегов')]
        rich,plain=screen('▷ Новая публикация',rows)
        extra=[(label,'Вкл.' if settings[key] else 'Выкл.') for key,label in MEDIA_SWITCHES.items()]
        block,_=table_html('Параметры публикации',extra)
        rich+='<details><summary>Параметры публикации · раскрыть</summary>'+block
        for key,label in MEDIA_SWITCHES.items():
            rich+='<p><tg-button type="callback_data" style="link" data="'+route('toggle_'+key)+'">'+h(label)+' · '+('Вкл.' if settings[key] else 'Выкл.')+'</tg-button></p>'
        rich+='</details>'
        plain+='\n\n'+' · '.join(label+': '+value for label,value in extra)
        note='Следующий шаг — подготовка плана и проверка аккаунтов. Публикация начнётся только после отдельного подтверждения плана.'
        rich+='<footer>'+note+'</footer>'
        plain+='\n\n'+note
        if error:rich+='<p>'+h(error)+'</p>';plain+='\n\n'+h(error)
        buttons=[[( '✓ Подготовить план',route('prepare'),'success')],
                 [('✎ Публикаций на аккаунт',route('edit_perAccount'),None)],
                 [('✎ Выбрать аккаунты',route('edit_accountIds'),None)],
                 [('✎ Подпись',route('edit_description'),None),('✎ Хештеги',route('edit_hashtags'),None)]]
        buttons.extend([(('✓ ' if settings[key] else '○ ')+label+' · '+('Вкл.' if settings[key] else 'Выкл.'),route('toggle_'+key),None)]
                       for key,label in MEDIA_SWITCHES.items())
        buttons.append([('× Отменить',route('cancel'),'danger')])
        if isinstance(self.tg,Telegram):
            await self.tg.rich(uid,rich,plain,keyboard(*buttons[:4],buttons[-1]),fallback_keyboard=keyboard(*buttons))
        else:
            await self.tg.rich(uid,rich,plain,keyboard(*buttons))

    async def media_order_input(self,uid:int,text:str)->bool:
        draft=self.db.draft(uid)
        if not draft or draft['mode']!='media_order':return False
        if not self.owner(uid):return True
        data=draft['data']
        field=data.get('field')
        try:
            if field=='perAccount':
                if not re.fullmatch(r'[0-9]{1,3}',text.strip()) or not 1<=int(text)<=100:
                    raise ValueError('Введи целое число от 1 до 100.')
                data['settings'][field]=int(text)
            elif field=='accountIds':
                ids=list(dict.fromkeys(re.split(r'[\s,]+',text.strip())))
                if not 1<=len(ids)<=300 or any(not VALID_ID.fullmatch(aid) for aid in ids):
                    raise ValueError('Нужно от 1 до 300 корректных ID аккаунтов.')
                data['settings'].update(accountIds=ids,allAccounts=False)
            elif field in MEDIA_TEXT_FIELDS:
                value=parse_media_text(field,text)
                data['settings'][field]=value
                data['patch'].update({field:value,'ownText':True})
            else:
                await self.media_order_screen(uid)
                return True
            data.pop('field',None)
            data['token']=token_hex(6)
            self.db.save_draft(uid,'media_order','review',data)
            await self.media_order_screen(uid)
        except ValueError as exc:await self.media_order_screen(uid,error=str(exc))
        return True

    async def media_order_callback(self,uid:int,action:str)->None:
        if not self.owner(uid):return
        parts=action.split(':')
        draft=self.db.draft(uid)
        if len(parts)!=3 or not draft or draft['mode']!='media_order':return
        _,verb,token=parts
        data=draft['data']
        if token!=data['token']:
            return await self.say(uid,'Кнопка устарела. Используй последнее сообщение с параметрами публикации.')
        if verb=='cancel':
            self.db.clear_draft(uid)
            return await self.draft_info(uid,data['id'])
        if verb=='prepare':
            try:
                settings=data['settings']
                if not 1<=settings['perAccount']<=100:raise ValueError('Укажи от 1 до 100 публикаций на аккаунт.')
                if not settings['allAccounts'] and not settings['accountIds']:raise ValueError('Выбери аккаунты для публикации.')
                await self.media_draft_for_plan(data['id'],kind=data['kind'])
                self._allowed_fields(settings,'PostSettings')
                data['token']=token_hex(6)
                self.db.save_draft(uid,'media_order','review',data)
                if data['patch']:
                    patch=dict(data['patch'])
                    if patch.get('ownText'):
                        for key in MEDIA_TEXT_FIELDS:
                            if key in patch:parse_media_text(key,settings[key])
                        patch.update({key:settings[key] for key in MEDIA_TEXT_FIELDS})
                    if patch.get('ownSettings'):
                        patch.update({key:settings[key] for key in MEDIA_DRAFT_SETTINGS_FIELDS})
                    await self.api.call('PATCH','/v1/posts/drafts/'+data['id'],payload=patch,retries=0)
                await self.create_post_plan(uid,{'draftIds':[data['id']],'settings':settings})
                self.db.clear_draft(uid)
            except (ApiError,ValueError) as exc:
                message=self.account_error_message(uid,exc) if isinstance(exc,ApiError) else str(exc)
                await self.media_order_screen(uid,error=message)
            return
        if verb.startswith('edit_') and verb[5:] in ('perAccount','accountIds','description','hashtags'):
            data['field']=verb[5:]
        elif verb.startswith('toggle_') and verb[7:] in MEDIA_SWITCHES:
            key=verb[7:]
            data['settings'][key]=not data['settings'][key]
            if key in ('comments','duet','stitch','aiGenerated'):
                data['patch'].update({key:data['settings'][key],'ownSettings':True})
        elif verb=='all' and data.get('field')=='accountIds':
            data['settings'].update(allAccounts=True,accountIds=[])
            data.pop('field',None)
        elif verb=='clear' and data.get('field') in MEDIA_TEXT_FIELDS:
            return await self.media_order_input(uid,'')
        elif verb.startswith('amount_') and data.get('field')=='perAccount':
            return await self.media_order_input(uid,verb[7:])
        elif verb=='back':data.pop('field',None)
        else:return
        data['token']=token_hex(6)
        self.db.save_draft(uid,'media_order','review',data)
        await self.media_order_screen(uid)
