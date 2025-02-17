import asyncio
from typing import Annotated
from urllib.parse import quote_plus
from aiohttp import ClientSession
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
)

from fastapi.responses import RedirectResponse
from jinja2_fragments.fastapi import Jinja2Blocks
from sqlalchemy import func
from sqlmodel import Session, col, select

from app.db import get_session
from app.models import BookRequest, GroupEnum, User
from app.util.auth import get_authenticated_user
from app.util.book_search import get_audnexus_book
from app.util.connection import get_connection
from app.util.prowlarr import (
    ProwlarrMisconfigured,
    start_download,
    prowlarr_config,
)
from app.util.query import query_sources


router = APIRouter(prefix="/wishlist")

templates = Jinja2Blocks(directory="templates")
templates.env.filters["quote_plus"] = lambda u: quote_plus(u)  # pyright: ignore[reportUnknownLambdaType,reportUnknownMemberType,reportUnknownArgumentType]


@router.get("")
async def wishlist(
    request: Request,
    user: Annotated[User, Depends(get_authenticated_user())],
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
):
    book_requests = session.exec(
        select(
            BookRequest.asin, func.count(col(BookRequest.user_username)).label("count")
        )
        .select_from(BookRequest)
        .group_by(BookRequest.asin)
    ).all()

    async def get_book(asin: str, count: int):
        book = await get_audnexus_book(client_session, asin)
        if book:
            book.amount_requested = count
        return book

    coros = [get_book(asin, count) for (asin, count) in book_requests]
    books = [b for b in await asyncio.gather(*coros) if b]

    return templates.TemplateResponse(
        "wishlist.html",
        {"request": request, "books": books, "is_admin": user.is_admin()},
    )


@router.post("/refresh/{asin}")
async def refresh_source(
    asin: str,
    user: Annotated[User, Depends(get_authenticated_user())],
    background_task: BackgroundTasks,
    force_refresh: bool = False,
):
    # causes the sources to be placed into cache once they're done
    background_task.add_task(query_sources, asin, force_refresh=force_refresh)
    return Response(status_code=202)


@router.get("/sources/{asin}")
async def list_sources(
    request: Request,
    asin: str,
    admin_user: Annotated[User, Depends(get_authenticated_user(GroupEnum.admin))],
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
):
    try:
        prowlarr_config.raise_if_invalid(session)
    except ProwlarrMisconfigured:
        return RedirectResponse(
            "/settings?prowlarr_misconfigured=1#prowlarr-base-url", status_code=302
        )

    result = await query_sources(asin, session=session, client_session=client_session)

    return templates.TemplateResponse(
        "sources.html",
        {
            "request": request,
            "book": result.book,
            "sources": result.sources,
            "indexers": result.indexers,
        },
    )


@router.post("/sources/{asin}")
async def download_book(
    asin: str,
    guid: str,
    indexer_id: int,
    admin_user: Annotated[User, Depends(get_authenticated_user(GroupEnum.admin))],
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
):
    try:
        resp = await start_download(session, client_session, guid, indexer_id)
    except ProwlarrMisconfigured as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not resp.ok:
        raise HTTPException(status_code=500, detail="Failed to start download")

    book = session.exec(select(BookRequest).where(BookRequest.asin == asin)).all()
    for b in book:
        session.delete(b)

    session.commit()
