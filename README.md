# xelqoom-opp-feed

Collecteur incrémental d'avis publics (OIT / UNGM) sans aucune donnée privée.
- `collector.py` : parse le flux HTML OIT et enrichit uniquement les fiches UNGM nouvelles ou modifiées.
- `state.json` : mémoire incrémentale (curseur et hash de contenu).
- `feed_delta.json` : journal horodaté glissant sur 7 jours (numéroté par `seq`).
- `feed_public.json` : liste des avis publics ouverts.
