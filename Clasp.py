import os
import logging
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

# ==============================================================================
# CONFIGURATION ET VARIABLES D'ENVIRONNEMENT
# ==============================================================================

# Récupération sécurisée depuis le tableau de bord Render (Environment Variables)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "VOTRE_TELEGRAM_BOT_TOKEN")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "VOTRE_CLE_API_ODDS")

# Paramètres de la stratégie 80/20
MIN_ODD = 1.15
MAX_ODD = 1.35
HOURS_AHEAD = 48

# Liste des sports à scanner via TheOddsAPI
SPORTS_TO_CHECK = [
    "soccer_epl", "soccer_france_ligue_1", "soccer_spain_la_liga",
    "soccer_italy_serie_a", "soccer_germany_bundesliga", "soccer_uefa_champs_league",
    "basketball_nba", "tennis_atp_wimbledon", "tennis_us_open", "mma_mixed_martial_arts"
]

# Configuration des logs
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Stockage en mémoire des tickets utilisateurs ({ user_id: [ selections ] })
USER_TICKETS = {}

# ==============================================================================
# LOGIQUE RÉCUPÉRATION ET ANALYSE DES COTES
# ==============================================================================

def fetch_odds_for_sport(sport_key: str):
    """Interroge TheOddsAPI pour obtenir les cotes H2H d'un sport donné."""
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu",
        "markets": "h2h",
        "dateFormat": "iso"
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
        logging.warning(f"Statut HTTP {response.status_code} pour le sport {sport_key}")
        return []
    except Exception as e:
        logging.error(f"Erreur lors de la requête API ({sport_key}) : {e}")
        return []

def analyze_upcoming_matches():
    """Scanne tous les sports configurés sur une fenêtre de 48h."""
    valid_matches = []
    now = datetime.utcnow()
    future_limit = now + timedelta(hours=HOURS_AHEAD)

    for sport in SPORTS_TO_CHECK:
        events = fetch_odds_for_sport(sport)
        for event in events:
            # Vérification de la fenêtre temporelle de 48 heures
            commence_time_str = event.get("commence_time")
            if not commence_time_str:
                continue

            # Formatage de la date (ISO Format)
            match_date = datetime.fromisoformat(commence_time_str.replace("Z", "+00:00")).replace(tzinfo=None)
            if not (now <= match_date <= future_limit):
                continue

            # Traitement des cotes
            bookmakers = event.get("bookmakers", [])
            if not bookmakers:
                continue

            # Utilisation du premier bookmaker disponible (ex: Unibet/1xBet)
            outcomes = bookmakers[0]["markets"][0]["outcomes"]
            
            # Recherche du favori entrant dans la tranche 80/20 (1.15 à 1.35)
            for outcome in outcomes:
                price = outcome.get("price", 0)
                if MIN_ODD <= price <= MAX_ODD:
                    team_fav = outcome["name"]
                    # Identifier l'adversaire
                    all_teams = [event.get("home_team"), event.get("away_team")]
                    opponent = [t for t in all_teams if t != team_fav]
                    opponent_str = opponent[0] if opponent else "Adversaire"

                    valid_matches.append({
                        "id": event.get("id"),
                        "sport": event.get("sport_title", sport),
                        "home_team": event.get("home_team"),
                        "away_team": event.get("away_team"),
                        "favorite": team_fav,
                        "opponent": opponent_str,
                        "odd": price,
                        "date": match_date.strftime("%d/%m à %H:%H")
                    })
                    break  # Un seul favori valide par match
    return valid_matches

# ==============================================================================
# COMMANDES & HANDLERS TELEGRAM
# ==============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Message de bienvenue du bot."""
    welcome_text = (
        "👋 **Bienvenue sur votre Bot de Paris Sportifs !**\n\n"
        "Ce bot scanne les événements sur 48h (multi-sports) pour filtrer les matchs "
        "répondant au critère **80/20** (cote du favori entre 1.15 et 1.35).\n\n"
        "📌 **Commandes disponibles :**\n"
        "▫️ `/analyser` - Lancer l'analyse et choisir vos pronostics\n"
        "▫️ `/ticket` - Afficher et vérifier votre ticket combiné\n"
        "▫️ `/vider` - Réinitialiser le coupon en cours"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Déclenche la recherche et affiche les options via boutons interactifs."""
    await update.message.reply_text("🔍 **Analyse des cotes sur 48h en cours...** Veuillez patienter.", parse_mode="Markdown")
    
    matches = analyze_upcoming_matches()

    if not matches:
        await update.message.reply_text("❌ Aucun match correspondant au critère 80/20 (cote 1.15 - 1.35) n'a été trouvé sur les prochaines 48h.")
        return

    await update.message.reply_text(f"🎯 **{len(matches)} match(s) éligible(s)** trouvé(s) ! Faîtes vos choix :")

    for match in matches:
        fav = match["favorite"]
        odd_sec = match["odd"]
        # Options de sécurité calculées pour la boîte de dialogue
        odd_sec_opt1 = round(odd_sec * 0.95, 2)  # Estimation marché alternatif (ex: Double Chance)

        msg_text = (
            f"🏆 **{match['sport']}**\n"
            f"⚔️ **{match['home_team']} vs {match['away_team']}**\n"
            f"📅 Date : {match['date']}\n"
            f"⭐ Favori détecté : **{fav}** (Cote : `{odd_sec}`)"
        )

        # Génération des boutons interactifs selon le type de sport
        sport_lower = match['sport'].lower()
        if "soccer" in sport_lower or "football" in sport_lower:
            opt1_label = f" Victoire {fav} @ {odd_sec}"
            opt2_label = f" Double Chance ({fav}/Nul) @ {odd_sec_opt1}"
        elif "tennis" in sport_lower:
            opt1_label = f" Victoire {fav} @ {odd_sec}"
            opt2_label = f" {fav} gagne au moins 1 set @ {odd_sec_opt1}"
        else:
            opt1_label = f" Victoire {fav} @ {odd_sec}"
            opt2_label = f" Handicap Sécurisé {fav} @ {odd_sec_opt1}"

        # Construction du clavier interactif InlineKeyboard
        keyboard = [
            [InlineKeyboardButton(opt1_label, callback_data=f"add|{match['home_team']} vs {match['away_team']}|Victoire {fav}|{odd_sec}")],
            [InlineKeyboardButton(opt2_label, callback_data=f"add|{match['home_team']} vs {match['away_team']}|Option Sécurisée ({fav})|{odd_sec_opt1}")]
        ]

        await update.message.reply_text(
            msg_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gère l'enregistrement de la sélection lorsque l'utilisateur clique sur un bouton."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data.split("|")

    if data[0] == "add":
        match_title = data[1]
        selection = data[2]
        odd = float(data[3])

        if user_id not in USER_TICKETS:
            USER_TICKETS[user_id] = []

        # Ajout du choix au coupon combiné
        USER_TICKETS[user_id].append({
            "match": match_title,
            "selection": selection,
            "odd": odd
        })

        await query.edit_message_text(
            text=f"✅ **Sélection ajoutée au ticket !**\n\n📌 Match : {match_title}\n🎯 Choix : {selection}\n📊 Cote : `{odd}`",
            parse_mode="Markdown"
        )

async def ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Compile et affiche le coupon complet avec la cote totale recalculée."""
    user_id = update.message.from_user.id
    ticket = USER_TICKETS.get(user_id, [])

    if not ticket:
        await update.message.reply_text("🎫 Votre ticket est actuellement vide. Lancez /analyser pour ajouter des paris.")
        return

    total_odd = 1.0
    ticket_msg = "🎫 **VOTRE TICKET COMBINÉ**\n"
    ticket_msg += "-----------------------------------\n"

    for idx, item in enumerate(ticket, 1):
        ticket_msg += f"{idx}. **{item['match']}**\n   └ Prono : {item['selection']} (`{item['odd']}`)\n"
        total_odd *= item["odd"]

    total_odd = round(total_odd, 2)
    ticket_msg += "-----------------------------------\n"
    ticket_msg += f"📊 **Nombre de matchs :** {len(ticket)}\n"
    ticket_msg += f"🔥 **Cote Totale Globale :** `{total_odd}`"

    await update.message.reply_text(ticket_msg, parse_mode="Markdown")

async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Efface la liste des paris de l'utilisateur."""
    user_id = update.message.from_user.id
    USER_TICKETS[user_id] = []
    await update.message.reply_text("🗑️ Votre ticket a été réinitialisé.")

# ==============================================================================
# EXECUTION DU BOT
# ==============================================================================

def main():
    """Initialisation et démarrage du serveur Telegram."""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "VOTRE_TELEGRAM_BOT_TOKEN":
        print("Erreur : TELEGRAM_BOT_TOKEN non configuré.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Enregistrement des commandes
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("analyser", analyze_command))
    app.add_handler(CommandHandler("ticket", ticket_command))
    app.add_handler(CommandHandler("vider", clear_command))
    
    # Enregistrement des callbacks de boutons
    app.add_handler(CallbackQueryHandler(button_callback))

    print("🤖 Bot de Paris Sportifs démarré avec succès !")
    app.run_polling()

if __name__ == "__main__":
    main()
