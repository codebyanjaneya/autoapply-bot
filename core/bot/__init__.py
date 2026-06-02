"""Bot package \u2014 routers exported for main.py to wire into the Dispatcher."""
from core.bot.contacts import router as contacts_router
from core.bot.handlers import router as commands_router
from core.bot.onboarding import router as onboarding_router
from core.bot.settings import router as settings_router
from core.bot.support import router as support_router

__all__ = [
    "onboarding_router", "settings_router", "contacts_router",
    "support_router", "commands_router",
]
