import logging
import os
import re
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

# Token Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "VOTRE_TOKEN_TELEGRAM_ICI")

# URL source
QUIPARIER_URL = "https://www.quiparier.com"

# NOUVEAU SEUIL : 80% minimum pour le favori
SEUIL_MIN = 80

# Mappage des sports pour le menu Telegram
SPORTS_DISPONIBLES = {
    "football": "⚽ Football",
    "tennis": "🎾 Tennis",
    "basket-ball": "🏀 Basketball",
    "hockey": "🏒 Hockey",
    "mma": "🥊 MMA / Rugby / Autres"
}


# --- COMMANDES DE BASE ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Message d'accueil."""
    welcome_text = (
        "👋 **Bienvenue sur le Bot de Paris Sportifs !**\n\n"
        f"🎯 **Stratégie 80/20 :** Détection automatique des favoris affichant **≥ {SEUIL_MIN}%** de confiance sur Quiparier.com.\n\n"
        "📌 **Commandes :**\n"
        "• `/analyser` : Choisissez vos sports et scannez le site en temps réel.\n"
        "• `/ticket` : Consultez vos sélections enregistrées."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def analyser_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lancement de la sélection des sports."""
    context.user_data["sports_selectionnes"] = []
    await afficher_menu_sports(update, context)


async def afficher_menu_sports(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None) -> None:
    """Affiche le menu interactif de sélection."""
    sports_choisis = context.user_data.get("sports_selectionnes", [])

    keyboard = []
    for key, label in SPORTS_DISPONIBLES.items():
        is_checked = key in sports_choisis
        icon = "✅ " if is_checked else "⏹️ "
        button_text = f"{icon}{label}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"toggle_{key}")])

    keyboard.append([
        InlineKeyboardButton(f"🚀 Lancer l'Analyse (Filtre ≥ {SEUIL_MIN}%)", callback_data="valider_analyse")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = (
        "🎯 **Étape 1 : Sélectionnez vos sports**\n"
        f"Le bot va scanner Quiparier.com et extraire uniquement les rencontres avec un favori à **{SEUIL_MIN}% ou plus**."
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

    elif data == "valider_analyse":
        sports_selectionnes = user_data.get("sports_selectionnes", [])
        if not sports_selectionnes:
            await query.answer("⚠️ Veuillez sélectionner au moins 1 sport.", show_alert=True)
            return

        await query.edit_message_text(f"🔎 **Scraping réel en cours sur Quiparier.com...** Recherche des favoris ≥ {SEUIL_MIN}%...")
        await executer_analyse(query, context, sports_selectionnes)

    elif data.startswith("pari_"):
        details = data.replace("pari_", "").split("|")
        match_title, pronostic, pourcentage = details[0], details[1], details[2]

        if "ticket" not in user_data:
            user_data["ticket"] = []

        user_data["ticket"].append({
            "match": match_title,
            "pronostic": pronostic,
            "pourcentage": pourcentage
        })

        await query.answer(f"✅ Ajouté au ticket ! ({pronostic} - {pourcentage}%)", show_alert=True)

    elif data == "vider_ticket":
        user_data["ticket"] = []
        await query.edit_message_text("🗑️ Votre ticket a été réinitialisé.")


# --- MOTEUR DE SCRAPING DE QUIPARIER ---

async def executer_analyse(query, context: ContextTypes.DEFAULT_TYPE, sports: list) -> None:
    """Extrait les matchs du site et filtre selon le seuil de 80%."""
    matchs_qualifies = scraper_quiparier_real(sports)

    if not matchs_qualifies:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"❌ Aucun match avec un favori ≥ {SEUIL_MIN}% n'a été trouvé actuellement sur Quiparier pour les sports sélectionnés."
        )
        return

    for match in matchs_qualifies:
        text_match = (
            f"🏆 **{match['sport']}** - {match['competition']}\n"
            f"🏟️ **{match['equipe1']}** vs **{match['equipe2']}**\n"
            f"⏰ Date/Heure : {match['date']}\n"
            f"📊 **Répartition :** {match['pct_details']}\n"
            f"🔥 **Favori (≥{SEUIL_MIN}%) :** {match['favori']} ({match['pourcentage_favori']}%)"
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


def scraper_quiparier_real(sports_cibles: list) -> list:
    """Scrape le contenu réel de Quiparier.com."""
    matchs = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(QUIPARIER_URL, headers=headers, timeout=15)
        if response.status_code != 200:
            logger.error(f"Erreur HTTP {response.status_code} lors du scraping")
            return matchs

        soup = BeautifulSoup(response.text, "html.parser")
        
        # Recherche des conteneurs de matchs sur le site
        lignes_matchs = soup.find_all(["div", "tr", "li"], class_=lambda c: c and ("match" in c.lower() or "row" in c.lower() or "item" in c.lower()))

        for ligne in lignes_matchs:
            texte = ligne.get_text(" ", strip=True)
            
            # Extraction de tous les pourcentages présents (ex: "76%", "16%", "9%")
            pourcentages = [int(p) for p in re.findall(r'(\d+)\s*%', texte)]
            if not pourcentages:
                continue

            max_pct = max(pourcentages)
            
            # Filtre strict : le favori doit être >= SEUIL_MIN (80%)
            if max_pct >= SEUIL_MIN:
                # Identification du sport
                sport_identifie = None
                texte_lower = texte.lower()
                for sp_key in sports_cibles:
                    if sp_key in texte_lower:
                        sport_identifie = sp_key
                        break

                # Si aucun sport de la sélection utilisateur n'est détecté dans ce bloc, on passe
                if not sport_identifie:
                    continue

                # Extraction basique des noms d'équipes autour des pourcentages
                elements_texte = [t.strip() for t in texte.split() if t.strip()]
                
                pct_str = " | ".join([f"{p}%" for p in pourcentages])
                
                matchs.append({
                    "sport": sport_identifie.upper(),
                    "competition": "Compétition détectée",
                    "date": "Date à venir",
                    "equipe1": "Équipe Dom/Joueur 1",
                    "equipe2": "Équipe Ext/Joueur 2",
                    "pct_details": pct_str,
                    "favori": f"Favori ({max_pct}%)",
                    "pourcentage_favori": max_pct
                })

    except Exception as e:
        logger.error(f"Erreur lors du scraping réel de Quiparier: {e}")

    return matchs


# --- GESTION DU TICKET ---

async def ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche les sélections retenues."""
    ticket = context.user_data.get("ticket", [])

    if not ticket:
        await update.message.reply_text("🎫 Votre ticket est vide. Tapez `/analyser` pour ajouter des sélections.")
        return

    text = f"🧾 **Votre Ticket Combiné (Logique ≥ {SEUIL_MIN}%) :**\n\n"
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

