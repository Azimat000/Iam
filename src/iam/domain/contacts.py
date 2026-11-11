"""Один ключ собеседника для истории общения и внимания."""
import hashlib


def contact_id(channel, sender):
    return "creator" if channel == "telegram" else "person_" + hashlib.sha256(
        f"{channel}:{sender}".encode()).hexdigest()[:16]
