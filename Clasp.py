import logging
import os
import time
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, date

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --- CONFIGURATION ---

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "VOTRE_TOKEN_TELEGRAM_ICI")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Région des bookmakers interrogée (eu = zone où 1xbet est le plus souvent listé)
ODDS_API_REGIONS = os.getenv("ODDS_API_REGIONS", "eu")

# Seuil de sélection : on ne garde un favori que si sa cote décimale est <= à ce seuil
# (cote 1.25 <=> probabilité implicite ~80%)
SEUIL_COTE_MAX = 1.25

# Nombre maximum de ligues/sport_keys interrogés en une seule analyse, pour ne pas
# vider le quota mensuel de l'API (plan gratuit = 500 requêtes/mois)
MAX_SPORT_KEYS_PAR_ANALYSE = int(os.getenv("ODDS_API_MAX_CALLS_PER_ANALYSE", "15"))

# Mots-clés utilisés pour repérer le bookmaker de référence dans la liste renvoyée par l'API
BOOKMAKER_PREFERE_MOTS_CLES = ["1xbet", "onexbet", "one x bet"]

# Port fourni par Render pour son scan de port (obligatoire sur un "Web Service").
# Le bot Telegram fonctionne en polling et n'a pas besoin de ce port pour lui-même :
# on ouvre juste un petit serveur HTTP factice pour satisfaire Render.
PORT_KEEPALIVE = int(os.getenv("PORT", "10000"))

# Mappage : sport affiché à l'utilisateur -> "group" tel que renvoyé par /v4/sports
SPORTS_DISPONIBLES = {
    "football": ("⚽ Football", "Soccer"),
    "basketball": ("🏀 Basketball", "Basketball"),
    "tennis": ("🎾 Tennis", "Tennis"),
    "handball": ("🤾 Handball", "Handball"),
    "volleyball": ("🏐 Volleyball", "Volleyball"),
    "hockey": ("🏒 Hockey", "Ice Hockey"),
}

# Cache mémoire du catalogue /v4/sports (évite de le rappeler à chaque analyse)
_CACHE_SPORTS = {"data": None, "timestamp": 0}
CACHE_TTL_SECONDES = 6 * 3600  # 6 heures


# --- COMMANDES DE BASE ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Message d'accueil."""
    welcome_text = (
        "👋 **Bienvenue sur Clashprono !**\n\n"
        f"🎯 **Stratégie :** sélection des favoris dont la cote est **≤ {SEUIL_COTE_MAX}** "
        "(≈80% de probabilité implicite), via TheOddsAPI.\n\n"
        "📌 **Commandes :**\n"
        "• `/analyser` : choisissez vos sports, puis indiquez deux dates à scanner.\n"
        "• `/ticket` : consultez vos sélections enregistrées."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def analyser_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lancement de la sélection des sports."""
    context.user_data["sports_selectionnes"] = []
    context.user_data["attente_dates"] = False
    await afficher_menu_sports(update, context)


async def afficher_menu_sports(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None) -> None:
    """Affiche le menu interactif de sélection des sports."""
    sports_choisis = context.user_data.get("sports_selectionnes", [])

    keyboard = []
    for key, (label, _group) in SPORTS_DISPONIBLES.items():
        is_checked = key in sports_choisis
        icon = "✅ " if is_checked else "⏹️ "
        keyboard.append([InlineKeyboardButton(f"{icon}{label}", callback_data=f"toggle_{key}")])

    keyboard.append([
        InlineKeyboardButton(f"🚀 Continuer (favoris ≤ {SEUIL_COTE_MAX})", callback_data="valider_sports")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = (
        "🎯 **Étape 1 : Sélectionnez vos sports**\n"
        f"Le bot va interroger TheOddsAPI et retenir les favoris avec une cote **≤ {SEUIL_COTE_MAX}**."
    )

    if query:
        await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")


# --- GESTION DES BOUTONS INTERACTIFS ---

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    user_data = context.user_data

    if "sports_selectionnes" not in user_data:
        user_data["sports_selectionnes"] = []

    if data.startswith("toggle_"):
        sport_key = data.replace("toggle_", "")
        if sport_key in user_data["sports_selectionnes"]:
            user_data["sports_selectionnes"].remove(sport_key)
        else:
            user_data["sports_selectionnes"].append(sport_key)
        await afficher_menu_sports(update, context, query=query)

    elif data == "valider_sports":
        sports_selectionnes = user_data.get("sports_selectionnes", [])
        if not sports_selectionnes:
            await query.answer("⚠️ Veuillez sélectionner au moins 1 sport.", show_alert=True)
            return

        user_data["attente_dates"] = True
        await query.edit_message_text(
            "🗓️ **Étape 2 : Choisissez les deux jours à scanner**\n\n"
            "Envoyez-moi les deux dates au format `JJ/MM/AAAA JJ/MM/AAAA` "
            "(séparées par un espace), par exemple :\n"
            "`14/08/2026 15/08/2026`",
            parse_mode="Markdown",
        )

    elif data.startswith("pari_"):
        idx_str = data.replace("pari_", "")
        derniers_matchs = user_data.get("derniers_matchs", [])
        try:
            idx = int(idx_str)
            match = derniers_matchs[idx]
        except (ValueError, IndexError):
            await query.answer("⚠️ Cette sélection n'est plus disponible.", show_alert=True)
            return

        if "ticket" not in user_data:
            user_data["ticket"] = []

        user_data["ticket"].append({
            "match": f"{match['equipe1']} vs {match['equipe2']}",
            "pronostic": match["favori"],
            "cote": match["cote_favori"],
        })

        await query.answer(f"✅ Ajouté au ticket ! ({match['favori']} @ {match['cote_favori']})", show_alert=True)

    elif data == "vider_ticket":
        user_data["ticket"] = []
        await query.edit_message_text("🗑️ Votre ticket a été réinitialisé.")


# --- SAISIE DES DEUX DATES (message texte) ---

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Traite le message texte contenant les deux dates, une fois les sports validés."""
    user_data = context.user_data
    if not user_data.get("attente_dates"):
        return  # message hors contexte, on ignore

    texte = update.message.text.strip()
    dates = parser_deux_dates(texte)

    if dates is None:
        await update.message.reply_text(
            "❌ Format non reconnu. Merci d'envoyer deux dates au format "
            "`JJ/MM/AAAA JJ/MM/AAAA`, par exemple `14/08/2026 15/08/2026`.",
            parse_mode="Markdown",
        )
        return

    date1, date2 = dates
    user_data["attente_dates"] = False
    sports_selectionnes = user_data.get("sports_selectionnes", [])

    msg_attente = await update.message.reply_text(
        f"🔎 **Analyse en cours sur TheOddsAPI**\n"
        f"Sports : {', '.join(sports_selectionnes)}\n"
        f"Dates : {date1.strftime('%d/%m/%Y')} et {date2.strftime('%d/%m/%Y')}...",
        parse_mode="Markdown",
    )

    await executer_analyse(update.message, context, sports_selectionnes, date1, date2)
    try:
        await msg_attente.delete()
    except Exception:
        pass


def parser_deux_dates(texte: str):
    """Parse un texte du type 'JJ/MM/AAAA JJ/MM/AAAA' -> (date, date) ou None si invalide."""
    morceaux = [m for m in texte.replace(",", " ").split() if m]
    if len(morceaux) != 2:
        return None

    resultats = []
    for morceau in morceaux:
        parsed = None
        for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d/%m"):
            try:
                d = datetime.strptime(morceau, fmt)
                if fmt == "%d/%m":
                    d = d.replace(year=date.today().year)
                parsed = d.date()
                break
            except ValueError:
                continue
        if parsed is None:
            return None
        resultats.append(parsed)

    return resultats[0], resultats[1]


# --- MOTEUR D'ANALYSE VIA THEODDSAPI ---

async def executer_analyse(message_source, context: ContextTypes.DEFAULT_TYPE, sports: list, date1: date, date2: date) -> None:
    """Interroge TheOddsAPI, filtre les favoris <= SEUIL_COTE_MAX sur les deux dates données."""
    if not ODDS_API_KEY:
        await context.bot.send_message(
            chat_id=message_source.chat_id,
            text="❌ Aucune clé TheOddsAPI configurée (variable ODDS_API_KEY manquante)."
        )
        return

    matchs_qualifies = await asyncio.to_thread(analyser_matchs_sync, sports, date1, date2)

    if matchs_qualifies is None:
        await context.bot.send_message(
            chat_id=message_source.chat_id,
            text="❌ Erreur lors de l'appel à TheOddsAPI (voir logs). Réessayez plus tard."
        )
        return

    if not matchs_qualifies:
        await context.bot.send_message(
            chat_id=message_source.chat_id,
            text=(
                f"❌ Aucun favori avec une cote ≤ {SEUIL_COTE_MAX} trouvé pour les sports et dates choisis.\n"
                "Il est possible que ce soit un jour creux pour ces sports — essayez d'autres dates."
            )
        )
        return

    context.user_data["derniers_matchs"] = matchs_qualifies

    for idx, match in enumerate(matchs_qualifies):
        avertissement_bookmaker = ""
        if not match["bookmaker_est_1xbet"]:
            avertissement_bookmaker = f"\n⚠️ Cote issue de *{match['bookmaker_nom']}* (1xbet indisponible sur ce match)"

        text_match = (
            f"🏆 **{match['sport_titre']}**\n"
            f"🏟️ **{match['equipe1']}** vs **{match['equipe2']}**\n"
            f"⏰ Coup d'envoi : {match['date_heure']}\n"
            f"🔥 **Favori (cote ≤ {SEUIL_COTE_MAX}) :** {match['favori']} @ {match['cote_favori']}"
            f"{avertissement_bookmaker}"
        )

        keyboard = [[
            InlineKeyboardButton(
                f"Sélectionner : {match['favori']} @ {match['cote_favori']}",
                callback_data=f"pari_{idx}"
            )
        ]]
        markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(
            chat_id=message_source.chat_id, text=text_match, reply_markup=markup, parse_mode="Markdown"
        )


def obtenir_catalogue_sports():
    """Retourne (et met en cache) la liste des sports actifs renvoyée par /v4/sports."""
    maintenant = time.time()
    if _CACHE_SPORTS["data"] is not None and (maintenant - _CACHE_SPORTS["timestamp"]) < CACHE_TTL_SECONDES:
        return _CACHE_SPORTS["data"]

    resp = requests.get(f"{ODDS_API_BASE}/sports", params={"apiKey": ODDS_API_KEY}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    _CACHE_SPORTS["data"] = data
    _CACHE_SPORTS["timestamp"] = maintenant
    return data


def resoudre_sport_keys(sports_cibles: list) -> list:
    """Convertit les catégories choisies par l'utilisateur en sport_keys actifs de TheOddsAPI."""
    groupes_cibles = {SPORTS_DISPONIBLES[s][1] for s in sports_cibles if s in SPORTS_DISPONIBLES}
    catalogue = obtenir_catalogue_sports()

    sport_keys = [
        entree["key"] for entree in catalogue
        if entree.get("group") in groupes_cibles and entree.get("active") and not entree.get("has_outrights")
    ]

    if len(sport_keys) > MAX_SPORT_KEYS_PAR_ANALYSE:
        logger.warning(
            f"{len(sport_keys)} ligues correspondent à la sélection, "
            f"analyse limitée aux {MAX_SPORT_KEYS_PAR_ANALYSE} premières pour préserver le quota API."
        )
        sport_keys = sport_keys[:MAX_SPORT_KEYS_PAR_ANALYSE]

    return sport_keys


def choisir_bookmaker(bookmakers: list):
    """Cherche 1xbet parmi les bookmakers du match, sinon retourne le premier disponible."""
    for bk in bookmakers:
        titre = bk.get("title", "").lower()
        if any(mot in titre for mot in BOOKMAKER_PREFERE_MOTS_CLES):
            return bk, True
    if bookmakers:
        return bookmakers[0], False
    return None, False


def analyser_matchs_sync(sports_cibles: list, date1: date, date2: date):
    """Version synchrone (exécutée dans un thread) : interroge TheOddsAPI et filtre les favoris."""
    try:
        sport_keys = resoudre_sport_keys(sports_cibles)
    except requests.RequestException as e:
        logger.error(f"Erreur lors de la récupération du catalogue de sports : {e}")
        return None

    if not sport_keys:
        return []

    dates_visees = {date1, date2}
    matchs_qualifies = []

    for sport_key in sport_keys:
        try:
            resp = requests.get(
                f"{ODDS_API_BASE}/sports/{sport_key}/odds",
                params={
                    "apiKey": ODDS_API_KEY,
                    "regions": ODDS_API_REGIONS,
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                },
                timeout=15,
            )
            if resp.status_code != 200:
                logger.error(f"Erreur HTTP {resp.status_code} pour {sport_key} : {resp.text[:200]}")
                continue
            evenements = resp.json()
        except requests.RequestException as e:
            logger.error(f"Erreur réseau pour {sport_key} : {e}")
            continue

        for evt in evenements:
            try:
                commence = datetime.fromisoformat(evt["commence_time"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue

            if commence.date() not in dates_visees:
                continue

            bookmakers = evt.get("bookmakers", [])
            bk, est_1xbet = choisir_bookmaker(bookmakers)
            if bk is None:
                continue

            marches_h2h = [m for m in bk.get("markets", []) if m.get("key") == "h2h"]
            if not marches_h2h:
                continue

            outcomes = marches_h2h[0].get("outcomes", [])
            if not outcomes:
                continue

            meilleur = min(outcomes, key=lambda o: o.get("price", float("inf")))
            cote_favori = meilleur.get("price")

            if cote_favori is None or cote_favori > SEUIL_COTE_MAX:
                continue

            matchs_qualifies.append({
                "sport_titre": evt.get("sport_title", sport_key),
                "equipe1": evt.get("home_team", "?"),
                "equipe2": evt.get("away_team", "?"),
                "date_heure": commence.strftime("%d/%m/%Y %H:%M UTC"),
                "favori": meilleur.get("name", "?"),
                "cote_favori": cote_favori,
                "bookmaker_nom": bk.get("title", "?"),
                "bookmaker_est_1xbet": est_1xbet,
            })

    return matchs_qualifies


# --- GESTION DU TICKET ---

async def ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche les sélections retenues."""
    ticket = context.user_data.get("ticket", [])

    if not ticket:
        await update.message.reply_text("🎫 Votre ticket est vide. Tapez `/analyser` pour ajouter des sélections.")
        return

    text = f"🧾 **Votre Ticket Combiné (favoris ≤ {SEUIL_COTE_MAX}) :**\n\n"
    for idx, item in enumerate(ticket, start=1):
        text += f"{idx}. {item['match']}\n   👉 Choix: *{item['pronostic']}* (cote {item['cote']})\n"

    keyboard = [[InlineKeyboardButton("🗑️ Vider le Ticket", callback_data="vider_ticket")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# --- SERVEUR HTTP FACTICE (keep-alive Render) ---

class _HandlerKeepAlive(BaseHTTPRequestHandler):
    """Répond OK à toute requête, juste pour que Render détecte un port ouvert."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Clashprono bot actif (mode polling Telegram).")

    def log_message(self, format, *args):
        pass  # on coupe les logs HTTP verbeux, les logs du bot suffisent


def demarrer_serveur_keepalive() -> None:
    """Lance le serveur HTTP factice dans un thread daemon séparé."""
    serveur = HTTPServer(("0.0.0.0", PORT_KEEPALIVE), _HandlerKeepAlive)
    thread = threading.Thread(target=serveur.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Serveur HTTP de keep-alive démarré sur le port {PORT_KEEPALIVE} (pour Render).")


# --- DÉMARRAGE BOT ---

def main() -> None:
    demarrer_serveur_keepalive()

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("analyser", analyser_command))
    application.add_handler(CommandHandler("ticket", ticket_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    application.run_polling()


if __name__ == "__main__":
    main()
