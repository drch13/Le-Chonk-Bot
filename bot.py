"""Bot Discord « Le Chonk » — réponses via l'API Mistral Large 4.

Caractéristiques :
- déclenché uniquement quand il est mentionné ;
- fenêtre glissante : envoie les 10 derniers messages du salon comme contexte ;
- pseudonymisation des auteurs (« Utilisateur A », « Utilisateur B », ...) ;
- salons « sans historique » configurables (opt-out) ;
- transparence : chaque réponse est préfixée par un emoji indiquant si le
  contexte du salon a été envoyé à l'API (🧠) ou non (🔒) ;
- aucune donnée persistée : l'historique est lu à la volée, jamais loggé.

Prérequis :
    pip install -r requirements.txt
    cp .env.example .env  # puis renseigner DISCORD_TOKEN et MISTRAL_API_KEY
    # optionnel : NO_HISTORY_CHANNEL_IDS="id1,id2" dans .env
"""

import logging
import os
from typing import Any

import discord
import httpx
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger: logging.Logger = logging.getLogger("chonk-bot")

# --- Configuration (variables d'environnement) ---

# Charge les variables du fichier .env s'il existe (sinon, environnement shell).
load_dotenv()

DISCORD_TOKEN: str | None = os.getenv("DISCORD_TOKEN")
MISTRAL_API_KEY: str | None = os.getenv("MISTRAL_API_KEY")
MISTRAL_MODEL: str = "mistral-large-4"
MISTRAL_API_URL: str = "https://api.mistral.ai/v1/chat/completions"

HISTORY_LIMIT: int = 10  # fenêtre glissante : 10 derniers messages du salon
MAX_RESPONSE_CHARS: int = 1900  # limite Discord ~2000 caractères

# Indicateur de transparence : préfixe la réponse selon que le contexte du
# salon (fenêtre glissante) a été envoyé à l'API (🧠) ou non (🔒).
CONTEXT_EMOJI: str = "🧠"
NO_CONTEXT_EMOJI: str = "🔒"

# Opt-out : IDs des salons « sans historique », séparés par des virgules.
# Dans ces salons, seul le message d'invocation est envoyé à l'API.
# Exemple : NO_HISTORY_CHANNEL_IDS="123456789012345678,987654321098765432"
NO_HISTORY_CHANNEL_IDS: frozenset[int] = frozenset(
    int(part.strip())
    for part in os.getenv("NO_HISTORY_CHANNEL_IDS", "").split(",")
    if part.strip().isdigit()
)

SYSTEM_PROMPT: str = (
    "Tu es Le Chonk, un assistant dans un serveur Discord. "
    "On te fournit les derniers messages d'un salon, dont les auteurs sont "
    "pseudonymisés (« Utilisateur A », « Utilisateur B », etc.) : ne cherche "
    "jamais à deviner ni révéler leur identité réelle. "
    "Réponds de façon concise, dans la langue de l'utilisateur, au dernier "
    "message qui t'est adressé, en t'appuyant sur le contexte du salon si utile."
)

if not DISCORD_TOKEN or not MISTRAL_API_KEY:
    raise RuntimeError("Définissez DISCORD_TOKEN et MISTRAL_API_KEY dans l'environnement.")

intents: discord.Intents = discord.Intents.default()
intents.message_content = True  # obligatoire pour lire le contenu des messages

client: discord.Client = discord.Client(intents=intents)


# --- Pseudonymisation ---

def pseudonym_label(index: int) -> str:
    """Convertit un index en lettre : 0 -> 'A', ..., 25 -> 'Z', 26 -> 'AA', etc."""
    label: str = ""
    n: int = index + 1
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


def build_transcript(
    messages: list[discord.Message], invoker_id: int
) -> tuple[str, str]:
    """Construit la transcription pseudonymisée du salon.

    - les auteurs humains reçoivent une lettre (A, B, C...) dans leur ordre
      d'apparition dans la fenêtre ;
    - les messages du bot lui-même sont attribués à « Le Chonk » ;
    - les messages des autres bots sont ignorés.

    Retourne (transcription, pseudonyme de l'utilisateur qui invoque).
    """
    bot_user: discord.ClientUser | None = client.user
    labels: dict[int, str] = {}
    human_count: int = 0
    lines: list[str] = []

    for msg in messages:
        author_id: int = msg.author.id
        is_own_message: bool = bot_user is not None and author_id == bot_user.id
        if msg.author.bot and not is_own_message:
            continue  # ignore les autres bots

        if author_id not in labels:
            if is_own_message:
                labels[author_id] = "Le Chonk"
            else:
                labels[author_id] = f"Utilisateur {pseudonym_label(human_count)}"
                human_count += 1

        content: str = msg.content.strip()
        if bot_user is not None:
            content = content.replace(f"<@{bot_user.id}>", "").strip()
        if not content:
            continue  # ignore les messages vides (pièces jointes, stickers, etc.)
        lines.append(f"[{labels[author_id]}] {content}")

    transcript: str = "\n".join(lines)
    return transcript, labels.get(invoker_id, "Utilisateur ?")


# --- Appel API Mistral ---

def parse_response_content(content: Any) -> tuple[str, str]:
    """Sépare le contenu d'une réponse Mistral en (texte_final, raisonnement).

    `content` peut être une simple chaîne, ou une liste de blocs typés
    (ex. [{'type': 'thinking', ...}, {'type': 'text', ...}] pour les modèles
    avec raisonnement). Les types inconnus sont ignorés.
    """
    if isinstance(content, str):
        return content, ""
    if not isinstance(content, list):
        return str(content), ""

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(str(block.get("text", "")))
        elif block_type == "thinking":
            inner: Any = block.get("thinking", [])
            if isinstance(inner, str):
                thinking_parts.append(inner)
            elif isinstance(inner, list):
                thinking_parts.append(
                    "".join(
                        str(chunk.get("text", ""))
                        for chunk in inner
                        if isinstance(chunk, dict)
                    )
                )
    return "\n".join(text_parts).strip(), "\n".join(thinking_parts).strip()


async def ask_mistral(prompt: str) -> tuple[str, str]:
    """Appelle l'API Mistral et retourne (réponse, raisonnement), ou lève une exception."""
    payload: dict[str, Any] = {
        "model": MISTRAL_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 1024,
        "temperature": 0.7,
    }
    headers: dict[str, str] = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60.0) as http:
        response: httpx.Response = await http.post(
            MISTRAL_API_URL, json=payload, headers=headers
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
    return parse_response_content(data["choices"][0]["message"]["content"])


# --- Construction du prompt ---

async def generate_answer(
    message: discord.Message, invocation: str
) -> tuple[str, str, bool]:
    """Prépare le prompt (fenêtre glissante ou salon sans historique) puis appelle l'API.

    Retourne (réponse, raisonnement, contexte_envoyé) : contexte_envoyé vaut
    True si les derniers messages du salon ont bien été transmis à l'API comme
    contexte.
    """
    if message.channel.id in NO_HISTORY_CHANNEL_IDS:
        # Salon « sans historique » : seul le message d'invocation part vers l'API.
        prompt: str = invocation
        context_sent: bool = False
    else:
        context_sent = True
        try:
            history: list[discord.Message] = [
                msg async for msg in message.channel.history(limit=HISTORY_LIMIT)
            ]
        except (discord.Forbidden, discord.HTTPException) as exc:
            # ex. permission « Read Message History » manquante : on dégrade proprement
            # (aucun message tiers n'est envoyé, donc l'indicateur reste 🔒)
            logger.warning(
                "Historique indisponible dans #%s (%s) : contexte réduit au message seul.",
                getattr(message.channel, "name", "dm"),
                exc,
            )
            history = [message]
            context_sent = False

        history.reverse()  # ordre chronologique, l'invocation en dernier

        transcript: str
        invoker_pseudo: str
        transcript, invoker_pseudo = build_transcript(history, message.author.id)
        prompt = (
            "Voici les derniers messages du salon, du plus ancien au plus récent :\n\n"
            f"{transcript}\n\n"
            f"Réponds au dernier message, adressé par « {invoker_pseudo} »."
        )

    answer: str
    thinking: str
    answer, thinking = await ask_mistral(prompt)
    return answer, thinking, context_sent


# --- Événements Discord ---

class ThinkingView(discord.ui.View):
    """Bouton qui affiche le raisonnement du modèle dans un message éphémère.

    Le message éphémère n'est visible que par l'utilisateur qui a cliqué :
    la trace de réflexion n'encombre pas le salon. Les boutons cessent de
    fonctionner après un redémarrage du bot (l'état n'est pas persisté).
    """

    MAX_EPHEMERAL_MESSAGES: int = 3  # limite Discord par interaction

    def __init__(self, thinking: str) -> None:
        super().__init__(timeout=None)
        self.thinking: str = thinking

    @discord.ui.button(
        label="Raisonnement", emoji="🤔", style=discord.ButtonStyle.secondary
    )
    async def show_thinking(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        header: str = "🤔 Raisonnement interne :\n\n"
        chunks: list[str] = [
            self.thinking[i : i + MAX_RESPONSE_CHARS]
            for i in range(0, len(self.thinking), MAX_RESPONSE_CHARS)
        ][: self.MAX_EPHEMERAL_MESSAGES]
        chunks[0] = f"{header}{chunks[0]}"
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)


@client.event
async def on_ready() -> None:
    logger.info("Connecté en tant que %s", client.user)


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    if client.user not in message.mentions:
        return  # le bot ne réagit que quand il est mentionné

    bot_user: discord.ClientUser | None = client.user
    if bot_user is None:
        return

    invocation: str = message.content.replace(f"<@{bot_user.id}>", "").strip()
    if not invocation:
        await message.reply(
            "Oui ? Mentionne-moi avec ta question, ex. `@Le Chonk résume la discussion`."
        )
        return

    async with message.channel.typing():
        try:
            answer: str
            thinking: str
            context_sent: bool
            answer, thinking, context_sent = await generate_answer(message, invocation)
        except httpx.HTTPStatusError as exc:
            logger.error("Erreur API Mistral (HTTP %s)", exc.response.status_code)
            await message.reply(
                "Désolé, l'API Mistral a renvoyé une erreur. Réessaie plus tard."
            )
            return
        except httpx.HTTPError as exc:
            logger.error("Erreur réseau : %s", exc)
            await message.reply(
                "Impossible de contacter l'API Mistral (erreur réseau)."
            )
            return

    # indicateur de transparence : 🧠 = contexte du salon envoyé, 🔒 = non
    emoji: str = CONTEXT_EMOJI if context_sent else NO_CONTEXT_EMOJI
    full_answer: str = f"{emoji} {answer}"

    # bouton « raisonnement » : le clic l'affiche en message éphémère, visible
    # uniquement par celui qui clique (ne prend pas de place dans le salon)
    view: ThinkingView | None = ThinkingView(thinking) if thinking else None

    # découpe la réponse si elle dépasse la limite Discord
    for i in range(0, len(full_answer), MAX_RESPONSE_CHARS):
        await message.reply(
            full_answer[i : i + MAX_RESPONSE_CHARS],
            view=view if i == 0 else None,
        )


def main() -> None:
    if DISCORD_TOKEN is not None:
        client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()