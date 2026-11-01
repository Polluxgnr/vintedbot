"""Telegram interface: guided hunt wizard, natural-language search, alerts with actions."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime
from datetime import time as dtime

from telegram import BotCommand, LinkPreviewOptions, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    Defaults,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

from ..ai.llm import BudgetExceeded
from ..config import RuntimeSettings
from ..models import Condition, Deal, SearchSpec
from ..config import Settings
from ..runtime import Services, build_services
from ..vinted.client import VintedUnavailable
from . import views as v

log = logging.getLogger(__name__)

VINTED_URL = re.compile(r"https?://(?:www\.)?vinted\.[a-z.]+/items/\d+[^\s]*")
Q_QUERY, Q_BUDGET = range(2)
INTENT_CONDITIONS = {"new_tags": Condition.NEW_WITH_TAGS, "new": Condition.NEW, "very_good": Condition.VERY_GOOD,
                     "good": Condition.GOOD, "satisfactory": Condition.SATISFACTORY}
QUIET_HOURS = [22, 23, 0, 1, 2, 3, 4, 5, 6]
RESULTS_PER_PAGE = 5


HUNT_STAGES = [("🔎", "Recherche sur Vinted"), ("🧮", "Filtres, cote & tri"), ("📖", "Lecture des descriptions"),
               ("🧠", "Analyse IA du texte"), ("📸", "Vérification des photos"), ("🎓", "Analyse experte"),
               ("📊", "Comparaison au marché en direct")]
_FRACTION = re.compile(r"(\d+)\s*/\s*(\d+)")


class Progress:
    """A live loading screen: one message, edited in place, showing every step as a checklist.

        ✅ Recherche sur Vinted — 248 annonces
        ⏳ Lecture des descriptions 8/15  ▓▓▓▓▓░░░░░
        ⬜ Analyse IA du texte
        ⏱ 23 s

    The engine reports plain lines starting with a stage emoji; a new emoji closes the previous stage.
    A ticker refreshes the timer (and Telegram's "typing…") so it never looks frozen.
    """

    def __init__(self, message, title: str, stages: list[tuple[str, str]] | None = None):
        self.message = message
        self.title = title
        self.planned = list(stages or HUNT_STAGES)
        self.done: dict[str, str] = {}
        self.current: tuple[str, str] | None = None
        self.started = time.monotonic()
        self._last_edit = 0.0
        self._rendered = ""
        self._ticker: asyncio.Task | None = None

    def start(self) -> "Progress":
        self._ticker = asyncio.create_task(self._tick())
        return self

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(4)
            try:
                await self.message.get_bot().send_chat_action(self.message.chat_id, ChatAction.TYPING)
            except TelegramError:
                pass
            await self._render(force=True)

    def stop(self) -> None:
        if self._ticker:
            self._ticker.cancel()

    async def __call__(self, text: str) -> None:
        emoji = text[:1]
        known = {e for e, _ in self.planned}
        if emoji not in known:                       # informative line (e.g. a Vinted pause): show it inline
            emoji = self.current[0] if self.current else "🔎"
        if self.current and self.current[0] != emoji:
            self.done[self.current[0]] = self.current[1]
        self.current = (emoji, text[1:].strip(" …."))
        await self._render()

    def _bar(self, text: str) -> str:
        m = _FRACTION.search(text)
        if not m or int(m.group(2)) == 0:
            return ""
        frac = min(1.0, int(m.group(1)) / int(m.group(2)))
        return "  " + "▓" * round(frac * 10) + "░" * (10 - round(frac * 10))

    async def _render(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_edit < 1.0:
            return
        lines = [f"<b>{self.title}</b>", ""]
        cur = self.current[0] if self.current else None
        reached = True
        for emoji, label in self.planned:
            if emoji in self.done:
                lines.append(f"✅ {html.escape(self.done[emoji])}")
            elif emoji == cur:
                lines.append(f"⏳ <b>{html.escape(self.current[1])}</b>{self._bar(self.current[1])}")
                reached = False
            elif reached and cur is None:
                lines.append(f"⬜ {label}")
            else:
                lines.append(f"⬜ {label}" if not reached else f"➖ {label}")
        lines.append(f"\n⏱ {time.monotonic() - self.started:.0f} s")
        text = "\n".join(lines)
        if text == self._rendered:
            return
        self._rendered, self._last_edit = text, time.monotonic()
        try:
            await self.message.edit_text(text)
        except TelegramError:
            pass


class HunterBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sv: Services
        self.app: Application | None = None

    @property
    def db(self):
        return self.sv.db

    @property
    def hunter(self):
        return self.sv.hunter

    # ================================================================ plumbing
    def build(self) -> Application:
        app = (
            ApplicationBuilder()
            .token(self.settings.telegram_token)
            # Default PTB pool is 1 connection: alerts + button presses queued behind each other -> "Timed out".
            .request(HTTPXRequest(connection_pool_size=32, connect_timeout=20, read_timeout=30, write_timeout=60,
                                  pool_timeout=30))
            .get_updates_request(HTTPXRequest(connection_pool_size=4, connect_timeout=20, read_timeout=40))
            .defaults(Defaults(parse_mode=ParseMode.HTML, link_preview_options=None))
            .concurrent_updates(True)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
            .build()
        )
        app.add_handler(TypeHandler(Update, self._gate), group=-1)

        wizard = ConversationHandler(
            entry_points=[
                CommandHandler("hunt", self.wiz_start),
                CallbackQueryHandler(self.wiz_start, pattern=r"^m:hunt$"),
            ],
            states={
                Q_QUERY: [CallbackQueryHandler(self.wiz_niche, pattern=r"^w:n:"),
                          MessageHandler(filters.TEXT & ~filters.COMMAND, self.wiz_query)],
                Q_BUDGET: [CallbackQueryHandler(self.wiz_budget, pattern=r"^w:b:"),
                           MessageHandler(filters.TEXT & ~filters.COMMAND, self.wiz_budget)],
            },
            fallbacks=[CommandHandler("cancel", self.wiz_cancel), CommandHandler("start", self.cmd_start)],
            allow_reentry=True,
        )
        app.add_handler(wizard)

        for name, fn in {
            "start": self.cmd_start, "menu": self.cmd_start, "niches": self.show_niches, "watches": self.show_watches,
            "analyze": self.cmd_analyze, "price": self.cmd_price, "stats": self.show_stats, "top": self.show_top,
            "portfolio": self.show_portfolio, "settings": self.show_settings, "help": self.show_help,
            "aide": self.show_help, "etude": self.show_study,
        }.items():
            app.add_handler(CommandHandler(name, fn))

        app.add_handler(CallbackQueryHandler(self.on_menu, pattern=r"^m:"))
        app.add_handler(CallbackQueryHandler(self.on_question, pattern=r"^q:"))
        app.add_handler(CallbackQueryHandler(self.on_watch_last, pattern=r"^sv$"))
        app.add_handler(CallbackQueryHandler(self.on_niche, pattern=r"^n:"))
        app.add_handler(CallbackQueryHandler(self.on_watch, pattern=r"^wa:"))
        app.add_handler(CallbackQueryHandler(self.on_deal, pattern=r"^d:"))
        app.add_handler(CallbackQueryHandler(self.on_setting, pattern=r"^s:"))
        app.add_handler(CallbackQueryHandler(self.on_more, pattern=r"^more$"))
        app.add_handler(CallbackQueryHandler(self.on_help, pattern=r"^h:"))
        app.add_handler(CallbackQueryHandler(self.on_radar, pattern=r"^(?:tp|rv|rr):"))
        app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern=r"^noop$"))
        app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer("Bouton expiré, relance la recherche")))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_error_handler(self._on_error)
        self.app = app
        return app

    async def _post_init(self, app: Application) -> None:
        # Services are created here so that aiosqlite/httpx live on the bot's event loop.
        self.sv = await build_services(self.settings)
        await app.bot.set_my_commands([
            BotCommand("hunt", "Nouvelle chasse guidée"), BotCommand("analyze", "Analyser un lien Vinted"),
            BotCommand("price", "Prix du marché d'un produit"), BotCommand("niches", "Niches surveillées"),
            BotCommand("watches", "Mes alertes"), BotCommand("top", "Radar : toutes les bonnes affaires, triables"),
            BotCommand("portfolio", "Achats / ventes / profit"), BotCommand("stats", "Statistiques & coût IA"),
            BotCommand("settings", "Réglages"), BotCommand("aide", "Comment sont calculés cote, confiance, score"),
            BotCommand("etude", "Étude des niches les plus rentables"),
            BotCommand("menu", "Menu principal"),
        ])
        app.bot_data["loop"] = asyncio.create_task(self.hunter.run_forever(self.notify))
        if app.job_queue:
            app.job_queue.run_daily(self.daily_digest, time=dtime(hour=9, tzinfo=datetime.now().astimezone().tzinfo))

    async def _post_shutdown(self, app: Application) -> None:
        if task := app.bot_data.get("loop"):
            task.cancel()
        if sv := getattr(self, "sv", None):     # absent when startup failed before post_init
            await sv.close()
            del self.sv

    async def _gate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if chat is None:
            raise ApplicationHandlerStop
        allowed = self.sv.settings.allowed_chats | await self.db.owners()
        if chat.id in allowed:
            return
        if not allowed and update.message and update.message.text and update.message.text.startswith("/start"):
            await self.db.add_owner(chat.id)          # first user claims a fresh bot
            await self._enable_default_niches(chat.id)
            log.info("Chat %s is now the bot owner", chat.id)
            return
        if update.message:
            await update.message.reply_text(f"⛔ Bot privé. Ton chat id : <code>{chat.id}</code>")
        raise ApplicationHandlerStop

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        if isinstance(err, BadRequest) and any(x in str(err) for x in ("not found", "not modified", "too old")):
            return
        if isinstance(err, (TimedOut, NetworkError)) and not isinstance(err, BadRequest):
            log.warning("Telegram network hiccup: %s", err)    # transient: never shown to the user
            return
        log.error("Handler error", exc_info=err)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await context.bot.send_message(update.effective_chat.id, f"⚠️ Erreur : {v.e(context.error)}")
            except TelegramError:
                pass

    @staticmethod
    async def _answer(update: Update, text: str | None = None) -> None:
        """Acknowledge a button press; a slow ack must never break the action itself."""
        if update.callback_query:
            try:
                await update.callback_query.answer(text)
            except TelegramError:
                pass

    async def _reply(self, update: Update, text: str, **kw):
        if update.callback_query:
            await self._answer(update)
            return await update.callback_query.message.reply_text(text, **kw)
        return await update.effective_message.reply_text(text, **kw)

    async def _rs(self, chat_id: int) -> RuntimeSettings:
        return await self.db.runtime_settings(chat_id)

    # ================================================================ deals out
    async def send_deal(self, chat_id: int, deal: Deal, *, personal: bool = False) -> None:
        caption = v.deal_caption(deal, personal=personal)
        kb = v.deal_keyboard(deal)
        bot = self.app.bot
        use_photo = bool(deal.listing.photos)
        for attempt in range(3):
            try:
                if use_photo:
                    await bot.send_photo(chat_id, deal.listing.photos[0], caption=caption, reply_markup=kb)
                else:
                    await bot.send_message(chat_id, caption, reply_markup=kb)
                return
            except RetryAfter as exc:
                wait = exc.retry_after
                await asyncio.sleep((wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)) + 1)
            except BadRequest as exc:
                if not use_photo:
                    raise
                log.debug("photo rejected (%s), sending text", exc)
                use_photo = False
            except (TimedOut, NetworkError) as exc:
                log.warning("send_deal retry %s: %s", attempt + 1, exc)
                await asyncio.sleep(2 * (attempt + 1))

    async def notify(self, chat_id: int, deal: Deal) -> None:
        rs = await self._rs(chat_id)
        if rs.alerts_paused or datetime.now().hour in rs.quiet_hours:
            return  # still stored: visible in /top
        try:
            await self.send_deal(chat_id, deal)
        except TelegramError:
            log.exception("could not push alert")

    async def _send_results(self, chat_id: int, deals: list[Deal], context: ContextTypes.DEFAULT_TYPE,
                            personal: bool) -> None:
        page, rest = deals[:RESULTS_PER_PAGE], deals[RESULTS_PER_PAGE:]
        for d in page:
            await self.db.save_deal(d, chat_id)
            await self.send_deal(chat_id, d, personal=personal)
        context.chat_data["more"] = (rest, personal)
        if rest:
            await self.app.bot.send_message(chat_id, f"➕ {len(rest)} autres résultats",
                                            reply_markup=v.Kb([[v.Btn("Voir la suite", callback_data="more")]]))

    async def on_more(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._answer(update)
        rest, personal = context.chat_data.pop("more", ([], False))
        try:
            await update.callback_query.message.delete()
        except TelegramError:
            pass
        await self._send_results(update.effective_chat.id, rest, context, personal)

    # ================================================================ hunt
    async def run_hunt(self, chat_id: int, spec: SearchSpec, context: ContextTypes.DEFAULT_TYPE) -> None:
        title = f"🔎 Chasse : {v.e(spec.describe())}"
        status = await self.app.bot.send_message(chat_id, f"<b>{title}</b>\n\n⏳ Démarrage…")
        progress = Progress(status, title).start()
        rs = await self._rs(chat_id)
        context.chat_data["last_spec"] = spec
        try:
            deals, niche = await self.hunter.hunt(spec, rs, progress)
        except VintedUnavailable as exc:
            await status.edit_text(f"⏳ {v.e(exc)} — réessaie dans quelques minutes.")
            return
        except Exception as exc:                       # never leave the user staring at a frozen screen
            log.exception("hunt failed")
            await status.edit_text(f"⚠️ La recherche a échoué ({v.e(type(exc).__name__)}). Réessaie dans un instant.")
            return
        finally:
            progress.stop()
        personal = spec.goal == "personal"
        min_profit = spec.min_profit or 0
        pepites = [d for d in deals if d.pepite]
        floor = max(15.0, min_profit * 0.5)
        good = pepites + [d for d in deals if not d.pepite and d.profit is not None and d.margin_pct is not None
                          and (personal or (d.profit >= floor and d.margin_pct >= 10))
                          and not (d.inspection and d.inspection.scams)]
        shown = (good or deals[:3])[:20]
        summary = f"✅ Chasse terminée : <b>{v.e(spec.describe())}</b>\n"
        if niche:
            summary += f"Niche : {v.e(niche.label)}\n"
        if pepites:
            summary += f"🔥 <b>{len(pepites)} pépite(s) vérifiée(s)</b> sur le marché en direct."
        elif good:
            best = good[0]
            summary += (f"Pas de pépite vérifiée, mais {len(good)} affaire(s) à ≥ {floor:.0f} € de profit — meilleure : "
                        f"<b>{best.profit:+.0f} €</b> ({v.e(best.listing.title[:40])}). "
                        "Chaque carte dit ce qui la bloque (🚫).")
        else:
            summary += ("Rien de rentable pour l'instant (les prix sont au niveau du marché). Voici les 3 plus proches 👇 "
                        "— active une alerte : je te préviens dès qu'une vraie affaire apparaît.")
        if self.hunter.ai_notice:
            summary += f"\n<i>ℹ️ {v.e(self.hunter.ai_notice)}</i>"
        summary += f"\n<i>⏱ {time.monotonic() - progress.started:.0f} s · {self.sv.vinted.requests_made} requêtes Vinted</i>"
        await status.edit_text(summary)
        await self._send_results(chat_id, shown, context, personal)
        await self.app.bot.send_message(
            chat_id, "Et maintenant ?", reply_markup=v.Kb([
                [v.Btn("👁 M'alerter sur cette recherche", callback_data="sv")],
                [v.Btn("🏆 Radar", callback_data="m:top"), v.Btn("🔎 Nouvelle chasse", callback_data="m:hunt")]]))

    async def add_watch(self, chat_id: int, spec: SearchSpec) -> None:
        niche = self.sv.niches.get(spec.niche_id)
        name = spec.query or (niche.name if niche else "Recherche")
        await self.db.add_watch(chat_id, "custom", name[:40], spec)
        await self.app.bot.send_message(
            chat_id, f"👁 Alerte créée : <b>{v.e(spec.describe())}</b>\nJe te préviens dès qu'une affaire passe la cascade.")

    async def on_watch_last(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        spec = context.chat_data.get("last_spec")
        await self._answer(update)
        await self._lock(update, "✅ Alerte activée" if spec else None)
        if spec:
            await self.add_watch(update.effective_chat.id, spec)

    @staticmethod
    async def _lock(update: Update, chosen: str | None) -> None:
        """Freeze the buttons of the message that was just answered (no double taps, visible choice)."""
        q = update.callback_query
        if not q or not q.message:
            return
        try:
            body = q.message.text_html or ""
            await q.message.edit_text(f"{body}\n<b>{v.e(chosen)}</b>" if chosen else body, reply_markup=None)
        except TelegramError:
            pass

    # ================================================================ guided hunt (2 steps)
    async def wiz_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data["draft"] = SearchSpec()
        await self._reply(
            update,
            "🔎 <b>Nouvelle chasse</b>\n\nQue cherches-tu ? Écris un produit, une marque, un modèle…\n"
            "<i>ex : « ps5 », « veste arc'teryx beta », « mac mini m2 », « lot lego »</i>\n\n"
            "…ou choisis une niche entière :",
            reply_markup=v.wizard_niches(list(self.sv.niches)),
        )
        return Q_QUERY

    async def wiz_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        draft: SearchSpec = context.user_data["draft"]
        draft.query = update.message.text.strip()[:80]
        await update.message.reply_text("💶 Budget maximum ? (ou écris un montant)", reply_markup=v.wizard_budget())
        return Q_BUDGET

    async def wiz_niche(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        draft: SearchSpec = context.user_data["draft"]
        draft.niche_id = update.callback_query.data.split(":", 2)[2]
        niche = self.sv.niches.get(draft.niche_id)
        await self._answer(update)
        await self._lock(update, f"✅ {niche.label}")
        await update.effective_chat.send_message("💶 Budget maximum ?", reply_markup=v.wizard_budget())
        return Q_BUDGET

    async def wiz_budget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        draft: SearchSpec = context.user_data.pop("draft", SearchSpec())
        raw = update.callback_query.data.split(":")[2] if update.callback_query else update.message.text
        m = re.search(r"\d+", raw or "")
        draft.price_max = float(m.group()) if m and int(m.group()) > 0 else None
        if update.callback_query:
            await self._answer(update)
            await self._lock(update, f"✅ ≤ {draft.price_max:.0f} €" if draft.price_max else "✅ Pas de limite")
        if draft.query:
            await self.understand(update, context, draft.query, base=draft)
        else:
            context.application.create_task(self.run_hunt(update.effective_chat.id, draft, context))
        return ConversationHandler.END

    async def wiz_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        context.user_data.pop("draft", None)
        await update.message.reply_text("Annulé.", reply_markup=v.main_menu())
        return ConversationHandler.END

    # ================================================================ free text
    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text.strip()
        chat_id = update.effective_chat.id
        awaiting = context.user_data.pop("awaiting", None)

        if url := VINTED_URL.search(text):
            await self.analyze(chat_id, url.group(0))
            return
        if awaiting == "analyze":
            await update.message.reply_text("Ce n'est pas un lien d'annonce Vinted (…/items/123…).")
            return
        if awaiting == "price":
            await self.price(chat_id, text)
            return
        if isinstance(awaiting, tuple) and awaiting[0] == "sold":
            m = re.search(r"\d+(?:[.,]\d+)?", text)
            if not m:
                await update.message.reply_text("Envoie juste le prix de vente, ex : 85")
                context.user_data["awaiting"] = awaiting
                return
            price = float(m.group().replace(",", "."))
            await self.db.set_deal_status(awaiting[1], "sold", sell_price=price)
            await update.message.reply_text(f"💸 Vente enregistrée à {price:.0f} €. Voir /portfolio")
            return

        await self.understand(update, context, text)

    async def understand(self, update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                         base: SearchSpec | None = None) -> None:
        """Free text -> search. At most 2 product-specific questions (version, storage, size...), then go."""
        chat = update.effective_chat
        thinking = await chat.send_message("🧭 Je comprends ta demande…")
        rs = await self._rs(chat.id)
        data: dict = {}
        if self.hunter.ai.enabled:
            try:
                data = await self.hunter.ai.parse_intent(text, list(self.sv.niches), rs)
            except (BudgetExceeded, RuntimeError) as exc:
                log.warning("intent parsing failed: %s", exc)
        spec = base or SearchSpec()
        spec.query = (data.get("query") or text)[:80]
        if self.sv.niches.get(data.get("niche_id")):
            spec.niche_id = data["niche_id"]
        elif not spec.niche_id and (niche := self.sv.niches.guess(spec.query)):
            spec.niche_id = niche.id
        spec.price_min = spec.price_min or data.get("price_min") or None
        spec.price_max = spec.price_max or data.get("price_max") or None
        spec.conditions = spec.conditions or sorted(
            {INTENT_CONDITIONS[c] for c in data.get("conditions") or [] if c in INTENT_CONDITIONS})
        spec.size_hint = spec.size_hint or data.get("size") or None
        if data.get("goal") == "personal":
            spec.goal = "personal"
        spec.min_profit = spec.min_profit or data.get("min_profit") or None
        spec.notes = data.get("notes") or spec.notes
        questions = [q for q in data.get("questions") or []
                     if isinstance(q, dict) and q.get("question") and len(q.get("options") or []) >= 2][:2]
        context.user_data["pending"] = {"spec": spec, "questions": questions, "step": 0,
                                        "watch": bool(data.get("watch"))}
        niche = self.sv.niches.get(spec.niche_id)
        understood = f"🧭 J'ai compris : <b>{v.e(spec.describe())}</b>" + (f" · {v.e(niche.label)}" if niche else "")
        try:
            await thinking.edit_text(understood)
        except TelegramError:
            pass
        await self._next_question(chat, context)

    async def _next_question(self, chat, context: ContextTypes.DEFAULT_TYPE) -> None:
        pending = context.user_data.get("pending")
        if not pending:
            return
        if pending["step"] < len(pending["questions"]):
            q = pending["questions"][pending["step"]]
            buttons = [v.Btn(str(o.get("label", "?"))[:30], callback_data=f"q:{pending['step']}:{i}")
                       for i, o in enumerate(q["options"][:6])]
            rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
            await chat.send_message(f"❓ <b>{v.e(q['question'])}</b>", reply_markup=v.Kb(rows))
            return
        context.user_data.pop("pending", None)
        spec: SearchSpec = pending["spec"]
        if pending["watch"]:
            await self.add_watch(chat.id, spec)
        context.application.create_task(self.run_hunt(chat.id, spec, context))

    async def on_question(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _, step, opt = update.callback_query.data.split(":")
        pending = context.user_data.get("pending")
        if not pending or int(step) != pending["step"]:
            await self._answer(update, "Question déjà répondue")
            return
        option = pending["questions"][int(step)]["options"][int(opt)]
        await self._answer(update)
        await self._lock(update, f"✅ {option.get('label', '')}")
        value = str(option.get("value") or "").strip()
        spec: SearchSpec = pending["spec"]
        if value and value.lower() not in spec.query.lower():
            spec.query = f"{spec.query} {value}"[:80]
        pending["step"] += 1
        await self._next_question(update.effective_chat, context)

    # ================================================================ analyze / price
    async def analyze(self, chat_id: int, url: str) -> None:
        status = await self.app.bot.send_message(chat_id, "🔗 Analyse de l'annonce…")
        progress = Progress(status, "🔗 Analyse de l'annonce", [
            ("🔗", "Lecture de l'annonce"), ("🧮", "Cote & filtres"), ("📖", "Description"), ("🧠", "Analyse IA du texte"),
            ("📸", "Vérification des photos"), ("🎓", "Analyse experte"), ("📊", "Comparaison au marché en direct")]).start()
        try:
            deal = await self.hunter.analyze_url(url, await self._rs(chat_id), progress)
        except VintedUnavailable as exc:
            await status.edit_text(f"⏳ {v.e(exc)}")
            return
        except Exception as exc:
            log.exception("analyze failed")
            await status.edit_text(f"⚠️ Analyse impossible ({v.e(type(exc).__name__)}). Réessaie dans un instant.")
            return
        finally:
            progress.stop()
        if not deal:
            await status.edit_text("❌ Annonce introuvable (vendue ou supprimée ?)")
            return
        await self.db.save_deal(deal, chat_id)
        await status.delete()
        await self.send_deal(chat_id, deal)
        if deal.verdict and deal.verdict.tier >= 4:
            await self.app.bot.send_message(chat_id, v.deep_report(deal))
        elif self.hunter.ai_notice:
            await self.app.bot.send_message(chat_id, f"ℹ️ {v.e(self.hunter.ai_notice)}")

    async def price(self, chat_id: int, query: str) -> None:
        status = await self.app.bot.send_message(chat_id, f"💶 J'étudie le marché de « {v.e(query)} »…")
        try:
            report = await self.hunter.price_check(query)
        except VintedUnavailable as exc:
            await status.edit_text(f"⏳ {v.e(exc)}")
            return
        await status.edit_text(v.price_report(report), link_preview_options=LinkPreviewOptions(is_disabled=True))

    async def cmd_analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if context.args:
            await self.analyze(update.effective_chat.id, context.args[0])
        else:
            context.user_data["awaiting"] = "analyze"
            await self._reply(update, "🔗 Envoie-moi le lien de l'annonce Vinted.")

    async def cmd_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if context.args:
            await self.price(update.effective_chat.id, " ".join(context.args))
        else:
            context.user_data["awaiting"] = "price"
            await self._reply(update, "💶 Quel produit ? (ex : « airpods pro 2 », « contax t2 »)")

    # ================================================================ menus & screens
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await self._reply(update, v.WELCOME, reply_markup=v.main_menu())
        return ConversationHandler.END

    async def on_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        action = update.callback_query.data.split(":")[1]
        handler = {"niches": self.show_niches, "watches": self.show_watches, "analyze": self.cmd_analyze,
                   "help": self.show_help, "study": self.show_study,
                   "price": self.cmd_price, "stats": self.show_stats, "top": self.show_top,
                   "pf": self.show_portfolio, "settings": self.show_settings}.get(action)
        if handler:
            context.args = []
            await handler(update, context)

    async def _enable_default_niches(self, chat_id: int) -> None:
        for niche in self.sv.niches:
            if niche.enabled_by_default and not await self.db.niche_watch(chat_id, niche.id):
                await self.db.add_watch(chat_id, "niche", niche.name, SearchSpec(niche_id=niche.id))

    async def _niche_screen(self, chat_id: int) -> tuple[str, v.Kb]:
        active = {w["niche_id"] for w in await self.db.watches(chat_id) if w["kind"] == "niche" and w["active"]}
        return v.niches_view(list(self.sv.niches), active)

    async def show_niches(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text, kb = await self._niche_screen(update.effective_chat.id)
        await self._reply(update, text, reply_markup=kb)

    async def on_niche(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        chat_id = update.effective_chat.id
        _, action, arg = q.data.split(":", 2)
        targets = [self.sv.niches.get(arg)] if action == "t" else list(self.sv.niches)
        for niche in filter(None, targets):
            w = await self.db.niche_watch(chat_id, niche.id)
            want = (not (w and w["active"])) if action == "t" else arg == "1"
            if w:
                await self.db.set_watch_active(w["id"], want)
            elif want:
                await self.db.add_watch(chat_id, "niche", niche.name, SearchSpec(niche_id=niche.id))
        await self._answer(update, "Mis à jour")
        text, kb = await self._niche_screen(chat_id)
        try:
            await q.message.edit_text(text, reply_markup=kb)
        except BadRequest:
            pass

    async def show_watches(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text, kb = v.watches_view([w for w in await self.db.watches(update.effective_chat.id)])
        await self._reply(update, text, reply_markup=kb)

    async def on_watch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        _, action, wid = q.data.split(":")
        watches = {w["id"]: w for w in await self.db.watches(update.effective_chat.id)}
        w = watches.get(int(wid))
        if not w:
            await self._answer(update, "Introuvable")
            return
        if action == "p":
            await self.db.set_watch_active(w["id"], not w["active"])
        elif action == "d":
            await self.db.delete_watch(w["id"])
        await self._answer(update, "OK")
        text, kb = v.watches_view(await self.db.watches(update.effective_chat.id))
        await q.message.edit_text(text, reply_markup=kb)

    async def show_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        rs = await self._rs(update.effective_chat.id)
        text, kb = v.settings_view(rs, await self.db.ai_spend_today())
        await self._reply(update, text, reply_markup=kb)

    async def on_setting(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        chat_id = update.effective_chat.id
        _, key, op = q.data.split(":")
        rs = await self._rs(chat_id)
        if op == "t":
            if key == "quiet_hours":
                rs.quiet_hours = [] if rs.quiet_hours else QUIET_HOURS
            elif key == "alert_mode":
                rs.alert_mode = "normal" if rs.alert_mode == "pepites" else "pepites"
            else:
                setattr(rs, key, not getattr(rs, key))
        elif key in v.SETTINGS_FIELDS:
            step = v.SETTINGS_FIELDS[key][2]
            value = max(0, getattr(rs, key) + (step if op == "+" else -step))
            setattr(rs, key, type(getattr(rs, key))(value))
        await self.db.save_runtime_settings(chat_id, rs)
        await self._answer(update, "Enregistré")
        text, kb = v.settings_view(rs, await self.db.ai_spend_today())
        try:
            await q.message.edit_text(text, reply_markup=kb)
        except BadRequest:
            pass

    async def show_study(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from ..study import format_study, niche_study

        msg = await self._reply(update, "📊 Analyse de la base de données…")
        study = await niche_study(self.db, self.sv.niches)
        try:
            await msg.edit_text(format_study(study))
        except TelegramError:
            await update.effective_chat.send_message(format_study(study))

    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, v.HELP_INTRO, reply_markup=v.help_keyboard())

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        topic = update.callback_query.data.split(":", 1)[1]
        await self._answer(update)
        try:
            await update.callback_query.message.edit_text(v.HELP_PAGES[topic], reply_markup=v.help_keyboard(topic))
        except BadRequest:
            await update.callback_query.message.reply_text(v.HELP_PAGES[topic], reply_markup=v.help_keyboard(topic))

    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        s = await self.db.stats(chat_id)
        rs = await self._rs(chat_id)
        usage = await self.db.ai_usage_today()
        tiers = {1: "intent/négo", 2: "tri texte", 3: "vision", 4: "expert", 5: "expert+"}
        lines = [
            "📊 <b>Statistiques</b>\n",
            f"🔎 Annonces analysées 24 h : <b>{s['seen_24h']}</b> (total {s['seen_total']})",
            f"🔔 Alertes 24 h : <b>{s['alerts_24h']}</b> · profit potentiel {s['potential_24h']:.0f} €",
            f"💸 Ventes réalisées : {s['sold']} · profit réel <b>{s['realized']:.0f} €</b>",
            f"🌐 Requêtes Vinted (session) : {self.sv.vinted.requests_made}"
            + (f" · ⏳ pause {self.sv.vinted.cooldown_remaining}s" if self.sv.vinted.circuit_open else ""),
            f"\n🧠 <b>IA aujourd'hui</b> : {await self.db.ai_spend_today():.4f}$ / {rs.ai_daily_budget_usd:g}$",
        ]
        for row in usage:
            lines.append(f"  • {tiers.get(row['tier'], row['tier'])} — {row['calls']} appels · {row['cost']:.4f}$ "
                         f"<i>({v.e(row['model'].split('/')[-1])})</i>")
        lines += await self._database_lines()
        learn = await self.db.learning_summary()
        lines.append(f"\n🌾 <b>Récolte de données</b> : {self.hunter.harvested} nouvelles annonces stockées depuis le "
                     f"démarrage ({self.hunter._harvest_steps} pages parcourues)")
        lines.append(f"\n🧠 <b>Apprentissage</b> : {learn['models']} modèles calibrés sur {learn['obs']} annonces · "
                     f"{learn['discovered']} modèles découverts par l'IA")
        for m in learn["movers"]:
            lines.append(f"  {'📈' if m['trend_pct'] > 0 else '📉'} {v.e(m['model_key'].split(':')[-1])} "
                         f"{m['trend_pct']:+.0f} % (≈ {m['calibrated']:.0f} €)")
        if self.hunter.ai_notice:
            lines.append(f"\nℹ️ {v.e(self.hunter.ai_notice)}")
        await self._reply(update, "\n".join(lines))

    async def _database_lines(self) -> list[str]:
        totals = await self.db.tracking_totals()
        lines = [f"\n🗄 <b>Base de ventes</b> : {sum(totals.values())} annonces suivies · "
                 f"{totals.get('sold', 0)} vendues · {totals.get('reserved', 0)} réservées · "
                 f"{totals.get('gone', 0)} disparues"]
        top = await self.db.top_models()
        if top:
            lines.append("⚡ <b>Modèles qui partent le plus vite</b>")
            for r in top:
                ref = None
                if ":" in r["model_key"]:
                    nid, key = r["model_key"].split(":", 1)
                    niche = self.sv.niches.get(nid)
                    ref = next((m for m in niche.models if m.key == key), None) if niche else None
                name = ref.name if ref else r["model_key"]
                lines.append(f"  • {v.e(name)} — {r['sold']} ventes, ~{r['days']:.1f} j, ~{r['price']:.0f} €")
        return lines

    async def daily_digest(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """9:00 every day: yesterday's pépites, the sales database, the hottest models."""
        for chat_id in self.settings.allowed_chats | await self.db.owners():
            s = await self.db.stats(chat_id)
            pepites = await self.db.top_deals(chat_id, hours=24, limit=3)
            lines = ["☀️ <b>Résumé du jour</b>\n",
                     f"🔎 {s['seen_24h']} annonces analysées · 🔥 {s['alerts_24h']} pépite(s) "
                     f"(potentiel {s['potential_24h']:.0f} €)",
                     f"🧠 IA hier : {await self.db.ai_spend_today():.3f} $"]
            for d in pepites:
                lines.append(f"  • <a href=\"{v.e(d.listing.url)}\">{v.e(d.listing.title[:45])}</a> — "
                             f"{d.listing.price:.0f} € → {d.profit or 0:+.0f} €")
            lines += await self._database_lines()
            try:
                await context.bot.send_message(chat_id, "\n".join(lines),
                                               link_preview_options=LinkPreviewOptions(is_disabled=True))
            except TelegramError:
                log.warning("digest not delivered to %s", chat_id)

    async def _radar_screen(self, st: dict) -> tuple[str, v.Kb]:
        per_page = 8
        mp = v.RADAR_MIN_PROFITS[st["mp"]]
        deals, total = await self.db.radar(hours=st["hours"], sort=st["sort"], niche=st["niche"], pepites=st["pep"],
                                           analysed=st["ana"], min_profit=mp, limit=per_page,
                                           offset=st["page"] * per_page)
        labels = {n.id: n.label for n in self.sv.niches}
        niches = await self.db.radar_niches(st["hours"], pepites=st["pep"], analysed=st["ana"], min_profit=mp)
        return v.radar_view(deals, total, st, niches, labels, per_page)

    async def show_top(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text, kb = await self._radar_screen(v.radar_state())
        await self._reply(update, text, reply_markup=kb, link_preview_options=LinkPreviewOptions(is_disabled=True))

    async def on_radar(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q.data.startswith("rv:"):
            await self._answer(update)
            deal = await self.db.get_deal(int(q.data.split(":")[1]))
            if deal:
                await self.send_deal(update.effective_chat.id, deal)
            return
        if q.data.startswith("rr:"):
            await self._answer(update, "Vérification en direct sur Vinted…")
            st = v.radar_state("tp:" + q.data[3:])
            await self._refresh_radar(q, st)
            return
        await self._answer(update)
        text, kb = await self._radar_screen(v.radar_state(q.data))
        try:
            await q.message.edit_text(text, reply_markup=kb, link_preview_options=LinkPreviewOptions(is_disabled=True))
        except BadRequest:
            pass

    async def _refresh_radar(self, q, st: dict) -> None:
        """Re-check the listings on screen: sold/deleted ones disappear, prices are refreshed."""
        deals, _ = await self.db.radar(hours=st["hours"], sort=st["sort"], niche=st["niche"], pepites=st["pep"],
                                       analysed=st["ana"], min_profit=v.RADAR_MIN_PROFITS[st["mp"]], limit=8,
                                       offset=st["page"] * 8)
        removed = 0
        for i, d in enumerate(deals, 1):
            try:
                await q.message.edit_text(f"🔄 Vérification en direct {i}/{len(deals)}…")
            except TelegramError:
                pass
            try:
                status, price = await self.hunter.check_availability(d.listing.id)
            except VintedUnavailable:
                break
            await self.db.update_radar_price(d.listing.id, status, price)
            removed += status in ("sold", "gone")
        text, kb = await self._radar_screen(st)
        note = f"\n\n<i>✅ Vérifié à l'instant · {removed} annonce(s) vendue(s)/retirée(s) enlevée(s)</i>"
        try:
            await q.message.edit_text(text + note, reply_markup=kb, link_preview_options=LinkPreviewOptions(is_disabled=True))
        except BadRequest:
            pass

    async def show_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        rows = await self.db.portfolio(update.effective_chat.id)
        if not rows:
            await self._reply(update, "💼 Portefeuille vide. Clique « ✅ Acheté » sur une alerte pour suivre ton profit.")
            return
        lines, kb_rows, invested, realized = ["💼 <b>Portefeuille</b>\n"], [], 0.0, 0.0
        for r in rows:
            d = Deal.model_validate_json(r["data"])
            title = v.e(d.listing.title[:38])
            invested += r["buy_price"] or 0
            if r["status"] == "sold":
                gain = (r["sell_price"] or 0) - (r["buy_price"] or 0)
                realized += gain
                lines.append(f"✅ {title} — acheté {r['buy_price']:.0f} € → vendu {r['sell_price']:.0f} € (<b>{gain:+.0f} €</b>)")
            else:
                lines.append(f"📦 {title} — acheté {r['buy_price']:.0f} €, cote {r['fair_value'] or 0:.0f} €")
                kb_rows.append([v.Btn(f"💸 Vendu : {d.listing.title[:25]}", callback_data=f"d:sold:{r['listing_id']}")])
        lines.append(f"\nInvesti : <b>{invested:.0f} €</b> · profit réalisé : <b>{realized:+.0f} €</b>")
        await self._reply(update, "\n".join(lines), reply_markup=v.Kb(kb_rows) if kb_rows else None)

    # ================================================================ deal actions
    async def on_deal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        chat_id = update.effective_chat.id
        _, action, raw_id = q.data.split(":")
        lid = int(raw_id)
        deal = await self.db.get_deal(lid)
        if not deal:
            await self._answer(update, "Annonce plus en mémoire")
            return
        rs = await self._rs(chat_id)

        if action == "why":
            await self._answer(update)
            await q.message.reply_text(v.explain_deal(deal), reply_markup=v.Kb(
                [[v.Btn("ℹ️ Comprendre les règles", callback_data="h:cote")]]))
            return

        if action in ("deep", "exp"):
            await self._answer(update, "Analyse en cours… (~20 s)")
            wait = await q.message.reply_text("🎓 Analyse approfondie : photos, cote, risques…")
            try:
                deal = await self.hunter.deepen(deal, rs, expert=action == "exp")
            except (BudgetExceeded, RuntimeError) as exc:
                await wait.edit_text(f"⚠️ {v.e(exc)}")
                return
            await self.db.save_deal(deal, chat_id)
            await wait.edit_text(v.deep_report(deal), reply_markup=v.deal_keyboard(deal))
        elif action == "neg":
            await self._answer(update)
            msg = deal.verdict.negotiation_message if deal.verdict else None
            offer = deal.verdict.max_buy_price if deal.verdict else None
            if not msg:
                try:
                    data = await self.hunter.ai.negotiation(deal, rs)
                    msg, offer = data.get("message"), data.get("offer_eur")
                except (BudgetExceeded, RuntimeError) as exc:
                    await q.message.reply_text(f"⚠️ {v.e(exc)}")
                    return
            await q.message.reply_text(
                f"💬 <b>Message à copier</b>{f' (offre ~{float(offer):.0f} €)' if offer else ''}\n<code>{v.e(msg)}</code>")
        elif action == "buy":
            await self.db.set_deal_status(lid, "bought", buy_price=deal.landed_cost)
            await self._answer(update, "Ajouté au portefeuille 💼")
            await q.message.reply_text(
                f"💼 Acheté pour {deal.landed_cost:.0f} € (frais inclus). Cote estimée {deal.valuation.fair_value or 0:.0f} €.",
                reply_markup=v.Kb([[v.Btn("💸 Marquer vendu", callback_data=f"d:sold:{lid}")]]))
            if deal.verdict and deal.verdict.resale_title:
                await q.message.reply_text(v.deep_report(deal))
        elif action == "sold":
            await self._answer(update)
            context.user_data["awaiting"] = ("sold", lid)
            await q.message.reply_text("💸 À quel prix l'as-tu vendu ?")
        elif action == "ign":
            await self.db.set_deal_status(lid, "ignored")
            await self._answer(update, "Ignoré")
            try:
                await q.message.delete()
            except TelegramError:
                pass


def run(settings: Settings) -> None:
    """Run forever: a dropped connection (Wi-Fi, DNS, sleep mode) restarts the bot instead of killing it."""
    import time

    delay = 10
    while True:
        asyncio.set_event_loop(asyncio.new_event_loop())   # run_polling closes its loop on exit
        bot = HunterBot(settings)
        try:
            # bootstrap_retries=-1: keep retrying the initial connection instead of aborting
            bot.build().run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True,
                                    bootstrap_retries=-1)
            return                                    # clean stop (Ctrl+C)
        except (NetworkError, TimedOut, RuntimeError) as exc:
            log.warning("No connection to Telegram (%s), retrying in %ss", exc, delay)
            time.sleep(delay)
            delay = min(delay * 2, 300)
