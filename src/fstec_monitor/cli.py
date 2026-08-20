from __future__ import annotations

import asyncio
import time

import typer
from rich.console import Console
from sqlalchemy import select

from .crawler import run_monitor
from .db import SessionLocal, init_db
from .models import Event
from .notify import notify_pending

app=typer.Typer(no_args_is_help=True); console=Console()

@app.command()
def init():
    init_db(); console.print("[green]Database initialized[/green]")

@app.command()
def baseline(limit:int=typer.Option(0,help="Limit documents for a test run")):
    init_db(); started=time.monotonic(); count=asyncio.run(run_monitor(baseline=True,limit=limit,trigger="cli")); console.print(f"Baseline created for {count} documents in {time.monotonic()-started:.0f}s")

@app.command()
def run(limit:int=typer.Option(0,help="Limit documents for a test run")):
    init_db(); started=time.monotonic(); count=asyncio.run(run_monitor(baseline=False,limit=limit,trigger="cli"))
    with SessionLocal() as s: sent=asyncio.run(notify_pending(s))
    console.print(f"Checked {count} documents in {time.monotonic()-started:.0f}s; notifications sent: {sent}")

@app.command("events")
def events(limit:int=50):
    init_db()
    with SessionLocal() as s:
        for e in s.scalars(select(Event).order_by(Event.id.desc()).limit(limit)):
            console.print(f"{e.created_at} [{e.severity}] {e.kind}: {e.summary}")
