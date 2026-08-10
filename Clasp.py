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
# 1. SERVEUR HTTP FACTICE POUR RENDER (Correction de l'erreur Port Scan)
# ==============================================================================

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    """Serveur HTTP minimaliste pour satisfaire les verifications de santé de Render."""
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(b"Bot Telegram en cours de fonctionnement sur Render !")

    def log_message(self, format, *args):
        # Desactive les logs HTTP pour ne pas encombrer la console Render
        return

def run_dummy_server():
    """Lance le serveur HTTP dans un thread sur le port fourni par Render."""
    port = int(os.environ.get("PORT", 8080))
    server_address = ('', port)
    httpd = HTTPServer(server_address, SimpleHTTPRequestHandler)
    print(f"[Render Health Check] Serveur HTTP factice demarre sur le port {port}")
    httpd.serve_forever()


# ==============================================================================
# 2. CONFIGURATION ET VARIABLES D'ENVIRONNEMENT
# ==============================================================================

# Verification et recuperation des clefs d'environnement
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "VOTRE_TELEGRAM_BOT_TOKEN")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "VOTRE_CLE_API_ODDS")

# Parametres de la strategie de paris
MIN_ODD = 1.15
MAX_ODD = 1.35
HOURS_AHEAD = 48

# Liste des sports/ligues supportes par TheOddsAPI
SPORTS_TO_CHECK = [
    "soccer_epl",
    "soccer_france_ligue_1",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_uefa_champs_league",
    "basketball_nba",
    "tennis_atp_wimbledon",
    "tennis_us_open",
    "mma_mixed_martial_arts"
]

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Stockage en memoire du ticket de l'utilisateur ({ user_id: [ selections ] })
USER_TICKETS = {}


# ==============================================================================
# 3. FONCTIONS D'INTERACTION AVEC L'API THEODDSAPI
# ==============================================================================

def fetch_odds_for_sport(sport_key: str):
    """Interroge l'API pour un sport specifique sur la fenetre de temps definie."""
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
        else:
            logging.error(f"Erreur API ({response.status_code}) pour le sport {sport_key}")
            return []
    except Exception as e:
        logging.error(f"Exception lors de l'appel API pour {sport_key}: {e}")
        return []


def analyze_matches():
    """Scanne les sports et filtre les matchs correspondant au critere (1.15 <= Cote <= 1.35)."""
    qualified_matches = []
    now = datetime.utcnow()
    time_limit = now + timedelta(hours=HOURS_AHEAD)

    for sport in SPORTS_TO_CHECK:
        matches = fetch_odds_for_sport(sport)
        for match in matches:
            # Verification de la date du match
            commence_time_str = match.get('commence_time')
            if not commence_time_str:
                continue
            
            commence_time = datetime.strptime(commence_time_str, "%Y-%m-%dT%H:%M:%SZ")
            if not (now <= commence_time <= time_limit):
                continue

            bookmakers = match.get('bookmakers', [])
            if not bookmakers:
                continue

            # Verification des cotes (Bookmaker 1)
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
                    break  # Eviter les doublons pour un meme match

    return qualified_matches


# ==============================================================================
# 4. COMMANDES ET HANDLERS DU BOT TELEGRAM
# ==============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /start : Accueil de l'utilisateur."""
    welcome_text = (
        "👋 <b>Bienvenue sur votre Bot d'Analyse de Paris Sportifs !</b>\n\n"
        "Ce bot recherche les opportunités basées sur la stratégie du favori "
        f"(Cote située entre {MIN_ODD} et {MAX_ODD}).\n\n"
        "📌 <b>Commandes disponibles :</b>\n"
        "▫️ /analyser - Lancer l'analyse et choisir vos pronostics\n"
        "▫️ /ticket - Voir et valider le ticket en cours\n"
        "▫️ /vider - Effacer le ticket actuel"
    )
    await update.message.reply_text(welcome_text, parse_mode="HTML")


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /analyser : Recherche des matchs et generation des options de pari."""
    await update.message.reply_text("🔍 <i>Analyse des matchs sur les prochaines 48h en cours... Veuillez patienter.</i>", parse_mode="HTML")

    matches = analyze_matches()

    if not matches:
        await update.message.reply_text("❌ Aucun match correspondant aux critères de sélection n'a été trouvé pour le moment.")
        return

    await update.message.reply_text(f"📊 <b>{len(matches)} match(s) éligible(s) trouvé(s) !</b>\nFaites vos choix ci-dessous :", parse_mode="HTML")

    for m in matches:
        text = (
            f"⚽ <b>{m['home_team']} vs {m['away_team']}</b>\n"
            f"🕒 Date : {m['commence_time']}\n"
            f"⭐ Favori identifié : <b>{m['favorite']}</b> (Cote : {m['odd']})"
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    f"Victoire Sèche ({m['favorite']}) @ {m['odd']}", 
                    callback_data=f"add_{m['favorite']}_victoire_{m['odd']}"
                )
            ],
            [
                InlineKeyboardButton(
                    "Option Sécurisée (Chancedouble / Draw No Bet)", 
                    callback_data=f"add_{m['favorite']}_securise_1.10"
                )
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gere le clic de l'utilisateur sur les boutons interactifs."""
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    if data.startswith("add_"):
        parts = data.split("_")
        team = parts[1]
        option_type = parts[2]
        odd = parts[3]

        if user_id not in USER_TICKETS:
            USER_TICKETS[user_id] = []

        selection = {
            'team': team,
            'type': option_type,
            'odd': float(odd)
        }
        USER_TICKETS[user_id].append(selection)

        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"✅ Ajouté au ticket : <b>{team}</b> ({option_type.capitalize()}) - Cote : {odd}", parse_mode="HTML")


async def ticket_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /ticket : Affiche le coupon de pari compile."""
    user_id = update.message.from_user.id
    ticket = USER_TICKETS.get(user_id, [])

    if not ticket:
        await update.message.reply_text("🎟️ Votre ticket est actuellement vide. Utilisez /analyser pour ajouter des matchs.")
        return

    total_odd = 1.0
    summary = "🎟️ <b>VOTRE TICKET COMBINÉ :</b>\n\n"

    for idx, item in enumerate(ticket, 1):
        summary += f"{idx}. <b>{item['team']}</b> ({item['type'].capitalize()}) - Cote : {item['odd']}\n"
        total_odd *= item['odd']

    summary += f"\n🔥 <b>Cote Totale Globale : {round(total_odd, 2)}</b>"
    await update.message.reply_text(summary, parse_mode="HTML")


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Commande /vider : Reinitialise le ticket en cours."""
    user_id = update.message.from_user.id
    USER_TICKETS[user_id] = []
    await update.message.reply_text("🗑️ Votre ticket a été réinitialisé.")


# ==============================================================================
# 5. APPLICATION MAIN
# ==============================================================================

def main():
    """Point d'entree principal de l'application."""
    # 1. Demarrer le serveur Web factice dans un thread pour passer les checks Render
    threading.Thread(target=run_dummy_server, daemon=True).start()

    # 2. Lancer le Bot Telegram
    if TELEGRAM_BOT_TOKEN == "VOTRE_TELEGRAM_BOT_TOKEN":
        logging.error("TELEGRAM_BOT_TOKEN non defini dans les variables d'environnement.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Enregistrement des commandes
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("analyser", analyze_command))
    app.add_handler(CommandHandler("ticket", ticket_command))
    app.add_handler(CommandHandler("vider", clear_command))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("🤖 Bot Telegram demarre et prêt a recevoir des commandes...")
    app.run_polling()


if __name__ == "__main__":
    main()
