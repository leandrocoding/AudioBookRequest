from collections import defaultdict
import uuid
from typing import Annotated, Literal, Optional

from aiohttp import ClientSession
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import RedirectResponse
from sqlmodel import Session, asc, col, select

from app.internal.models import (
    BookRequest,
    BookWishlistResult,
    GroupEnum,
    ManualBookRequest,
)
from app.internal.prowlarr.prowlarr import (
    ProwlarrMisconfigured,
    prowlarr_config,
    start_download,
)
from app.internal.indexers.mam import mam_config
from app.internal.query import query_sources
from app.internal.auth.authentication import DetailedUser, get_authenticated_user
from app.util.connection import get_connection
from app.util.db import get_session, open_session
from app.util.templates import template_response

router = APIRouter(prefix="/wishlist")


def get_wishlist_books(
    session: Session,
    username: Optional[str] = None,
    response_type: Literal["all", "downloaded", "not_downloaded"] = "all",
) -> list[BookWishlistResult]:
    """
    Gets the books that have been requested. If a username is given only the books requested by that
    user are returned. If no username is given, all book requests are returned.
    """
    if username:
        query = select(BookRequest).where(BookRequest.user_username == username)
    else:
        query = select(BookRequest).where(col(BookRequest.user_username).is_not(None))

    book_requests = session.exec(query).all()

    # group by asin and aggregate all usernames
    usernames: dict[str, list[str]] = defaultdict(list)
    distinct_books: dict[str, BookRequest] = {}
    for book in book_requests:
        if book.asin not in distinct_books:
            distinct_books[book.asin] = book
        if book.user_username:
            usernames[book.asin].append(book.user_username)

    # add information of what users requested the book
    books: list[BookWishlistResult] = []
    downloaded: list[BookWishlistResult] = []
    for asin, book in distinct_books.items():
        b = BookWishlistResult.model_validate(book)
        b.requested_by = usernames[asin]
        if b.downloaded:
            downloaded.append(b)
        else:
            books.append(b)

    if response_type == "downloaded":
        return downloaded
    if response_type == "not_downloaded":
        return books
    return books + downloaded


@router.get("")
async def wishlist(
    request: Request,
    user: Annotated[DetailedUser, Depends(get_authenticated_user())],
    session: Annotated[Session, Depends(get_session)],
):
    username = None if user.is_admin() else user.username
    books = get_wishlist_books(session, username, "not_downloaded")
    return template_response(
        "wishlist_page/wishlist.html",
        request,
        user,
        {"books": books, "page": "wishlist"},
    )


@router.get("/downloaded")
async def downloaded(
    request: Request,
    user: Annotated[DetailedUser, Depends(get_authenticated_user())],
    session: Annotated[Session, Depends(get_session)],
):
    username = None if user.is_admin() else user.username
    books = get_wishlist_books(session, username, "downloaded")
    return template_response(
        "wishlist_page/wishlist.html",
        request,
        user,
        {"books": books, "page": "downloaded"},
    )


@router.patch("/downloaded/{asin}")
async def update_downloaded(
    request: Request,
    asin: str,
    admin_user: Annotated[
        DetailedUser, Depends(get_authenticated_user(GroupEnum.admin))
    ],
    session: Annotated[Session, Depends(get_session)],
):
    books = session.exec(select(BookRequest).where(BookRequest.asin == asin)).all()
    for book in books:
        book.downloaded = True
        session.add(book)
    session.commit()

    username = None if admin_user.is_admin() else admin_user.username
    books = get_wishlist_books(session, username, "not_downloaded")
    return template_response(
        "wishlist_page/wishlist.html",
        request,
        admin_user,
        {"books": books, "page": "wishlist"},
        block_name="book_wishlist",
    )


@router.get("/manual")
async def manual(
    request: Request,
    user: Annotated[DetailedUser, Depends(get_authenticated_user())],
    session: Annotated[Session, Depends(get_session)],
):
    books = session.exec(
        select(ManualBookRequest).order_by(asc(ManualBookRequest.downloaded))
    ).all()
    return template_response(
        "wishlist_page/manual.html",
        request,
        user,
        {"books": books, "page": "manual"},
    )


@router.patch("/manual/{id}")
async def downloaded_manual(
    request: Request,
    id: uuid.UUID,
    admin_user: Annotated[
        DetailedUser, Depends(get_authenticated_user(GroupEnum.admin))
    ],
    session: Annotated[Session, Depends(get_session)],
):
    book = session.get(ManualBookRequest, id)
    if book:
        book.downloaded = True
        session.add(book)
        session.commit()

    books = session.exec(
        select(ManualBookRequest).order_by(asc(ManualBookRequest.downloaded))
    ).all()
    return template_response(
        "wishlist_page/manual.html",
        request,
        admin_user,
        {"books": books, "page": "manual"},
        block_name="book_wishlist",
    )


@router.delete("/manual/{id}")
async def delete_manual(
    request: Request,
    id: uuid.UUID,
    admin_user: Annotated[
        DetailedUser, Depends(get_authenticated_user(GroupEnum.admin))
    ],
    session: Annotated[Session, Depends(get_session)],
):
    book = session.get(ManualBookRequest, id)
    if book:
        session.delete(book)
        session.commit()

    books = session.exec(select(ManualBookRequest)).all()
    return template_response(
        "wishlist_page/manual.html",
        request,
        admin_user,
        {"books": books, "page": "manual"},
        block_name="book_wishlist",
    )


@router.post("/refresh/{asin}")
async def refresh_source(
    asin: str,
    user: Annotated[DetailedUser, Depends(get_authenticated_user())],
    background_task: BackgroundTasks,
    force_refresh: bool = False,
):
    # causes the sources to be placed into cache once they're done
    with open_session() as session:
        async with ClientSession() as client_session:
            background_task.add_task(
                query_sources,
                asin=asin,
                session=session,
                client_session=client_session,
                force_refresh=force_refresh,
                requester_username=user.username,
            )
    return Response(status_code=202)


@router.get("/sources/{asin}")
async def list_sources(
    request: Request,
    asin: str,
    admin_user: Annotated[
        DetailedUser, Depends(get_authenticated_user(GroupEnum.admin))
    ],
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
):
    try:
        prowlarr_config.raise_if_invalid(session)
    except ProwlarrMisconfigured:
        return RedirectResponse(
            "/settings/prowlarr?prowlarr_misconfigured=1", status_code=302
        )

    result = await query_sources(
        asin,
        session=session,
        client_session=client_session,
        requester_username=admin_user.username,
    )

    return template_response(
        "wishlist_page/sources.html",
        request,
        admin_user,
        {
            "book": result.book,
            "sources": result.sources,
            "mam_active": mam_config.is_active(session),
        },
    )


@router.post("/sources/{asin}")
async def download_book(
    asin: str,
    guid: Annotated[str, Form()],
    indexer_id: Annotated[int, Form()],
    admin_user: Annotated[
        DetailedUser, Depends(get_authenticated_user(GroupEnum.admin))
    ],
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
):
    try:
        resp = await start_download(
            session=session,
            client_session=client_session,
            guid=guid,
            indexer_id=indexer_id,
            requester_username=admin_user.username,
            book_asin=asin,
        )
    except ProwlarrMisconfigured as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not resp.ok:
        raise HTTPException(status_code=500, detail="Failed to start download")

    book = session.exec(select(BookRequest).where(BookRequest.asin == asin)).all()
    for b in book:
        b.downloaded = True
        session.add(b)
    session.commit()

    return Response(status_code=204)


@router.post("/auto-download/{asin}")
async def start_auto_download(
    request: Request,
    asin: str,
    user: Annotated[DetailedUser, Depends(get_authenticated_user(GroupEnum.trusted))],
    session: Annotated[Session, Depends(get_session)],
    client_session: Annotated[ClientSession, Depends(get_connection)],
):
    download_error: Optional[str] = None
    try:
        await query_sources(
            asin=asin,
            start_auto_download=True,
            session=session,
            client_session=client_session,
            requester_username=user.username,
        )
    except HTTPException as e:
        download_error = e.detail

    username = None if user.is_admin() else user.username
    books = get_wishlist_books(session, username)
    if download_error:
        errored_book = [b for b in books if b.asin == asin][0]
        errored_book.download_error = download_error

    return template_response(
        "wishlist_page/wishlist.html",
        request,
        user,
        {"books": books},
        block_name="book_wishlist",
    )
