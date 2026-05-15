import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from app.db import create_tables, engine
from app.routes.dashboard import router as dashboard_router
from app.routes.desk import router as desk_router
from app.routes.inbox import router as inbox_router
from app.routes.ticket import router as ticket_router
from app.routes.training import router as training_router
from app.services.reference_lookup import get_merchant_domains
from app.services.rules import reload_merchant_domains

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    log.info("tables ready")
    with Session(engine) as s:
        domains = get_merchant_domains(s)
    reload_merchant_domains(domains)
    log.info("merchant domains loaded (%d)", len(domains))
    yield


app = FastAPI(title="Support Co-Pilot", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(inbox_router)
app.include_router(ticket_router)
app.include_router(dashboard_router)
app.include_router(training_router)
app.include_router(desk_router)
