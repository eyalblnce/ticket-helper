import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import create_tables
from app.routes.inbox import router as inbox_router
from app.routes.ticket import router as ticket_router
from app.services.poller import run_poller

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    log.info("tables ready")
    # Poller does an initial sync on first run, then polls every 90s
    task = asyncio.create_task(run_poller())
    yield
    task.cancel()


app = FastAPI(title="Support Co-Pilot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(inbox_router)
app.include_router(ticket_router)
