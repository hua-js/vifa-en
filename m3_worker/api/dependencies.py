"""Authentication and configured-station dependencies."""

from typing import Annotated
import secrets

from fastapi import Depends, Header, HTTPException, Path, Request


def require_admin(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = request.app.state.settings.admin_api_token.get_secret_value()
    supplied = ""
    if authorization is not None:
        scheme, separator, credentials = authorization.partition(" ")
        if scheme == "Bearer" and separator and credentials and " " not in credentials:
            supplied = credentials
    if not secrets.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="unauthorized")


AdminDep = Annotated[None, Depends(require_admin)]


def configured_station(
    request: Request,
    station_id: Annotated[str, Path(min_length=1, max_length=128)],
) -> str:
    if station_id not in request.app.state.settings.station_ids:
        raise HTTPException(status_code=404, detail="station not configured")
    return station_id


StationDep = Annotated[str, Depends(configured_station)]
