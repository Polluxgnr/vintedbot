# VF Finder

*English version: [README.md](README.md)*

Un outil qui me trouve les bonnes affaires d'occasion. Je décris ce que je cherche (« vidéoprojecteur, 50 à 900 € »), il cherche sur Vinted, Leboncoin, eBay, Wallapop, Rakuten et Cash Express, écarte les annonces hors sujet, lit les caractéristiques dans le titre et la description, puis classe ce qui reste par rapport qualité-prix. Interface graphique et ligne de commande.

## Pourquoi c'est moins simple que ça en a l'air

- **Anti-bot.** Vinted, Leboncoin (DataDome) et eBay bloquent la navigation automatisée. Chaque plateforme a son scraper avec cookies persistés, limite de requêtes, temps de pause et un mode navigateur visible pour résoudre un captcha une fois. Un interblocage entre threads sur Vinted a été corrigé avec un verrou réentrant et un timeout au démarrage.
- **Des résultats bruités.** Chercher « PS4 » renvoie des manettes, des jeux et des boîtes vides. Les titres passent d'abord par des règles (garder / rejeter / douteux) ; seuls les douteux partent vers un LLM, ce qui limite coût et latence.
- **Pas cher ne veut pas dire bon.** Chaque catégorie a des règles de veto strictes : vidéoprojecteur sans HDMI, téléphone bloqué iCloud, écran de DS cassé sont rejetés même à 1 €. Le reste est noté selon les specs extraites, la gamme de la marque, la complétude du lot et la décote par rapport au prix de référence.

## Fonctionnement

```
intention -> scraping multi-plateformes -> tri des titres -> extraction des specs -> veto + score -> top 5 -> CSV / Excel / rapport
```

Les catégories (vidéoprojecteur, Nintendo DS, PlayStation, vélo, climatiseur, téléphone) sont définies à un seul endroit, `CategoryConfig` dans `vf/models.py` : marques par gamme, specs à extraire, motifs de veto, décote minimale par gamme. Ajouter une catégorie, c'est ajouter une entrée.

## Lancer

```powershell
git clone https://github.com/Polluxgnr/vintedbot.git
cd vintedbot
py -m pip install -r requirements.txt
Copy-Item .env.example .env      # OPENROUTER_API_KEY est optionnelle

py -m vf --gui                   # ou double-clic sur run_gui.bat
py -m vf --q "video projecteur" --category projector --price-min 50 --price-max 900
py -m vf --q "new 3ds xl" --category console_ds --headed   # navigateur visible pour les captchas
```

Les fonctions LLM (tri des titres douteux, contrôle visuel du top) ne s'activent qu'avec `--ai` et une clé OpenRouter ; les règles seules suffisent.

`py -m vf_scrape` est un banc de test séparé pour vérifier chaque scraper.

## Tests

```powershell
$env:VF_NO_AI="1"
py -m unittest discover -s tests -t .
```

138 tests unitaires (intention, tri, extraction, score, classement), lancés sur Python 3.10 à 3.12 par GitHub Actions.

## Dépannage

| Problème | Solution |
|---|---|
| Vinted vide ou bloqué | relancer avec `--headed` et résoudre le captcha une fois ; les cookies sont gardés en local |
| Leboncoin (DataDome) | baisser `VF_LBC_RATE_PER_MIN`, augmenter `VF_LBC_BLOCK_COOLDOWN_MIN` |
| eBay demande une vérification | `--headed`, valider dans Chrome, relancer |
| Erreur d'import de la GUI | `pip install ".[gui]"` |

Les données locales (cookies, caches, exports) vont dans `brouillon/data/`, ignoré par git.

## Licence

MIT
