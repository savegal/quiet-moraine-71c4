import asyncio
import logging
import aiohttp
from .api import Telegram
from .config import Config
from .advanced_db import ExtendedStore
from .workspace_app import WorkspaceApp


async def main() -> None:
    cfg=Config.from_env()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    db=ExtendedStore(cfg.database_path,timezone_name=cfg.timezone_name)
    db.owner_id=cfg.owner_id
    try:
        async with aiohttp.ClientSession() as session:
            tg=Telegram(session,cfg.telegram_token)
            app=WorkspaceApp(cfg,db,tg,session)
            try:
                await app.run()
            finally:
                await app.close()
    finally:
        db.close()


if __name__=='__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger('proxima_bot').info('Бот остановлен.')
