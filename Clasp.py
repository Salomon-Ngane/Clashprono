import os
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
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
# 1. SERVEUR HTTP POUR RENDER
# ==============================================================================

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(b"Bot Telegram actif et fonctionnel sur Render !")

    def log_message(self, format, *args):
        return

def run_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server_address = ('', port)
    httpd = HTTPServer(server_address, SimpleHTTPRequestHandler)
    print(f"[Render Check] Serveur HTTP actif sur le port {port}")
    httpd.serve_forever()


# ==============================================================================
# 2. CONFIGURATION ET DICTIONNAIRES DE SPORTS
# ==============================================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "VOTRE_TOKEN")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "VOTRE_CLE_ODDS")

MIN_ODD = 1.15
MAX_ODD = 1.35
HOURS_AHEAD = 48

# Tous les sports disponibles via TheOddsAPI
AVAILABLE_SPORTS = {
    "soccer_epl": "⚽ Football - Premier League (ENG)",
    "soccer_france_ligue_1": "⚽ Football - Ligue 1 (FRA)",
    "soccer_spain_la_liga": "⚽ Football - La Liga (ESP)",
    "soccer_italy_serie_a": "⚽ Football - Serie A (ITA)",
    "soccer_germany_bundesliga": "⚽ Football - Bundesliga (GER)",
    "soccer_uefa_champs_league": "⚽ Football - Champions League",
    "basketball_nba": "🏀 Basketball - NBA",
    "tennis_atp_wimbledon": "🎾 Tennis - ATP Wimbledon",
    "tennis_us_open": "🎾 Tennis - US Open",
    "mma_mixed_martial_arts": "🥊 MMA / UFC"
}

# Stockage par utilisateur (Mémoire)
USER_SELECTED_SPORTS = {}  # { user_id: ["soccer_epl", ...] }
USER_TICKETS = {}          # { user_id: [ selections ] }

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ==============================================================================
# 3. INTERACTION API THEODDSAPI
# ==============================================================================

def fetch_odds_for_sport(sport_key: str):
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {
        'apiKey': ODDS_API_KEY,
        'regions': 'eu',
        'markets': 'h2h',
        'oddsFormat': 'decimal'
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
        logging.error(f"Erreur API ({response.status_code}) pour {sport_key}")
        return []
    except Exception as e:
        logging.error(f"Exception API pour {sport_key}: {e}")
        return []


def analyze_matches_for_user(user_id: int):
    # Récupère les sports choisis par l'utilisateur (ou tout par défaut)
    sports_to_scan = USER_SELECTED_SPORTS.get(user_id, list(AVAILABLE_SPORTS.keys()))
    
    qualified_matches = []
    now = datetime.utcnow()
    time_limit = now + timedelta(hours=HOURS_AHEAD)

    for sport in sports_to_scan:
        matches = fetch_odds_for_sport(sport)
        for match in matches:
            commence_time_str = match.get('commence_time')
            if not commence_time_str:
                continue
            
            commence_time = datetime.strptime(commence_time_str, "%Y-%m-%dT%H:%M:%SZ")
            if not (now <= commence_time <= time_limit):
                continue

            bookmakers = match.get('bookmakers', [])
            if not bookmakers:
                continue

            outcomes = bookmakers[0].get('markets', [{}])[0].get('outcomes', [])
            for outcome in outcomes:
                price = outcome.get('price', 0)
                if MIN_ODD <= price <= MAX_ODD:
                    qualified_matches.append({
                        'id': match.get('id'),
                        'home_team': match.get('home_team'),
                        'away_team': match.get('away_team'),
                        'favorite': outcome.get('name'),
                        'odd': price,
                        'commence_time': commence_time.strftime("%d/%m à %H:%M UTC")
                    })
                    break

    return qualified_matches


# ==============================================================================
# 4. COMMANDES ET GESTIONNAIRES TELEGRAM
# ==============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "👋 <b>Bienvenue sur votre Bot d'Analyse de Paris Sportifs !</b>\n\n"
        "<b>Commandes disponibles :</b>\n"
        "▫️ /sports - Choisir les sports à analyser\n"
        "▫️ /analyser - Lancer l'analyse sur vos sports sélectionnés\n"
        "▫️ /ticket - Voir et valider le ticket en cours\n"
        "▫️ /vider - Effacer le ticket actuel"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")


async def sports_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Affiche le menu de sélection des sports à activer ou désactiver."""
    user_id = update.message.from_user.id
    if user_id not in USER_SELECTED_SPORTS:
        # Par défaut tous les sports sont activés
        USER_SELECTED_SPORTS[user_id] = list(AVAILABLE_SPORTS.keys())

    keyboard = []
    active_sports = USER_SELECTED_SPORTS[user_id]

    for key, name in AVAILABLE_SPORTS.items():
        status = "✅" if key in active_sports else "❌"
        keyboard.append([InlineKeyboardButton(f"{status} {name}", callback_data=f"toggle_sport_{key}")])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "⚙️ <b>Configuration des Sports</b>\n"
        "Cliquez sur un sport pour l'activer (✅) ou le désactiver (❌) du scan :",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    await update.message.reply_text("🔍 <i>Analyse des matchs en cours... Veuillez patienter.</i>", parse_mode="HTML")

    matches = analyze_matches_for_user(user_id)

    if not matches:
        await update.message.reply_text("❌ Aucun match trouvé selon vos filtres et critères actuels.")
        return

    await update.message.reply_text(f"📊 <b>{len(matches)} match(s) trouvé(s) !</b>", parse_mode="HTML")

    for m in matches:
        text = (
            f"⚽ <b>{m['home_team']} vs {m['away_team']}</b>\n"
            f"🕒 Date : {m['commence_time']}\n"
            f"⭐ Favori : <b>{m['favorite']}</b> (Cote : {m['odd']})"
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    f"Ajouter Victoire ({m['favorite']}) @ {m['odd']}", 
                    callback_data=f"add_{m['favorite']}_victoire_{m['odd']}"
                )
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    # Gestion de l'activation/désactivation des sports
    if data.startswith("toggle_sport_"):
        sport_key = data.replace("toggle_sport_", "")
        if user_id not in USER_SELECTED_SPORTS:
            USER_SELECTED_SPORTS[user_id] = list(AVAILABLE_SPORTS.keys())

        if sport_key in USER_SELECTED_SPORTS[user_id]:
            USER_SELECTED_SPORTS[user_id].remove(sport_key)
        else:
            USER_SELECTED_SPORTS[user_id].append(sport_key)

        # Reconstruire le clavier interactif
        keyboard = []
        for key, name in AVAILABLE_SPORTS.items():
            status = "✅" if key in USER_SELECTED_SPORTS[user_id] else "❌"
            keyboard.append([InlineKeyboardButton(f"{status} {name}", callback_data=f"toggle_sport_{key}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_reply_markup(reply_markup=reply_markup)

    # Gestion de l'ajout au ticket
    elif data.startswith("add_"):
        parts = data.split("_")
        team = parts[1]
        option_type = parts[2]
        odd = parts[3]

        if user_id not in USER_TICKETS:
            USER_TICKETS[user_id] = []

        USER_TICKETS[user_id].append({
            'team': team,
            'type': option_type,
            'odd': float(odd)
        })

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"✅ Ajouté au ticket : <b>{team}</b> - Cote : {odd}", parse_mode="HTML")


async def ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    ticket = USER_TICKETS.get(user_id, [])

    if not ticket:
        await update.message.reply_text("🎟️ Votre ticket est vide. Utilisez /analyser pour ajouter des matchs.")
        return

    total_odd = 1.0
    summary = "🎟️ <b>VOTRE TICKET COMBINÉ :</b>\n\n"

    for idx, item in enumerate(ticket, 1):
        summary += f"{idx}. <b>{item['team']}</b> - Cote : {item['odd']}\n"
        total_odd *= item['odd']

    summary += f"\n🔥 <b>Cote Totale Globale : {round(total_odd, 2)}</b>"
    await update.message.reply_text(summary, parse_mode="HTML")


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    USER_TICKETS[user_id] = []
    await update.message.reply_text("🗑️ Votre ticket a été réinitialisé.")


# ==============================================================================
# 5. INITIALISATION
# ==============================================================================

def main():
    threading.Thread(target=run_dummy_server, daemon=True).start()

    if TELEGRAM_BOT_TOKEN in ["VOTRE_TOKEN", ""]:
        logging.error("TELEGRAM_BOT_TOKEN non configuré.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("sports", sports_command))
    app.add_handler(CommandHandler("analyser", analyze_command))
    app.add_handler(CommandHandler("ticket", ticket_command))
    app.add_handler(CommandHandler("vider", clear_command))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("🤖 Bot Telegram démarré...")
    app.run_polling()


if __name__ == "__main__":
    main()
