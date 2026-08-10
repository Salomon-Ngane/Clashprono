import logging
import os
import requests
from datetime import datetime, timedelta
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

# Clés d'API & Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "VOTRE_TOKEN_TELEGRAM_ICI")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "VOTRE_CLE_API_ODDS_ICI")

# Paramètres de la stratégie 75/25
MIN_ODD = 1.20
MAX_ODD = 1.45

# Sports pris en charge par TheOddsAPI
SPORTS_DISPONIBLES = {
    "soccer_epl": "⚽ Football (EPL)",
    "soccer_france_ligue_one": "⚽ Football (Ligue 1)",
    "soccer_spain_la_liga": "⚽ Football (La Liga)",
    "soccer_italy_serie_a": "⚽ Football (Serie A)",
    "soccer_germany_bundesliga": "⚽ Football (Bundesliga)",
    "tennis_atp": "🎾 Tennis (ATP)",
    "basketball_nba": "🏀 Basketball (NBA)",
    "icehockey_nhl": "🏒 Hockey (NHL)",
    "mma_mixed_martial_arts": "🥊 MMA",
}


# --- COMMANDES DE BASE ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Message d'accueil expliquant le fonctionnement du bot."""
    welcome_text = (
        "👋 **Bienvenue sur le Bot de Paris Sportifs !**\n\n"
        f"🎯 **Stratégie 75/25 :** Détection des favoris à forte probabilité avec des cotes entre **{MIN_ODD}** et **{MAX_ODD}**.\n\n"
        "📌 **Commandes disponibles :**\n"
        "• `/analyser` : Choisissez vos sports et lancez la recherche sur 48h.\n"
        "• `/ticket` : Consultez vos paris enregistrés dans le ticket du jour."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def analyser_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Initialise le menu interactif de sélection des sports pour l'analyse."""
    context.user_data["sports_selectionnes"] = []
    await afficher_menu_sports(update, context)


async def afficher_menu_sports(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None) -> None:
    """Génère le clavier interactif pour choisir les sports."""
    sports_choisis = context.user_data.get("sports_selectionnes", [])

    keyboard = []
    for key, label in SPORTS_DISPONIBLES.items():
        is_checked = key in sports_choisis
        icon = "✅ " if is_checked else "⏹️ "
        button_text = f"{icon}{label}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"toggle_{key}")])

    # Boutons d'action
    keyboard.append([
        InlineKeyboardButton("🚀 Lancer l'Analyse 75/25", callback_data="valider_analyse")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)
    msg_text = (
        "🎯 **Étape 1 : Sélectionnez vos sports**\n"
        "Cliquez sur les sports à inclure dans le scan 75/25, puis validez."
    )

    if query:
        await query.edit_message_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="Markdown")


# --- GESTION DES BOUTONS INTERACTIFS ---

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Gère les clics sur les boutons (sélection des sports, pronostics, ticket)."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_data = context.user_data

    if "sports_selectionnes" not in user_data:
        user_data["sports_selectionnes"] = []

    # 1. Gestion de la coche/décoche des sports
    if data.startswith("toggle_"):
        sport_key = data.replace("toggle_", "")
        if sport_key in user_data["sports_selectionnes"]:
            user_data["sports_selectionnes"].remove(sport_key)
        else:
            user_data["sports_selectionnes"].append(sport_key)
        
        await afficher_menu_sports(update, context, query=query)

    # 2. Validation de l'analyse (Mode par défaut requis)
    elif data == "valider_analyse":
        sports_selectionnes = user_data.get("sports_selectionnes", [])
        
        if not sports_selectionnes:
            await query.answer("⚠️ Veuillez sélectionner au moins 1 sport avant de lancer l'analyse.", show_alert=True)
            return

        await query.edit_message_text(
            f"🔎 **Analyse en cours...** Recherche des cotes comprises entre {MIN_ODD} et {MAX_ODD} (Stratégie 75/25) sur les 48h à venir..."
        )
        await executer_analyse(query, context, sports_selectionnes)

    # 3. Ajout au ticket de pari
    elif data.startswith("pari_"):
        details_pari = data.replace("pari_", "").split("|")
        match, pronostic, cote = details_pari[0], details_pari[1], details_pari[2]

        if "ticket" not in user_data:
            user_data["ticket"] = []

        user_data["ticket"].append({
            "match": match,
            "pronostic": pronostic,
            "cote": float(cote)
        })

        await query.answer(f"✅ Pari ajouté au ticket ! ({pronostic} à {cote})", show_alert=True)

    # 4. Vider le ticket
    elif data == "vider_ticket":
        user_data["ticket"] = []
        await query.edit_message_text("🗑️ Votre ticket a été réinitialisé.")


# --- MOTEUR D'ANALYSE & RÉCUPÉRATION DES COTES ---

async def executer_analyse(query, context: ContextTypes.DEFAULT_TYPE, sports: list) -> None:
    """Scanne l'API pour les sports choisis et filtre selon le critère 75/25."""
    resultats_trouves = 0

    for sport_key in sports:
        matchs_qualifies = verifier_opportunites_sport(sport_key)
        
        for match_data in matchs_qualifies:
            resultats_trouves += 1
            text_match = (
                f"🏟️ **{match_data['sport']}** : {match_data['equipe1']} vs {match_data['equipe2']}\n"
                f"⏰ Date/Heure : {match_data['date']}\n"
                f"💡 **Analyse 75/25 :** Favori détecté (Cote {match_data['cote_favori']})"
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        f"Direct: {match_data['favori']} ({match_data['cote_favori']})",
                        callback_data=f"pari_{match_data['equipe1']} vs {match_data['equipe2']}|{match_data['favori']}|{match_data['cote_favori']}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"Sécurisé: {match_data['choix_securise']} ({match_data['cote_securisee']})",
                        callback_data=f"pari_{match_data['equipe1']} vs {match_data['equipe2']}|{match_data['choix_securise']}|{match_data['cote_securisee']}"
                    )
                ]
            ]
            markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(chat_id=query.message.chat_id, text=text_match, reply_markup=markup, parse_mode="Markdown")

    if resultats_trouves == 0:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"❌ Aucune opportunité respectant la logique 75/25 (cotes {MIN_ODD} - {MAX_ODD}) n'a été trouvée pour les sports sélectionnés."
        )


def verifier_opportunites_sport(sport_key: str) -> list:
    """
    Interroge l'API (TheOddsAPI) pour récupérer les cotes et applique la logique 75/25.
    """
    if ODDS_API_KEY == "VOTRE_CLE_API_ODDS_ICI":
        return [
            {
                "sport": SPORTS_DISPONIBLES.get(sport_key, sport_key),
                "equipe1": "Équipe A",
                "equipe2": "Équipe B",
                "date": (datetime.now() + timedelta(hours=12)).strftime("%d/%m %H:%M"),
                "favori": "Équipe A",
                "cote_favori": "1.30",
                "choix_securise": "Victoire Équipe A ou Nul",
                "cote_securisee": "1.10"
            }
        ]

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/?apiKey={ODDS_API_KEY}&regions=eu&markets=h2h"
    matchs_qualifies = []

    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            events = response.json()
            for event in events:
                event_time = datetime.fromisoformat(event['commence_time'].replace('Z', '+00:00'))
                if event_time > datetime.now().astimezone() + timedelta(hours=48):
                    continue

                bookmakers = event.get("bookmakers", [])
                if not bookmakers:
                    continue

                outcomes = bookmakers[0]["markets"][0]["outcomes"]
                for outcome in outcomes:
                    cote = outcome.get("price", 0)
                    # Filtre 75/25 strict : Cote entre MIN_ODD (1.20) et MAX_ODD (1.45)
                    if MIN_ODD <= cote <= MAX_ODD:
                        matchs_qualifies.append({
                            "sport": SPORTS_DISPONIBLES.get(sport_key, sport_key),
                            "equipe1": event.get("home_team"),
                            "equipe2": event.get("away_team"),
                            "date": event_time.strftime("%d/%m %H:%M"),
                            "favori": outcome.get("name"),
                            "cote_favori": str(cote),
                            "choix_securise": f"Double Chance / Handicap {outcome.get('name')}",
                            "cote_securisee": str(round(cote - 0.12, 2))
                        })
    except Exception as e:
        logger.error(f"Erreur API pour {sport_key}: {e}")

    return matchs_qualifies


# --- GESTION DU TICKET DE PARI ---

async def ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Affiche le coupon combiné en cours d'assemblage."""
    ticket = context.user_data.get("ticket", [])

    if not ticket:
        await update.message.reply_text("🎫 Votre ticket est vide pour le moment. Tapez `/analyser` pour ajouter des sélections.")
        return

    text = "🧾 **Votre Ticket Combiné (Logique 75/25) :**\n\n"
    cote_totale = 1.0

    for idx, item in enumerate(ticket, start=1):
        text += f"{idx}. {item['match']}\n   👉 Prono: *{item['pronostic']}* @ **{item['cote']}**\n"
        cote_totale *= item["cote"]

    text += f"\n📊 **Cote Totale Combinée :** `{round(cote_totale, 2)}`"
    
    keyboard = [[InlineKeyboardButton("🗑️ Vider le Ticket", callback_data="vider_ticket")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# --- LANCEMENT DU BOT ---

def main() -> None:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("analyser", analyser_command))
    application.add_handler(CommandHandler("ticket", ticket_command))

    application.add_handler(CallbackQueryHandler(callback_handler))

    application.run_polling()


if __name__ == "__main__":
    main()
