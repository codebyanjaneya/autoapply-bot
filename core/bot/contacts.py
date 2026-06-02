"""User-supplied recruiter contacts: /add_contacts, /contacts, /clear_contacts.

Two input modes for /add_contacts:
  1. Paste a comma/newline-separated list. Emails are extracted via regex
     so "John Doe <john@google.com>, jane@microsoft.com" works.
  2. Upload a CSV with an ``email`` header column (optional ``company`` /
     ``name`` columns). Tab-separated also works (Sheets export).

Pasted text doesn't carry company info, so the bot prompts a single follow-up
when N>0 contacts were added with no company set, asking the user whether to
tag them. (Skipped for v1 simplicity \u2014 see TODO at end of file. Users who
care about per-company matching should upload a CSV.)
"""
from __future__ import annotations

import csv
import io
import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from core.db import get_session
from core.models import User, UserStatus
from core.repositories import contacts as contacts_repo

log = logging.getLogger(__name__)
router = Router(name="contacts")

# Generous \u2014 a 10 MB CSV is ~200k rows which is way past MAX_CONTACTS_PER_USER
# anyway, but we cap the bot.download size to keep memory bounded.
MAX_CSV_BYTES = 2 * 1024 * 1024
# RFC 5322-lite. Good enough for "is this plausibly an email", which is all
# we need before INSERTing. Real validation happens when SMTP tries to deliver.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


class Contacts(StatesGroup):
    WAITING_INPUT = State()    # bot is waiting for paste OR CSV upload
    CONFIRM_CLEAR = State()    # bot has prompted "are you sure?" for /clear


async def _ensure_active(message: Message) -> bool:
    assert message.from_user is not None
    async with get_session() as session:
        user = await session.get(User, message.from_user.id)
    if user is None:
        await message.answer("You haven't onboarded yet. Send /start to begin.")
        return False
    if user.status == UserStatus.onboarding:
        await message.answer("Finish /start onboarding first, then come back.")
        return False
    return True


# ---------- /add_contacts ----------
@router.message(Command("add_contacts", "addcontacts"))
async def cmd_add_contacts(message: Message, state: FSMContext) -> None:
    if not await _ensure_active(message):
        return
    await state.set_state(Contacts.WAITING_INPUT)
    await message.answer(
        "\U0001f4ec <b>Add recruiter contacts</b>\n\n"
        "Send either:\n"
        "\u2022 <b>Paste emails</b> separated by commas/newlines, e.g.\n"
        "  <code>john@google.com, jane@microsoft.com</code>\n"
        "\u2022 Or <b>upload a CSV</b> with columns: <code>email,company,name</code> "
        "(only <code>email</code> is required).\n\n"
        "Tip: include the <b>company</b> column to enable auto-match \u2014 when "
        "a job at that company is scraped, we'll email your contact directly "
        "instead of guessing via Hunter.\n\n"
        "Send /cancel to abort.",
        disable_web_page_preview=True,
    )


@router.message(Command("cancel"), StateFilter(Contacts.WAITING_INPUT, Contacts.CONFIRM_CLEAR))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Cancelled.")


@router.message(Contacts.WAITING_INPUT, F.document)
async def on_csv(message: Message, state: FSMContext) -> None:
    doc = message.document
    assert doc is not None
    if (doc.file_size or 0) > MAX_CSV_BYTES:
        await message.answer(f"CSV is too big (max {MAX_CSV_BYTES // 1024} KB). Try a smaller file.")
        return
    bot = message.bot
    assert bot is not None
    buf = await bot.download(doc)
    if buf is None:
        await message.answer("Couldn't download that file. Try again.")
        return
    raw = buf.read()
    try:
        text = raw.decode("utf-8-sig")  # strip BOM if Excel added one
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except Exception:
            await message.answer("Couldn't read the CSV \u2014 please save as UTF-8.")
            return
    items = _parse_csv(text)
    if not items:
        await message.answer(
            "Couldn't find any emails in that CSV. Make sure the first row is "
            "a header with an <code>email</code> column."
        )
        return
    await _persist_and_reply(message, state, items, source="csv")


@router.message(Contacts.WAITING_INPUT, F.text)
async def on_paste(message: Message, state: FSMContext) -> None:
    emails = _extract_emails(message.text or "")
    if not emails:
        await message.answer(
            "I didn't spot any email addresses in that message. Paste them "
            "separated by commas, or send /cancel."
        )
        return
    items = [{"email": e} for e in emails]
    await _persist_and_reply(message, state, items, source="paste")


async def _persist_and_reply(
    message: Message, state: FSMContext, items: list[dict], *, source: str
) -> None:
    assert message.from_user is not None
    async with get_session() as session:
        existing = await contacts_repo.count_contacts(session, message.from_user.id)
        room = contacts_repo.MAX_CONTACTS_PER_USER - existing
        if room <= 0:
            await state.clear()
            await message.answer(
                f"You're at the contact cap ({contacts_repo.MAX_CONTACTS_PER_USER}). "
                f"/clear_contacts first to make room."
            )
            return
        if len(items) > room:
            items = items[:room]
        added = await contacts_repo.add_contacts(session, message.from_user.id, items)
        await session.commit()
    await state.clear()
    if added == 0:
        await message.answer(
            "Those emails are already in your contact list \u2014 nothing new added."
        )
        return
    await message.answer(
        f"\u2705 Added <b>{added}</b> contact{'s' if added != 1 else ''} "
        f"(via {source}). They'll be checked first during tomorrow's run \u2014 "
        f"if any of your contacts work at the companies we scrape, we'll "
        f"email them directly instead of using Hunter.\n\n"
        f"/contacts to view all."
    )


def _extract_emails(text: str) -> list[str]:
    """Return de-duped emails (lowercased), preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in _EMAIL_RE.findall(text or ""):
        e = raw.strip().lower()
        if e in seen:
            continue
        seen.add(e)
        out.append(e)
    return out


def _parse_csv(text: str) -> list[dict]:
    """Parse a CSV/TSV with email/company/name headers (case-insensitive).

    Falls back to extracting emails via regex from each line if no header row
    is present (so a one-column \"email per line\" file still works).
    """
    # Sniff delimiter; csv.Sniffer occasionally raises on tiny files, so try
    # comma first and tab if that produces only single-field rows.
    rows: list[dict] = []
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = [(f or "").strip().lower() for f in (reader.fieldnames or [])]
    if "email" in fieldnames:
        # Normalise header lookup once.
        email_key = (reader.fieldnames or [])[fieldnames.index("email")]
        company_key = (reader.fieldnames or [])[fieldnames.index("company")] if "company" in fieldnames else None
        name_key = (reader.fieldnames or [])[fieldnames.index("name")] if "name" in fieldnames else None
        for r in reader:
            email = (r.get(email_key) or "").strip()
            if not email or not _EMAIL_RE.fullmatch(email):
                continue
            rows.append({
                "email": email,
                "company": (r.get(company_key) or "").strip() if company_key else None,
                "name": (r.get(name_key) or "").strip() if name_key else None,
            })
        return rows
    # No "email" header \u2014 fall back to regex over the whole blob.
    return [{"email": e} for e in _extract_emails(text)]


# ---------- /contacts ----------
@router.message(Command("contacts"))
async def cmd_contacts(message: Message) -> None:
    if not await _ensure_active(message):
        return
    assert message.from_user is not None
    async with get_session() as session:
        rows = await contacts_repo.list_contacts(session, message.from_user.id)
    if not rows:
        await message.answer(
            "You haven't added any contacts yet. /add_contacts to paste recruiter "
            "emails \u2014 the bot will email them directly when a matching job "
            "comes up."
        )
        return
    lines = [f"\U0001f4ec <b>Your contacts</b> ({len(rows)} total)\n"]
    # Cap the displayed list so we don't blow Telegram's 4096-char message limit.
    DISPLAY_CAP = 50
    for c in rows[:DISPLAY_CAP]:
        company = f" \u2014 <b>{c.company}</b>" if c.company else ""
        name = f" ({c.name})" if c.name else ""
        lines.append(f"\u2022 {c.email}{company}{name}")
    if len(rows) > DISPLAY_CAP:
        lines.append(f"\n\u2026and {len(rows) - DISPLAY_CAP} more.")
    lines.append("\n/add_contacts to add more  \u2022  /clear_contacts to remove all")
    await message.answer("\n".join(lines), disable_web_page_preview=True)


# ---------- /clear_contacts ----------
def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="\u2705 Yes, clear all", callback_data="contacts:clear:yes"),
        InlineKeyboardButton(text="\u274c Cancel",          callback_data="contacts:clear:no"),
    ]])


@router.message(Command("clear_contacts", "clearcontacts"))
async def cmd_clear_contacts(message: Message, state: FSMContext) -> None:
    if not await _ensure_active(message):
        return
    assert message.from_user is not None
    async with get_session() as session:
        n = await contacts_repo.count_contacts(session, message.from_user.id)
    if n == 0:
        await message.answer("Nothing to clear \u2014 you have no saved contacts.")
        return
    await state.set_state(Contacts.CONFIRM_CLEAR)
    await message.answer(
        f"\u26a0\ufe0f This will delete <b>all {n} contact{'s' if n != 1 else ''}</b>. "
        f"This can't be undone \u2014 you'd need to re-paste them.",
        reply_markup=_confirm_kb(),
    )


@router.callback_query(F.data.startswith("contacts:clear:"))
async def cb_clear_confirm(cb: CallbackQuery, state: FSMContext) -> None:
    assert cb.data is not None and cb.from_user is not None
    choice = cb.data.rsplit(":", 1)[1]
    await state.clear()
    if isinstance(cb.message, Message):
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    if choice != "yes":
        await cb.answer("Cancelled.")
        if isinstance(cb.message, Message):
            await cb.message.answer("Cancelled \u2014 your contacts are untouched.")
        return
    async with get_session() as session:
        n = await contacts_repo.clear_contacts(session, cb.from_user.id)
        await session.commit()
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.answer(f"\u2705 Cleared {n} contact{'s' if n != 1 else ''}.")
