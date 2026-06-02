"""User feedback flow: /review (FSM with star buttons) + /reviews (operator-only).

Flow
----
1. User runs /review \u2192 inline keyboard with 5 star buttons.
2. User taps a rating \u2192 bot stores rating in FSM and asks for an optional
   comment with a "Skip" button.
3. User either sends text OR taps Skip \u2192 we UPSERT the user_reviews row
   (unique on user_id), forward a nicely-formatted card to OPERATOR_CHAT_ID,
   and ACK the user warmly.

/reviews is operator-only (gated on OPERATOR_CHAT_ID) and paginates 10/page
with average rating in the header.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from core.db import get_session
from core.models import User, UserReview

log = logging.getLogger(__name__)
router = Router(name="reviews")

_IST = ZoneInfo("Asia/Kolkata")
_MAX_COMMENT_CHARS = 1000
_REVIEWS_PAGE_SIZE = 10


class Review(StatesGroup):
    WAITING_COMMENT = State()  # rating captured; waiting for text or Skip


def _operator_chat_id() -> int | None:
    raw = os.environ.get("OPERATOR_CHAT_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _stars(rating: int) -> str:
    rating = max(1, min(5, int(rating)))
    return "\u2b50" * rating


def _rating_kb() -> InlineKeyboardMarkup:
    # One row per rating so the labels are readable on narrow phone screens.
    rows = [
        [InlineKeyboardButton(text=f"{_stars(n)}  {n}", callback_data=f"review:rate:{n}")]
        for n in range(5, 0, -1)  # 5 at top \u2014 the happy default
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Skip \u2014 submit rating only", callback_data="review:skip"),
    ]])


# ---------- /review ----------
@router.message(Command("review"))
async def cmd_review(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "\u2b50 <b>How would you rate AutoApply?</b>\n\n"
        "Your feedback shapes what we build next. Tap a rating below \u2014 "
        "you can update it anytime by running /review again.",
        reply_markup=_rating_kb(),
    )


@router.callback_query(F.data.startswith("review:rate:"))
async def cb_review_rate(cq: CallbackQuery, state: FSMContext) -> None:
    assert cq.data is not None
    try:
        rating = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer("Invalid rating.", show_alert=True)
        return
    if rating not in range(1, 6):
        await cq.answer("Rating must be 1\u20135.", show_alert=True)
        return
    await state.set_state(Review.WAITING_COMMENT)
    await state.update_data(rating=rating)
    await cq.answer(f"Got it \u2014 {rating} star{'s' if rating > 1 else ''}!")
    if cq.message is not None:
        try:
            await cq.message.edit_text(
                f"You rated us {_stars(rating)} ({rating}/5).\n\n"
                "<b>Thanks!</b> Would you like to add a comment? "
                "<i>(optional \u2014 tap Skip to submit just your rating)</i>",
                reply_markup=_skip_kb(),
            )
        except TelegramAPIError:
            await cq.message.answer(
                f"You rated us {_stars(rating)} ({rating}/5).\n\n"
                "Add a comment, or tap Skip below.",
                reply_markup=_skip_kb(),
            )


@router.callback_query(F.data == "review:skip", StateFilter(Review.WAITING_COMMENT))
async def cb_review_skip(cq: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    rating = int(data.get("rating", 0))
    await cq.answer()
    if cq.message is not None and cq.from_user is not None:
        await _finalise_review(cq.message, cq.from_user.id, cq.from_user.full_name,
                               rating, comment=None, state=state)


@router.message(Command("cancel"), StateFilter(Review.WAITING_COMMENT))
async def cmd_review_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "No problem \u2014 review cancelled. Run /review again whenever you want."
    )


@router.message(StateFilter(Review.WAITING_COMMENT), F.text)
async def review_collect_comment(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    text = (message.text or "").strip()
    if not text:
        await message.answer("Please type a short comment, or tap Skip on the message above.")
        return
    if len(text) > _MAX_COMMENT_CHARS:
        text = text[:_MAX_COMMENT_CHARS] + "\u2026 [truncated]"
    data = await state.get_data()
    rating = int(data.get("rating", 0))
    await _finalise_review(message, message.from_user.id, message.from_user.full_name,
                           rating, comment=text, state=state)


async def _finalise_review(
    message: Message,
    user_id: int,
    display_name_fallback: str | None,
    rating: int,
    comment: str | None,
    state: FSMContext,
) -> None:
    """Upsert the review, forward to operator, ACK the user, clear state."""
    if rating not in range(1, 6):
        await message.answer("Something went wrong with that rating \u2014 run /review again.")
        await state.clear()
        return

    display_name = display_name_fallback or "?"
    tier_label = "?"
    try:
        async with get_session() as session:
            user = await session.get(User, user_id)
            if user is not None:
                tier_label = user.subscription_tier.value
                if user.first_name:
                    display_name = user.first_name
            # UPSERT \u2014 unique(user_id) means re-running /review overwrites.
            stmt = pg_insert(UserReview).values(
                user_id=user_id, rating=rating, comment=comment,
            ).on_conflict_do_update(
                index_elements=["user_id"],
                set_={"rating": rating, "comment": comment, "updated_at": func.now()},
            )
            await session.execute(stmt)
            await session.commit()
    except Exception:  # pragma: no cover - logged + degraded
        log.exception("review: failed to upsert review for user %s", user_id)
        await message.answer(
            "\u26a0\ufe0f We hit a snag saving that \u2014 please try /review again in a moment."
        )
        await state.clear()
        return

    # Forward to operator (best-effort).
    op_id = _operator_chat_id()
    if op_id is not None:
        now_ist = datetime.now(_IST).strftime("%d %b %Y, %I:%M %p IST")
        op_text = (
            "\u2b50 <b>New Review</b>\n"
            f"<b>User:</b> {display_name} (ID: <code>{user_id}</code>)\n"
            f"<b>Tier:</b> {tier_label}\n"
            f"<b>Rating:</b> {_stars(rating)} ({rating}/5)\n"
            f"<b>Time:</b> {now_ist}\n"
            f"\n<b>Comment:</b> {comment or '<i>(no comment)</i>'}"
        )
        try:
            await message.bot.send_message(op_id, op_text)
        except TelegramAPIError:
            log.exception("review: failed forwarding review from user %s", user_id)
    else:
        log.warning("review: OPERATOR_CHAT_ID not set; review from user %s NOT forwarded", user_id)

    await state.clear()
    await message.answer(
        "\u2705 <b>Thank you for your review!</b>\n\n"
        "Your feedback helps us improve AutoApply for everyone. "
        "\U0001f9e1"
    )


# ---------- /reviews (operator only) ----------
def _reviews_kb(page: int, total_pages: int) -> InlineKeyboardMarkup:
    btns = []
    if page > 1:
        btns.append(InlineKeyboardButton(text="\u2b05\ufe0f Prev", callback_data=f"reviews:page:{page - 1}"))
    btns.append(InlineKeyboardButton(text=f"Page {page}/{total_pages}", callback_data="reviews:noop"))
    if page < total_pages:
        btns.append(InlineKeyboardButton(text="Next \u27a1\ufe0f", callback_data=f"reviews:page:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[btns])


async def _render_reviews(page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    page = max(1, page)
    async with get_session() as session:
        total = (await session.execute(select(func.count(UserReview.id)))).scalar_one()
        if not total:
            return ("No reviews yet.", None)
        avg = (await session.execute(select(func.avg(UserReview.rating)))).scalar_one() or 0
        total_pages = (total + _REVIEWS_PAGE_SIZE - 1) // _REVIEWS_PAGE_SIZE
        page = min(page, total_pages)
        rows = (await session.execute(
            select(UserReview, User)
            .join(User, User.id == UserReview.user_id, isouter=True)
            .order_by(UserReview.created_at.desc())
            .limit(_REVIEWS_PAGE_SIZE)
            .offset((page - 1) * _REVIEWS_PAGE_SIZE)
        )).all()

    lines = [
        f"\u2b50 <b>Reviews</b> \u2014 <b>{total}</b> total, average "
        f"<b>{float(avg):.2f}/5</b>",
        "",
    ]
    for review, user in rows:
        name = (user.first_name if user and user.first_name else "(unknown)")
        when = review.created_at.astimezone(_IST).strftime("%d %b")
        comment = review.comment or "<i>(no comment)</i>"
        if len(comment) > 200:
            comment = comment[:200] + "\u2026"
        lines.append(
            f"{_stars(review.rating)} ({review.rating}/5) \u2014 <b>{name}</b> "
            f"<i>{when}</i>\n  {comment}"
        )
    return ("\n".join(lines), _reviews_kb(page, total_pages))


@router.message(Command("reviews"))
async def cmd_reviews(message: Message) -> None:
    op_id = _operator_chat_id()
    if op_id is None or (message.from_user is None) or message.from_user.id != op_id:
        # Pretend the command doesn't exist for non-operators \u2014 less surface.
        return
    text, kb = await _render_reviews(page=1)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("reviews:page:"))
async def cb_reviews_page(cq: CallbackQuery) -> None:
    op_id = _operator_chat_id()
    if op_id is None or cq.from_user is None or cq.from_user.id != op_id:
        await cq.answer("Operator only.", show_alert=True)
        return
    assert cq.data is not None
    try:
        page = int(cq.data.split(":")[2])
    except (IndexError, ValueError):
        await cq.answer()
        return
    text, kb = await _render_reviews(page=page)
    await cq.answer()
    if cq.message is not None:
        try:
            await cq.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except TelegramAPIError:
            pass  # identical content \u2014 swallow


@router.callback_query(F.data == "reviews:noop")
async def cb_reviews_noop(cq: CallbackQuery) -> None:
    await cq.answer()
