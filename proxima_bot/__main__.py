import asyncio
import logging
import aiohttp
from .api import Telegram,Tokora
from .config import Config
from .advanced_db import ExtendedStore
from .worker import Worker
from .extended import ProximaApp


async def main() -> None:
    cfg=Config.from_env()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    db=ExtendedStore(cfg.database_path)
    db.owner_id=cfg.owner_id
    async with aiohttp.ClientSession() as session:
        tg=Telegram(session,cfg.telegram_token)
        upstream=Tokora(session,cfg.base_url,cfg.upstream_key,cfg.upstream_timeout)
        worker=Worker(db,upstream,tg,cfg)
        app=ProximaApp(cfg,db,tg,upstream)
        try:
            async with asyncio.TaskGroup() as services:
                services.create_task(app.run())
                services.create_task(worker.run())
                services.create_task(app.live_cards.run())
        finally:
            db.close()


if __name__=='__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger('proxima_bot').info('Бот остановлен.')
