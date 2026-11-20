"""Sauvegarde et restauration de la base de l'agent chef (mémoire, journal, discussion, réglages).

Export en JSON, indépendant de la version de Postgres. Utilise la même base que l'appli : DATABASE_URL
(Postgres) si elle est définie, sinon le fichier SQLite local.

    python sauvegarde.py exporter memoire.json
    python sauvegarde.py restaurer memoire.json             # refuse si la base contient déjà des souvenirs
    python sauvegarde.py restaurer memoire.json --remplacer # vide d'abord les tables sauvegardées

La sauvegarde de chaque nuit (.github/workflows/sauvegarde.yml) chiffre ce fichier avant de le déposer
dans GitHub Actions : le dépôt est public. Voir README, « Sauvegarde ».
"""
import json
import sys

import db

# Tables sauvegardées, dans l'ordre de restauration
TABLES = ["memories", "memory_log", "events", "messages", "kv", "dev_requests"]
VERSION = 1


def exporter():
    db.init()
    return {"version": VERSION, "date": db.now(), "base": "postgres" if db.PG else "sqlite",
            "tables": {t: db.run(f"SELECT * FROM {t} ORDER BY 1") for t in TABLES}}


def restaurer(sauvegarde, remplacer=False):
    """Recharge une sauvegarde. Renvoie le nombre de lignes restaurées par table."""
    if not isinstance(sauvegarde, dict) or sauvegarde.get("version") != VERSION:
        raise ValueError("Fichier de sauvegarde illisible ou d'une autre version.")
    db.init()
    tables = sauvegarde.get("tables") or {}
    if not remplacer and db.run("SELECT COUNT(*) AS n FROM memories")[0]["n"]:
        raise ValueError("La base contient déjà des souvenirs : ajoute --remplacer pour l'écraser.")
    colonnes = {t: (db._columns(t)) for t in TABLES}
    if remplacer:
        for t in reversed(TABLES):
            db.run(f"DELETE FROM {t}")
    compte = {}
    for t in TABLES:
        lignes = tables.get(t) or []
        for ligne in lignes:
            noms = [c for c in ligne if c in colonnes[t]]
            db.run(f"INSERT INTO {t}({', '.join(noms)}) VALUES({', '.join('?' * len(noms))})",
                   [ligne[c] for c in noms])
        compte[t] = len(lignes)
    if db.PG:   # les compteurs d'identifiants reprennent après le plus grand identifiant restauré
        for t in ("memories", "memory_log", "events", "messages"):
            db.run(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), COALESCE((SELECT MAX(id) FROM {t}), 0) + 1, false)")
    return compte


def main(args):
    if len(args) < 2 or args[0] not in ("exporter", "restaurer"):
        print(__doc__)
        return 2
    if args[0] == "exporter":
        donnees = exporter()
        with open(args[1], "w", encoding="utf-8") as f:
            json.dump(donnees, f, ensure_ascii=False)
        print("Sauvegarde : " + ", ".join(f"{t} {len(l)}" for t, l in donnees["tables"].items()))
        return 0
    with open(args[1], encoding="utf-8") as f:
        compte = restaurer(json.load(f), "--remplacer" in args)
    print("Restauré : " + ", ".join(f"{t} {n}" for t, n in compte.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
