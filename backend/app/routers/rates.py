from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel, Field
from app.db import db
from app.services import core
from app.services import rates as svc

router = APIRouter()


class RateIn(BaseModel):
    名称: str = Field(min_length=1)
    峰价: float
    平价: float
    谷价: float


class RatePatchIn(BaseModel):
    峰价: float
    平价: float
    谷价: float
    备注: str = ""


@router.get("/费率")
def list_rates():
    with db() as conn:
        return core.list_rates(conn)


@router.post("/费率")
def create_rate(body: RateIn):
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO rate_plans(name,peak_price,flat_price,valley_price) VALUES(?,?,?,?)",
            (body.名称, body.峰价, body.平价, body.谷价),
        )
        return {"编号": cur.lastrowid, "名称": body.名称}


@router.post("/费率/{rate_plan_id}/改档")
def update_rate(rate_plan_id: int, body: RatePatchIn):
    with db() as conn:
        return svc.update_rate(conn, rate_plan_id, body.峰价, body.平价, body.谷价, body.备注)


@router.get("/费率/改档记录")
def rate_changes(费率编号: int | None = None):
    with db() as conn:
        return core.list_rate_changes(conn, 费率编号)
