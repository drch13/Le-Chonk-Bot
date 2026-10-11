# Le Chonk 🐱

Bot Discord assistant, propulsé par l'API Mistral (modèle `mistral-large-4`).

## But

Le Chonk répond aux questions des membres d'un serveur Discord quand il est
mentionné (`@Le Chonk ta question`). Il s'appuie sur les derniers messages du
salon pour répondre en contexte.

## Fonctionnalités

- **Déclenchement par mention** : le bot n'agit que s'il est mentionné.
- **Fenêtre glissante** : les 10 derniers messages du salon sont envoyés comme
  contexte au modèle.
- **Pseudonymisation** : les auteurs sont renommés (« Utilisateur A »,
  « Utilisateur B »...) avant l'envoi à l'API ; aucune identité réelle ne part.
- **Salons « sans historique »** : opt-out par ID de salon
  (`NO_HISTORY_CHANNEL_IDS`), seul le message d'invocation est alors envoyé.
- **Transparence** : chaque réponse est préfixée par 🧠 (contexte du salon envoyé)
  ou 🔒 (contexte non envoyé).
- **Raisonnement visible** : un bouton 🤔 affiche le raisonnement du modèle dans
  un message éphémère, visible uniquement de celui qui clique.
- **Aucune persistance** : l'historique est lu à la volée, jamais loggé ni stocké.

## Installation

```bash
pip install -r requirements.txt
cp .env.example .env   # puis renseigner DISCORD_TOKEN et MISTRAL_API_KEY
python bot.py
```

Variables d'environnement :

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Token du bot Discord (obligatoire) |
| `MISTRAL_API_KEY` | Clé API Mistral (obligatoire) |
| `NO_HISTORY_CHANNEL_IDS` | IDs des salons sans historique, séparés par des virgules (optionnel) |
