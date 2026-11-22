import subprocess


def get_secret(entry: str) -> str:
    """Lit une entrée pass complète, y compris les valeurs multilignes."""
    result = subprocess.run(["pass", "show", entry], capture_output=True, text=True, check=True)
    return result.stdout


def get_password(entry: str = "Service/FamilleService") -> str:
    """Renvoie la première ligne de `pass show <entry>`."""
    return get_secret(entry).splitlines()[0]
