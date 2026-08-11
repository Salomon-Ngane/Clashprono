import logging
import os
import requests
from datetime import datetime, timedelta, timezone
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
FENETRE_HEURES = 48  # horizon de recherche des matchs

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

    # 2. Validation de l'analyse
    elif data == "valider_analyse":
        sports_selectionnes = user_data.get("sports_selectionnes", [])

        if not sports_selectionnes:
            await query.answer("⚠️ Veuillez sélectionner au moins 1 sport avant de lancer l'analyse.", show_alert=True)
            return

        await query.edit_message_text(
            f"🔎 **Analyse en cours...** Recherche des cotes comprises entre {MIN_ODD} et {MAX_ODD} (Stratégie 75/25) sur les {FENETRE_HEURES}h à venir..."
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
        try:
            matchs_qualifies = verifier_opportunites_sport(sport_key)
        except Exception as e:
            logger.error(f"Erreur inattendue pour {sport_key}: {e}")
            continue

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
    Pour chaque match, on identifie le VRAI favori (cote h2h la plus basse) et on ne
    retient que ceux dont la cote favorite tombe dans la fourchette 75/25.
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
                "choix_securise": "Double Chance Équipe A ou Nul",
                "cote_securisee": "1.10"
            }
        ]

    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/?apiKey={ODDS_API_KEY}&regions=eu&markets=h2h,double_chance"
    matchs_qualifies = []
    maintenant = datetime.now(timezone.utc)
    limite = maintenant + timedelta(hours=FENETRE_HEURES)

    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            logger.error(f"TheOddsAPI a répondu {response.status_code} pour {sport_key}")
            return matchs_qualifies

        events = response.json()

        for event in events:
            try:
                event_time = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue

            # On ne garde que les matchs à venir, dans la fenêtre définie
            if event_time < maintenant or event_time > limite:
                continue

            bookmakers = event.get("bookmakers", [])
            if not bookmakers:
                continue

            favori = extraire_favori_h2h(bookmakers)
            if favori is None:
                continue

            nom_favori, cote_favori = favori
            if not (MIN_ODD <= cote_favori <= MAX_ODD):
                continue

            cote_securisee = extraire_cote_double_chance(bookmakers, nom_favori)

            matchs_qualifies.append({
                "sport": SPORTS_DISPONIBLES.get(sport_key, sport_key),
                "equipe1": event.get("home_team"),
                "equipe2": event.get("away_team"),
                "date": event_time.astimezone().strftime("%d/%m %H:%M"),
                "favori": nom_favori,
                "cote_favori": str(cote_favori),
                "choix_securise": f"Double Chance {nom_favori}",
                "cote_securisee": str(cote_securisee) if cote_securisee else "N/A",
            })

    except requests.RequestException as e:
        logger.error(f"Erreur réseau API pour {sport_key}: {e}")
    except Exception as e:
        logger.error(f"Erreur inattendue lors du parsing pour {sport_key}: {e}")

    return matchs_qualifies


def extraire_favori_h2h(bookmakers: list):
    """
    Calcule la cote moyenne par équipe/issue sur tous les bookmakers disposant
    d'un marché h2h, puis retourne (nom, cote_moyenne_arrondie) de la cote la plus basse.
    Retourne None si aucune donnée exploitable.
    """
    cotes_par_issue = {}

    for bk in bookmakers:
        for marche in bk.get("markets", []):
            if marche.get("key") != "h2h":
                continue
            for outcome in marche.get("outcomes", []):
                nom = outcome.get("name")
                prix = outcome.get("price")
                if nom is None or prix is None:
                    continue
                cotes_par_issue.setdefault(nom, []).append(prix)

    if not cotes_par_issue:
        return None

    moyennes = {nom: sum(prix_list) / len(prix_list) for nom, prix_list in cotes_par_issue.items()}
    nom_favori = min(moyennes, key=moyennes.get)
    return nom_favori, round(moyennes[nom_favori], 2)


def extraire_cote_double_chance(bookmakers: list, nom_favori: str):
    """
    Cherche une vraie cote de marché 'double_chance' correspondant au favori.
    Retourne None si le marché n'est pas disponible (l'appelant affichera 'N/A').
    """
    cotes = []
    for bk in bookmakers:
        for marche in bk.get("markets", []):
            if marche.get("key") != "double_chance":
                continue
            for outcome in marche.get("outcomes", []):
                nom = outcome.get("name", "")
                prix = outcome.get("price")
                if prix is None:
                    continue
                # Le nom du marché double chance inclut généralement le favori (ex: "Team A or Draw")
                if nom_favori in nom:
                    cotes.append(prix)

    if not cotes:
        return None
    return round(sum(cotes) / len(cotes), 2)


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
