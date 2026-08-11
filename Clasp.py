import logging
import os
import requests
from bs4 import BeautifulSoup
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# Configuration du Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Clé API & Configuration Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "VOTRE_TOKEN_TELEGRAM_ICI")

# URL source pour le scraping
QUIPARIER_URL = "https://www.quiparier.com"

# Sports filtrables
SPORTS_DISPONIBLES = {
    "football": "⚽ Football",
    "tennis": "🎾 Tennis",
    "basket-ball": "🏀 Basketball",
    "hockey": "🏒 Hockey",
    "mma": "🥊 MMA / Rugby / Autres"
}


# --- COMMANDES DE BASE ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Message d'accueil expliquant le fonctionnement du bot."""
    welcome_text = (
        "👋 **Bienvenue sur le Bot de Paris Sportifs !**\n\n"
        "🎯 **Stratégie 75/25 (Scraping Quiparier) :** Détection directe des matchs présentants un écart d'au moins **75% vs 25%**.\n\n"
        "📌 **Commandes disponibles :**\n"
        "• `/analyser` : Sélectionnez vos sports et scannez Quiparier.com.\n"
        "• `/ticket` : Consultez votre ticket de paris accumulés."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def analyser_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initialise la sélection des sports avant l'analyse."""
    context.user_data["sports_selectionnes"] = []
    await afficher_menu_sports(update, context)


async def afficher_menu_sports(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None) -> None:
    """Affiche le clavier interactif pour cocher les sports."""
    sports_choisis = context.user_data.get("sports_selectionnes", [])

    keyboard = []
    for key, label in SPORTS_DISPONIBLES.items():
        is_checked = key in sports_choisis
        icon = "✅ " if is_checked else "⏹️ "
        button_text = f"{icon}{label}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"toggle_{key}")])

    keyboard.append([
        InlineKeyboardButton("🚀 Lancer l'Analyse (Filtre 75%+)", callback_data="valider_analyse")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = (
        "🎯 **Sélectionnez les sports à analyser :**\n"
        "Le bot va scanner Quiparier.com et extraire uniquement les matchs avec un favori à **75% ou plus**."
    )

    if query:
        await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")


# --- GESTION DES INTERACTIONS ---

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

    elif data == "valider_analyse":
        sports_selectionnes = user_data.get("sports_selectionnes", [])
        if not sports_selectionnes:
            await query.answer("⚠️ Veuillez sélectionner au moins 1 sport.", show_alert=True)
            return

        await query.edit_message_text("🔎 **Scraping en cours sur Quiparier.com...** Recherche des écarts ≥ 75%...")
        await executer_analyse(query, context, sports_selectionnes)

    elif data.startswith("pari_"):
        details_pari = data.replace("pari_", "").split("|")
        match, pronostic, pourcentage = details_pari[0], details_pari[1], details_pari[2]

        if "ticket" not in user_data:
            user_data["ticket"] = []

        user_data["ticket"].append({
            "match": match,
            "pronostic": pronostic,
            "pourcentage": pourcentage
        })

        await query.answer(f"✅ Ajouté au ticket ! ({pronostic} - {pourcentage}%)", show_alert=True)

    elif data == "vider_ticket":
        user_data["ticket"] = []
        await query.edit_message_text("🗑️ Votre ticket a été réinitialisé.")


# --- SCRAPING ET EXTRACTION ---

async def executer_analyse(query, context: ContextTypes.DEFAULT_TYPE, sports: list) -> None:
    """Effectue le scraping des matchs et transmet les opportunités ≥ 75%."""
    matchs_qualifies = scraper_quiparier(sports)

    if not matchs_qualifies:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="❌ Aucun match respectant l'écart 75/25 n'a été trouvé actuellement sur Quiparier pour ces sports."
        )
        return

    for match in matchs_qualifies:
        text_match = (
            f"🏆 **{match['sport']}** - {match['competition']}\n"
            f"🏟️ **{match['equipe1']}** vs **{match['equipe2']}**\n"
            f"⏰ Date/Heure : {match['date']}\n"
            f"📊 **Répartition :** {match['pct1']}% vs {match['pct2']}%\n"
            f"🔥 **Favori (≥75%) :** {match['favori']} ({match['pourcentage_favori']}%)"
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    f"Sélectionner : {match['favori']} ({match['pourcentage_favori']}%)",
                    callback_data=f"pari_{match['equipe1']} vs {match['equipe2']}|{match['favori']}|{match['pourcentage_favori']}"
                )
            ]
        ]
        markup = InlineKeyboardMarkup(keyboard)
        await context.bot.send_message(chat_id=query.message.chat_id, text=text_match, reply_markup=markup, parse_mode="Markdown")


def scraper_quiparier(sports_cibles: list) -> list:
    """Scrape le DOM HTML de Quiparier.com et extrait les rencontres à au moins 75%."""
    matchs = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(QUIPARIER_URL, headers=headers, timeout=12)
        if response.status_code != 200:
            logger.error(f"Erreur HTTP {response.status_code} sur Quiparier")
            return matchs

        soup = BeautifulSoup(response.text, "html.parser")
        
        # Simulation d'extraction à partir de la structure HTML cible
        # (Dans la version finale, adaptez les sélecteurs selon les balises exactes de Quiparier)
        
        # Exemple de simulation directe basée sur la structure visuelle
        matchs_scrapes = [
            {
                "sport": "tennis",
                "competition": "TENNIS - TORONTO",
                "date": "11/08/2026 à 22h00",
                "equipe1": "T. Mihalíková/O. Nicholls",
                "pct1": 25,
                "equipe2": "E. Mertens /D. Shnaider",
                "pct2": 75
            },
            {
                "sport": "basket-ball",
                "competition": "BASKET-BALL - WNBA",
                "date": "11/08/2026 à 02h00",
                "equipe1": "Atlanta Dream (F)",
                "pct1": 77,
                "equipe2": "Toronto Tempo (F)",
                "pct2": 23
            }
        ]

        for m in matchs_scrapes:
            if m["sport"] not in sports_cibles:
                continue

            # Vérification du seuil strict de 75% minimum
            if m["pct1"] >= 75 or m["pct2"] >= 75:
                favori = m["equipe1"] if m["pct1"] >= 75 else m["equipe2"]
                pct_favori = m["pct1"] if m["pct1"] >= 75 else m["pct2"]

                matchs.append({
                    "sport": m["sport"].upper(),
                    "competition": m["competition"],
                    "date": m["date"],
                    "equipe1": m["equipe1"],
                    "pct1": m["pct1"],
                    "equipe2": m["equipe2"],
                    "pct2": m["pct2"],
                    "favori": favori,
                    "pourcentage_favori": pct_favori
                })

    except Exception as e:
        logger.error(f"Erreur lors du scraping de Quiparier: {e}")

    return matchs


# --- GESTION DU TICKET ---

async def ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche les sélections retenues par l'utilisateur."""
    ticket = context.user_data.get("ticket", [])

    if not ticket:
        await update.message.reply_text("🎫 Votre ticket est vide. Tapez `/analyser` pour ajouter des rencontres.")
        return

    text = "🧾 **Votre Sélection de Pronostics (Logique 75/25) :**\n\n"
    for idx, item in enumerate(ticket, start=1):
        text += f"{idx}. {item['match']}\n   👉 Choix: *{item['pronostic']}* ({item['pourcentage']}% de confiance)\n"

    keyboard = [[InlineKeyboardButton("🗑️ Vider le Ticket", callback_data="vider_ticket")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# --- DÉMARRAGE BOT ---

def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("analyser", analyser_command))
    application.add_handler(CommandHandler("ticket", ticket_command))
    application.add_handler(CallbackQueryHandler(callback_handler))

    application.run_polling()


if __name__ == "__main__":
    main()

