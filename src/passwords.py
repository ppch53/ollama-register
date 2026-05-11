from __future__ import annotations

import secrets
import string
import random


SPECIAL_CHARS = "!@#$%^&*()-_=+[]{}"



def generate_strong_password(length: int = 20) -> str:
    if length < 12:
        raise ValueError("Password length must be at least 12")

    randomizer = random.SystemRandom()
    required_chars = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice(SPECIAL_CHARS),
    ]
    alphabet = string.ascii_letters + string.digits + SPECIAL_CHARS
    remaining_chars = [secrets.choice(alphabet) for _ in range(length - len(required_chars))]
    password_chars = required_chars + remaining_chars
    randomizer.shuffle(password_chars)
    return "".join(password_chars)
