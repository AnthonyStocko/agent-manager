#!/usr/bin/env python3
"""Vérifie qu'un serveur démarre : lance la commande, attend que /healthz réponde, puis l'arrête.

Usage : python demarrage.py PORT COMMANDE...
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request


def main():
    port, command = sys.argv[1], sys.argv[2:]
    # Base de données dans un dossier temporaire ; aucun secret de production transmis au serveur
    env = {**os.environ, "DATA_DIR": tempfile.mkdtemp(prefix="demarrage-"), "PORT": port}
    for name in ("RENDER", "DATABASE_URL", "APP_PASSWORD"):
        env.pop(name, None)
    proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(30):
            time.sleep(1)
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as resp:
                    if resp.status == 200:
                        print("Le serveur démarre et répond sur /healthz.")
                        return 0
            except OSError:
                continue
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            out = proc.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            out = proc.communicate()[0]
    print("Le serveur ne démarre pas ou ne répond pas sur /healthz. Sortie :\n" + (out or "")[-3000:])
    return 1


if __name__ == "__main__":
    sys.exit(main())
