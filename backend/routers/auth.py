import httpx
import os
from fastapi import APIRouter, HTTPException, Response, Cookie
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

INVIDIOUS_URL = os.getenv("INVIDIOUS_URL", "http://192.168.1.173:3000")


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginRequest, response: Response):
    async with httpx.AsyncClient(base_url=INVIDIOUS_URL) as client:
        try:
            resp = await client.post(
                "/api/v1/auth/tokens",
                json={
                    "email": body.username,
                    "password": body.password,
                    "action": "signin",
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))

    data = resp.json()
    token = data.get("token")
    if not token:
        raise HTTPException(status_code=502, detail="No token returned")

    response.set_cookie(
        key="inv_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 30,
    )
    return {"token": token}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("inv_token")
    return {"ok": True}


@router.get("/me")
async def me(inv_token: Optional[str] = Cookie(None)):
    if not inv_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    async with httpx.AsyncClient(base_url=INVIDIOUS_URL) as client:
        try:
            resp = await client.get(
                "/api/v1/auth/feed",
                headers={"Authorization": f"Bearer {inv_token}"},
            )
            resp.raise_for_status()
        except Exception:
            raise HTTPException(status_code=401, detail="Session expired")
    return {"authenticated": True, "token": inv_token}
