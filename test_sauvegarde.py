"""Tests de la sauvegarde : export JSON, restauration dans une base vide, refus d'écraser par erreur.

Lancement : python -m unittest test_sauvegarde -v
"""
import json
import os
import tempfile
import unittest

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="test-sauvegarde-")
os.environ.pop("DATABASE_URL", None)

import db  # noqa: E402
import memory  # noqa: E402
import sauvegarde  # noqa: E402


class Sauvegarde(unittest.TestCase):
    def setUp(self):
        db.init()
        for t in sauvegarde.TABLES:
            db.run(f"DELETE FROM {t}")

    def remplir(self):
        a = memory.add("profil", "Vit à Lyon", "manuel", 4)
        b = memory.add("recits", "A redressé un projet en retard", "emploi", 5, "3,4")
        memory.update(a, "Vit à Lyon (Croix-Rousse)")
        memory.delete(b)
        db.run("INSERT INTO messages(role, content, agent, created_at) VALUES('user', 'Bonjour', '', ?)", (db.now(),))
        db.kv_set("reglage", "valeur")
        return a

    def test_aller_retour(self):
        a = self.remplir()
        donnees = json.loads(json.dumps(sauvegarde.exporter(), ensure_ascii=False))   # comme le fichier
        self.assertEqual(donnees["version"], 1)
        self.assertEqual(donnees["base"], "sqlite")
        avant = {t: db.run(f"SELECT * FROM {t} ORDER BY 1") for t in sauvegarde.TABLES}
        for t in sauvegarde.TABLES:
            db.run(f"DELETE FROM {t}")
        compte = sauvegarde.restaurer(donnees)
        self.assertEqual(compte, {t: len(l) for t, l in avant.items()})
        self.assertEqual({t: db.run(f"SELECT * FROM {t} ORDER BY 1") for t in sauvegarde.TABLES}, avant)
        # Le journal reste utilisable : la suppression peut encore être annulée
        suppression = [e for e in memory.journal() if e["op"] == "delete"][0]
        memory.undo(suppression["id"])
        self.assertEqual(len(memory.recall()), 2)
        self.assertGreater(memory.add("profil", "Nouveau"), a)

    def test_ne_remplace_pas_par_erreur(self):
        self.remplir()
        donnees = sauvegarde.exporter()
        with self.assertRaises(ValueError):
            sauvegarde.restaurer(donnees)
        memory.add("profil", "Ajouté après la sauvegarde")
        sauvegarde.restaurer(donnees, remplacer=True)
        self.assertNotIn("Ajouté après la sauvegarde", memory.as_text())

    def test_fichier_illisible(self):
        for mauvais in (None, [], {"version": 99, "tables": {}}):
            with self.subTest(mauvais=mauvais), self.assertRaises(ValueError):
                sauvegarde.restaurer(mauvais)

    def test_ligne_de_commande(self):
        self.remplir()
        fichier = os.path.join(os.environ["DATA_DIR"], "memoire.json")
        self.assertEqual(sauvegarde.main(["exporter", fichier]), 0)
        for t in sauvegarde.TABLES:
            db.run(f"DELETE FROM {t}")
        self.assertEqual(sauvegarde.main(["restaurer", fichier]), 0)
        self.assertEqual(len(memory.recall()), 1)
        self.assertEqual(sauvegarde.main([]), 2)


if __name__ == "__main__":
    unittest.main()
